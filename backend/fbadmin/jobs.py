"""Background jobs (backup, restore, sweep, statistics) - pgAdmin's "Processes".

Cancelling a job
----------------
A job started with ``cancellable=True`` can be cancelled (``JobManager.cancel``). Cancelling is
cooperative: it sets a flag that the job's code checks at safe points (``check()``, called in
the Services API read loops and the data compare loops) and runs the job's interrupt hooks
(``on_cancel``). The hooks stop work that is blocked on the server: they cancel the job's running
statements (``MON$STATEMENTS``) or detach the attachment of its service action (gbak, gfix) from
``MON$ATTACHMENTS``. Service connections opened inside a job carry the process name
``service_tag()``, which Firebird copies into the attachments of the service action, so exactly
those attachments can be found. The job then ends with status ``cancelled``.
"""

from __future__ import annotations

import contextvars
import logging
import threading
import time
import traceback
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field, fields

log_ = logging.getLogger("fbadmin.jobs")


class Cancelled(Exception):
    """The job was cancelled by the user."""

    def __init__(self, message: str = "Cancelled by the user"):
        super().__init__(message)


@dataclass
class Job:
    id: str
    kind: str
    description: str
    server_id: str
    status: str = "running"  # running | finished | failed | cancelled
    started_at: float = field(default_factory=time.time)
    finished_at: float | None = None
    log: list[str] = field(default_factory=list)
    error: str | None = None
    cancellable: bool = False
    cancel_requested: bool = False
    _hooks: list[Callable[[], str | None]] = field(default_factory=list, repr=False)
    _hook_lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def summary(self, with_log: bool = False) -> dict:
        d = {f.name: getattr(self, f.name) for f in fields(self) if not f.name.startswith("_")}
        d["log"] = list(self.log)
        if not with_log:
            d["log_lines"] = len(self.log)
            d.pop("log")
        return d


_current: contextvars.ContextVar[Job | None] = contextvars.ContextVar("fbadmin_job", default=None)


def current() -> Job | None:
    """The job the calling code runs in, or None outside jobs."""
    return _current.get()


def cancelling() -> bool:
    job = _current.get()
    return bool(job and job.cancel_requested)


def check() -> None:
    """Raise ``Cancelled`` when the current job was cancelled (a no-op outside jobs)."""
    if cancelling():
        raise Cancelled()


def interrupt() -> int:
    """From the job's own thread: run its interrupt hooks again (e.g. a service action that
    attached after the user pressed Cancel). Returns how many hooks ran."""
    job = _current.get()
    if job is None:
        return 0
    with job._hook_lock:
        hooks = list(job._hooks)
    for hook in hooks:
        _run_hook(job, hook)
    return len(hooks)


def interrupting_log(log: Callable[[str], None]) -> Callable[[str], None]:
    """A log callback for service helpers with their own read loop (firebirdsql's backup/sweep): after
    a cancel it re-runs the interrupt hooks (at most twice a second) until the action stops."""
    last = [0.0]

    def cb(line: str) -> None:
        log(line)
        if cancelling() and time.time() - last[0] >= 0.5:
            last[0] = time.time()
            interrupt()

    return cb


def service_tag() -> str | None:
    """Process name for service connections of the current cancellable job (see the module doc)."""
    job = _current.get()
    return f"fbadmin-job-{job.id}" if job is not None and job.cancellable else None


@contextmanager
def on_cancel(hook: Callable[[], str | None]) -> Iterator[None]:
    """Run ``hook`` (from the thread that cancels) if the current job is cancelled inside the block.
    A message it returns goes to the job log."""
    job = _current.get()
    if job is None or not job.cancellable:
        yield
        return
    with job._hook_lock:
        job._hooks.append(hook)
        run_now = job.cancel_requested
    if run_now:  # cancelled before the work started
        _run_hook(job, hook)
    try:
        yield
    finally:
        with job._hook_lock:
            if hook in job._hooks:
                job._hooks.remove(hook)


def _run_hook(job: Job, hook: Callable[[], str | None]) -> None:
    try:
        msg = hook()
        if msg:
            job.log.append(msg)
    except Exception as exc:  # noqa: BLE001 - the job still stops at its next check
        msg = str(exc).strip().splitlines()[0] if str(exc).strip() else exc.__class__.__name__
        job.log.append(f"Could not interrupt the running operation: {msg}")
        log_.info("job %s: interrupt hook failed: %s", job.id, msg)


class JobManager:
    def __init__(self, keep: int = 100):
        self._jobs: dict[str, Job] = {}
        self._lock = threading.Lock()
        self.keep = keep

    def start(self, kind: str, description: str, server_id: str, fn: Callable[[Callable[[str], None]], None],
              *, cancellable: bool = False) -> Job:
        job = Job(id=uuid.uuid4().hex[:12], kind=kind, description=description, server_id=server_id,
                  cancellable=cancellable)
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
            _current.set(job)
            try:
                fn(log)
                job.status = "finished"
            except Exception as exc:  # noqa: BLE001
                if isinstance(exc, Cancelled) or job.cancel_requested:
                    job.status = "cancelled"
                    job.log.append("Cancelled")
                else:
                    job.status = "failed"
                    job.error = str(exc) or exc.__class__.__name__
                    job.log.append("ERROR: " + job.error)
                    job.log.extend(traceback.format_exc().splitlines()[-3:])
            finally:
                job.finished_at = time.time()

        ctx = contextvars.copy_context()  # keeps the request's fbadmin user (server mode)
        threading.Thread(target=ctx.run, args=(run,), name=f"job-{job.id}", daemon=True).start()
        return job

    def cancel(self, job_id: str) -> Job:
        """Ask a running job to stop (see the module doc). Returns at once; the job ends shortly after."""
        job = self.get(job_id)
        if job.status != "running":
            raise ValueError("The job has already ended")
        if not job.cancellable:
            raise ValueError("This job cannot be cancelled")
        with job._hook_lock:
            first = not job.cancel_requested
            job.cancel_requested = True
            hooks = list(job._hooks) if first else []
        if first:
            job.log.append("Cancelling…")
            for hook in hooks:
                _run_hook(job, hook)
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
