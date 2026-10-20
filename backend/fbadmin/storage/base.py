"""Shared plumbing for the cloud storage clients: HTTP requests, errors, keys and the connection test."""

from __future__ import annotations

import email.utils
import json
import secrets
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, BinaryIO

# Upload chunk size: S3 parts, Azure blocks and GCS resumable chunks. Read at call time, so
# tests can lower it (S3 needs >= 5 MiB except for the last part; GCS needs a multiple of 256 KiB).
CHUNK_SIZE = 16 * 1024 * 1024
TIMEOUT = 60.0          # seconds, for metadata requests
UPLOAD_TIMEOUT = 300.0  # seconds, for one part/block/chunk
RETRIES = 3             # attempts for network errors, 5xx and 429
USER_AGENT = "fbadmin-storage"


class StorageError(Exception):
    """A provider returned an error or could not be reached; the message names the provider's code."""

    def __init__(self, message: str, status: int | None = None, code: str | None = None):
        super().__init__(message)
        self.status = status
        self.code = code


def nolog(_msg: str) -> None:
    pass


@dataclass
class Response:
    status: int
    headers: Any
    body: bytes = b""

    def header(self, name: str, default: str = "") -> str:
        return self.headers.get(name, default) if self.headers is not None else default

    def json(self) -> Any:
        return json.loads(self.body or b"{}")

    def xml(self) -> ET.Element:
        return ET.fromstring(self.body)


@dataclass
class Request:
    method: str
    url: str
    headers: dict[str, str] = field(default_factory=dict)
    body: bytes | None = None


def _opener() -> urllib.request.OpenerDirector:
    # No redirect handler: a 3xx (e.g. GCS's 308 "Resume Incomplete") is returned, not followed.
    # The default handlers include ProxyHandler, so HTTPS_PROXY / NO_PROXY are respected.
    handlers = [h for h in urllib.request.build_opener().handlers
                if not isinstance(h, urllib.request.HTTPRedirectHandler)]
    op = urllib.request.OpenerDirector()
    for h in handlers:
        op.add_handler(h)
    return op


_OPENER = _opener()


def send(req: Request, *, ok: Iterable[int] = (200, 201, 202, 204, 206), timeout: float = TIMEOUT,
         retries: int = RETRIES, sign: Callable[[Request], None] | None = None,
         provider: str = "storage", parse_error: Callable[[Response], tuple[str, str]] | None = None
         ) -> Response:
    """Send one request (re-signed on every attempt), retrying network errors, 5xx and 429."""
    ok = tuple(ok)
    last: Exception | None = None
    for attempt in range(max(1, retries)):
        if attempt:
            time.sleep(min(2 ** attempt, 10) * 0.5)
        r = Request(req.method, req.url, dict(req.headers), req.body)
        if sign:
            sign(r)
        r.headers.setdefault("User-Agent", USER_AGENT)
        if r.body is not None:
            r.headers.setdefault("Content-Type", "application/octet-stream")
        ureq = urllib.request.Request(r.url, data=r.body, headers=r.headers, method=r.method)
        try:
            with _OPENER.open(ureq, timeout=timeout) as resp:
                res = Response(resp.status, resp.headers, resp.read())
        except urllib.error.HTTPError as e:
            res = Response(e.code, e.headers, e.read() if e.fp else b"")
        except (urllib.error.URLError, OSError) as e:
            reason = getattr(e, "reason", e)
            last = StorageError(f"{provider}: cannot reach {_origin(r.url)}: {reason}")
            continue
        if res.status in ok:
            return res
        code, message = (parse_error or generic_error)(res)
        last = StorageError(f"{provider}: {code or 'HTTP ' + str(res.status)}"
                            + (f" - {message}" if message else ""), res.status, code)
        if res.status < 500 and res.status != 429:
            break
    assert last is not None
    raise last


def _origin(url: str) -> str:
    from urllib.parse import urlsplit

    u = urlsplit(url)
    return f"{u.scheme}://{u.netloc}"


