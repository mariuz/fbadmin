"""Encryption of the backups fbadmin keeps and uploads (roadmap 2.2).

A backup is encrypted while it streams in from gbak, so no plaintext copy is written to disk, and it is
decrypted while it streams back to gbak (restore, test restore, restore drill). Files are named
``*.fbk.enc``:

    header (28 bytes): b"FBADMENC" | version 1 | key id (8 bytes) | nonce prefix (7 bytes) | chunk size (4, LE)
    chunks:            AES-256-GCM(chunk of up to ``chunk size`` bytes) + 16-byte tag

Each chunk's nonce is the nonce prefix, the chunk number (4 bytes, big endian) and a final flag (1 byte); the
header is the associated data of every chunk. So a changed, reordered, truncated or extended file fails to
decrypt (the STREAM construction), and the key id tells which key a file needs.

Keys are random 256-bit keys, written as ``fbk1:<base64url>``. They come from FBADMIN_BACKUP_KEY (one or more,
comma-separated; the first encrypts) or from ``<data dir>/backup-keys.json`` (created in the UI). A backup can
be decrypted without fbadmin running: ``fbadmin backup-decrypt FILE --key fbk1:...``.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
import struct
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, BinaryIO
from collections.abc import Iterator

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from ..config import settings

MAGIC = b"FBADMENC"
VERSION = 1
HEADER_LEN = 28
TAG = 16
CHUNK = 1 << 20
SUFFIX = ".fbk.enc"
KEY_PREFIX = "fbk1:"


class BackupKeyError(ValueError):
    """No key, a wrong key, or a damaged file."""


# ------------------------------------------------------------------ keys
def key_id(key: bytes) -> str:
    return hashlib.sha256(b"fbadmin backup key\0" + key).hexdigest()[:16]


def format_key(key: bytes) -> str:
    return KEY_PREFIX + base64.urlsafe_b64encode(key).decode().rstrip("=")


def parse_key(text: str) -> bytes:
    text = (text or "").strip()
    if not text.startswith(KEY_PREFIX):
        raise BackupKeyError("A backup key looks like fbk1:… (as fbadmin shows it when it creates one)")
    raw = text[len(KEY_PREFIX):]
    try:
        key = base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4))
    except (ValueError, TypeError):
        raise BackupKeyError("The backup key is not valid base64") from None
    if len(key) != 32:
        raise BackupKeyError("A backup key has 256 bits (43 characters after fbk1:)")
    return key


class Keyring:
    """The backup keys: from FBADMIN_BACKUP_KEY (read-only here) and stored in the data directory."""

    def __init__(self, data_dir: Path | None = None):
        self.file = Path(data_dir or settings.data_dir) / "backup-keys.json"
        self._lock = threading.Lock()

    # env keys: the first one encrypts
    @staticmethod
    def _env() -> list[bytes]:
        raw = os.environ.get("FBADMIN_BACKUP_KEY", "")
        return [parse_key(k) for k in raw.split(",") if k.strip()]

    def _stored(self) -> list[dict[str, Any]]:
        if not self.file.exists():
            return []
        return json.loads(self.file.read_text()).get("keys", [])

    def _save(self, keys: list[dict[str, Any]]) -> None:
        self.file.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.file.with_suffix(".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump({"keys": keys}, f, indent=1)
        os.replace(tmp, self.file)

    def list(self) -> list[dict[str, Any]]:
        """Key ids, where they come from and which one encrypts; never the keys themselves."""
        active = self.active_id()
        out = [{"id": key_id(k), "source": "environment", "active": key_id(k) == active, "created": None}
               for k in self._env()]
        seen = {o["id"] for o in out}
        for s in self._stored():
            if s["id"] not in seen:
                out.append({"id": s["id"], "source": "stored", "active": s["id"] == active,
                            "created": s.get("created")})
        return out

    def active_id(self) -> str | None:
        env = self._env()
        if env:
            return key_id(env[0])
        return next((s["id"] for s in self._stored() if s.get("active")), None)

    def active(self) -> tuple[str, bytes] | None:
        env = self._env()
        if env:
            return key_id(env[0]), env[0]
        s = next((s for s in self._stored() if s.get("active")), None)
        return (s["id"], parse_key(s["key"])) if s else None

    def get(self, kid: str) -> bytes:
        for k in self._env():
            if key_id(k) == kid:
                return k
        for s in self._stored():
            if s["id"] == kid:
                return parse_key(s["key"])
        raise BackupKeyError(f"The backup was encrypted with key {kid}, which fbadmin doesn't have: add it under "
                             "Backup encryption (or to FBADMIN_BACKUP_KEY)")

    def generate(self) -> str:
        """A new key; it encrypts from now on (unless FBADMIN_BACKUP_KEY is set). Returns it as text, once."""
        key = AESGCM.generate_key(256)
        with self._lock:
            keys = [{**s, "active": False} for s in self._stored()]
            keys.append({"id": key_id(key), "key": format_key(key), "created": time.time(), "active": True})
            self._save(keys)
        return format_key(key)

    def add(self, text: str, *, activate: bool = False) -> str:
        """Add a key (for older backups, or another fbadmin's)."""
        key = parse_key(text)
        kid = key_id(key)
        with self._lock:
            keys = [s for s in self._stored() if s["id"] != kid]
            if activate:
                keys = [{**s, "active": False} for s in keys]
            keys.append({"id": kid, "key": format_key(key), "created": time.time(), "active": activate})
            self._save(keys)
        return kid

    def activate(self, kid: str) -> None:
        with self._lock:
            keys = self._stored()
            if not any(s["id"] == kid for s in keys):
                raise KeyError(f"Unknown stored key {kid}")
            self._save([{**s, "active": s["id"] == kid} for s in keys])

    def remove(self, kid: str) -> None:
        with self._lock:
            keys = self._stored()
            if not any(s["id"] == kid for s in keys):
                raise KeyError(f"Unknown stored key {kid}")
            self._save([s for s in keys if s["id"] != kid])

    def export(self, kid: str) -> str:
        for s in self._stored():
            if s["id"] == kid:
                return s["key"]
        raise KeyError(f"Unknown stored key {kid}")


def keyring() -> Keyring:
    return Keyring()


# ------------------------------------------------------------------ format
def _nonce(prefix: bytes, n: int, final: bool) -> bytes:
    return prefix + struct.pack(">I", n) + (b"\x01" if final else b"\x00")


class EncryptingWriter:
    """File-like writer: ``write`` plaintext, ``close`` writes the final chunk."""

    def __init__(self, f: BinaryIO, key: bytes, chunk: int = CHUNK):
        self.f, self.aead, self.chunk = f, AESGCM(key), chunk
        self.prefix = secrets.token_bytes(7)
        self.header = MAGIC + bytes([VERSION]) + bytes.fromhex(key_id(key)) + self.prefix + struct.pack("<I", chunk)
        f.write(self.header)
        self.buf = bytearray()
        self.n = 0
        self.closed = False

    def _emit(self, data: bytes, final: bool) -> None:
        if self.n >= 0xFFFFFFFF:
            raise BackupKeyError("The backup is too large for one encrypted file")
        self.f.write(self.aead.encrypt(_nonce(self.prefix, self.n, final), bytes(data), self.header))
        self.n += 1

    def write(self, data: bytes) -> int:
        self.buf += data
        # keep at least one byte back: the last chunk must be written with the final flag
        while len(self.buf) > self.chunk:
            self._emit(self.buf[:self.chunk], False)
            del self.buf[:self.chunk]
        return len(data)

    def close(self) -> None:
        if not self.closed:
            self._emit(self.buf, True)
            self.buf = bytearray()
            self.closed = True


def read_header(f: BinaryIO) -> dict[str, Any] | None:
    h = f.read(HEADER_LEN)
    if len(h) < HEADER_LEN or not h.startswith(MAGIC):
        return None
    if h[8] != VERSION:
        raise BackupKeyError(f"Unknown encrypted backup version {h[8]}")
    return {"header": h, "key_id": h[9:17].hex(), "prefix": h[17:24], "chunk": struct.unpack("<I", h[24:28])[0]}


class DecryptingReader:
    """File-like reader of the plaintext of an encrypted backup (raises BackupKeyError when it was changed)."""

    def __init__(self, f: BinaryIO, ring: Keyring | None = None, key: bytes | None = None):
        self.f = f
        h = read_header(f)
        if h is None:
            raise BackupKeyError("Not an encrypted fbadmin backup")
        self.h = h
        self.key_id = h["key_id"]
        key = key or (ring or keyring()).get(h["key_id"])
        if key_id(key) != h["key_id"]:
            raise BackupKeyError(f"This backup needs key {h['key_id']}, not {key_id(key)}")
        self.aead = AESGCM(key)
        self.size = h["chunk"] + TAG
        self.n = 0
        self.pending = f.read(self.size)
        self.out = bytearray()
        self.done = False

    def _next(self) -> None:
        nxt = self.f.read(self.size)
        final = not nxt
        try:
            plain = self.aead.decrypt(_nonce(self.h["prefix"], self.n, final), self.pending, self.h["header"])
        except InvalidTag:
            raise BackupKeyError("The encrypted backup is damaged, truncated or was changed (authentication "
                                 f"failed at chunk {self.n})") from None
        self.n += 1
        self.out += plain
        self.pending = nxt
        self.done = final

    def read(self, n: int = -1) -> bytes:
        while not self.done and (n < 0 or len(self.out) < n):
            self._next()
        if n < 0:
            n = len(self.out)
        data = bytes(self.out[:n])
        del self.out[:n]
        return data


def is_encrypted(path: Path) -> bool:
    try:
        with open(path, "rb") as f:
            return f.read(len(MAGIC)) == MAGIC
    except OSError:
        return False


def info(path: Path) -> dict[str, Any] | None:
    """Key id and plaintext size of an encrypted backup (None: not encrypted)."""
    with open(path, "rb") as f:
        h = read_header(f)
    if h is None:
        return None
    body = path.stat().st_size - HEADER_LEN
    chunks = max(1, -(-body // (h["chunk"] + TAG)))
    return {"key_id": h["key_id"], "plain_size": body - TAG * chunks}


@contextmanager
def open_plain(path: Path, ring: Keyring | None = None) -> Iterator[tuple[BinaryIO | DecryptingReader, int]]:
    """(reader, plaintext size) for an archived backup, encrypted or not."""
    with open(path, "rb") as f:
        h = read_header(f)
        f.seek(0)
        if h is None:
            yield f, path.stat().st_size
            return
        size = info(path)["plain_size"]  # type: ignore[index]
        yield DecryptingReader(f, ring), size


@contextmanager
def writer(f: BinaryIO, key: bytes | None) -> Iterator[Any]:
    """Write plaintext to ``f``, encrypted when ``key`` is given."""
    if key is None:
        yield f
        return
    w = EncryptingWriter(f, key)
    yield w
    w.close()


def decrypt_file(src: Path, dest: Path, *, key: bytes | None = None, ring: Keyring | None = None) -> int:
    """Decrypt ``src`` into ``dest`` (written as ``dest.part`` and renamed when complete)."""
    tmp = dest.with_name(dest.name + ".part")
    n = 0
    try:
        with open(src, "rb") as f, open(tmp, "wb") as out:
            r = DecryptingReader(f, ring, key)
            while True:
                data = r.read(CHUNK)
                if not data:
                    break
                out.write(data)
                n += len(data)
        tmp.replace(dest)
    finally:
        tmp.unlink(missing_ok=True)
    return n
