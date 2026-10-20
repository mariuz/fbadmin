"""Scheduled jobs (cron) for backups with retention, sweeps and validations."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from fastapi import APIRouter
from pydantic import BaseModel

from ..maintenance import archive, ops
from ..maintenance.cron import Cron
from ..maintenance.scheduler import Schedule, UploadOptions, get_scheduler
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


@router.get("")
def list_schedules():
    return get_scheduler().list()


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
    get_state().store.get_database(body.server_id, body.database_id)
    data = body.model_dump(exclude_none=True)
    s = get_scheduler().add(Schedule(**data))
    return get_scheduler().view(s)


@router.get("/history")
def all_history(limit: int = 100):
    return get_scheduler().history_for(None, limit)


@router.get("/{schedule_id}")
def get_one(schedule_id: str):
    sch = get_scheduler()
    return sch.view(sch.get(schedule_id))


@router.put("/{schedule_id}")
def update(schedule_id: str, body: ScheduleUpdate):
    sch = get_scheduler()
    changes = {k: v for k, v in body.model_dump(exclude_unset=True).items()
               if v is not None or k in ("verify_path", "upload")}
    s = sch.update(schedule_id, changes)
    return sch.view(s)


@router.delete("/{schedule_id}", status_code=204)
def delete(schedule_id: str, delete_backups: bool = False):
    get_scheduler().remove(schedule_id)
    if delete_backups:
        for b in archive.list_backups(schedule_id):
            archive.delete(b["path"])


@router.post("/{schedule_id}/run", status_code=202)
def run_now(schedule_id: str):
    return get_scheduler().run_now(schedule_id)


@router.get("/{schedule_id}/history")
def history(schedule_id: str, limit: int = 100):
    get_scheduler().get(schedule_id)
    return get_scheduler().history_for(schedule_id, limit)


@router.get("/{schedule_id}/backups")
def backups(schedule_id: str):
    return archive.list_backups(schedule_id)


@router.post("/{schedule_id}/backups/verify", status_code=202)
def verify_backup(schedule_id: str, body: VerifyIn):
    """Test-restore one archived backup into a scratch database (then dropped), as a background job."""
    return get_scheduler().verify_now(schedule_id, body.path)
