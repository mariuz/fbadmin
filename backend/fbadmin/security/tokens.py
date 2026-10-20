"""Personal API tokens for scripts and automation (server mode).

A token is ``fbat_`` + 43 random URL-safe characters (256 bits). Only its SHA-256 hash is
stored (a high-entropy secret needs no slow hash), so it is shown once. Requests send
``Authorization: Bearer <token>`` and act as the token's owner, limited by the token's scope:

* ``read-only`` tokens can only read (queries, metadata, monitoring) - like a read-only
  server registration, statements are classified and writes refused;
* tokens can be limited to some server registrations;
* the owner's fbadmin permissions (access.py) still apply, and a token never does more than
  its owner may.

Bearer requests do not use cookies at all (so no CSRF token is needed and a session cookie
is never accepted in place of a token), and a token cannot create other tokens, change
passwords or two-factor settings.

Saved Firebird passwords are encrypted with the user's vault key, which is only in memory
while the user is signed in. When a token is created from a signed-in session, a copy of
that key is stored wrapped (AES-GCM) under a key derived from the token itself, so the token
can use saved passwords even when its owner is signed out - but only someone holding the
token can unwrap it.
"""

from __future__ import annotations

import base64
import hashlib
import json
import secrets
import threading
import time
from typing import Any

from .crypto import decrypt, encrypt
from .errors import SecurityError
from .sessions import Principal

PREFIX = "fbat_"
SCOPES = ("read-only", "read-write")
MAX_DAYS = 3650
TOUCH_EVERY = 60  # seconds between last-used updates of one token

SCHEMA = """
CREATE TABLE IF NOT EXISTS api_tokens (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    name TEXT NOT NULL,
    token_hash TEXT NOT NULL UNIQUE,
    hint TEXT,
    scope TEXT NOT NULL DEFAULT 'read-write',
    server_ids TEXT,
    created_at REAL NOT NULL,
    expires_at REAL,
    last_used_at REAL,
    last_used_ip TEXT,
    revoked_at REAL,
    wrapped_key TEXT
);
"""
PUBLIC = ("id", "user_id", "name", "hint", "scope", "server_ids", "created_at", "expires_at", "last_used_at",
          "last_used_ip", "revoked_at")


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _wrap_key(token: str) -> bytes:
    return hashlib.sha256(b"fbadmin-token-vault:" + token.encode()).digest()


def new_token() -> str:
    return PREFIX + secrets.token_urlsafe(32)


