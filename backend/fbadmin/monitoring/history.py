"""Monitoring history: periodic MON$ snapshots stored in a local SQLite database.

The file is ``<data_dir>/monitoring.sqlite``. Only databases the user opted in ("Record history" in the
UI) are sampled. Each sample stores the cumulative counters plus the gauges; per-second rates are
computed when reading. Statements seen running for longer than ``slow_seconds`` are kept too, as a
workload for the index advisor.

Settings (interval, retention, slow-statement threshold, metrics exporter) live in the same file;
environment variables give the defaults:

* ``FBADMIN_HISTORY_INTERVAL`` - seconds between samples (default 60; 0 disables the sampler)
* ``FBADMIN_HISTORY_RETENTION_HOURS`` - how long samples are kept (default 168 = 7 days)
* ``FBADMIN_METRICS`` - ``1`` enables the Prometheus ``/metrics`` endpoint
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

from ..config import settings
from . import probes

COLUMNS = ("ts",) + probes.COUNTERS + probes.GAUGES

DEFAULTS: dict[str, Any] = {
    "interval": int(os.environ.get("FBADMIN_HISTORY_INTERVAL", "60")),
    "retention_hours": int(os.environ.get("FBADMIN_HISTORY_RETENTION_HOURS", "168")),
    "slow_seconds": int(os.environ.get("FBADMIN_HISTORY_SLOW_SECONDS", "5")),
    "metrics_enabled": os.environ.get("FBADMIN_METRICS", "").lower() in ("1", "true", "yes", "on"),
}


class HistoryStore:
    def __init__(self, path: Path):
        self.path = path
        self._lock = threading.RLock()
        path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
        self._db.execute("PRAGMA journal_mode=WAL")
        cols = ", ".join(f"{c} {'REAL' if c == 'ts' else 'INTEGER'}" for c in COLUMNS)
        with self._lock:
            self._db.executescript(
                f"""
                CREATE TABLE IF NOT EXISTS samples (server_id TEXT NOT NULL, db_id TEXT NOT NULL, {cols});
                CREATE INDEX IF NOT EXISTS samples_db_ts ON samples (server_id, db_id, ts);
                CREATE TABLE IF NOT EXISTS tracked (server_id TEXT NOT NULL, db_id TEXT NOT NULL,
                    last_sample REAL, last_error TEXT, PRIMARY KEY (server_id, db_id));
                CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT);
                CREATE TABLE IF NOT EXISTS slow_statements (server_id TEXT NOT NULL, db_id TEXT NOT NULL,
                    sql_hash TEXT NOT NULL, sql_text TEXT, plan TEXT, first_seen REAL, last_seen REAL,
                    max_seconds REAL, seen INTEGER, PRIMARY KEY (server_id, db_id, sql_hash));
                """
            )

    def close(self) -> None:
        with self._lock:
            self._db.close()

    # ---------------------------------------------------------------- settings
    def get_settings(self) -> dict[str, Any]:
        out = dict(DEFAULTS)
        with self._lock:
            for k, v in self._db.execute("SELECT key, value FROM settings"):
                if k in out:
                    out[k] = json.loads(v)
        return out

    def set_settings(self, values: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            for k, v in values.items():
                if k in DEFAULTS and v is not None:
                    self._db.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", (k, json.dumps(v)))
        return self.get_settings()

    # ---------------------------------------------------------------- tracking
    def tracked(self) -> list[dict[str, Any]]:
        with self._lock:
            cur = self._db.execute("SELECT server_id, db_id, last_sample, last_error FROM tracked")
            return [dict(zip(("server_id", "db_id", "last_sample", "last_error"), r, strict=True)) for r in cur]

    def is_tracked(self, server_id: str, db_id: str) -> dict[str, Any] | None:
        return next((t for t in self.tracked() if t["server_id"] == server_id and t["db_id"] == db_id), None)

    def set_tracked(self, server_id: str, db_id: str, enabled: bool) -> None:
        with self._lock:
            if enabled:
                self._db.execute("INSERT OR IGNORE INTO tracked (server_id, db_id) VALUES (?, ?)", (server_id, db_id))
            else:
                self._db.execute("DELETE FROM tracked WHERE server_id = ? AND db_id = ?", (server_id, db_id))

    def mark(self, server_id: str, db_id: str, error: str | None) -> None:
        with self._lock:
            self._db.execute("UPDATE tracked SET last_sample = ?, last_error = ? WHERE server_id = ? AND db_id = ?",
                             (time.time(), error, server_id, db_id))

    # ---------------------------------------------------------------- samples
    def add_sample(self, server_id: str, db_id: str, snap: dict[str, Any]) -> None:
        marks = ", ".join("?" for _ in COLUMNS)
        with self._lock:
            self._db.execute(
                f"INSERT INTO samples (server_id, db_id, {', '.join(COLUMNS)}) VALUES (?, ?, {marks})",
                [server_id, db_id] + [snap.get(c) for c in COLUMNS],
            )

    def samples(self, server_id: str, db_id: str, since: float | None = None, until: float | None = None,
                max_points: int = 500) -> list[dict[str, Any]]:
        """Samples with per-second rates, down-sampled to at most ``max_points`` (keeping every N-th)."""
        sql = f"SELECT {', '.join(COLUMNS)} FROM samples WHERE server_id = ? AND db_id = ?"
        params: list[Any] = [server_id, db_id]
        if since is not None:
            sql += " AND ts >= ?"
            params.append(since)
        if until is not None:
            sql += " AND ts <= ?"
            params.append(until)
        sql += " ORDER BY ts"
        with self._lock:
            rows = [dict(zip(COLUMNS, r, strict=True)) for r in self._db.execute(sql, params)]
        out = []
        prev = None
        for r in rows:
            item = dict(r)
            item["rates"] = probes.rates(prev, r)
            out.append(item)
            prev = r
        if len(out) > max_points > 0:
            step = len(out) / max_points
            out = [out[int(i * step)] for i in range(max_points - 1)] + [out[-1]]
        return out

    def prune(self, retention_hours: float) -> int:
        cutoff = time.time() - retention_hours * 3600
        with self._lock:
            n = self._db.execute("DELETE FROM samples WHERE ts < ?", (cutoff,)).rowcount
            self._db.execute("DELETE FROM slow_statements WHERE last_seen < ?", (cutoff,))
        return n

    def forget(self, server_id: str, db_id: str) -> None:
        with self._lock:
            self._db.execute("DELETE FROM samples WHERE server_id = ? AND db_id = ?", (server_id, db_id))
            self._db.execute("DELETE FROM slow_statements WHERE server_id = ? AND db_id = ?", (server_id, db_id))

    # ---------------------------------------------------------------- slow statements
    def add_slow(self, server_id: str, db_id: str, sql_text: str, plan: str | None, seconds: float) -> None:
        import hashlib

        h = hashlib.sha1(" ".join(sql_text.split()).encode()).hexdigest()
        now = time.time()
        with self._lock:
            self._db.execute(
                "INSERT INTO slow_statements (server_id, db_id, sql_hash, sql_text, plan, first_seen, last_seen, "
                "max_seconds, seen) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1) "
                "ON CONFLICT (server_id, db_id, sql_hash) DO UPDATE SET last_seen = excluded.last_seen, "
                "max_seconds = MAX(max_seconds, excluded.max_seconds), seen = seen + 1, "
                "plan = COALESCE(excluded.plan, plan)",
                (server_id, db_id, h, sql_text, plan, now, now, seconds),
            )

    def slow_statements(self, server_id: str, db_id: str, limit: int = 200) -> list[dict[str, Any]]:
        keys = ("sql_text", "plan", "first_seen", "last_seen", "max_seconds", "seen")
        with self._lock:
            cur = self._db.execute(
                f"SELECT {', '.join(keys)} FROM slow_statements WHERE server_id = ? AND db_id = ? "
                "ORDER BY max_seconds DESC LIMIT ?", (server_id, db_id, limit))
            return [dict(zip(keys, r, strict=True)) for r in cur]


_store: HistoryStore | None = None
_store_lock = threading.Lock()


def get_store() -> HistoryStore:
    """The store for the current data dir (re-opened if the data dir changed, e.g. in tests)."""
    global _store
    path = Path(settings.data_dir) / "monitoring.sqlite"
    with _store_lock:
        if _store is None or _store.path != path:
            if _store is not None:
                _store.close()
            _store = HistoryStore(path)
        return _store


def sample_database(server_id: str, db_id: str) -> dict[str, Any]:
    """Take one sample of a database and store it (also used by "Sample now" and tests)."""
    from ..api.deps import get_state
    from ..query import end_transaction

    store = get_store()
    slow_after = store.get_settings()["slow_seconds"]
    try:
        with get_state().manager.meta(server_id, db_id) as conn:
            snap = probes.snapshot(conn)
            slow = _slow_running(conn, slow_after)
            end_transaction(conn, commit=True)
    except Exception as exc:  # noqa: BLE001
        store.mark(server_id, db_id, str(exc).strip()[:500])
        raise
    store.add_sample(server_id, db_id, snap)
    for s in slow:
        store.add_slow(server_id, db_id, s["sql_text"], s["explained_plan"], s["seconds"])
    store.mark(server_id, db_id, None)
    return snap


def _slow_running(conn, min_seconds: float) -> list[dict[str, Any]]:
    from ..metadata import Catalog

    rows = Catalog(conn).rows(
        "SELECT s.MON$SQL_TEXT AS sql_text, s.MON$EXPLAINED_PLAN AS explained_plan, "
        "DATEDIFF(MILLISECOND FROM s.MON$TIMESTAMP TO CURRENT_TIMESTAMP) / 1000.0 AS seconds "
        "FROM MON$STATEMENTS s WHERE s.MON$STATE = 1 AND s.MON$ATTACHMENT_ID <> CURRENT_CONNECTION "
        "AND s.MON$SQL_TEXT IS NOT NULL"
    )
    return [r for r in rows if r["seconds"] is not None and float(r["seconds"]) >= min_seconds]


def sample_all() -> None:
    from ..api.deps import get_state

    store = get_store()
    st = get_state()
    for t in store.tracked():
        try:
            st.store.get_database(t["server_id"], t["db_id"])
        except Exception:  # noqa: BLE001 - database was removed from the registry
            store.set_tracked(t["server_id"], t["db_id"], False)
            continue
        try:
            sample_database(t["server_id"], t["db_id"])
        except Exception:  # noqa: BLE001 - error is recorded in the tracked table
            pass
    store.prune(store.get_settings()["retention_hours"])


class Sampler:
    """Background thread started from the app lifespan."""

    def __init__(self) -> None:
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="fbadmin-history", daemon=True)

    def start(self) -> Sampler:
        self._thread.start()
        return self

    def stop(self) -> None:
        self._stop.set()

    def _run(self) -> None:
        last = 0.0
        while not self._stop.wait(1.0):
            try:
                interval = get_store().get_settings()["interval"]
            except Exception:  # noqa: BLE001
                continue
            if interval <= 0 or time.time() - last < interval:
                continue
            last = time.time()
            try:
                sample_all()
            except Exception:  # noqa: BLE001
                pass


def start_sampler() -> Sampler:
    return Sampler().start()
