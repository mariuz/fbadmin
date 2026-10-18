"""Background jobs (backup, restore, sweep, statistics) - pgAdmin's "Processes"."""

from __future__ import annotations

import threading
import time
import traceback
import uuid
from collections.abc import Callable
from dataclasses import asdict, dataclass, field


@dataclass
class Job:
    id: str
    kind: str
    description: str
    server_id: str
    status: str = "running"  # running | finished | failed
    started_at: float = field(default_factory=time.time)
    finished_at: float | None = None
    log: list[str] = field(default_factory=list)
    error: str | None = None

    def summary(self, with_log: bool = False) -> dict:
        d = asdict(self)
        if not with_log:
            d["log_lines"] = len(self.log)
            d.pop("log")
        return d


class JobManager:
    def __init__(self, keep: int = 100):
        self._jobs: dict[str, Job] = {}
        self._lock = threading.Lock()
        self.keep = keep

    def start(self, kind: str, description: str, server_id: str, fn: Callable[[Callable[[str], None]], None]) -> Job:
        job = Job(id=uuid.uuid4().hex[:12], kind=kind, description=description, server_id=server_id)
        with self._lock:
            self._jobs[job.id] = job
            if len(self._jobs) > self.keep:
                for old in sorted(self._jobs.values(), key=lambda j: j.started_at)[: len(self._jobs) - self.keep]:
                    if old.status != "running":
                        self._jobs.pop(old.id, None)

        def log(line: str) -> None:
            for part in str(line).splitlines():
                job.log.append(part)

        def run() -> None:
            try:
                fn(log)
                job.status = "finished"
            except Exception as exc:  # noqa: BLE001
                job.status = "failed"
                job.error = str(exc) or exc.__class__.__name__
                job.log.append("ERROR: " + job.error)
                job.log.extend(traceback.format_exc().splitlines()[-3:])
            finally:
                job.finished_at = time.time()

        threading.Thread(target=run, name=f"job-{job.id}", daemon=True).start()
        return job

    def list(self) -> list[Job]:
        with self._lock:
            return sorted(self._jobs.values(), key=lambda j: j.started_at, reverse=True)

    def get(self, job_id: str) -> Job:
        with self._lock:
            try:
                return self._jobs[job_id]
            except KeyError:
                raise KeyError(f"Unknown job {job_id!r}") from None
