"""Continuous trace capture (roadmap 2.4): an always-on trace session per opted-in database, feeding the slow
statement log and query regressions with every statement that finished above a threshold.

The history sampler sees only the statements running at the moment it samples, so a statement that takes 3 s
between two samples a minute apart is never seen. A trace session reports each statement when it finishes, with
its exact duration and plan (``explain_plan``, the same format as ``MON$EXPLAINED_PLAN``, so switching does not
look like a plan change).

* Needs the database's history to be recorded (the observations go to the same store).
* While the capture runs, the sampler and the Query Tool stop recording runs of that database: the trace sees
  them all, and counting them twice would skew the medians. A statement still running shows in the live
  dashboard and is recorded when it finishes.
* fbadmin keeps the session running: it starts one when the capture is turned on (and when fbadmin starts),
  and starts a new one when it ends (the server restarted, the session was stopped from outside), waiting
  longer after each failure (30 s doubling to 10 min).
* At most ``FBADMIN_TRACE_CAPTURE_PER_MINUTE`` statements a minute are recorded per database (default 6000);
  the rest are counted as dropped.
* The trace session runs as the Firebird user of the server registration. Without SYSDBA (or the
  TRACE_ANY_ATTACHMENT privilege) it sees only that user's own connections.

Settings are kept in fbadmin's ``monitoring.sqlite`` (table ``trace_capture``); the status lives in memory.
"""

from __future__ import annotations

import contextvars
import json
import os
import threading
import time
from typing import Any

PER_MINUTE = int(os.environ.get("FBADMIN_TRACE_CAPTURE_PER_MINUTE", "6000"))
CHECK_EVERY = float(os.environ.get("FBADMIN_TRACE_CAPTURE_CHECK", "5"))
DEFAULT_THRESHOLD_MS = 200
MIN_LIFE = 60  # a session that ends sooner counts as a failure (backoff)
_init_lock = threading.Lock()
_lock = threading.RLock()
_caps: dict[tuple[str, str], Capture] = {}


def _ensure(store) -> None:
    if getattr(store, "_trace_capture_ready", False):
        return
    with _init_lock, store._lock:
        store._db.execute(
            "CREATE TABLE IF NOT EXISTS trace_capture (server_id TEXT NOT NULL, db_id TEXT NOT NULL, "
            "enabled INTEGER NOT NULL DEFAULT 0, threshold_ms INTEGER NOT NULL DEFAULT 200, run_as TEXT, "
            "updated_at REAL, PRIMARY KEY (server_id, db_id))")
        store._trace_capture_ready = True


def _store():
    from .history import get_store

    s = get_store()
    _ensure(s)
    return s


def config(server_id: str, db_id: str) -> dict[str, Any]:
    s = _store()
    with s._lock:
        row = s._db.execute("SELECT enabled, threshold_ms, run_as, updated_at FROM trace_capture "
                            "WHERE server_id = ? AND db_id = ?", (server_id, db_id)).fetchone()
    if not row:
        return {"enabled": False, "threshold_ms": DEFAULT_THRESHOLD_MS, "run_as": None, "updated_at": None}
    return {"enabled": bool(row[0]), "threshold_ms": int(row[1]), "run_as": json.loads(row[2]) if row[2] else None,
            "updated_at": row[3]}


def save(server_id: str, db_id: str, enabled: bool, threshold_ms: int, run_as: dict | None) -> dict | None:
    """Store the settings; returns the previous run-as credential (to revoke) when it is replaced."""
    s = _store()
    old = config(server_id, db_id)["run_as"]
    with s._lock:
        s._db.execute("INSERT INTO trace_capture (server_id, db_id, enabled, threshold_ms, run_as, updated_at) "
                      "VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT (server_id, db_id) DO UPDATE SET enabled = excluded.enabled, "
                      "threshold_ms = excluded.threshold_ms, run_as = COALESCE(excluded.run_as, run_as), "
                      "updated_at = excluded.updated_at",
                      (server_id, db_id, int(enabled), int(threshold_ms), json.dumps(run_as) if run_as else None,
                       time.time()))
    return old if run_as is not None and old != run_as else None


def enabled_all() -> list[dict[str, Any]]:
    s = _store()
    with s._lock:
        rows = s._db.execute("SELECT server_id, db_id FROM trace_capture WHERE enabled = 1").fetchall()
    return [{"server_id": r[0], "db_id": r[1]} for r in rows]


