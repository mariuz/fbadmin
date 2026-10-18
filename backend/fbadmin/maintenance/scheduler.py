"""Scheduled maintenance jobs (pgAgent-like): backups with retention and sweeps on a cron schedule.

Schedules and their run history are stored as JSON in the data directory. An
in-process thread checks every few seconds which schedules are due and runs
them through the normal background job manager (so they show up in Processes).
Runs missed while fbadmin was not running are skipped, not caught up.
"""

from __future__ import annotations

import json
import os
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

from ..config import settings
from . import archive, ops
from .cron import Cron

HISTORY_KEEP = 500


class Schedule(BaseModel):
    id: str = Field(default_factory=lambda: uuid.uuid4().hex[:10])
    name: str
    kind: Literal["backup", "sweep", "validate"] = "backup"
    server_id: str
    database_id: str
    cron: str
    enabled: bool = True
    keep: int = 7  # backups to keep (0 = keep all)
    options: ops.BackupOptions = Field(default_factory=ops.BackupOptions)
    created_at: float = Field(default_factory=time.time)
    last_run: float | None = None
    last_status: str | None = None


class Scheduler:
    def __init__(self, data_dir: Path | None = None):
        self.dir = Path(data_dir or settings.data_dir)
        self.file = self.dir / "schedules.json"
        self.history_file = self.dir / "schedule_history.json"
        self._lock = threading.RLock()
        self._next: dict[str, float] = {}
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.schedules: dict[str, Schedule] = {}
        self.history: list[dict] = []
        self._load()

    # ------------------------------------------------------------ persistence
    def _load(self) -> None:
        if self.file.exists():
            data = json.loads(self.file.read_text())
            self.schedules = {s["id"]: Schedule(**s) for s in data.get("schedules", [])}
        if self.history_file.exists():
            self.history = json.loads(self.history_file.read_text()).get("history", [])

    def _write(self, path: Path, data: dict) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=1))
        os.replace(tmp, path)

    def _save(self) -> None:
        self._write(self.file, {"schedules": [s.model_dump() for s in self.schedules.values()]})

    def _save_history(self) -> None:
        self._write(self.history_file, {"history": self.history[-HISTORY_KEEP:]})

    # ------------------------------------------------------------------- CRUD
    def list(self) -> list[dict]:
        with self._lock:
            return [self.view(s) for s in sorted(self.schedules.values(), key=lambda s: s.created_at)]

    def view(self, s: Schedule) -> dict:
        d = s.model_dump()
        nxt = self.next_run(s)
        d["next_run"] = nxt
        return d

    def get(self, sid: str) -> Schedule:
        try:
            return self.schedules[sid]
        except KeyError:
            raise KeyError(f"Unknown schedule {sid!r}") from None

    def add(self, s: Schedule) -> Schedule:
        Cron(s.cron)
        with self._lock:
            self.schedules[s.id] = s
            self._next.pop(s.id, None)
            self._save()
        return s

    def update(self, sid: str, changes: dict) -> Schedule:
        with self._lock:
            cur = self.get(sid)
            new = Schedule(**{**cur.model_dump(), **changes, "id": sid})
            Cron(new.cron)
            self.schedules[sid] = new
            self._next.pop(sid, None)
            self._save()
            return new

    def remove(self, sid: str) -> None:
        with self._lock:
            self.get(sid)
            del self.schedules[sid]
            self._next.pop(sid, None)
            self._save()

    def history_for(self, sid: str | None = None, limit: int = 100) -> list[dict]:
        with self._lock:
            items = [h for h in self.history if sid is None or h["schedule_id"] == sid]
        return list(reversed(items))[:limit]

    # ------------------------------------------------------------- scheduling
    def next_run(self, s: Schedule) -> float | None:
        if not s.enabled:
            return None
        if s.id not in self._next:
            self._next[s.id] = Cron(s.cron).next(datetime.now()).timestamp()
        return self._next[s.id]

    def tick(self, now: float | None = None) -> list[str]:
        """Start every due schedule; returns the ids of started jobs."""
        now = now or time.time()
        started = []
        with self._lock:
            due = [s for s in self.schedules.values() if s.enabled and (self.next_run(s) or now + 1) <= now]
            for s in due:
                self._next[s.id] = Cron(s.cron).next(datetime.fromtimestamp(now)).timestamp()
        for s in due:
            try:
                started.append(self.run_now(s.id)["id"])
            except Exception as exc:  # noqa: BLE001
                self._record(s, None, now, "failed", str(exc))
        return started

    def _record(self, s: Schedule, job_id: str | None, started: float, status: str, error: str | None = None,
                **extra) -> None:
        with self._lock:
            self.history.append({"schedule_id": s.id, "schedule_name": s.name, "kind": s.kind, "job_id": job_id,
                                 "started_at": started, "finished_at": time.time(), "status": status,
                                 "error": error, **extra})
            cur = self.schedules.get(s.id)
            if cur is not None:
                cur.last_run = started
                cur.last_status = status
                self._save()
            self._save_history()

    def run_now(self, sid: str) -> dict:
        from ..api.deps import get_state

        s = self.get(sid)
        st = get_state()
        db = st.store.get_database(s.server_id, s.database_id)
        started = time.time()
        job_ref: dict = {}

        def fn(log) -> None:
            extra: dict = {}
            try:
                with st.manager.service(s.server_id) as svc:
                    major = ops.engine_major(svc)
                    if s.kind == "backup":
                        dest = archive.new_path(s.id, db.name)
                        log(f"Backing up {db.path} to {dest}")
                        size = ops.backup_to_local_file(svc, db.path, dest, s.options, major, log)
                        removed = archive.apply_retention(s.id, s.keep)
                        for r in removed:
                            log(f"Retention: deleted {r}")
                        extra = {"file": f"{dest.parent.name}/{dest.name}", "size": size, "removed": removed}
                    elif s.kind == "sweep":
                        log(f"Sweeping {db.path}")
                        ops.repair(svc, db.path, "sweep", log=log)
                    else:
                        log(f"Validating {db.path} (online)")
                        ops.validate_online(svc, db.path, log=log)
                log("Done")
            except Exception as exc:
                self._record(s, job_ref.get("id"), started, "failed", str(exc) or exc.__class__.__name__)
                raise
            self._record(s, job_ref.get("id"), started, "finished", None, **extra)

        job = st.jobs.start(f"scheduled-{s.kind}", f"Scheduled {s.kind}: {s.name} ({db.name})", s.server_id, fn)
        job_ref["id"] = job.id
        return job.summary()

    # ------------------------------------------------------------------ thread
    def start(self, interval: float = 15) -> None:
        if self._thread and self._thread.is_alive():
            return

        def loop() -> None:
            while not self._stop.wait(interval):
                try:
                    self.tick()
                except Exception:  # noqa: BLE001
                    pass

        self._thread = threading.Thread(target=loop, name="fbadmin-scheduler", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()


_scheduler: Scheduler | None = None


def get_scheduler() -> Scheduler:
    global _scheduler
    if _scheduler is None or _scheduler.dir != Path(settings.data_dir):
        _scheduler = Scheduler()
    return _scheduler


def startup() -> Scheduler:
    """Called from the app lifespan: (re)load schedules from the data dir and start the thread."""
    global _scheduler
    if _scheduler is not None:
        _scheduler.stop()
    _scheduler = Scheduler()
    _scheduler.start(float(os.environ.get("FBADMIN_SCHEDULER_INTERVAL", "15")))
    return _scheduler


def shutdown() -> None:
    if _scheduler is not None:
        _scheduler.stop()
