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
ENC_SUFFIX = ".fbk.enc"  # encrypted by fbadmin (backupcrypt)


def is_backup_name(name: str) -> bool:
    return name.endswith(SUFFIX) or name.endswith(ENC_SUFFIX)


def root() -> Path:
    return settings.data_dir / "backups"


def _safe(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", name).strip("._") or "backup"


def new_path(folder: str, db_name: str, encrypted: bool = False) -> Path:
    stamp = time.strftime("%Y%m%d-%H%M%S")
    base = f"{root() / _safe(folder) / _safe(db_name)}_{stamp}"
    n = 0
    while True:
        stem = base if n == 0 else f"{base}_{n}"
        if not Path(stem + SUFFIX).exists() and not Path(stem + ENC_SUFFIX).exists():
            return Path(stem + (ENC_SUFFIX if encrypted else SUFFIX))
        n += 1


def resolve(rel: str) -> Path:
    r = root().resolve()
    p = (r / rel).resolve()
    if r not in p.parents or not is_backup_name(p.name):
        raise ValueError("Invalid backup path")
    if not p.is_file():
        raise LookupError(f"Backup {rel!r} not found")
    return p


def list_backups(folder: str | None = None) -> list[dict]:
    r = root()
    if not r.exists():
        return []
    from . import backupcrypt

    out = []
    for p in r.glob(f"{_safe(folder) if folder else '*'}/*"):
        if not is_backup_name(p.name) or not p.is_file():
            continue
        st = p.stat()
        enc = backupcrypt.info(p) if p.name.endswith(ENC_SUFFIX) else None
        out.append({"path": f"{p.parent.name}/{p.name}", "folder": p.parent.name, "name": p.name,
                    "size": st.st_size, "modified": st.st_mtime, "encrypted": enc is not None,
                    "key_id": enc["key_id"] if enc else None})
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
