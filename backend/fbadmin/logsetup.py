"""Logging configuration: plain or JSON (structured) output, plus request logging.

Configured from the command line (``--log-level``, ``--log-format``) or the environment
(``FBADMIN_LOG_LEVEL``, ``FBADMIN_LOG_FORMAT=json|text``, ``FBADMIN_ACCESS_LOG=0`` to turn
request logging off). Request logs go to the ``fbadmin.access`` logger with the method, path,
status and duration in milliseconds as structured fields; uvicorn's own access log is
disabled so every request is logged exactly once.
"""

from __future__ import annotations

import json
import logging
import os
import time
from datetime import datetime, timezone
from typing import Any

LEVELS = ("critical", "error", "warning", "info", "debug")

# Attributes every LogRecord has; anything else passed via ``extra=`` is a structured field.
_RESERVED = set(vars(logging.LogRecord("x", 0, "x", 0, "x", None, None))) | {"message", "asctime", "taskName", "color_message"}


class JsonFormatter(logging.Formatter):
    """One JSON object per line: ts, level, logger, msg, plus any ``extra`` fields."""

    def format(self, record: logging.LogRecord) -> str:
        out: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, timezone.utc).isoformat(timespec="milliseconds"),
            "level": record.levelname.lower(),
            "logger": record.name,
            "msg": record.getMessage(),
        }
        for k, v in record.__dict__.items():
            if k not in _RESERVED and not k.startswith("_"):
                out[k] = v
        if record.exc_info:
            out["exc"] = self.formatException(record.exc_info)
        return json.dumps(out, default=str)


class TextFormatter(logging.Formatter):
    def __init__(self) -> None:
        super().__init__("%(asctime)s %(levelname)-7s %(name)s: %(message)s")


def configure_logging(level: str | None = None, fmt: str | None = None) -> dict:
    """Configure the root, fbadmin and uvicorn loggers. Returns a uvicorn ``log_config``."""
    level = (level or os.environ.get("FBADMIN_LOG_LEVEL") or "info").lower()
    fmt = (fmt or os.environ.get("FBADMIN_LOG_FORMAT") or "text").lower()
    if level not in LEVELS:
        raise ValueError(f"invalid log level {level!r}; choose one of {', '.join(LEVELS)}")
    formatter = "json" if fmt == "json" else "text"
    return {
        "version": 1,
        "disable_existing_loggers": False,
        "formatters": {
            "json": {"()": JsonFormatter},
            "text": {"()": TextFormatter},
        },
        "handlers": {
            "default": {"class": "logging.StreamHandler", "formatter": formatter, "stream": "ext://sys.stderr"},
        },
        "loggers": {
            "": {"handlers": ["default"], "level": level.upper()},
            "uvicorn": {"handlers": [], "level": level.upper(), "propagate": True},
            "uvicorn.error": {"handlers": [], "level": level.upper(), "propagate": True},
            # Requests are logged by RequestLogMiddleware instead (with timing).
            "uvicorn.access": {"handlers": [], "level": "WARNING", "propagate": False},
        },
    }


def apply_logging(level: str | None = None, fmt: str | None = None) -> dict:
    import logging.config

    cfg = configure_logging(level, fmt)
    logging.config.dictConfig(cfg)
    return cfg


access_log = logging.getLogger("fbadmin.access")


class RequestLogMiddleware:
    """Pure ASGI middleware logging one line per HTTP request with its duration."""

    def __init__(self, app, enabled: bool | None = None) -> None:
        self.app = app
        if enabled is None:
            enabled = os.environ.get("FBADMIN_ACCESS_LOG", "1").lower() not in ("0", "false", "no", "off")
        self.enabled = enabled

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or not self.enabled:
            return await self.app(scope, receive, send)
        start = time.perf_counter()
        status = {"code": 500}

        async def _send(message):
            if message["type"] == "http.response.start":
                status["code"] = message["status"]
            await send(message)

        try:
            await self.app(scope, receive, _send)
        finally:
            ms = round((time.perf_counter() - start) * 1000, 1)
            path = scope.get("path", "")
            # Static assets are noise at info level.
            lvl = logging.DEBUG if path.startswith("/assets/") else logging.INFO
            if status["code"] >= 500:
                lvl = logging.ERROR
            client = scope.get("client") or ("-", 0)
            access_log.log(
                lvl,
                "%s %s %s %.1fms",
                scope.get("method"), path, status["code"], ms,
                extra={"method": scope.get("method"), "path": path, "status": status["code"],
                       "duration_ms": ms, "client": client[0]},
            )
