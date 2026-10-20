"""Time-based one-time passwords (RFC 6238 / RFC 4226) with the standard library only.

Authenticator apps (Google Authenticator, Aegis, 1Password, FreeOTP, ...) use HMAC-SHA1,
6 digits and 30-second steps, which are the defaults here.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import struct
import time
import urllib.parse

DIGITS = 6
PERIOD = 30
SECRET_BYTES = 20  # 160 bits, as recommended by RFC 4226
_ALGS = {"SHA1": hashlib.sha1, "SHA256": hashlib.sha256, "SHA512": hashlib.sha512}


def new_secret() -> str:
    """A random base32 secret (no padding), as shown to users for manual entry."""
    return base64.b32encode(secrets.token_bytes(SECRET_BYTES)).decode().rstrip("=")


def _key(secret_b32: str) -> bytes:
    s = secret_b32.strip().replace(" ", "").upper()
    return base64.b32decode(s + "=" * (-len(s) % 8))


def hotp(key: bytes, counter: int, digits: int = DIGITS, algorithm: str = "SHA1") -> str:
    """RFC 4226 HOTP value of ``counter`` (dynamic truncation)."""
    mac = hmac.new(key, struct.pack(">Q", counter), _ALGS[algorithm]).digest()
    offset = mac[-1] & 0x0F
    code = (struct.unpack(">I", mac[offset:offset + 4])[0] & 0x7FFFFFFF) % (10 ** digits)
    return str(code).zfill(digits)


def step(at: float | None = None, period: int = PERIOD) -> int:
    return int((time.time() if at is None else at) // period)


def totp(secret_b32: str, at: float | None = None, digits: int = DIGITS, period: int = PERIOD,
         algorithm: str = "SHA1") -> str:
    """RFC 6238 TOTP code for time ``at`` (default: now)."""
    return hotp(_key(secret_b32), step(at, period), digits, algorithm)


def match_step(secret_b32: str, code: str, at: float | None = None, window: int = 1,
               period: int = PERIOD) -> int | None:
    """The time step (within ±``window`` steps of ``at``) whose code equals ``code``, else None.

    Callers store the returned step and refuse any later code whose step is not greater
    (replay protection: each code works once)."""
    code = (code or "").strip().replace(" ", "")
    if not code.isdigit() or len(code) != DIGITS:
        return None
    key = _key(secret_b32)
    now = step(at, period)
    found = None
    for s in range(now - window, now + window + 1):
        if hmac.compare_digest(hotp(key, s), code):  # no early exit: constant work per check
            found = s
    return found


def otpauth_uri(secret_b32: str, account: str, issuer: str = "fbadmin") -> str:
    """``otpauth://`` URI for QR codes (Key Uri Format used by authenticator apps)."""
    label = urllib.parse.quote(f"{issuer}:{account}", safe="@:")
    q = urllib.parse.urlencode({"secret": secret_b32, "issuer": issuer, "algorithm": "SHA1",
                                "digits": DIGITS, "period": PERIOD})
    return f"otpauth://totp/{label}?{q}"
