"""Amazon S3 and S3-compatible storage (MinIO, Cloudflare R2, Wasabi, SeaweedFS, Backblaze B2).

Requests are signed with AWS Signature Version 4; uploads larger than one chunk use multipart
upload (CreateMultipartUpload / UploadPart / CompleteMultipartUpload, aborted on failure).
"""

from __future__ import annotations

import hashlib
import hmac
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote, urlsplit
from xml.sax.saxutils import escape

from . import base
from .base import BaseClient, Request, Response, StorageError, nolog, send, xml_error, xml_text

EMPTY_SHA256 = hashlib.sha256(b"").hexdigest()


def uri_encode(s: str, safe: str = "-_.~") -> str:
    return quote(s, safe=safe)


def canonical_query(params: list[tuple[str, str]]) -> str:
    return "&".join(f"{uri_encode(k)}={uri_encode(v)}" for k, v in sorted(params))


def canonical_request(method: str, path: str, params: list[tuple[str, str]], headers: dict[str, str],
                      payload_hash: str) -> tuple[str, str]:
    """(canonical request, signed headers). ``path`` must already be URI-encoded (S3 does not encode twice)."""
    h = {k.lower().strip(): " ".join(str(v).split()) for k, v in headers.items()}
    names = sorted(h)
    signed = ";".join(names)
    canon = "\n".join([method, path or "/", canonical_query(params),
                       "".join(f"{n}:{h[n]}\n" for n in names), signed, payload_hash])
    return canon, signed


def _hmac(key: bytes, msg: str) -> bytes:
    return hmac.new(key, msg.encode(), hashlib.sha256).digest()


def signing_key(secret: str, date: str, region: str, service: str) -> bytes:
    k = _hmac(("AWS4" + secret).encode(), date)
    k = _hmac(k, region)
    k = _hmac(k, service)
    return _hmac(k, "aws4_request")


def sign_v4(method: str, path: str, params: list[tuple[str, str]], headers: dict[str, str],
            payload_hash: str, access_key: str, secret_key: str, region: str, service: str,
            amz_date: str) -> dict[str, str]:
    """Return the Authorization header value plus the intermediate strings (for tests)."""
    canon, signed = canonical_request(method, path, params, headers, payload_hash)
    scope = f"{amz_date[:8]}/{region}/{service}/aws4_request"
    sts = "\n".join(["AWS4-HMAC-SHA256", amz_date, scope, hashlib.sha256(canon.encode()).hexdigest()])
    sig = hmac.new(signing_key(secret_key, amz_date[:8], region, service), sts.encode(),
                   hashlib.sha256).hexdigest()
    return {"canonical_request": canon, "string_to_sign": sts, "signature": sig,
            "authorization": f"AWS4-HMAC-SHA256 Credential={access_key}/{scope}, "
                             f"SignedHeaders={signed}, Signature={sig}"}


