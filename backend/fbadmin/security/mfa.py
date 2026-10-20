"""Two-factor sign-in with TOTP authenticator apps (server mode, internal and LDAP users).

* Enrolment: a new secret is stored (encrypted) as *pending*; it becomes active only after
  the user typed a valid code. Ten single-use recovery codes are shown once and stored hashed
  (Argon2id, like passwords).
* Sign-in: after the password, ``/api/auth/login`` answers ``{"mfa_required": true,
  "mfa_token": ...}`` instead of a session; ``/api/auth/login/mfa`` takes the code. A code is
  accepted within ±1 time step (30 s) and only once (the last used step is stored).
* Administrators can require 2FA per user or for every internal/LDAP user (they enrol at their
  next sign-in) and reset a user's 2FA (lost phone).
* OAuth2/OIDC and reverse-proxy users sign in at their identity provider; enforcing a second
  factor there is the provider's job.

Secrets at rest: TOTP secrets are encrypted with AES-256-GCM (``crypto.encrypt``) under a
server key that is *not* stored in ``fbadmin.db``: ``FBADMIN_SECRET_KEY`` if set, otherwise
``<data_dir>/secret.key`` (created with mode 0600). They are deliberately not tied to the
user's login-password vault: an LDAP password change or an administrator's password reset
replaces that vault and would make the second factor unreadable (locking the user out).
"""

from __future__ import annotations

import base64
import hashlib
import os
import secrets
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import totp
from .crypto import decrypt, encrypt, hash_password, verify_password
from .errors import SecurityError

SCHEMA = """
CREATE TABLE IF NOT EXISTS user_mfa (
    user_id INTEGER PRIMARY KEY,
    secret TEXT,
    enabled INTEGER NOT NULL DEFAULT 0,
    required INTEGER NOT NULL DEFAULT 0,
    last_step INTEGER,
    created_at REAL,
    enabled_at REAL
);
CREATE TABLE IF NOT EXISTS mfa_recovery (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    code_hash TEXT NOT NULL,
    used_at REAL
);
"""
MFA_SOURCES = ("internal", "ldap")
RECOVERY_CODES = 10
CHALLENGE_TTL = 300
CHALLENGE_ATTEMPTS = 5


