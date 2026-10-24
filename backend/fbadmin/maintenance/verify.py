"""Backup verification: restore a backup into a scratch database on the server, check it opens,
then drop it. A backup that cannot be restored is not a backup."""

from __future__ import annotations

import re
import time
from pathlib import Path
from typing import Any

from . import ops

SCRATCH_PREFIX = "fbadmin_verify_"
_ERRORS = re.compile(r"(\d+)\s+ERRORS?\s+found", re.I)


def validation_errors(lines: list[str]) -> int:
    """Number of errors reported by an online validation (``Relation 128 (T) : 2 ERRORS found``)."""
    return sum(int(m.group(1)) for line in lines for m in _ERRORS.finditer(line))


def _same(a: str, b: str) -> bool:
    norm = lambda p: p.replace("\\", "/").rstrip("/").lower()  # noqa: E731
    return norm(a) == norm(b)


def scratch_path(db_path: str, schedule_id: str, override: str | None = None) -> str:
    """Where the test restore goes: next to the database unless a path is given."""
    if override and override.strip():
        return override.strip()
    m = re.match(r"^(.*[\\/])[^\\/]+$", db_path)
    if not m:
        raise ValueError(f"The database is registered by alias ({db_path!r}), so fbadmin cannot tell its directory. "
                         "Set a test-restore path on the server for this schedule.")
    return f"{m.group(1)}{SCRATCH_PREFIX}{schedule_id}.fdb"


def check_scratch(path: str, registered: list[str]) -> None:
    for p in registered:
        if _same(p, path):
            raise ValueError(f"The test-restore path {path!r} is a registered database; choose another path.")


def verify_backup(svc, srv, password: str, src: Path, target: str, major: int, log: ops.Log,
                  registered: list[str]) -> dict[str, Any]:
    """Restore ``src`` into ``target`` on the server, open it, count its objects and drop it again."""
    from ..connections import connect

    check_scratch(target, registered)
    # Only overwrite a leftover of fbadmin's own scratch database, never an arbitrary file.
    replace = Path(target.replace("\\", "/")).name.lower().startswith(SCRATCH_PREFIX)
    t0 = time.time()
    log(f"Test restore of {src.name} into {target}")
    try:
        ops.restore_from_local_file(svc, src, target, ops.RestoreOptions(replace=replace), major, log)
        conn = connect(srv, target, password)
        try:
            cur = conn.cursor()
            cur.execute("SELECT "
                        "(SELECT COUNT(*) FROM RDB$RELATIONS WHERE COALESCE(RDB$SYSTEM_FLAG, 0) = 0), "
                        "(SELECT COUNT(*) FROM RDB$INDICES WHERE COALESCE(RDB$SYSTEM_FLAG, 0) = 0), "
                        "(SELECT COUNT(*) FROM RDB$PROCEDURES WHERE COALESCE(RDB$SYSTEM_FLAG, 0) = 0) "
                        "FROM RDB$DATABASE")
            tables, indices, procedures = cur.fetchone()
            conn.commit()
        finally:
            conn.drop_database()
        log(f"Test restore ok: {tables} tables, {indices} indices, {procedures} procedures; scratch database dropped")
        return {"ok": True, "seconds": round(time.time() - t0, 1), "tables": tables, "indices": indices,
                "procedures": procedures, "path": target}
    except Exception as exc:
        if replace:  # never drop a file fbadmin did not name itself
            _cleanup(srv, password, target, log, cancelled=isinstance(exc, ops.RestoreCancelled))
        raise


def _cleanup(srv, password: str, target: str, log: ops.Log, cancelled: bool = False) -> None:
    """Drop a half-restored scratch database, if one was left behind."""
    from ..connections import connect

    if cancelled:  # gbak lets go of the file a moment after its service connection is closed
        ops.drop_partial(lambda: connect(srv, target, password), target, log)
        return
    try:
        connect(srv, target, password).drop_database()
        log(f"Dropped the incomplete scratch database {target}")
    except Exception:  # noqa: BLE001 - nothing was created, or it cannot be opened
        pass
