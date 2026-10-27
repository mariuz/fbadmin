"""Scheduled maintenance jobs (pgAgent-like): backups with retention (optionally encrypted), sweeps, validations,
schema snapshots, schema drift checks and restore drills on a cron schedule.

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
from contextlib import nullcontext
from datetime import datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

from .. import alerts, jobs
from ..config import settings
from . import archive, backupcrypt, drift, drill, ops, verify
from .drift import DriftOptions
from .drill import DrillOptions
from .cron import Cron

HISTORY_KEEP = 500


class UploadOptions(BaseModel):
    """Copy each backup to a cloud storage target (see ``fbadmin.storage``)."""
    target_id: str
    prefix: str = ""  # folder (key prefix) in the bucket/container
    keep: int = 0     # backups to keep in the cloud under the prefix (0 = keep all)


class Schedule(BaseModel):
    id: str = Field(default_factory=lambda: uuid.uuid4().hex[:10])
    name: str
    kind: Literal["backup", "sweep", "validate", "schema_snapshot", "schema_drift", "restore_drill"] = "backup"
    server_id: str
    database_id: str
    cron: str
    enabled: bool = True
    keep: int = 7  # backups to keep (0 = keep all)
    options: ops.BackupOptions = Field(default_factory=ops.BackupOptions)
    verify: bool = False              # test-restore each backup into a scratch database, then drop it
    verify_path: str | None = None    # scratch database path on the server (default: next to the database)
    upload: UploadOptions | None = None
    encrypt: bool = False             # encrypt the backups with the active backup key (backupcrypt)
    drift: DriftOptions | None = None  # kind schema_drift
    drill: DrillOptions | None = None  # kind restore_drill
    # server mode: run as the user who scheduled it (security/runas.py); never returned by the API
    run_as: dict | None = None
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
        ra = d.pop("run_as", None)
        d["run_as_user"] = ra.get("user") if ra else None
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
            gone = self.schedules.pop(sid)
            self._next.pop(sid, None)
            self._save()
        drift.delete_report(sid)
        drill.delete_report(sid)
        from ..security import runas

        runas.revoke(gone.run_as)

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
                started.append(self._run_as_owner(s)["id"])
            except Exception as exc:  # noqa: BLE001
                self._record(s, None, now, "failed", str(exc) or exc.__class__.__name__)
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
        if status == "failed":
            alerts.emit("schedule.failed", "critical", f"Scheduled {s.kind} failed: {s.name}",
                        f"The scheduled {s.kind} \"{s.name}\" failed: {error}",
                        server_id=s.server_id, database_id=s.database_id, key=f"schedule:{s.id}",
                        details={"schedule_id": s.id, "job_id": job_id, **extra})

    def _run_as_owner(self, s: Schedule) -> dict:
        """Start a due schedule. In server mode it runs as the user who scheduled it, so it sees that
        user's servers and saved passwords (the scheduler thread has no signed-in user)."""
        from ..security.runas import acting_as

        with acting_as(s.run_as, "this schedule"):
            return self.run_now(s.id)

    def run_now(self, sid: str) -> dict:
        from ..api.deps import get_state

        s = self.get(sid)
        st = get_state()
        db = st.store.get_database(s.server_id, s.database_id)
        started = time.time()
        job_ref: dict = {}

        def fn(log) -> None:
            extra: dict = {}
            failure: str | None = None
            try:
                # no service connection to the schedule's server needed (a drill opens its own)
                snap = s.kind in ("schema_snapshot", "schema_drift", "restore_drill")
                with (nullcontext() if snap else st.manager.service(s.server_id)) as svc, \
                        (nullcontext() if snap else st.manager.detach_on_cancel(s.server_id, db.path)):
                    major = 0 if snap else ops.option_major(svc)
                    if s.kind == "schema_drift":
                        extra = self._schema_drift(s, db, log)
                    elif s.kind == "restore_drill":
                        extra, failure = self._restore_drill(s, db, log)
                    elif snap:
                        extra, failure = self._schema_snapshot(s, log)
                    elif s.kind == "backup":
                        extra, failure = self._backup(s, db, svc, major, log)
                    elif s.kind == "sweep":
                        log(f"Sweeping {db.path}")
                        ops.repair(svc, db.path, "sweep", log=log)
                    else:
                        log(f"Validating {db.path} (online)")
                        errors = verify.validation_errors(ops.validate_online(svc, db.path, log=log))
                        extra = {"validation_errors": errors}
                        if errors:
                            failure = f"Online validation found {errors} error(s)"
                            alerts.emit("validation.errors", "critical", f"Validation errors in {db.name}",
                                        f"Online validation of {db.path} found {errors} error(s). See the job log "
                                        "and run a full validation (gfix -v -full).",
                                        server_id=s.server_id, database_id=s.database_id, key=f"validate:{s.id}",
                                        details={"errors": errors})
                if not extra.get("drift"):
                    log("Done" if not failure else failure)
            except Exception as exc:
                if isinstance(exc, jobs.Cancelled) or jobs.cancelling():
                    self._record(s, job_ref.get("id"), started, "cancelled", None, **extra)
                else:
                    self._record(s, job_ref.get("id"), started, "failed", str(exc) or exc.__class__.__name__,
                                 **extra)
                raise
            if failure:
                self._record(s, job_ref.get("id"), started, "failed", failure, **extra)
                raise RuntimeError(failure)
            self._record(s, job_ref.get("id"), started, "drift" if extra.get("drift") else "finished", None,
                         **extra)

        job = st.jobs.start(f"scheduled-{s.kind}", f"Scheduled {s.kind}: {s.name} ({db.name})", s.server_id, fn,
                            cancellable=True)
        job_ref["id"] = job.id
        return job.summary()

    def _schema_drift(self, s: Schedule, db, log) -> dict:
        """Compare the schema with the baseline; alert on new or changed drift, and once when it is gone."""
        opts = s.drift or DriftOptions()
        report = drift.check(s.server_id, s.database_id, opts, log)
        report["schedule_id"] = s.id
        drift.save_report(s.id, report)
        with self._lock:
            prev = next((h for h in reversed(self.history)
                         if h["schedule_id"] == s.id and h["status"] in ("drift", "finished")), None)
        was = (prev or {}).get("drift") or False
        before = (prev or {}).get("fingerprint") or ""
        summary = report["summary"]
        base = report["baseline"]["label"]
        if report["drift"]:
            parts = [f"{summary[k]} {k}" for k in ("missing", "extra", "changed") if summary[k]]
            names = ", ".join(f"{i['name']} ({i['status']})" for i in report["items"][:10])
            more = len(report["items"]) - 10
            alerts.emit("schema.drift", "warning", f"Schema drift in {db.name}",
                        f"The schema of {db.name} differs from {base}: {', '.join(parts)}. {names}"
                        + (f" and {more} more" if more > 0 else "") + ". The report and a script that makes the "
                        "database match the baseline are in the schedule's drift report.",
                        server_id=s.server_id, database_id=s.database_id, key=f"drift:{s.id}",
                        details={"schedule_id": s.id, "summary": summary, "baseline": base},
                        force=report["fingerprint"] != before)  # new or changed drift is news
        elif was:
            alerts.emit("schema.drift", "resolved", f"Resolved: schema drift in {db.name}",
                        f"The schema of {db.name} matches {base} again.",
                        server_id=s.server_id, database_id=s.database_id, key=f"drift:{s.id}",
                        details={"schedule_id": s.id, "baseline": base}, force=True)
        return {"drift": report["drift"], "summary": summary, "ignored": report["ignored"],
                "fingerprint": report["fingerprint"], "baseline": base}

    def _restore_drill(self, s: Schedule, db, log) -> tuple[dict, str | None]:
        """Restore the newest backup into a scratch database and check it (``drill``); alert when it fails,
        and once when a drill works again."""
        from ..api.deps import get_state
        from ..connections import connect

        st = get_state()
        opts = s.drill or DrillOptions()
        rsid = opts.server_id or s.server_id
        rsrv = st.store.get(rsid)
        pw = st.store.password_for(rsid) or ""
        opts = opts.model_copy(update={"path": drill.scratch_path(
            db.path if rsid == s.server_id else (rsrv.databases[0].path if rsrv.databases else db.path),
            s.id, opts.path)})
        report: dict = {"schedule_id": s.id, "started_at": time.time(), "server": rsrv.name, "database": db.name}
        try:
            if opts.source == "storage":
                if not opts.target_id:
                    raise ValueError("Choose the storage target of the drill")
                source = drill.newest_stored(opts.target_id, opts.prefix, log)
            else:
                bsid = opts.backup_schedule_id
                if not bsid:
                    raise ValueError("Choose the backup schedule whose backups the drill restores")
                self.get(bsid)
                source = drill.newest_archived(bsid)
            log(f"Newest backup: {source['name']} ({source['size']} bytes, "
                f"{datetime.fromtimestamp(source['modified']).isoformat(sep=' ', timespec='minutes')})")

            def live_counts(tables: list[str]):
                with st.manager.meta(s.server_id, s.database_id) as conn:
                    cur = conn.cursor()
                    live = drill._user_tables(cur)
                    conn.commit()
                    return drill._counts(conn, live)

            try:
                report.update(drill.run(
                    opts=opts, schedule_id=s.id, source=source, svc_factory=lambda: st.manager.service(rsid),
                    connect=lambda p: connect(rsrv, p, pw), live_counts=live_counts,
                    registered=[d.path for d in rsrv.databases], log=log))
            finally:
                if source.get("kind") == "storage":
                    source["local"].unlink(missing_ok=True)
            if report.get("kept"):
                from ..store import DatabaseEntry

                if not any(d.path == opts.path for d in rsrv.databases):
                    st.store.add_database(rsid, DatabaseEntry(name=f"{db.name} (restore drill)", path=opts.path))
        except jobs.Cancelled:
            raise
        except Exception as exc:  # noqa: BLE001 - no backup, no target, ...: the drill failed
            report.setdefault("checks", []).append({"code": "source", "ok": False,
                                                    "message": str(exc).strip() or exc.__class__.__name__})
        failed = [c for c in report.get("checks", []) if c["ok"] is False]
        report["ok"] = not failed
        report["finished_at"] = time.time()
        drill.save_report(s.id, report)
        with self._lock:
            prev = next((h for h in reversed(self.history) if h["schedule_id"] == s.id
                         and h["status"] in ("failed", "finished")), None)
        src = report.get("source") or {}
        extra = {"drill_ok": report["ok"], "backup": src.get("name"), "age_hours": src.get("age_hours"),
                 "recovery_seconds": report.get("recovery_seconds"), "rows": report.get("rows"),
                 "failed_checks": [c["code"] for c in failed]}
        if failed:
            msg = "; ".join(c["message"] for c in failed)
            alerts.emit("restore.drill_failed", "critical", f"Restore drill failed: {db.name}",
                        f"The restore drill \"{s.name}\" of {db.name} failed: {msg}. The drill report has the details.",
                        server_id=s.server_id, database_id=s.database_id, key=f"drill:{s.id}",
                        details={"schedule_id": s.id, "backup": src.get("name"), "checks": [c["code"] for c in failed]})
            return extra, f"Restore drill failed: {msg}"
        if prev and prev["status"] == "failed":
            alerts.emit("restore.drill_failed", "resolved", f"Resolved: restore drill of {db.name}",
                        f"The restore drill \"{s.name}\" of {db.name} works again.",
                        server_id=s.server_id, database_id=s.database_id, key=f"drill:{s.id}",
                        details={"schedule_id": s.id}, force=True)
        return extra, None

    def _schema_snapshot(self, s: Schedule, log) -> tuple[dict, str | None]:
        """Commit the schema to the database's history repository (see ``fbadmin.schemahistory``)."""
        from .. import schemahistory

        res = schemahistory.snapshot(s.server_id, s.database_id, trigger=f"schedule {s.name}", log=log)
        extra = {"commit": res["commit"] if res["committed"] else None,
                 "changes": res["changes"], "push": res["push"]}
        if res["push"] and not res["push"].get("ok"):
            return extra, f"Committed, but the push failed: {res['push'].get('error')}"
        return extra, None

    def _backup(self, s: Schedule, db, svc, major: int, log) -> tuple[dict, str | None]:
        """Back up, then optionally test-restore and upload. Old backups are only pruned (locally and in
        the cloud) when the new one passed its test restore, so a bad backup never replaces good ones."""
        from ..api.deps import get_state

        st = get_state()
        key = None
        if s.encrypt:
            key = backupcrypt.keyring().active()
            if key is None:
                raise ValueError("The schedule encrypts its backups but there is no backup key: create one under "
                                 "Backup encryption (or set FBADMIN_BACKUP_KEY)")
        dest = archive.new_path(s.id, db.name, encrypted=key is not None)
        log(f"Backing up {db.path} to {dest}")
        size = ops.backup_to_local_file(svc, db.path, dest, s.options, major, log, key=key[1] if key else None)
        extra: dict = {"file": f"{dest.parent.name}/{dest.name}", "size": size}
        if key:
            extra["encrypted"] = key[0]
        if s.verify:
            srv = st.store.get(s.server_id)
            try:
                target = verify.scratch_path(db.path, s.id, s.verify_path)
                extra["verify"] = verify.verify_backup(svc, srv, st.store.password_for(s.server_id) or "", dest,
                                                       target, major, log, [d.path for d in srv.databases])
            except jobs.Cancelled:
                raise
            except Exception as exc:  # noqa: BLE001
                err = str(exc).strip() or exc.__class__.__name__
                extra["verify"] = {"ok": False, "error": err}
                alerts.emit("backup.verify_failed", "critical", f"Backup of {db.name} failed its test restore",
                            f"The backup {dest.name} of {db.path} could not be restored: {err}. Older backups were "
                            "kept and the backup was not uploaded.",
                            server_id=s.server_id, database_id=s.database_id, key=f"verify:{s.id}",
                            details={"file": extra["file"]})
                return extra, f"Backup verification failed: {err}"
        removed = archive.apply_retention(s.id, s.keep)
        for r in removed:
            log(f"Retention: deleted {r}")
        extra["removed"] = removed
        if s.upload and s.upload.target_id:
            from ..storage import upload_with_retention

            try:
                extra["upload"] = {"ok": True, **upload_with_retention(s.upload.target_id, dest, s.upload.prefix,
                                                                      s.upload.keep, log)}
            except jobs.Cancelled:
                raise
            except Exception as exc:  # noqa: BLE001
                err = str(exc).strip() or exc.__class__.__name__
                extra["upload"] = {"ok": False, "error": err}
                alerts.emit("upload.failed", "critical", f"Upload of the {db.name} backup failed",
                            f"The backup {dest.name} could not be uploaded to cloud storage: {err}. "
                            "It is kept in the local archive.",
                            server_id=s.server_id, database_id=s.database_id, key=f"upload:{s.id}",
                            details={"file": extra["file"]})
                return extra, f"Upload failed: {err}"
        return extra, None

    def verify_now(self, sid: str, rel: str) -> dict:
        """Test-restore one archived backup of a schedule (button in the UI)."""
        from ..api.deps import get_state

        s = self.get(sid)
        st = get_state()
        db = st.store.get_database(s.server_id, s.database_id)
        src = archive.resolve(rel)
        if src.parent.name != archive._safe(s.id):
            raise ValueError("The backup does not belong to this schedule")

        def fn(log) -> None:
            srv = st.store.get(s.server_id)
            target = verify.scratch_path(db.path, s.id, s.verify_path)
            with st.manager.service(s.server_id) as svc:
                verify.verify_backup(svc, srv, st.store.password_for(s.server_id) or "", src, target,
                                     ops.option_major(svc), log, [d.path for d in srv.databases])

        return st.jobs.start("verify-backup", f"Test restore: {src.name}", s.server_id, fn,
                             cancellable=True).summary()

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
