"""Password hashing, key derivation and authenticated encryption.

* Login passwords are hashed with Argon2id (``argon2-cffi``); scrypt is used when
  argon2 is not installed.
* A *vault* protects saved Firebird passwords: a random 256-bit data key encrypts
  each password with AES-256-GCM; the data key itself is stored wrapped (AES-GCM)
  under a key derived from the master password (Argon2id, or scrypt as a fallback).
  Changing the master password only re-wraps the data key.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
import secrets
from typing import Any

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

try:  # pragma: no cover - import guard
    from argon2 import PasswordHasher
    from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError
    from argon2.low_level import Type, hash_secret_raw

    HAVE_ARGON2 = True
except ImportError:  # pragma: no cover
    HAVE_ARGON2 = False

TOKEN_PREFIX = "fbenc:v1:"

# Argon2id parameters (OWASP: m=19 MiB, t=2, p=1 minimum).
ARGON2_T = 2
ARGON2_M = 19456
ARGON2_P = 1
SCRYPT_N = 2**15


def _b64e(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).decode().rstrip("=")


def _b64d(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


# ------------------------------------------------------------ login passwords
def hash_password(password: str) -> str:
    if HAVE_ARGON2:
        return PasswordHasher(time_cost=ARGON2_T, memory_cost=ARGON2_M, parallelism=ARGON2_P).hash(password)
    salt = os.urandom(16)
    dk = hashlib.scrypt(password.encode(), salt=salt, n=SCRYPT_N, r=8, p=1, maxmem=64 * 1024 * 1024, dklen=32)
    return f"scrypt${SCRYPT_N}$8$1${_b64e(salt)}${_b64e(dk)}"


def verify_password(stored: str | None, password: str) -> bool:
    if not stored or not password:
        return False
    if stored.startswith("scrypt$"):
        _, n, r, p, salt, dk = stored.split("$")
        got = hashlib.scrypt(password.encode(), salt=_b64d(salt), n=int(n), r=int(r), p=int(p),
                             maxmem=64 * 1024 * 1024, dklen=32)
        return hmac.compare_digest(got, _b64d(dk))
    if HAVE_ARGON2 and stored.startswith("$argon2"):
        try:
            return PasswordHasher().verify(stored, password)
        except (VerifyMismatchError, VerificationError, InvalidHashError):
            return False
    return False


def dummy_verify() -> None:
    """Spend about the same time as a real verification (hides which usernames exist)."""
    verify_password(_DUMMY_HASH, "not the password")


_DUMMY_HASH = hash_password(secrets.token_hex(8))


# ------------------------------------------------------------ key derivation
def new_kdf_params() -> dict[str, Any]:
    if HAVE_ARGON2:
        return {"kdf": "argon2id", "t": ARGON2_T, "m": ARGON2_M, "p": ARGON2_P, "salt": _b64e(os.urandom(16))}
    return {"kdf": "scrypt", "n": SCRYPT_N, "r": 8, "p": 1, "salt": _b64e(os.urandom(16))}


def derive_key(password: str, params: dict[str, Any]) -> bytes:
    salt = _b64d(params["salt"])
    if params["kdf"] == "argon2id":
        if not HAVE_ARGON2:
            raise RuntimeError("argon2-cffi is required to open this vault")
        return hash_secret_raw(password.encode(), salt, time_cost=params["t"], memory_cost=params["m"],
                               parallelism=params["p"], hash_len=32, type=Type.ID)
    if params["kdf"] == "scrypt":
        return hashlib.scrypt(password.encode(), salt=salt, n=params["n"], r=params["r"], p=params["p"],
                              maxmem=128 * 1024 * 1024, dklen=32)
    raise ValueError(f"Unknown key derivation {params['kdf']!r}")


# ------------------------------------------------------------ AES-GCM
def encrypt(key: bytes, plaintext: str, aad: str = "") -> str:
    nonce = os.urandom(12)
    ct = AESGCM(key).encrypt(nonce, plaintext.encode("utf-8"), aad.encode() or None)
    return TOKEN_PREFIX + _b64e(nonce + ct)


def decrypt(key: bytes, token: str, aad: str = "") -> str:
    if not is_token(token):
        raise ValueError("Not an encrypted value")
    raw = _b64d(token[len(TOKEN_PREFIX):])
    try:
        return AESGCM(key).decrypt(raw[:12], raw[12:], aad.encode() or None).decode("utf-8")
    except InvalidTag:
        raise ValueError("Cannot decrypt (wrong key or tampered data)") from None


def is_token(value: Any) -> bool:
    return isinstance(value, str) and value.startswith(TOKEN_PREFIX)


# ------------------------------------------------------------ vault
class WrongPassword(ValueError):
    pass


def vault_create(password: str) -> tuple[dict[str, Any], bytes]:
    """New vault record protected by ``password`` and its (random) data key."""
    if not password:
        raise ValueError("The master password must not be empty")
    dek = AESGCM.generate_key(256)
    return vault_wrap(dek, password), dek


def vault_wrap(dek: bytes, password: str) -> dict[str, Any]:
    params = new_kdf_params()
    kek = derive_key(password, params)
    return {"version": 1, **params, "wrapped_key": encrypt(kek, _b64e(dek), "fbadmin-vault")}


def vault_unlock(record: dict[str, Any], password: str) -> bytes:
    kek = derive_key(password, record)
    try:
        return _b64d(decrypt(kek, record["wrapped_key"], "fbadmin-vault"))
    except ValueError:
        raise WrongPassword("Wrong master password") from None
