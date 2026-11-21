"""Error log from the continuous trace (roadmap 2.5): the errors statements raise, grouped by error code, with
deadlocks and lock conflicts called out, and the ``trace.errors`` alert when a new kind of error appears or one
spikes.

The continuous trace session (``trace_capture.py``) logs errors (``log_errors``). Each ``ERROR AT …`` event lists
the status vector as ``<code> : <message>`` lines. Generic codes (``Dynamic SQL Error``, ``SQL error code = -204``,
the position, string arguments) are skipped: an error is keyed by its first specific code, e.g. *Table unknown*.
The statement that failed comes from the ``FAILED EXECUTE_STATEMENT_FINISH`` event just before it on the same
attachment; Firebird reports that only for statements slower than the trace's threshold, so quick failures are
recorded without their SQL (a threshold of 0 records it every time, at a higher cost).

Kept in ``monitoring.sqlite`` (table ``trace_errors``) for ``KEEP_DAYS``.
"""

from __future__ import annotations

import hashlib
import re
import threading
import time
from typing import Any

KEEP_DAYS = 30
NEW_DAYS = 7            # an error code not seen for this long is "new"
SPIKE_FACTOR = 5.0      # an hour with this many times the usual hourly count...
SPIKE_MIN = 20          # ...and at least this many errors is a spike
GENERIC = {335544569, 335544436, 335544382, 336397208, 336397207, 335544342, 335544343}
LOCK = {335544336, 335544345, 335544510, 335544878, 335545096, 335544451}
CONSTRAINT = {335544665, 335544466, 335544347, 335544558, 335544838, 335544839}
_LINE = re.compile(r"^\s*(\d{9})\s*:\s*(.*)$")
_init_lock = threading.Lock()


def _ensure(store) -> None:
    if getattr(store, "_trace_errors_ready", False):
        return
    with _init_lock, store._lock:
        store._db.executescript(
            """
            CREATE TABLE IF NOT EXISTS trace_errors (server_id TEXT NOT NULL, db_id TEXT NOT NULL, ts REAL NOT NULL,
                code INTEGER, message TEXT, detail TEXT, category TEXT, sql_hash TEXT, sql_text TEXT, username TEXT,
                attachment INTEGER, at_function TEXT);
            CREATE INDEX IF NOT EXISTS trace_errors_db ON trace_errors (server_id, db_id, ts);
            CREATE TABLE IF NOT EXISTS trace_errors_alerted (server_id TEXT NOT NULL, db_id TEXT NOT NULL,
                code INTEGER NOT NULL, ts REAL, kind TEXT, PRIMARY KEY (server_id, db_id, code));
            """
        )
        store._trace_errors_ready = True


def _store():
    from .history import get_store

    s = get_store()
    _ensure(s)
    return s


def category(code: int | None) -> str:
    return "lock" if code in LOCK else "constraint" if code in CONSTRAINT else "error"


def parse(ev: dict[str, Any]) -> dict[str, Any] | None:
    """An ``ERROR AT …`` event as ``{code, message, detail, at_function}``; None if it has no codes."""
    lines = []
    for line in (ev.get("text") or "").splitlines()[1:]:
        m = _LINE.match(line)
        if m:
            lines.append((int(m.group(1)), m.group(2).strip()))
    if not lines:
        return None
    code, message = next(((c, t) for c, t in lines if c not in GENERIC), lines[0])
    return {"code": code, "message": message, "detail": "\n".join(f"{c} : {t}" for c, t in lines)[:4000],
            "at_function": (ev.get("type") or "").replace("ERROR AT", "").strip() or None, "category": category(code)}


def record(server_id: str, db_id: str, err: dict[str, Any], *, sql: str | None, user: str | None,
           attachment: int | None, ts: float | None = None) -> None:
    s = _store()
    h = hashlib.sha1(" ".join(sql.split()).encode()).hexdigest()[:16] if sql else None
    with s._lock:
        s._db.execute("INSERT INTO trace_errors (server_id, db_id, ts, code, message, detail, category, sql_hash, "
                      "sql_text, username, attachment, at_function) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                      (server_id, db_id, ts or time.time(), err["code"], err["message"][:1000], err["detail"],
                       err["category"], h, (sql or None) and sql[:8000], user, attachment, err["at_function"]))


def prune(now: float | None = None) -> None:
    s = _store()
    with s._lock:
        s._db.execute("DELETE FROM trace_errors WHERE ts < ?", ((now or time.time()) - KEEP_DAYS * 86400,))


def forget(server_id: str, db_id: str) -> None:
    s = _store()
    with s._lock:
        s._db.execute("DELETE FROM trace_errors WHERE server_id = ? AND db_id = ?", (server_id, db_id))
        s._db.execute("DELETE FROM trace_errors_alerted WHERE server_id = ? AND db_id = ?", (server_id, db_id))