def server_key(data_dir: Path) -> bytes:
    """32-byte key for secrets fbadmin must read back (TOTP secrets)."""
    env = os.environ.get("FBADMIN_SECRET_KEY")
    if env:
        return hashlib.sha256(env.encode()).digest()
    f = data_dir / "secret.key"
    if not f.exists():
        f.parent.mkdir(parents=True, exist_ok=True)
        tmp = f.with_suffix(".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as fh:
            fh.write(base64.urlsafe_b64encode(secrets.token_bytes(32)).decode())
        os.replace(tmp, f)
    return hashlib.sha256(f.read_text().strip().encode()).digest()


def new_recovery_code() -> str:
    raw = base64.b32encode(secrets.token_bytes(7)).decode().lower().rstrip("=")[:10]
    return f"{raw[:5]}-{raw[5:]}"


def _clean_recovery(code: str) -> str:
    return code.strip().lower().replace(" ", "").replace("-", "")


@dataclass
class Challenge:
    """A password check that passed and waits for the second factor (kept in memory)."""

    user_id: int
    password: str = field(repr=False)
    kind: str  # "verify" | "enrol"
    ip: str | None
    expires: float
    attempts: int = 0
    secret: str | None = field(default=None, repr=False)


class MfaService:
    def __init__(self, sec):
        self.sec = sec
        self._ready = False
        self._lock = threading.RLock()
        self._challenges: dict[str, Challenge] = {}
        self._key: bytes | None = None

    # ------------------------------------------------------------ storage
    def _db(self):
        if not self._ready:
            with self._lock:
                if not self._ready:
                    self.sec.db.conn().executescript(SCHEMA)
                    self._ready = True
        return self.sec.db

    def _k(self) -> bytes:
        if self._key is None:
            self._key = server_key(self.sec.settings.data_dir)
        return self._key

    def _row(self, user_id: int) -> dict[str, Any] | None:
        r = self._db().execute("SELECT * FROM user_mfa WHERE user_id = ?", (user_id,)).fetchone()
        return dict(r) if r else None

    def _secret(self, row: dict[str, Any]) -> str | None:
        if not row.get("secret"):
            return None
        try:
            return decrypt(self._k(), row["secret"], f"totp:{row['user_id']}")
        except ValueError:
            return None  # server key changed: the user must be reset by an administrator

    # ------------------------------------------------------------ policy
    @property
    def require_all(self) -> bool:
        return self.sec.db.exists and self._db().get_meta("mfa_require_all") == "1"

    def set_require_all(self, value: bool) -> None:
        self._db().set_meta("mfa_require_all", "1" if value else "0")

    @staticmethod
    def applies(user: dict[str, Any]) -> bool:
        return user.get("auth_source") in MFA_SOURCES

    def status(self, user: dict[str, Any]) -> dict[str, Any]:
        row = self._row(user["id"]) or {}
        enabled = bool(row.get("enabled"))
        remaining = 0
        if enabled:
            remaining = self._db().execute("SELECT COUNT(*) FROM mfa_recovery WHERE user_id = ? AND used_at IS NULL",
                                           (user["id"],)).fetchone()[0]
        return {
            "available": self.applies(user),
            "enabled": enabled,
            "required": self.applies(user) and (bool(row.get("required")) or self.require_all),
            "required_for_user": bool(row.get("required")),
            "required_for_all": self.require_all,
            "pending": bool(row.get("secret")) and not enabled,
            "recovery_codes_left": remaining,
            "enabled_at": row.get("enabled_at"),
        }

    def users_overview(self) -> dict[int, dict[str, Any]]:
        rows = self._db().execute("SELECT user_id, enabled, required FROM user_mfa").fetchall()
        return {int(r["user_id"]): {"enabled": bool(r["enabled"]), "required": bool(r["required"])} for r in rows}

    def set_required(self, user_id: int, required: bool) -> None:
        if self._row(user_id) is None:
            self._db().execute("INSERT INTO user_mfa (user_id, required, created_at) VALUES (?, ?, ?)",
                               (user_id, int(required), time.time()))
        else:
            self._db().execute("UPDATE user_mfa SET required = ? WHERE user_id = ?", (int(required), user_id))

    def reset(self, user_id: int) -> None:
        """Remove the secret and recovery codes (lost phone); the "required" flag stays."""
        db = self._db()
        db.execute("UPDATE user_mfa SET secret = NULL, enabled = 0, last_step = NULL, enabled_at = NULL "
                   "WHERE user_id = ?", (user_id,))
        db.execute("DELETE FROM mfa_recovery WHERE user_id = ?", (user_id,))

    def forget_user(self, user_id: int) -> None:
        if self.sec.db.exists:
            self.reset(user_id)
            self._db().execute("DELETE FROM user_mfa WHERE user_id = ?", (user_id,))

    # ------------------------------------------------------------ enrolment
    def begin(self, user: dict[str, Any]) -> dict[str, Any]:
        """New pending secret; replaces an earlier pending one. Refused while 2FA is enabled."""
        if not self.applies(user):
            raise SecurityError(400, "Two-factor sign-in for this account is handled by its identity provider",
                                "mfa_not_applicable")
        row = self._row(user["id"])
        if row and row.get("enabled"):
            raise SecurityError(400, "Two-factor authentication is already enabled", "mfa_enabled")
        secret = totp.new_secret()
        token = encrypt(self._k(), secret, f"totp:{user['id']}")
        if row is None:
            self._db().execute("INSERT INTO user_mfa (user_id, secret, enabled, created_at) VALUES (?, ?, 0, ?)",
                               (user["id"], token, time.time()))
        else:
            self._db().execute("UPDATE user_mfa SET secret = ?, enabled = 0, last_step = NULL, created_at = ? "
                               "WHERE user_id = ?", (token, time.time(), user["id"]))
        return self._enrol_view(user, secret)

    @staticmethod
    def _enrol_view(user: dict[str, Any], secret: str) -> dict[str, Any]:
        account = user.get("email") or user["username"]
        return {"secret": secret, "otpauth_uri": totp.otpauth_uri(secret, account), "issuer": "fbadmin",
                "account": account, "digits": totp.DIGITS, "period": totp.PERIOD}

    def confirm(self, user: dict[str, Any], code: str) -> list[str]:
        """Activate the pending secret with a valid code; returns the new recovery codes."""
        row = self._row(user["id"])
        if not row or not row.get("secret"):
            raise SecurityError(400, "Start the two-factor setup first", "mfa_not_started")
        if row.get("enabled"):
            raise SecurityError(400, "Two-factor authentication is already enabled", "mfa_enabled")
        secret = self._secret(row)
        stp = totp.match_step(secret, code) if secret else None
        if stp is None:
            raise SecurityError(400, "Wrong code - check the time on your phone and try again", "invalid_code")
        self._db().execute("UPDATE user_mfa SET enabled = 1, last_step = ?, enabled_at = ? WHERE user_id = ?",
                           (stp, time.time(), user["id"]))
        return self.new_recovery_codes(user["id"])

    def new_recovery_codes(self, user_id: int) -> list[str]:
        codes = [new_recovery_code() for _ in range(RECOVERY_CODES)]
        db = self._db()
        db.execute("DELETE FROM mfa_recovery WHERE user_id = ?", (user_id,))
        for c in codes:
            db.execute("INSERT INTO mfa_recovery (user_id, code_hash) VALUES (?, ?)",
                       (user_id, hash_password(_clean_recovery(c))))
        return codes

    # ------------------------------------------------------------ verification
    def verify(self, user_id: int, code: str) -> str | None:
        """Check a TOTP code (each time step once) or an unused recovery code (then used up).
        Returns "totp" / "recovery" or None."""
        row = self._row(user_id)
        if not row or not row.get("enabled"):
            return None
        code = (code or "").strip()
        digits = code.replace(" ", "")
        if digits.isdigit() and len(digits) == totp.DIGITS:
            secret = self._secret(row)
            if not secret:
                return None
            with self._lock:
                stp = totp.match_step(secret, digits)
                last = self._row(user_id) or {}
                if stp is None or (last.get("last_step") is not None and stp <= last["last_step"]):
                    return None  # wrong, or already used (replay)
                self._db().execute("UPDATE user_mfa SET last_step = ? WHERE user_id = ?", (stp, user_id))
                return "totp"
        clean = _clean_recovery(code)
        if len(clean) != 10:
            return None
        with self._lock:
            rows = self._db().execute("SELECT id, code_hash FROM mfa_recovery WHERE user_id = ? AND used_at IS NULL",
                                      (user_id,)).fetchall()
            for r in rows:
                if verify_password(r["code_hash"], clean):
                    self._db().execute("UPDATE mfa_recovery SET used_at = ? WHERE id = ?", (time.time(), r["id"]))
                    return "recovery"
        return None

    def check_code(self, user: dict[str, Any], code: str, ip: str | None) -> str:
        """``verify`` with rate limiting; raises on failure."""
        keys = [f"mfa:user:{user['id']}", f"mfa:ip:{ip or '?'}"]
        limiter = self.sec.login_limiter
        wait = max(limiter.blocked(k) for k in keys)
        if wait:
            raise SecurityError(429, f"Too many wrong codes - try again in {int(wait)} s", "rate_limited",
                                {"Retry-After": str(int(wait))})
        how = self.verify(user["id"], code)
        if how is None:
            for k in keys:
                limiter.hit(k)
            raise SecurityError(401, "Wrong or already used code", "invalid_code")
        limiter.reset(keys[0])
        return how

    def disable(self, user: dict[str, Any], code: str, ip: str | None) -> None:
        st = self.status(user)
        if not st["enabled"]:
            raise SecurityError(400, "Two-factor authentication is not enabled", "mfa_disabled")
        if st["required"]:
            raise SecurityError(400, "An administrator requires two-factor authentication for your account",
                                "mfa_required")
        self.check_code(user, code, ip)
        self.reset(user["id"])

    # ------------------------------------------------------------ sign-in challenges
    def _purge(self) -> None:
        now = time.time()
        for k in [k for k, c in self._challenges.items() if c.expires < now]:
            self._challenges.pop(k, None)

    def challenge(self, user: dict[str, Any], password: str, ip: str | None) -> dict[str, Any] | None:
        """After a correct password: None if no second factor is needed, else the answer for the
        browser (a code is required, or enrolment is required first)."""
        if not self.applies(user):
            return None
        st = self.status(user)
        if not st["enabled"] and not st["required"]:
            return None
        token = secrets.token_urlsafe(32)
        kind = "verify" if st["enabled"] else "enrol"
        ch = Challenge(user_id=user["id"], password=password, kind=kind, ip=ip, expires=time.time() + CHALLENGE_TTL)
        out: dict[str, Any] = {"mfa_token": token, "authenticated": False}
        if kind == "verify":
            out["mfa_required"] = True
        else:
            ch.secret = totp.new_secret()
            out["mfa_setup_required"] = True
            out["enrol"] = self._enrol_view(user, ch.secret)
        with self._lock:
            self._purge()
            self._challenges[token] = ch
        return out

    def take(self, token: str, kind: str) -> Challenge:
        with self._lock:
            self._purge()
            ch = self._challenges.get(token or "")
            if ch is None or ch.kind != kind:
                raise SecurityError(401, "The sign-in expired - enter your password again", "mfa_expired")
            ch.attempts += 1
            if ch.attempts > CHALLENGE_ATTEMPTS:
                self._challenges.pop(token, None)
                raise SecurityError(401, "Too many wrong codes - enter your password again", "mfa_expired")
            return ch

    def done(self, token: str) -> None:
        with self._lock:
            self._challenges.pop(token, None)

    def enrol_at_login(self, ch: Challenge, user: dict[str, Any], code: str) -> list[str]:
        """Required enrolment during sign-in: activate the challenge's secret with a valid code."""
        stp = totp.match_step(ch.secret or "", code)
        if stp is None:
            limiter = self.sec.login_limiter
            limiter.hit(f"mfa:user:{user['id']}")
            raise SecurityError(400, "Wrong code - check the time on your phone and try again", "invalid_code")
        token = encrypt(self._k(), ch.secret or "", f"totp:{user['id']}")
        if self._row(user["id"]) is None:
            self._db().execute("INSERT INTO user_mfa (user_id, secret, enabled, last_step, created_at, enabled_at) "
                               "VALUES (?, ?, 1, ?, ?, ?)", (user["id"], token, stp, time.time(), time.time()))
        else:
            self._db().execute("UPDATE user_mfa SET secret = ?, enabled = 1, last_step = ?, enabled_at = ? "
                               "WHERE user_id = ?", (token, stp, time.time(), user["id"]))
        return self.new_recovery_codes(user["id"])
