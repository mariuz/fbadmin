"""Backups kept on the fbadmin host (``<data dir>/backups/<folder>/*.fbk``).

Scheduled backups and "backup to archive" jobs stream the backup through the
Services API into this directory, so no shared filesystem with the Firebird
server is needed. Files can be downloaded, restored or deleted from the UI.
"""

from __future__ import annotations

import re
import time
from pathlib import Path

from ..config import settings

SUFFIX = ".fbk"


def root() -> Path:
    return settings.data_dir / "backups"


def _safe(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", name).strip("._") or "backup"


def new_path(folder: str, db_name: str) -> Path:
    stamp = time.strftime("%Y%m%d-%H%M%S")
    base = root() / _safe(folder) / f"{_safe(db_name)}_{stamp}"
    p = base.with_suffix(SUFFIX)
    n = 1
    while p.exists():
        p = Path(f"{base}_{n}{SUFFIX}")
        n += 1
    return p


def resolve(rel: str) -> Path:
    r = root().resolve()
    p = (r / rel).resolve()
    if r not in p.parents or p.suffix != SUFFIX:
        raise ValueError("Invalid backup path")
    if not p.is_file():
        raise LookupError(f"Backup {rel!r} not found")
    return p


def list_backups(folder: str | None = None) -> list[dict]:
    r = root()
    if not r.exists():
        return []
    out = []
    for p in r.glob(f"{_safe(folder) if folder else '*'}/*{SUFFIX}"):
        st = p.stat()
        out.append({"path": f"{p.parent.name}/{p.name}", "folder": p.parent.name, "name": p.name,
                    "size": st.st_size, "modified": st.st_mtime})
    out.sort(key=lambda b: b["modified"], reverse=True)
    return out


def delete(rel: str) -> None:
    resolve(rel).unlink()


def apply_retention(folder: str, keep: int) -> list[str]:
    """Delete the oldest backups of a folder so that at most ``keep`` remain."""
    if keep <= 0:
        return []
    removed = []
    for b in list_backups(folder)[keep:]:
        (root() / b["path"]).unlink(missing_ok=True)
        removed.append(b["path"])
    return removed
