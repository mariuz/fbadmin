"""Scheduled jobs (cron) for backups with retention, sweeps and validations."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from fastapi import APIRouter
from pydantic import BaseModel

from ..maintenance import archive, ops
from ..maintenance.cron import Cron
from ..maintenance.scheduler import Schedule, UploadOptions, get_scheduler
from ..security.access import ADMIN, require_level
from ..security.core import require_admin
from .deps import get_state

router = APIRouter(prefix="/api/schedules", tags=["schedules"])


class ScheduleIn(BaseModel):
    name: str
    kind: Literal["backup", "sweep", "validate"] = "backup"
    server_id: str
    database_id: str
    cron: str
    enabled: bool = True
    keep: int = 7
    options: ops.BackupOptions | None = None
    verify: bool = False
    verify_path: str | None = None
    upload: UploadOptions | None = None


class ScheduleUpdate(BaseModel):
    name: str | None = None
    kind: Literal["backup", "sweep", "validate"] | None = None
    cron: str | None = None
    enabled: bool | None = None
    keep: int | None = None
    options: ops.BackupOptions | None = None
    server_id: str | None = None
    database_id: str | None = None
    verify: bool | None = None
    verify_path: str | None = None  # null clears it
    upload: UploadOptions | None = None  # null removes the upload


class VerifyIn(BaseModel):
    path: str  # archive path as listed by /backups ("<folder>/<file>.fbk")


def _allowed(server_id: str, database_id: str) -> None:
    """Server mode: a schedule runs admin tools (backup, sweep, validation) on its database, so it needs
    the 'admin' level there (security/access.py). Hidden databases answer 404."""
    get_state().store.get_database(server_id, database_id)
    require_level(server_id, database_id, ADMIN, "scheduled maintenance")


def _check(s: Schedule) -> Schedule:
    _allowed(s.server_id, s.database_id)
    return s


def _upload_allowed(upload) -> None:
    """Uploads use the cloud credentials of storage targets, which only administrators manage."""
    if upload:
        require_admin()  # desktop mode: allowed


def _visible(s: dict) -> bool:
    try:
        _allowed(s["server_id"], s["database_id"])
        return True
    except Exception:  # noqa: BLE001 - KeyError (hidden/unknown) or AccessDenied
        return False


@router.get("")
def list_schedules():
    return [s for s in get_scheduler().list() if _visible(s)]


@router.get("/cron-preview")
def cron_preview(expr: str, count: int = 5):
    c = Cron(expr)
    out, t = [], datetime.now()
    for _ in range(max(1, min(count, 20))):
        t = c.next(t)
        out.append(t.isoformat(timespec="minutes"))
    return {"expr": expr, "next": out}


@router.post("", status_code=201)
def create(body: ScheduleIn):
    _allowed(body.server_id, body.database_id)
    _upload_allowed(body.upload)
    data = body.model_dump(exclude_none=True)
    s = get_scheduler().add(Schedule(**data))
    return get_scheduler().view(s)


@router.get("/history")
def all_history(limit: int = 100):
    sch = get_scheduler()
    ok = {s["id"] for s in sch.list() if _visible(s)}
    return [h for h in sch.history_for(None, limit) if h["schedule_id"] in ok]


@router.get("/{schedule_id}")
def get_one(schedule_id: str):
    sch = get_scheduler()
    return sch.view(_check(sch.get(schedule_id)))


@router.put("/{schedule_id}")
def update(schedule_id: str, body: ScheduleUpdate):
    sch = get_scheduler()
    cur = _check(sch.get(schedule_id))
    changes = {k: v for k, v in body.model_dump(exclude_unset=True).items()
               if v is not None or k in ("verify_path", "upload")}
    _allowed(changes.get("server_id") or cur.server_id, changes.get("database_id") or cur.database_id)
    if changes.get("upload") is not None and (cur.upload is None or changes["upload"] != cur.upload.model_dump()):
        _upload_allowed(changes["upload"])
    s = sch.update(schedule_id, changes)
    return sch.view(s)


@router.delete("/{schedule_id}", status_code=204)
def delete(schedule_id: str, delete_backups: bool = False):
    _check(get_scheduler().get(schedule_id))
    get_scheduler().remove(schedule_id)
    if delete_backups:
        for b in archive.list_backups(schedule_id):
            archive.delete(b["path"])


@router.post("/{schedule_id}/run", status_code=202)
def run_now(schedule_id: str):
    _check(get_scheduler().get(schedule_id))
    return get_scheduler().run_now(schedule_id)


@router.get("/{schedule_id}/history")
def history(schedule_id: str, limit: int = 100):
    _check(get_scheduler().get(schedule_id))
    return get_scheduler().history_for(schedule_id, limit)


@router.get("/{schedule_id}/backups")
def backups(schedule_id: str):
    _check(get_scheduler().get(schedule_id))
    return archive.list_backups(schedule_id)


@router.post("/{schedule_id}/backups/verify", status_code=202)
def verify_backup(schedule_id: str, body: VerifyIn):
    """Test-restore one archived backup into a scratch database (then dropped), as a background job."""
    _check(get_scheduler().get(schedule_id))
    return get_scheduler().verify_now(schedule_id, body.path)