class TokenService:
    def __init__(self, sec):
        self.sec = sec
        self._ready = False
        self._lock = threading.Lock()
        self._touched: dict[int, float] = {}

    def _db(self):
        if not self._ready:
            with self._lock:
                if not self._ready:
                    self.sec.db.conn().executescript(SCHEMA)
                    self._ready = True
        return self.sec.db

    @staticmethod
    def _view(r) -> dict[str, Any]:
        d = {k: r[k] for k in PUBLIC}
        d["server_ids"] = json.loads(d["server_ids"]) if d["server_ids"] else None
        now = time.time()
        d["status"] = ("revoked" if d["revoked_at"] else
                       "expired" if d["expires_at"] and d["expires_at"] < now else "active")
        return d

    # ------------------------------------------------------------ management
    def create(self, p: Principal, name: str, *, scope: str = "read-write", expires_days: int | None = 90,
               server_ids: list[str] | None = None, vault_key: bytes | None = None) -> tuple[dict[str, Any], str]:
        name = (name or "").strip()
        if not name or len(name) > 100:
            raise SecurityError(400, "Give the token a name (up to 100 characters)", "bad_name")
        if scope not in SCOPES:
            raise SecurityError(400, f"Scope must be one of {', '.join(SCOPES)}", "bad_scope")
        if expires_days is not None and not (1 <= expires_days <= MAX_DAYS):
            raise SecurityError(400, f"Expiry must be between 1 and {MAX_DAYS} days (or none)", "bad_expiry")
        token = new_token()
        now = time.time()
        wrapped = None
        if vault_key is not None:
            wrapped = encrypt(_wrap_key(token), base64.urlsafe_b64encode(vault_key).decode(), "fbadmin-token")
        cur = self._db().execute(
            "INSERT INTO api_tokens (user_id, name, token_hash, hint, scope, server_ids, created_at, expires_at, "
            "wrapped_key) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (p.user_id, name, hash_token(token), token[:len(PREFIX) + 4] + "…", scope,
             json.dumps(sorted(set(server_ids))) if server_ids else None, now,
             now + expires_days * 86400 if expires_days else None, wrapped))
        row = self._db().execute("SELECT * FROM api_tokens WHERE id = ?", (cur.lastrowid,)).fetchone()
        return self._view(row), token

    def list_for(self, user_id: int) -> list[dict[str, Any]]:
        if not self.sec.db.exists:
            return []
        rows = self._db().execute("SELECT * FROM api_tokens WHERE user_id = ? ORDER BY created_at DESC", (user_id,))
        return [self._view(r) for r in rows.fetchall()]

    def list_all(self) -> list[dict[str, Any]]:
        if not self.sec.db.exists:
            return []
        rows = self._db().execute(
            "SELECT t.*, u.username FROM api_tokens t LEFT JOIN users u ON u.id = t.user_id "
            "ORDER BY t.created_at DESC").fetchall()
        return [{**self._view(r), "username": r["username"]} for r in rows]

    def get(self, token_id: int) -> dict[str, Any]:
        r = self._db().execute("SELECT * FROM api_tokens WHERE id = ?", (token_id,)).fetchone()
        if r is None:
            raise KeyError(f"Unknown token {token_id}")
        return self._view(r)

    def revoke(self, token_id: int) -> dict[str, Any]:
        self._db().execute("UPDATE api_tokens SET revoked_at = ?, wrapped_key = NULL WHERE id = ? AND revoked_at IS NULL",
                           (time.time(), token_id))
        mgr = getattr(self.sec.state, "manager", None)
        if mgr is not None and hasattr(mgr, "close_token"):
            mgr.close_token(token_id)
        return self.get(token_id)

    def delete(self, token_id: int) -> None:
        self.revoke(token_id)
        self._db().execute("DELETE FROM api_tokens WHERE id = ?", (token_id,))

    def forget_user(self, user_id: int) -> None:
        if not self.sec.db.exists:
            return
        for t in self.list_for(user_id):
            self.delete(t["id"])

    # ------------------------------------------------------------ authentication
    def authenticate(self, token: str, ip: str | None) -> Principal | None:
        """The principal for a bearer token, or None if it is unknown, expired, revoked or its user disabled."""
        if not token.startswith(PREFIX) or len(token) > 200 or not self.sec.db.exists:
            return None
        r = self._db().execute("SELECT * FROM api_tokens WHERE token_hash = ?", (hash_token(token),)).fetchone()
        now = time.time()
        if r is None or r["revoked_at"] or (r["expires_at"] and r["expires_at"] < now):
            return None
        user = self.sec.db.get_user(r["user_id"])
        if user is None or not user["active"]:
            return None
        key = None
        if r["wrapped_key"]:
            try:
                key = base64.urlsafe_b64decode(decrypt(_wrap_key(token), r["wrapped_key"], "fbadmin-token"))
            except ValueError:
                key = None
        if now - self._touched.get(r["id"], 0) > TOUCH_EVERY:
            self._touched[r["id"]] = now
            self._db().execute("UPDATE api_tokens SET last_used_at = ?, last_used_ip = ? WHERE id = ?",
                               (now, ip, r["id"]))
        return Principal(user_id=user["id"], username=user["username"], role=user["role"],
                         auth_source=user["auth_source"], token_id=r["id"], token_name=r["name"],
                         token_read_only=r["scope"] == "read-only",
                         token_servers=json.loads(r["server_ids"]) if r["server_ids"] else None, token_key=key)
