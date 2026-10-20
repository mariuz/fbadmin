"""Azure Blob Storage with Shared Key or SAS authentication (also Azurite).

Files larger than one chunk are uploaded as blocks (Put Block) and committed with Put Block List.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
from collections.abc import Callable
from email.utils import formatdate
from pathlib import Path
from urllib.parse import parse_qsl, quote, unquote, urlsplit
from xml.sax.saxutils import escape

from . import base
from .base import BaseClient, Request, Response, nolog, send, xml_error, xml_find, xml_text

API_VERSION = "2021-08-06"

# Standard headers in the Shared Key string-to-sign, in order (after the verb)
_STD = ["content-encoding", "content-language", "content-length", "content-md5", "content-type", "date",
        "if-modified-since", "if-match", "if-none-match", "if-unmodified-since", "range"]


def string_to_sign(method: str, url: str, headers: dict[str, str], account: str) -> str:
    """Shared Key string-to-sign for the Blob service (version 2009-09-19 and later)."""
    h = {k.lower(): str(v).strip() for k, v in headers.items()}
    if h.get("content-length") == "0":
        h["content-length"] = ""
    lines = [method.upper()] + [h.get(n, "") for n in _STD]
    canon_headers = "".join(f"{k}:{h[k]}\n" for k in sorted(h) if k.startswith("x-ms-"))
    u = urlsplit(url)
    resource = f"/{account}{u.path or '/'}"
    params: dict[str, list[str]] = {}
    for k, v in parse_qsl(u.query, keep_blank_values=True):
        params.setdefault(k.lower(), []).append(unquote(v))
    for k in sorted(params):
        resource += f"\n{k}:{','.join(sorted(params[k]))}"
    return "\n".join(lines) + "\n" + canon_headers + resource


def shared_key(method: str, url: str, headers: dict[str, str], account: str, key: str) -> str:
    sig = hmac.new(base64.b64decode(key), string_to_sign(method, url, headers, account).encode(),
                   hashlib.sha256).digest()
    return f"SharedKey {account}:{base64.b64encode(sig).decode()}"


class AzureClient(BaseClient):
    provider = "Azure"

    def __init__(self, account: str, container: str, account_key: str = "", sas_token: str = "",
                 endpoint: str = "", prefix: str = ""):
        super().__init__(prefix)
        self.account, self.container = account, container
        self.key, self.sas = account_key, sas_token.lstrip("?")
        self.endpoint = (endpoint or f"https://{account}.blob.core.windows.net").rstrip("/")

    def describe(self) -> str:
        return f"Azure container {self.container} at {self.endpoint}"

    def _call(self, method: str, key: str = "", params: list[tuple[str, str]] | None = None,
              body: bytes | None = None, headers: dict[str, str] | None = None, ok=(200, 201, 202),
              timeout: float = base.TIMEOUT) -> Response:
        path = f"/{quote(self.container)}" + (f"/{quote(key, safe='/-_.~')}" if key else "")
        query = "&".join(f"{quote(k, safe='')}={quote(v, safe='')}" for k, v in (params or []))
        if self.sas and not self.key:
            query = "&".join(q for q in (query, self.sas) if q)
        url = self.endpoint + path + ("?" + query if query else "")
        headers = dict(headers or {})
        if body is not None:
            headers.setdefault("Content-Type", "application/octet-stream")

        def sign(r: Request) -> None:
            r.headers["x-ms-date"] = formatdate(usegmt=True)
            r.headers["x-ms-version"] = API_VERSION
            if self.key:
                h = dict(r.headers)
                if r.body is not None:
                    h["Content-Length"] = str(len(r.body))
                r.headers["Authorization"] = shared_key(r.method, r.url, h, self.account, self.key)

        return send(Request(method, url, headers, body), ok=ok, timeout=timeout, sign=sign,
                    provider=self.provider, parse_error=xml_error)

    def put_bytes(self, key: str, data: bytes) -> None:
        self._call("PUT", key, body=data, headers={"x-ms-blob-type": "BlockBlob"}, timeout=base.UPLOAD_TIMEOUT)

    def size_of(self, key: str) -> int:
        return int(self._call("HEAD", key).header("Content-Length", "0"))

    def delete(self, key: str) -> None:
        self._call("DELETE", key)

    def list(self, prefix: str) -> list[dict]:
        out: list[dict] = []
        marker = ""
        while True:
            params = [("restype", "container"), ("comp", "list"), ("prefix", prefix)]
            if marker:
                params.append(("marker", marker))
            root = self._call("GET", "", params).xml()
            blobs = xml_find(root, "Blobs")
            for b in blobs if blobs is not None else []:
                if base.strip_ns(b.tag) != "Blob":
                    continue
                props = xml_find(b, "Properties")
                out.append({"key": xml_text(b, "Name"), "size": int(xml_text(props, "Content-Length", "0")),
                            "modified": base.parse_time(xml_text(props, "Last-Modified"))})
            marker = xml_text(root, "NextMarker")
            if not marker:
                return base.sort_newest(out)

    def upload_file(self, path: Path, key: str, log: Callable[[str], None] = nolog) -> dict:
        size = path.stat().st_size
        chunk = min(base.CHUNK_SIZE, 4000 * 1024 * 1024)  # Azure's maximum block size
        if size <= chunk:
            with open(path, "rb") as f:
                self.put_bytes(key, f.read())
            log(f"Uploaded {key} ({size} bytes) to {self.describe()}")
            return {"key": key, "size": size}
        ids: list[str] = []
        total = -(-size // chunk)
        with open(path, "rb") as f:
            for off, data in base.read_chunks(f, size, chunk):
                bid = base64.b64encode(f"fbadmin-{len(ids):06d}".encode()).decode()  # same length for all
                self._call("PUT", key, [("comp", "block"), ("blockid", bid)], body=data,
                           timeout=base.UPLOAD_TIMEOUT)
                ids.append(bid)
                log(f"Uploaded block {len(ids)}/{total} of {key} ({off + len(data)}/{size} bytes)")
        xml = ('<?xml version="1.0" encoding="utf-8"?><BlockList>'
               + "".join(f"<Latest>{escape(i)}</Latest>" for i in ids) + "</BlockList>")
        self._call("PUT", key, [("comp", "blocklist")], body=xml.encode(),
                   headers={"Content-Type": "application/xml"}, timeout=base.UPLOAD_TIMEOUT)
        log(f"Uploaded {key} ({size} bytes, {len(ids)} blocks) to {self.describe()}")
        return {"key": key, "size": size}
