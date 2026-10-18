"""Installs the engineering middlewares (request log, rate limit, body size) on the app."""

from __future__ import annotations

from fastapi import FastAPI

from .limits import BodySizeLimitMiddleware, RateLimitMiddleware
from .logsetup import RequestLogMiddleware


def install_middleware(app: FastAPI) -> None:
    # Starlette wraps in reverse order: the last one added is the outermost, so the
    # request log also records requests rejected by the limits (429 / 413).
    app.add_middleware(BodySizeLimitMiddleware)
    app.add_middleware(RateLimitMiddleware)
    app.add_middleware(RequestLogMiddleware)
