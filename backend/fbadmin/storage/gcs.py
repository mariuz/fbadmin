"""Google Cloud Storage through the JSON API (also fake-gcs-server).

Authentication: a service account key (OAuth2 JWT bearer flow, RS256). Uploads use a
resumable upload session, sent in chunks (multiples of 256 KiB).
"""

from __future__ import annotations

import base64
import json
import threading
import time
from collections.abc import Callable
from pathlib import Path
from urllib.parse import quote, urlencode

from . import base
from .base import BaseClient, Request, Response, StorageError, generic_error, nolog, send

SCOPE = "https://www.googleapis.com/auth/devstorage.read_write"
TOKEN_URI = "https://oauth2.googleapis.com/token"
_tokens: dict[str, tuple[str, float]] = {}  # client_email + key id -> (access token, expiry)
_tokens_lock = threading.Lock()


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def make_jwt(info: dict, now: float | None = None) -> str:
    """A signed JWT assertion for the OAuth2 JWT bearer grant."""
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import padding

    now = int(now or time.time())
    header = {"alg": "RS256", "typ": "JWT"}
    if info.get("private_key_id"):
        header["kid"] = info["private_key_id"]
    claims = {"iss": info["client_email"], "scope": SCOPE, "aud": info.get("token_uri") or TOKEN_URI,
              "iat": now, "exp": now + 3600}
    signing_input = _b64(json.dumps(header).encode()) + "." + _b64(json.dumps(claims).encode())
    key = serialization.load_pem_private_key(info["private_key"].encode(), password=None)
    sig = key.sign(signing_input.encode(), padding.PKCS1v15(), hashes.SHA256())  # type: ignore[union-attr]
    return signing_input + "." + _b64(sig)


def access_token(info: dict) -> str:
    """OAuth2 access token for a service account, cached until shortly before it expires."""
    cache_key = f"{info['client_email']}:{info.get('private_key_id', '')}:{info.get('token_uri', '')}"
    with _tokens_lock:
        tok = _tokens.get(cache_key)
        if tok and tok[1] > time.time() + 60:
            return tok[0]
    body = urlencode({"grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
                      "assertion": make_jwt(info)}).encode()
    res = send(Request("POST", info.get("token_uri") or TOKEN_URI,
                       {"Content-Type": "application/x-www-form-urlencoded"}, body),
               ok=(200,), provider="GCS OAuth", parse_error=generic_error)
    data = res.json()
    if not data.get("access_token"):
        raise StorageError("GCS OAuth: the token response has no access_token")
    with _tokens_lock:
        _tokens[cache_key] = (data["access_token"], time.time() + float(data.get("expires_in", 3600)))
    return data["access_token"]


class GCSClient(BaseClient):
    provider = "GCS"

    def __init__(self, bucket: str, service_account_json: str = "", endpoint: str = "", prefix: str = ""):
        super().__init__(prefix)
        self.bucket = bucket
        self.endpoint = (endpoint or "https://storage.googleapis.com").rstrip("/")
        self.info: dict | None = None
        if service_account_json:
            try:
                self.info = json.loads(service_account_json)
            except ValueError as e:
                raise ValueError(f"service_account_json is not valid JSON: {e}") from None

    def describe(self) -> str:
        return f"gs://{self.bucket} at {self.endpoint}"

    def _sign(self, r: Request) -> None:
        if self.info:
            r.headers["Authorization"] = f"Bearer {access_token(self.info)}"

    def _call(self, method: str, url: str, body: bytes | None = None, headers: dict[str, str] | None = None,
              ok=(200, 201, 204), timeout: float = base.TIMEOUT) -> Response:
        return send(Request(method, url, dict(headers or {}), body), ok=ok, timeout=timeout, sign=self._sign,
                    provider=self.provider, parse_error=generic_error)

    def _obj_url(self, key: str) -> str:
        return f"{self.endpoint}/storage/v1/b/{quote(self.bucket, safe='')}/o/{quote(key, safe='')}"

    def put_bytes(self, key: str, data: bytes) -> None:
        url = (f"{self.endpoint}/upload/storage/v1/b/{quote(self.bucket, safe='')}/o?"
               + urlencode({"uploadType": "media", "name": key}))
        self._call("POST", url, data, timeout=base.UPLOAD_TIMEOUT)

    def size_of(self, key: str) -> int:
        return int(self._call("GET", self._obj_url(key)).json().get("size", 0))

    def delete(self, key: str) -> None:
        self._call("DELETE", self._obj_url(key))

    def get_range(self, key: str, start: int, end: int) -> Response:
        return self._call("GET", self._obj_url(key) + "?alt=media", headers={"Range": f"bytes={start}-{end}"},
                          ok=(200, 206), timeout=base.UPLOAD_TIMEOUT)

    def list(self, prefix: str) -> list[dict]:
        out: list[dict] = []
        token = ""
        while True:
            q = {"prefix": prefix, "fields": "items(name,size,updated),nextPageToken"}
            if token:
                q["pageToken"] = token
            data = self._call("GET", f"{self.endpoint}/storage/v1/b/{quote(self.bucket, safe='')}/o?"
                              + urlencode(q)).json()
            for it in data.get("items", []):
                out.append({"key": it["name"], "size": int(it.get("size", 0)),
                            "modified": base.parse_time(it.get("updated", ""))})
            token = data.get("nextPageToken", "")
            if not token:
                return base.sort_newest(out)

    def upload_file(self, path: Path, key: str, log: Callable[[str], None] = nolog) -> dict:
        size = path.stat().st_size
        quantum = 256 * 1024
        chunk = max(quantum, base.CHUNK_SIZE // quantum * quantum)
        if size <= chunk:
            with open(path, "rb") as f:
                self.put_bytes(key, f.read())
            log(f"Uploaded {key} ({size} bytes) to {self.describe()}")
            return {"key": key, "size": size}
        url = (f"{self.endpoint}/upload/storage/v1/b/{quote(self.bucket, safe='')}/o?"
               + urlencode({"uploadType": "resumable", "name": key}))
        res = self._call("POST", url, json.dumps({"name": key}).encode(),
                         {"Content-Type": "application/json; charset=UTF-8",
                          "X-Upload-Content-Type": "application/octet-stream",
                          "X-Upload-Content-Length": str(size)})
        session = res.header("Location")
        if not session:
            raise StorageError("GCS: the resumable upload request returned no session URI")
        total = -(-size // chunk)
        n = 0
        with open(path, "rb") as f:
            for off, data in base.read_chunks(f, size, chunk):
                n += 1
                end = off + len(data) - 1
                res = self._call("PUT", session, data, {"Content-Range": f"bytes {off}-{end}/{size}"},
                                 ok=(200, 201, 308), timeout=base.UPLOAD_TIMEOUT)
                if res.status == 308:
                    got = res.header("Range")  # "bytes=0-N": what the server has persisted
                    if got and int(got.rsplit("-", 1)[-1]) != end:
                        raise StorageError(f"GCS: server persisted {got}, expected up to byte {end}")
                log(f"Uploaded chunk {n}/{total} of {key} ({end + 1}/{size} bytes)")
        if res.status not in (200, 201):
            raise StorageError(f"GCS: upload of {key} did not complete (HTTP {res.status})")
        log(f"Uploaded {key} ({size} bytes, {n} chunks) to {self.describe()}")
        return {"key": key, "size": size}