class S3Client(BaseClient):
    provider = "S3"

    def __init__(self, bucket: str, access_key_id: str, secret_access_key: str, region: str = "us-east-1",
                 endpoint: str = "", path_style: bool | None = None, session_token: str = "",
                 prefix: str = ""):
        super().__init__(prefix)
        self.bucket, self.region = bucket, region or "us-east-1"
        self.access_key, self.secret_key, self.token = access_key_id, secret_access_key, session_token
        self.endpoint = (endpoint or f"https://s3.{self.region}.amazonaws.com").rstrip("/")
        self.path_style = (bool(endpoint) or "." in bucket) if path_style is None else path_style

    def describe(self) -> str:
        return f"s3://{self.bucket} at {self.endpoint}"

    # ----------------------------------------------------------------- requests
    def _url(self, key: str) -> tuple[str, str]:
        """(origin, encoded path) of an object (or the bucket when ``key`` is empty)."""
        u = urlsplit(self.endpoint)
        base_path = u.path.rstrip("/")
        enc = uri_encode(key, safe="-_.~/")
        if self.path_style:
            return f"{u.scheme}://{u.netloc}", f"{base_path}/{self.bucket}" + (f"/{enc}" if key else "")
        return f"{u.scheme}://{self.bucket}.{u.netloc}", f"{base_path}/{enc}" if key else (base_path or "/")

    def _call(self, method: str, key: str = "", params: list[tuple[str, str]] | None = None,
              body: bytes | None = None, headers: dict[str, str] | None = None, ok=(200, 204),
              timeout: float = base.TIMEOUT) -> Response:
        params = params or []
        origin, path = self._url(key)
        url = origin + path + (("?" + canonical_query(params)) if params else "")
        payload_hash = hashlib.sha256(body).hexdigest() if body else EMPTY_SHA256

        def sign(r: Request) -> None:
            amz_date = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            signed = {"host": urlsplit(origin).netloc, "x-amz-date": amz_date,
                      "x-amz-content-sha256": payload_hash}
            if self.token:
                signed["x-amz-security-token"] = self.token
            signed.update({k.lower(): v for k, v in r.headers.items() if k.lower().startswith("x-amz-")})
            auth = sign_v4(method, path, params, signed, payload_hash, self.access_key, self.secret_key,
                           self.region, "s3", amz_date)["authorization"]
            r.headers.update({("Host" if k == "host" else k): v for k, v in signed.items()})
            r.headers["Authorization"] = auth

        return send(Request(method, url, dict(headers or {}), body), ok=ok, timeout=timeout, sign=sign,
                    provider=self.provider, parse_error=xml_error)

    # --------------------------------------------------------------- operations
    def put_bytes(self, key: str, data: bytes) -> None:
        self._call("PUT", key, body=data, timeout=base.UPLOAD_TIMEOUT)

    def size_of(self, key: str) -> int:
        return int(self._call("HEAD", key).header("Content-Length", "0"))

    def delete(self, key: str) -> None:
        self._call("DELETE", key, ok=(200, 204))

    def get_range(self, key: str, start: int, end: int) -> Response:
        return self._call("GET", key, headers={"Range": f"bytes={start}-{end}"}, ok=(200, 206),
                          timeout=base.UPLOAD_TIMEOUT)

    def list(self, prefix: str) -> list[dict]:
        out: list[dict] = []
        token = ""
        while True:
            params = [("list-type", "2"), ("prefix", prefix)]
            if token:
                params.append(("continuation-token", token))
            root = self._call("GET", "", params).xml()
            for el in root:
                if base.strip_ns(el.tag) == "Contents":
                    out.append({"key": xml_text(el, "Key"), "size": int(xml_text(el, "Size", "0")),
                                "modified": base.parse_time(xml_text(el, "LastModified"))})
            token = xml_text(root, "NextContinuationToken")
            if xml_text(root, "IsTruncated") != "true" or not token:
                return base.sort_newest(out)

    def upload_file(self, path: Path, key: str, log: Callable[[str], None] = nolog) -> dict:
        size = path.stat().st_size
        chunk = base.CHUNK_SIZE
        if size <= chunk:
            with open(path, "rb") as f:
                self.put_bytes(key, f.read())
            log(f"Uploaded {key} ({size} bytes) to {self.describe()}")
            return {"key": key, "size": size}
        root = self._call("POST", key, [("uploads", "")], body=b"").xml()
        upload_id = xml_text(root, "UploadId")
        if not upload_id:
            raise StorageError("S3: CreateMultipartUpload returned no UploadId")
        parts: list[tuple[int, str]] = []
        total = -(-size // chunk)
        try:
            with open(path, "rb") as f:
                for off, data in base.read_chunks(f, size, chunk):
                    n = len(parts) + 1
                    res = self._call("PUT", key, [("partNumber", str(n)), ("uploadId", upload_id)], body=data,
                                     timeout=base.UPLOAD_TIMEOUT)
                    parts.append((n, res.header("ETag")))
                    log(f"Uploaded part {n}/{total} of {key} ({off + len(data)}/{size} bytes)")
            xml = "<CompleteMultipartUpload>" + "".join(
                f"<Part><PartNumber>{n}</PartNumber><ETag>{escape(etag)}</ETag></Part>" for n, etag in parts
            ) + "</CompleteMultipartUpload>"
            res = self._call("POST", key, [("uploadId", upload_id)], body=xml.encode(),
                             headers={"Content-Type": "application/xml"}, timeout=base.UPLOAD_TIMEOUT)
            root = res.xml()  # S3 may report a failure with 200 and an <Error> body
            if base.strip_ns(root.tag) == "Error":
                code, msg = xml_error(res)
                raise StorageError(f"S3: {code} - {msg}", res.status, code)
        except BaseException:
            try:
                self._call("DELETE", key, [("uploadId", upload_id)], ok=(200, 204, 404), timeout=base.TIMEOUT)
            except StorageError as e:
                log(f"Could not abort the multipart upload of {key}: {e}")
            raise
        log(f"Uploaded {key} ({size} bytes, {len(parts)} parts) to {self.describe()}")
        return {"key": key, "size": size}

