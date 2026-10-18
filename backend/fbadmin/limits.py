"""Generic request limits: per-client rate limiting and a maximum request body size.

Both are plain ASGI middlewares, independent of authentication (login throttling for
server mode lives with the auth code). Configuration (environment):

``FBADMIN_RATE_LIMIT``
    Requests per minute per client IP for ``/api/*``, e.g. ``600``. ``0`` (default) turns
    rate limiting off. A burst of up to the same number is allowed (token bucket).
``FBADMIN_RATE_LIMIT_TRUST_PROXY``
    ``1`` to key clients by the first address in ``X-Forwarded-For`` (only behind a
    trusted reverse proxy).
``FBADMIN_MAX_REQUEST_BYTES``
    Maximum request body size in bytes (default 32 MiB, ``0`` = unlimited). Requests
    above it get HTTP 413.
"""

from __future__ import annotations

import json
import os
import threading
import time


def _int_env(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except ValueError:
        return default


async def _reject(send, status: int, detail: str, code: str, headers: list | None = None) -> None:
    body = json.dumps({"detail": detail, "code": code}).encode()
    await send({
        "type": "http.response.start",
        "status": status,
        "headers": [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode())]
        + (headers or []),
    })
    await send({"type": "http.response.body", "body": body})


class RateLimiter:
    """Token bucket per key: ``rate`` requests per ``per`` seconds, burst ``rate``."""

    def __init__(self, rate: int, per: float = 60.0) -> None:
        self.rate = rate
        self.per = per
        self._buckets: dict[str, tuple[float, float]] = {}
        self._lock = threading.Lock()

    def hit(self, key: str) -> float:
        """Take one token. Returns 0 when allowed, else seconds until a token is available."""
        now = time.monotonic()
        refill = self.rate / self.per
        with self._lock:
            tokens, last = self._buckets.get(key, (float(self.rate), now))
            tokens = min(float(self.rate), tokens + (now - last) * refill)
            if tokens >= 1:
                self._buckets[key] = (tokens - 1, now)
                wait = 0.0
            else:
                self._buckets[key] = (tokens, now)
                wait = (1 - tokens) / refill
            if len(self._buckets) > 10000:  # forget idle clients
                cutoff = now - self.per
                self._buckets = {k: v for k, v in self._buckets.items() if v[1] > cutoff}
        return wait


class RateLimitMiddleware:
    def __init__(self, app, rate: int | None = None, per: float = 60.0, trust_proxy: bool | None = None) -> None:
        self.app = app
        rate = _int_env("FBADMIN_RATE_LIMIT", 0) if rate is None else rate
        self.limiter = RateLimiter(rate, per) if rate > 0 else None
        if trust_proxy is None:
            trust_proxy = os.environ.get("FBADMIN_RATE_LIMIT_TRUST_PROXY", "0") in ("1", "true", "yes")
        self.trust_proxy = trust_proxy

    def _client(self, scope) -> str:
        if self.trust_proxy:
            for k, v in scope.get("headers") or []:
                if k == b"x-forwarded-for":
                    return v.decode("latin-1").split(",")[0].strip()
        client = scope.get("client")
        return client[0] if client else "-"

    async def __call__(self, scope, receive, send):
        path = scope.get("path", "")
        # Health checks (container/Kubernetes probes) are never throttled.
        if self.limiter is None or scope["type"] != "http" or not path.startswith("/api/") or path == "/api/health":
            return await self.app(scope, receive, send)
        wait = self.limiter.hit(self._client(scope))
        if wait > 0:
            return await _reject(send, 429, "Too many requests", "rate_limited",
                                 [(b"retry-after", str(max(1, round(wait))).encode())])
        return await self.app(scope, receive, send)


class BodySizeLimitMiddleware:
    def __init__(self, app, max_bytes: int | None = None) -> None:
        self.app = app
        self.max_bytes = _int_env("FBADMIN_MAX_REQUEST_BYTES", 32 * 1024 * 1024) if max_bytes is None else max_bytes

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or self.max_bytes <= 0:
            return await self.app(scope, receive, send)
        for k, v in scope.get("headers") or []:
            if k == b"content-length":
                try:
                    too_big = int(v) > self.max_bytes
                except ValueError:
                    too_big = False
                if too_big:
                    return await _reject(send, 413, f"Request body larger than {self.max_bytes} bytes",
                                         "request_too_large")
        # Chunked bodies have no Content-Length: count while streaming. When the limit is
        # exceeded we answer 413 ourselves and drop whatever the app sends afterwards.
        state = {"received": 0, "started": False, "rejected": False}

        async def _send(message):
            if state["rejected"]:
                return
            if message["type"] == "http.response.start":
                state["started"] = True
            await send(message)

        async def _receive():
            message = await receive()
            if message["type"] == "http.request" and not state["rejected"]:
                state["received"] += len(message.get("body", b""))
                if state["received"] > self.max_bytes and not state["started"]:
                    state["rejected"] = True
                    await _reject(send, 413, f"Request body larger than {self.max_bytes} bytes",
                                  "request_too_large")
                    raise RequestTooLarge
            return message

        try:
            await self.app(scope, _receive, _send)
        except Exception:
            if not state["rejected"]:
                raise


class RequestTooLarge(Exception):
    """Raised from ``receive`` when a streamed body exceeds the limit."""