def groups(server_id: str, db_id: str, hours: float = 24, now: float | None = None) -> dict[str, Any]:
    """The errors of the last ``hours``, grouped by code, most frequent first; hourly counts for a chart."""
    now = now or time.time()
    since = now - hours * 3600
    s = _store()
    with s._lock:
        rows = s._db.execute(
            "SELECT code, MAX(message), category, COUNT(*), MIN(ts), MAX(ts), COUNT(DISTINCT username), "
            "COUNT(DISTINCT sql_hash) FROM trace_errors WHERE server_id = ? AND db_id = ? AND ts >= ? "
            "GROUP BY code, category ORDER BY COUNT(*) DESC", (server_id, db_id, since)).fetchall()
        out = []
        for code, msg, cat, n, first, last, users, stmts in rows:
            ex = s._db.execute("SELECT sql_text, username, detail, ts FROM trace_errors WHERE server_id = ? AND db_id = ? "
                               "AND code = ? AND ts >= ? ORDER BY (sql_text IS NULL), ts DESC LIMIT 1",
                               (server_id, db_id, code, since)).fetchone()
            seen_before = s._db.execute("SELECT 1 FROM trace_errors WHERE server_id = ? AND db_id = ? AND code = ? "
                                        "AND ts < ? AND ts >= ? LIMIT 1",
                                        (server_id, db_id, code, first, first - NEW_DAYS * 86400)).fetchone()
            out.append({"code": code, "message": msg, "category": cat, "count": n, "first": first, "last": last,
                        "users": users, "statements": stmts, "example_sql": ex[0] if ex else None,
                        "example_user": ex[1] if ex else None, "detail": ex[2] if ex else None,
                        "new": seen_before is None})
        hourly = s._db.execute("SELECT CAST(ts / 3600 AS INTEGER), COUNT(*), SUM(category = 'lock') FROM trace_errors "
                               "WHERE server_id = ? AND db_id = ? AND ts >= ? GROUP BY 1 ORDER BY 1",
                               (server_id, db_id, since)).fetchall()
    return {"hours": hours, "total": sum(g["count"] for g in out), "lock": sum(g["count"] for g in out if g["category"] == "lock"),
            "groups": out, "hourly": [{"ts": h * 3600, "count": c, "lock": lk or 0} for h, c, lk in hourly]}


def check_alerts(server_id: str, db_id: str, db_name: str, now: float | None = None) -> list[str]:
    """``trace.errors``: a kind of error not seen for NEW_DAYS, or an hour with SPIKE_FACTOR times the usual count.
    Each code alerts once per kind (new, spike) until it has been quiet for a day."""
    from ..alerts import emit

    now = now or time.time()
    s = _store()
    raised = []
    with s._lock:
        recent = s._db.execute("SELECT code, MAX(message), category, COUNT(*) FROM trace_errors WHERE server_id = ? "
                               "AND db_id = ? AND ts >= ? GROUP BY code, category", (server_id, db_id, now - 3600)).fetchall()
        alerted = {r[0]: (r[1], r[2]) for r in s._db.execute(
            "SELECT code, ts, kind FROM trace_errors_alerted WHERE server_id = ? AND db_id = ?", (server_id, db_id))}
    for code, msg, cat, n in recent:
        with s._lock:
            before = s._db.execute("SELECT COUNT(*) FROM trace_errors WHERE server_id = ? AND db_id = ? AND code = ? "
                                   "AND ts < ? AND ts >= ?", (server_id, db_id, code, now - 3600, now - NEW_DAYS * 86400)
                                   ).fetchone()[0]
            day = s._db.execute("SELECT COUNT(*) FROM trace_errors WHERE server_id = ? AND db_id = ? AND code = ? "
                                "AND ts < ? AND ts >= ?", (server_id, db_id, code, now - 3600, now - 86400)).fetchone()[0]
        kind = "new" if before == 0 else "spike" if n >= SPIKE_MIN and n >= SPIKE_FACTOR * max(day / 23, 1) else None
        if kind is None:
            continue
        last = alerted.get(code)
        if last and last[1] == kind and now - last[0] < 86400:
            continue
        what = "lock conflict" if cat == "lock" else "error"
        title = (f"New {what} in {db_name}: {msg}" if kind == "new"
                 else f"Spike of {what}s in {db_name}: {n} × {msg} in the last hour")
        emit("trace.errors", "critical" if cat == "lock" and kind == "spike" else "warning", title,
             f"{title}\nError code {code}." + (f" Usually about {round(day / 23, 1)} an hour." if kind == "spike" else "")
             + "\n\nOpen the History tab of the database for the error log.",
             server_id=server_id, database_id=db_id, key=f"trace-errors:{code}:{kind}", force=True,
             details={"code": code, "message": msg, "category": cat, "kind": kind, "count_last_hour": n})
        with s._lock:
            s._db.execute("INSERT INTO trace_errors_alerted (server_id, db_id, code, ts, kind) VALUES (?, ?, ?, ?, ?) "
                          "ON CONFLICT (server_id, db_id, code) DO UPDATE SET ts = excluded.ts, kind = excluded.kind",
                          (server_id, db_id, code, now, kind))
        raised.append(f"{code}:{kind}")
    return raised