def strip_ns(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def xml_find(el: ET.Element, name: str) -> ET.Element | None:
    for c in el:
        if strip_ns(c.tag) == name:
            return c
    return None


def xml_text(el: ET.Element | None, name: str, default: str = "") -> str:
    c = xml_find(el, name) if el is not None else None
    return (c.text or default) if c is not None else default


def xml_error(res: Response) -> tuple[str, str]:
    """(code, message) from an S3/Azure ``<Error><Code/><Message/></Error>`` body."""
    code = res.header("x-ms-error-code")
    message = ""
    try:
        root = ET.fromstring(res.body)
        if strip_ns(root.tag) == "Error":
            code = xml_text(root, "Code") or code
            message = xml_text(root, "Message").strip().split("\n")[0]
    except ET.ParseError:
        message = res.body[:300].decode("utf-8", "replace").strip()
    return code, message


def generic_error(res: Response) -> tuple[str, str]:
    try:
        data = json.loads(res.body)
    except ValueError:
        return xml_error(res)
    err = data.get("error") if isinstance(data, dict) else None
    if isinstance(err, dict):  # Google JSON API
        reason = (err.get("errors") or [{}])[0].get("reason", "")
        return str(reason or err.get("status") or err.get("code") or ""), str(err.get("message", ""))
    if isinstance(err, str):  # OAuth2 token endpoint
        return err, str(data.get("error_description", ""))
    return "", res.body[:300].decode("utf-8", "replace")


def parse_time(value: str) -> float:
    """ISO 8601 (S3, GCS) or RFC 1123 (Azure) timestamp -> epoch seconds."""
    if not value:
        return 0.0
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return email.utils.parsedate_to_datetime(value).timestamp()


def join_key(*parts: str) -> str:
    """Join key segments with single slashes, ignoring empty ones."""
    return "/".join(p.strip("/") for p in parts if p and p.strip("/"))


def read_chunks(f: BinaryIO, size: int, chunk: int):
    """Yield (offset, bytes) for a file of ``size`` bytes, ``chunk`` bytes at a time."""
    off = 0
    while off < size or (size == 0 and off == 0):
        data = f.read(min(chunk, size - off))
        yield off, data
        off += len(data)
        if not data:
            break


class BaseClient:
    """Common parts of the S3, Azure and GCS clients. Keys are full object names (no implicit prefix)."""

    provider = "storage"

    def __init__(self, prefix: str = ""):
        self.prefix = prefix.strip("/")

    # implemented per provider
    def put_bytes(self, key: str, data: bytes) -> None: raise NotImplementedError
    def size_of(self, key: str) -> int: raise NotImplementedError
    def list(self, prefix: str) -> list[dict]: raise NotImplementedError
    def delete(self, key: str) -> None: raise NotImplementedError
    def describe(self) -> str: raise NotImplementedError

    def upload_file(self, path: Path, key: str, log: Callable[[str], None] = nolog) -> dict:
        raise NotImplementedError

    def test(self) -> dict:
        """Write, list, stat and delete a tiny object under the target's prefix."""
        key = join_key(self.prefix, f".fbadmin-test-{secrets.token_hex(6)}")
        data = b"fbadmin storage target test\n"
        t0 = time.monotonic()
        self.put_bytes(key, data)
        try:
            listed = [o for o in self.list(key) if o["key"] == key]
            if not listed:
                raise StorageError(f"{self.provider}: wrote {key} but it is not listed")
            size = self.size_of(key)
            if size != len(data):
                raise StorageError(f"{self.provider}: wrote {len(data)} bytes to {key} but read back {size}")
        finally:
            self.delete(key)
        ms = round((time.monotonic() - t0) * 1000)
        return {"ok": True, "details": f"{self.describe()}: wrote, listed, read and deleted {key} in {ms} ms"}


def sort_newest(objs: list[dict]) -> list[dict]:
    return sorted(objs, key=lambda o: (o["modified"], o["key"]), reverse=True)