def forget(server_id: str, db_id: str) -> None:
    """The database's history is deleted: stop the capture and drop its settings."""
    stop(server_id, db_id)
    s = _store()
    with s._lock:
        s._db.execute("DELETE FROM trace_capture WHERE server_id = ? AND db_id = ?", (server_id, db_id))


# ===================================================================== the running capture
class Capture:
    def __init__(self, server_id: str, db_id: str):
        self.server_id, self.db_id = server_id, db_id
        self.sess = None
        self.threshold_ms = DEFAULT_THRESHOLD_MS
        self.started_at: float | None = None
        self.captured = 0          # statements recorded
        self.slow = 0              # of which slower than the slow-statement threshold
        self.dropped = 0           # over the per-minute limit
        self.last_event_at: float | None = None
        self.error: str | None = None
        self.failures = 0
        self.retry_at = 0.0
        self.sessions = 0          # sessions started
        self._minute = (0, 0)      # (minute, count)

    def running(self) -> bool:
        return self.sess is not None and self.sess.status in ("starting", "running")

    def status(self) -> dict[str, Any]:
        state = "running" if self.running() and self.sess.status == "running" else \
            "starting" if self.running() else "waiting" if self.error else "stopped"
        return {"state": state, "session_id": self.sess.session_id if self.sess else None,
                "started_at": self.started_at, "captured": self.captured, "slow": self.slow, "dropped": self.dropped,
                "last_event_at": self.last_event_at, "error": self.error, "sessions": self.sessions,
                "retry_at": self.retry_at if self.error and not self.running() else None}

    # called in the trace session's reading thread
    def on_event(self, ev: dict[str, Any]) -> None:
        if ev.get("type") != "EXECUTE_STATEMENT_FINISH" or not ev.get("sql") or ev.get("duration_ms") is None:
            return
        if ev["duration_ms"] < self.threshold_ms:
            return
        now = time.time()
        minute = int(now // 60)
        m, n = self._minute
        if m != minute:
            m, n = minute, 0
        if n >= PER_MINUTE:
            self.dropped += 1
            self._minute = (m, n)
            return
        self._minute = (m, n + 1)
        from . import history, regressions

        store = history.get_store()
        seconds = ev["duration_ms"] / 1000
        plan = ev.get("plan")
        regressions.record(store, self.server_id, self.db_id, ev["sql"], seconds, plan=plan, source="trace", ts=now)
        if seconds >= float(store.get_settings()["slow_seconds"] or 0) > 0:
            store.add_slow(self.server_id, self.db_id, ev["sql"], plan, seconds)
            self.slow += 1
        self.captured += 1
        self.last_event_at = now


def trace_config(db_path: str, threshold_ms: int) -> str:
    from ..maintenance.trace import database_pattern

    body = ["enabled = true", "log_statement_finish = true", "print_plan = true", "explain_plan = true",
            f"time_threshold = {int(threshold_ms)}", "max_sql_length = 16000"]
    return f"database = {database_pattern(db_path)}\n{{\n" + "\n".join("  " + b for b in body) + "\n}\n"


def active(server_id: str, db_id: str) -> bool:
    """The capture is running for the database (the sampler and the Query Tool then leave runs to it)."""
    with _lock:
        cap = _caps.get((server_id, db_id))
        return cap is not None and cap.running()


def status(server_id: str, db_id: str) -> dict[str, Any]:
    with _lock:
        cap = _caps.get((server_id, db_id))
        return cap.status() if cap else Capture(server_id, db_id).status()


def start(server_id: str, db_id: str, threshold_ms: int, wait: float = 10) -> Capture:
    """Start the trace session (in the caller's context: the user it runs as)."""
    from ..api.deps import get_state
    from ..maintenance.trace import TraceSession

    st = get_state()
    db = st.store.get_database(server_id, db_id)
    with _lock:
        cap = _caps.setdefault((server_id, db_id), Capture(server_id, db_id))
        if cap.running():
            return cap
        cap.threshold_ms = threshold_ms
    sess = TraceSession(server_id, f"fbadmin continuous capture: {db.name}", trace_config(db.path, threshold_ms),
                        "continuous", db.path, keep_file=False, on_event=cap.on_event)
    cap.sess = sess
    cap.started_at = time.time()
    cap.sessions += 1
    ctx = contextvars.copy_context()  # server mode: the session's service connection is opened as this user
    threading.Thread(target=ctx.run, args=(sess.run, lambda: st.manager.service(server_id)),
                     name=f"trace-capture-{db_id}", daemon=True).start()
    sess.started.wait(wait)
    if sess.status == "running":
        cap.error = None
    else:
        _failed(cap, sess.error or "The trace session did not start")
    return cap


def _failed(cap: Capture, error: str) -> None:
    cap.failures += 1
    cap.error = " ".join(str(error).split())[:500]
    cap.retry_at = time.time() + min(600, 30 * 2 ** (cap.failures - 1))


def stop(server_id: str, db_id: str) -> None:
    from ..api.deps import get_state
    from ..maintenance.trace import stop_session

    with _lock:
        cap = _caps.pop((server_id, db_id), None)
    if cap is None or cap.sess is None:
        return
    sess = cap.sess
    sess._stop.set()
    if sess.session_id is not None and sess.status == "running":
        try:
            with get_state().manager.service(server_id) as svc:
                stop_session(svc, sess.session_id)
        except Exception:  # noqa: BLE001 - it ends with fbadmin's service connection anyway
            pass


def sync(server_id: str, db_id: str) -> dict[str, Any]:
    """Make the running capture match the settings (in the caller's context)."""
    from .history import get_store

    cfg = config(server_id, db_id)
    if cfg["enabled"] and get_store().is_tracked(server_id, db_id):
        with _lock:
            cap = _caps.get((server_id, db_id))
            restart = cap is not None and cap.running() and cap.threshold_ms != cfg["threshold_ms"]
        if restart:
            stop(server_id, db_id)
        with _lock:
            cap = _caps.get((server_id, db_id))
            if cap is not None and not cap.running():
                cap.retry_at = 0  # turned on (again) by hand: no waiting
        start(server_id, db_id, cfg["threshold_ms"])
    else:
        stop(server_id, db_id)
    return status(server_id, db_id)


# ===================================================================== the supervisor
def check_all(now: float | None = None) -> None:
    """Start the captures that should run and do not (after their backoff), stop the ones turned off."""
    from ..api.deps import get_state
    from ..security.runas import Unavailable, acting_as
    from .history import get_store

    now = now or time.time()
    hist = get_store()
    wanted = {(c["server_id"], c["db_id"]) for c in enabled_all() if hist.is_tracked(c["server_id"], c["db_id"])}
    with _lock:
        running = list(_caps.items())
    for key, cap in running:
        if key not in wanted:
            stop(*key)
        elif cap.running() and now - (cap.started_at or now) > MIN_LIFE:
            cap.failures = 0
        elif cap.sess is not None and not cap.running() and cap.sess.stopped_at:
            # the session ended by itself: a short life counts as a failure
            if cap.sess.stopped_at - (cap.started_at or 0) < MIN_LIFE and not cap.error:
                _failed(cap, cap.sess.error or "The trace session ended")
            elif not cap.error:
                cap.failures = 0
            cap.sess = None
    for sid, dbid in wanted:
        with _lock:
            cap = _caps.get((sid, dbid))
            if cap is not None and (cap.running() or now < cap.retry_at):
                continue
        cfg = config(sid, dbid)
        try:
            with acting_as(cfg["run_as"], "continuous trace capture for this database"):
                get_state().store.get_database(sid, dbid)
                start(sid, dbid, cfg["threshold_ms"])
        except (Unavailable, Exception) as exc:  # noqa: BLE001 - shown in the status, retried later
            with _lock:
                cap = _caps.setdefault((sid, dbid), Capture(sid, dbid))
                _failed(cap, str(exc) or exc.__class__.__name__)


class Supervisor:
    def __init__(self) -> None:
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="fbadmin-trace-capture", daemon=True)

    def start(self) -> Supervisor:
        self._thread.start()
        return self

    def stop(self) -> None:
        self._stop.set()

    def _run(self) -> None:
        while not self._stop.wait(CHECK_EVERY):
            try:
                check_all()
            except Exception:  # noqa: BLE001
                pass


def start_supervisor() -> Supervisor:
    return Supervisor().start()
