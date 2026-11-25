"""Large uploads streamed to disk (issue #10): restore from an uploaded backup and data import files.

These routes write the request body to disk in chunks, so they are exempt from the request body limits meant for
ordinary API requests (``FBADMIN_MAX_REQUEST_BYTES``, and ``FBADMIN_MAX_REQUEST_SIZE`` in server mode), and the
server-mode security layer does not read their body into memory. Instead:

``FBADMIN_MAX_UPLOAD_BYTES``
    Maximum size of such an upload in bytes (default ``0`` = no limit other than free space).
``FBADMIN_UPLOAD_FREE_MARGIN``
    Free space (bytes) to leave in fbadmin's data directory (default 256 MiB): an upload that would leave less is
    refused before it starts (from its ``Content-Length``), and stopped if the disk fills up while it runs.
"""

from __future__ import annotations

import os
import re
import shutil
from pathlib import Path

STREAMED = re.compile(r"^/api/(servers/[^/]+/maintenance/restore/upload|schema/uploads)$")


def _int_env(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except ValueError:
        return default


def max_upload() -> int:
    return _int_env("FBADMIN_MAX_UPLOAD_BYTES", 0)


def margin() -> int:
    return _int_env("FBADMIN_UPLOAD_FREE_MARGIN", 256 * 1024 * 1024)


def is_streamed(path: str) -> bool:
    return bool(STREAMED.match(path))


def _mib(n: int) -> str:
    return f"{n / 1024 / 1024:,.0f} MB"


class NoSpace(Exception):
    """The upload does not fit: answered with HTTP 507 and the reason."""


def free_bytes(folder: Path) -> int:
    folder.mkdir(parents=True, exist_ok=True)
    return shutil.disk_usage(folder).free


def check(folder: Path, size: int | None) -> None:
    """Before writing: the declared size against the limit and the free space (raises NoSpace / ValueError)."""
    cap = max_upload()
    if size is not None and cap > 0 and size > cap:
        raise ValueError(f"The file is {_mib(size)}; uploads are limited to {_mib(cap)} (FBADMIN_MAX_UPLOAD_BYTES)")
    if size is not None:
        free = free_bytes(folder)
        if size + margin() > free:
            raise NoSpace(f"The file is {_mib(size)} but fbadmin's data directory has {_mib(free)} free "
                          f"(keeping {_mib(margin())}, FBADMIN_UPLOAD_FREE_MARGIN): free some space or copy the "
                          "backup to the database server and restore it from there")


def check_running(folder: Path, written: int) -> None:
    """While writing (every few MB): stop before the disk is full, or past the limit."""
    cap = max_upload()
    if cap > 0 and written > cap:
        raise ValueError(f"Uploads are limited to {_mib(cap)} (FBADMIN_MAX_UPLOAD_BYTES)")
    if free_bytes(folder) < margin():
        raise NoSpace(f"fbadmin's data directory is almost full ({_mib(margin())} kept free, "
                      "FBADMIN_UPLOAD_FREE_MARGIN): the upload was stopped")


def declared_size(headers) -> int | None:
    v = headers.get("content-length")
    return int(v) if v and v.isdigit() else None
