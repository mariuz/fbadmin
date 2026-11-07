"""Database timeline (roadmap 2.3): one history per database, to answer "what changed before it broke?".

The timeline merges what fbadmin already keeps with a small event log of its own:

* **fbadmin's event log** (``<data dir>/timeline.sqlite``): maintenance done through fbadmin (backups,
  restores over the database, sweeps, validation, shutdown and online, property changes, encryption,
  nbackup locks, upgrade trials), DDL run from the Query Tool and the object dialogs, data changes made
  in the grid or by data compare, dropped objects - recorded by :class:`TimelineMiddleware` as the
  requests happen, with the user and, for jobs, their outcome. It also records the engine version and
  ODS each time fbadmin looks at the database, so upgrades of the server show up.
* **scheduled jobs**: the scheduler's run history (backups with their test restores and uploads,
  restore drills, sweeps, validation, schema snapshots, drift checks);
* **schema history**: the commits of the database's schema history repository, when configured;
* **versioned migrations**: the scripts recorded in ``FBADMIN_MIGRATIONS``;
* **nbackup**: ``RDB$BACKUP_HISTORY``;
* **alerts** sent for the database (or its server);
* **the audit log** (server mode, fbadmin administrators only).

Nothing here changes the database. Events are kept for ``FBADMIN_TIMELINE_KEEP_DAYS`` (default 730).
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

from .config import settings

KEEP_DAYS = float(os.environ.get("FBADMIN_TIMELINE_KEEP_DAYS", "730"))
CATEGORIES = ("schema", "data", "backup", "maintenance", "upgrade", "alert", "audit")
MAX_BODY = 1024 * 1024        # request bodies read for the event details
MAX_RESPONSE = 4 * 1024 * 1024  # responses read to see whether a statement failed


# ===================================================================== store
class TimelineStore:
    def __init__(self, path: Path):
        self.path = path
        self._lock = threading.Lock()
        path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
        self._db.executescript(
            """
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS events (
                id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL, server_id TEXT NOT NULL, db_id TEXT NOT NULL,
                category TEXT NOT NULL, action TEXT NOT NULL, title TEXT NOT NULL, detail TEXT, username TEXT,
                status TEXT NOT NULL DEFAULT 'ok', job_id TEXT, finished_at REAL, error TEXT);
            CREATE INDEX IF NOT EXISTS events_db ON events(server_id, db_id, ts);
            CREATE INDEX IF NOT EXISTS events_job ON events(job_id);
            CREATE TABLE IF NOT EXISTS observations (
                server_id TEXT NOT NULL, db_id TEXT NOT NULL, engine_version TEXT, ods TEXT, ts REAL NOT NULL,
                PRIMARY KEY (server_id, db_id));
            """
        )
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
        self._seen: dict[tuple[str, str], tuple[str | None, str | None]] = {}
        self._pruned = 0.0

    def close(self) -> None:
        with self._lock:
            self._db.close()

    def add(self, server_id: str, db_id: str, category: str, action: str, title: str, *, detail: str | None = None,
            username: str | None = None, status: str = "ok", job_id: str | None = None, error: str | None = None,
            ts: float | None = None) -> int:
        now = time.time()
        with self._lock:
            cur = self._db.execute(
                "INSERT INTO events (ts, server_id, db_id, category, action, title, detail, username, status, job_id, "
                "error, finished_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (ts or now, server_id, db_id, category, action, title[:500], (detail or None) and detail[:20000],
                 username, status, job_id, error, None if status == "running" else (ts or now)))
            if now - self._pruned > 3600:
                self._pruned = now
                self._db.execute("DELETE FROM events WHERE ts < ?", (now - KEEP_DAYS * 86400,))
            return int(cur.lastrowid or 0)

    def job_finished(self, job_id: str, status: str, error: str | None, finished_at: float | None) -> None:
        st = {"finished": "ok", "failed": "failed", "cancelled": "cancelled"}.get(status, status)
        with self._lock:
            self._db.execute("UPDATE events SET status = ?, error = ?, finished_at = ? WHERE job_id = ?",
                             (st, error, finished_at or time.time(), job_id))

    def events(self, server_id: str, db_id: str, since: float | None, until: float | None) -> list[dict[str, Any]]:
        sql = "SELECT * FROM events WHERE server_id = ? AND db_id = ?"
        params: list[Any] = [server_id, db_id]
        if since:
            sql += " AND ts >= ?"
            params.append(since)
        if until:
            sql += " AND ts <= ?"
            params.append(until)
        with self._lock:
            cur = self._db.execute(sql + " ORDER BY ts DESC LIMIT 5000", params)
            names = [d[0] for d in cur.description]
            return [dict(zip(names, r, strict=True)) for r in cur.fetchall()]

    def forget(self, server_id: str, db_id: str) -> None:
        with self._lock:
            self._db.execute("DELETE FROM events WHERE server_id = ? AND db_id = ?", (server_id, db_id))
            self._db.execute("DELETE FROM observations WHERE server_id = ? AND db_id = ?", (server_id, db_id))
        self._seen.pop((server_id, db_id), None)

    def observe(self, server_id: str, db_id: str, engine_version: str | None, ods: str | None) -> None:
        """Remember the engine version and ODS; a change becomes an "upgrade" event."""
        key = (server_id, db_id)
        if not engine_version or self._seen.get(key) == (engine_version, ods):
            return
        with self._lock:
            row = self._db.execute("SELECT engine_version, ods FROM observations WHERE server_id = ? AND db_id = ?",
                                   key).fetchone()
            self._db.execute(
                "INSERT INTO observations (server_id, db_id, engine_version, ods, ts) VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT (server_id, db_id) DO UPDATE SET engine_version = excluded.engine_version, "
                "ods = excluded.ods, ts = excluded.ts", (*key, engine_version, ods, time.time()))
        self._seen[key] = (engine_version, ods)
        if row is None:
            return  # the first look is the baseline
        old_v, old_ods = row
        if old_v != engine_version:
            self.add(server_id, db_id, "upgrade", "engine", f"Firebird {old_v} → {engine_version}",
                     detail=f"The server now runs Firebird {engine_version} (it was {old_v} when fbadmin last looked).")
        if old_ods and ods and old_ods != ods:
            self.add(server_id, db_id, "upgrade", "ods", f"On-disk structure (ODS) {old_ods} → {ods}",
                     detail="The database file format changed: it was restored or upgraded on a newer engine.")


_store: TimelineStore | None = None
_store_lock = threading.Lock()


def get_store() -> TimelineStore:
    global _store
    path = Path(settings.data_dir) / "timeline.sqlite"
    with _store_lock:
        if _store is None or _store.path != path:
            if _store is not None:
                _store.close()
            _store = TimelineStore(path)
        return _store


def observe(server_id: str, db_id: str, engine_version: str | None, ods: str | None) -> None:
    """Called wherever fbadmin reads the engine version of a database (never raises)."""
    try:
        get_store().observe(server_id, db_id, engine_version, ods)
    except Exception:  # noqa: BLE001 - the timeline must never break the caller
        pass


def _job_hook(job) -> None:
    try:
        get_store().job_finished(job.id, job.status, job.error, job.finished_at)
    except Exception:  # noqa: BLE001
        pass


# ===================================================================== request recording
def _user() -> str | None:
    try:
        from .security.sessions import current_principal

        p = current_principal.get()
        return p.username if p is not None else None
    except Exception:  # noqa: BLE001
        return None


def _json(raw: bytes) -> Any:
    try:
        return json.loads(raw.decode("utf-8")) if raw else None
    except Exception:  # noqa: BLE001
        return None


def _statements(sql: str, kinds: tuple[str, ...]) -> list[str]:
    from .security.readonly import category
    from .sqlsplit import split_sql

    try:
        return [s.sql.strip() for s in split_sql(sql or "") if category(s.sql) in kinds]
    except Exception:  # noqa: BLE001
        return []


def _summary(stmts: list[str]) -> str:
    first = " ".join(stmts[0].split())
    first = first if len(first) <= 120 else first[:117] + "…"
    return first + (f" (+{len(stmts) - 1} more)" if len(stmts) > 1 else "")


def _script_failed(resp: Any) -> str | None:
    if isinstance(resp, dict) and isinstance(resp.get("error"), dict):
        return str(resp["error"].get("message") or "failed")
    return None


def _job_id(resp: Any) -> str | None:
    if isinstance(resp, dict) and isinstance(resp.get("id"), str) and resp.get("status") == "running" and "kind" in resp:
        return resp["id"]
    return None


_PROPS = {"page_buffers": "page buffers", "sweep_interval": "sweep interval", "forced_writes": "forced writes",
          "reserve_space": "reserve space", "read_only": "read-only", "dialect": "SQL dialect", "linger": "linger",
          "default_charset": "default character set"}

# (method, path after /api/servers/{sid}/databases/{dbid}, category, action, title)
_DB_RULES: list[tuple[str, re.Pattern[str], str, str, str]] = [
    ("POST", re.compile(r"^/maintenance/backup$"), "backup", "backup", "Backup"),
    ("POST", re.compile(r"^/maintenance/backup/stream$"), "backup", "backup", "Backup (downloaded)"),
    ("POST", re.compile(r"^/maintenance/nbackup$"), "backup", "nbackup", "Incremental backup (nbackup)"),
    ("POST", re.compile(r"^/maintenance/nbackup/lock$"), "backup", "nbackup_lock", "Locked for a file copy (nbackup -L)"),
    ("POST", re.compile(r"^/maintenance/nbackup/unlock$"), "backup", "nbackup_unlock", "nbackup lock ended (nbackup -N)"),
    ("POST", re.compile(r"^/maintenance/nbackup/fixup$"), "backup", "nbackup_fixup", "nbackup fix-up (nbackup -F)"),
    ("POST", re.compile(r"^/maintenance/validate$"), "maintenance", "validate", "Validation"),
    ("POST", re.compile(r"^/maintenance/sweep$"), "maintenance", "sweep", "Sweep"),
    ("POST", re.compile(r"^/maintenance/icu$"), "maintenance", "icu", "ICU fix-up"),
    ("POST", re.compile(r"^/maintenance/shutdown$"), "maintenance", "shutdown", "Shut down"),
    ("POST", re.compile(r"^/maintenance/online$"), "maintenance", "online", "Brought online"),
    ("PUT", re.compile(r"^/maintenance/properties$"), "maintenance", "properties", "Properties changed"),
    ("POST", re.compile(r"^/maintenance/encrypt$"), "maintenance", "encrypt", "Encryption started"),
    ("POST", re.compile(r"^/maintenance/decrypt$"), "maintenance", "decrypt", "Decryption started"),
    ("POST", re.compile(r"^/maintenance/limbo/([^/]+)/([^/]+)$"), "maintenance", "limbo", "Limbo transaction resolved"),
    ("POST", re.compile(r"^/monitoring/checkup/statistics$"), "maintenance", "selectivity", "Index selectivity recomputed"),
    ("POST", re.compile(r"^/upgrade/trial$"), "upgrade", "trial", "Upgrade trial (test restore on another version)"),
    ("POST", re.compile(r"^/execute$"), "schema", "ddl", "DDL"),
    ("DELETE", re.compile(r"^/objects/([^/]+)/([^/]+)$"), "schema", "drop", "Dropped"),
    ("POST", re.compile(r"^/schema/import$"), "schema", "import", "Schema imported"),
    ("POST", re.compile(r"^/tables/([^/]+)/data$"), "data", "grid", "Rows changed in the grid"),
    ("POST", re.compile(r"^/schema/generate$"), "data", "generate", "Test data generated"),
    ("POST", re.compile(r"^/datacompare/([^/]+)/apply$"), "data", "datacompare", "Data compare script applied"),
    ("POST", re.compile(r"^/drop$"), "maintenance", "drop", "Database dropped"),
]
_DB_PATH = re.compile(r"^/api/servers/([^/]+)/databases/([^/]+)(/.*)$")
_QUERY_EXEC = re.compile(r"^/api/query/sessions/([^/]+)/execute$")
_RESTORE = re.compile(r"^/api/servers/([^/]+)/maintenance/restore$")


class _Match:
    def __init__(self, server_id: str, db_id: str, category: str, action: str, title: str,
                 groups: tuple[str, ...] = (), needs_response: bool = False):
        self.server_id, self.db_id, self.category, self.action, self.title = server_id, db_id, category, action, title
        self.groups = groups
        self.needs_response = needs_response


def _match(method: str, path: str) -> _Match | None:
    m = _DB_PATH.match(path)
    if m:
        sid, dbid, rest = m.groups()
        for meth, pat, cat, action, title in _DB_RULES:
            mm = pat.match(rest)
            if mm and meth == method:
                return _Match(sid, dbid, cat, action, title, mm.groups(), needs_response=True)
        return None
    if method == "POST" and (m := _QUERY_EXEC.match(path)):
        try:
            from .api.deps import get_state

            sess = get_state().manager.session(m.group(1))
            return _Match(sess.server_id, sess.db_id, "schema", "ddl", "DDL", needs_response=True)
        except Exception:  # noqa: BLE001
            return None
    if method == "POST" and (m := _RESTORE.match(path)):
        return _Match(m.group(1), "", "backup", "restore", "Restored over the database", needs_response=True)
    return None


def _record(m: _Match, status_code: int, body: Any, resp: Any) -> None:
    """Turn one finished request into a timeline event (or nothing)."""
    if status_code >= 400 and status_code not in (409,):
        return  # refused requests changed nothing
    store = get_store()
    user = _user()
    detail: str | None = None
    title = m.title
    status, error = "ok", None
    job = _job_id(resp)
    category = m.category
    sid, dbid = m.server_id, m.db_id
    b = body if isinstance(body, dict) else {}
    if m.action == "ddl":
        sql = str(b.get("sql") or "")
        ddl = _statements(sql, ("DDL",))
        if not ddl:
            return  # queries and data changes from the Query Tool are in the audit log (server mode)
        title, detail = f"DDL: {_summary(ddl)}", ";\n".join(ddl[:50])
        err = _script_failed(resp)
        if err:
            status, error = "failed", err
    elif m.action == "drop":
        otype, name = m.groups
        title = f"Dropped {otype.rstrip('s')} {name}"
    elif m.action == "properties":
        changed = (resp or {}).get("changed") if isinstance(resp, dict) else None
        if changed:
            detail = "\n".join(str(c) for c in changed)
            title = "Properties changed: " + ", ".join(str(c) for c in changed)[:300]
        else:
            keys = [_PROPS.get(k, k) for k, v in b.items() if v is not None]
            if not keys:
                return
            title = "Properties changed: " + ", ".join(keys)
    elif m.action == "shutdown":
        mode = b.get("mode") or b.get("shutdown_mode")
        title = "Shut down" + (f" ({mode})" if mode else "")
    elif m.action == "limbo":
        tx, act = m.groups
        title = f"Limbo transaction {tx}: {act}"
    elif m.action == "validate":
        title = f"Validation ({b.get('mode') or 'full'})"
    elif m.action == "nbackup":
        title = f"Incremental backup (nbackup level {b.get('level', '?')})"
    elif m.action == "grid":
        title = f"Rows changed in {m.groups[0]} (grid)"
        if isinstance(resp, dict) and resp.get("affected") is not None:
            detail = f"{resp.get('statements')} statement(s), {resp.get('affected')} row(s) affected"
    elif m.action == "restore":
        path = str(b.get("database_path") or "")
        try:
            from .api.deps import get_state

            srv = get_state().store.get(sid)
            hit = next((d for d in srv.databases if d.path == path), None)
        except Exception:  # noqa: BLE001
            hit = None
        if hit is None:
            return  # restored to a new file: not this database's history
        dbid = hit.id
        detail = f"From {b.get('backup_file')}"
    if not sid or not dbid:
        return
    store.add(sid, dbid, category, m.action, title, detail=detail, username=user,
              status="running" if job else status, job_id=job, error=error)


class TimelineMiddleware:
    """Records changes made through fbadmin (see the module doc). Sits inside the security middleware,
    so the signed-in user is known; reads the bodies of the few requests it records."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope.get("type") != "http" or scope.get("method") in ("GET", "HEAD", "OPTIONS"):
            return await self.app(scope, receive, send)
        try:
            m = _match(scope["method"], scope.get("path", ""))
        except Exception:  # noqa: BLE001
            m = None
        if m is None:
            return await self.app(scope, receive, send)

        chunks: list[bytes] = []
        size = 0
        more = True
        msgs = []
        while more:
            msg = await receive()
            msgs.append(msg)
            if msg["type"] != "http.request":
                break
            part = msg.get("body", b"")
            size += len(part)
            if size <= MAX_BODY:
                chunks.append(part)
            more = msg.get("more_body", False)
        replay = iter(msgs)

        async def receive2():
            try:
                return next(replay)
            except StopIteration:
                return await receive()

        status = {"code": 500}
        out: list[bytes] = []
        out_size = {"n": 0}

        async def send2(message):
            if message["type"] == "http.response.start":
                status["code"] = message["status"]
            elif message["type"] == "http.response.body" and m.needs_response:
                part = message.get("body", b"")
                out_size["n"] += len(part)
                if out_size["n"] <= MAX_RESPONSE:
                    out.append(part)
            await send(message)

        await self.app(scope, receive2, send2)
        try:
            body = _json(b"".join(chunks)) if size <= MAX_BODY else None
            resp = _json(b"".join(out)) if out_size["n"] <= MAX_RESPONSE else None
            _record(m, status["code"], body, resp)
        except Exception:  # noqa: BLE001 - never break a request for the timeline
            pass


def install(app) -> None:
    from . import jobs

    if _job_hook not in jobs.finish_hooks:
        jobs.finish_hooks.append(_job_hook)
    app.add_middleware(TimelineMiddleware)


# ===================================================================== reading
def _ev(source: str, sid: str, ts: float, category: str, action: str, title: str, *, detail: str | None = None,
        status: str = "ok", user: str | None = None, ref: dict | None = None, finished_at: float | None = None,
        error: str | None = None) -> dict[str, Any]:
    return {"id": f"{source}:{sid}", "ts": ts, "category": category, "action": action, "title": title,
            "detail": detail, "status": status, "user": user, "source": source, "ref": ref or {},
            "finished_at": finished_at, "error": error}


def _local(server_id: str, db_id: str, since, until) -> list[dict]:
    out = []
    for r in get_store().events(server_id, db_id, since, until):
        out.append(_ev("fbadmin", str(r["id"]), r["ts"], r["category"], r["action"], r["title"], detail=r["detail"],
                       status=r["status"], user=r["username"], finished_at=r["finished_at"], error=r["error"],
                       ref={"type": "job", "job_id": r["job_id"]} if r["job_id"] else {}))
    return out


_SCHEDULE_KINDS = {
    "backup": ("backup", "Scheduled backup"), "sweep": ("maintenance", "Scheduled sweep"),
    "validate": ("maintenance", "Scheduled validation"), "restore_drill": ("backup", "Restore drill"),
    "schema_snapshot": ("schema", "Schema snapshot"), "schema_drift": ("schema", "Schema drift check"),
    "notebook": ("data", "Scheduled notebook"),
}


def _schedules(server_id: str, db_id: str, since, until) -> list[dict]:
    from .maintenance.scheduler import get_scheduler

    sch = get_scheduler()
    with sch._lock:
        mine = {s.id: s for s in sch.schedules.values() if s.server_id == server_id and s.database_id == db_id}
        hist = [h for h in sch.history if h.get("schedule_id") in mine]
    out = []
    for i, h in enumerate(hist):
        ts = h.get("finished_at") or h.get("started_at") or 0
        if (since and ts < since) or (until and ts > until):
            continue
        cat, label = _SCHEDULE_KINDS.get(h.get("kind"), ("maintenance", f"Scheduled {h.get('kind')}"))
        lines = []
        extra = {k: v for k, v in h.items() if k not in ("schedule_id", "schedule_name", "kind", "job_id", "started_at",
                                                         "finished_at", "status", "error")}
        for k, v in extra.items():
            if v not in (None, "", [], {}):
                lines.append(f"{k.replace('_', ' ')}: {v if not isinstance(v, (dict, list)) else json.dumps(v, default=str)[:600]}")
        status = {"finished": "ok", "failed": "failed", "drift": "warning"}.get(h.get("status"), h.get("status") or "ok")
        out.append(_ev("schedule", f"{h.get('schedule_id')}:{i}", ts, cat, h.get("kind") or "job",
                       f"{label}: {h.get('schedule_name')}", detail="\n".join(lines) or None, status=status,
                       error=h.get("error"),
                       ref={"type": "schedule", "schedule_id": h.get("schedule_id"), "job_id": h.get("job_id")}))
    return out


def _schema_history(server_id: str, db_id: str, since, until) -> list[dict]:
    from . import schemahistory

    try:
        schemahistory.get_store().get(server_id, db_id)
    except KeyError:
        return []
    out = []
    data = schemahistory.log_entries(server_id, db_id, limit=500)
    for c in data["commits"]:
        ts = float(c["time"])
        if (since and ts < since) or (until and ts > until):
            continue
        ch = c.get("changes") or {}
        lines = [f"{k}: {', '.join(v)}" for k, v in ch.items() if v]
        out.append(_ev("schema_history", c["sha"], ts, "schema", "commit", f"Schema change: {c['subject']}",
                       detail="\n".join(lines) or c.get("body") or None, user=c.get("author"),
                       ref={"type": "schema_commit", "sha": c["sha"]}))
    return out


def _database(server_id: str, db_id: str, since, until) -> tuple[list[dict], dict[str, Any]]:
    """Migrations and nbackup history from the database itself; also records the engine version and ODS."""
    from .api.deps import get_state
    from .metadata import Catalog
    from .query import end_transaction

    out: list[dict] = []
    info: dict[str, Any] = {}
    with get_state().manager.meta(server_id, db_id) as conn:
        cat = Catalog(conn)
        try:
            d = cat.one("SELECT MON$ODS_MAJOR AS ods_major, MON$ODS_MINOR AS ods_minor FROM MON$DATABASE", scoped=False) or {}
            info = {"engine_version": cat.engine_version, "ods": f"{d.get('ods_major')}.{d.get('ods_minor')}"}
            observe(server_id, db_id, info["engine_version"], info["ods"])
            if cat.one("SELECT COUNT(*) AS n FROM RDB$RELATIONS WHERE RDB$RELATION_NAME = 'FBADMIN_MIGRATIONS'",
                       scoped=False)["n"]:
                for r in cat.rows("SELECT MIGRATION_VERSION AS version, DESCR AS descr, KIND AS kind, SUCCESS AS ok, "
                                  "INSTALLED_BY AS who, INSTALLED_AT AS installed_at, EXECUTION_MS AS ms, "
                                  "ERROR_TEXT AS err FROM FBADMIN_MIGRATIONS", scoped=False):
                    ts = r["installed_at"].timestamp() if r["installed_at"] else 0
                    if (since and ts < since) or (until and ts > until):
                        continue
                    kind = (r["kind"] or "").strip()
                    title = (f"Migration baseline {r['version']}" if kind == "baseline" else
                             f"Migration V{r['version']}" + (f" — {r['descr']}" if r["descr"] else ""))
                    out.append(_ev("migration", str(r["version"]), ts, "schema", "migration", title,
                                   status="ok" if r["ok"] else "failed", user=(r["who"] or "").strip() or None,
                                   detail=f"{r['ms']} ms" if r["ms"] is not None else None, error=r["err"],
                                   ref={"type": "migrations"}))
            for i, r in enumerate(cat.rows(
                    "SELECT h.RDB$TIMESTAMP AS ts, h.RDB$BACKUP_LEVEL AS lvl, TRIM(h.RDB$FILE_NAME) AS fname, "
                    "h.RDB$SCN AS scn FROM RDB$BACKUP_HISTORY h", scoped=False)):
                ts = r["ts"].timestamp() if r["ts"] else 0
                if (since and ts < since) or (until and ts > until):
                    continue
                out.append(_ev("nbackup", f"{r['scn']}:{i}", ts, "backup", "nbackup",
                               f"nbackup level {r['lvl']}", detail=r["fname"], ref={"type": "nbackup"}))
        finally:
            try:
                end_transaction(conn, commit=False)
            except Exception:  # noqa: BLE001
                pass
    return out, info


def _alerts(server_id: str, db_id: str, since, until) -> list[dict]:
    from .alerts.engine import get_engine

    out = []
    for a in get_engine().recent(500):
        if a.get("server_id") != server_id or (a.get("database_id") not in (None, "", db_id)):
            continue
        ts = a.get("time") or 0
        if (since and ts < since) or (until and ts > until):
            continue
        status = {"critical": "failed", "warning": "warning"}.get(a.get("level"), "info")
        out.append(_ev("alert", a["id"], ts, "alert", a.get("event") or "alert", a.get("title") or a.get("event"),
                       detail=a.get("message"), status=status, ref={"type": "alert", "id": a["id"], "rule": a.get("rule")}))
    return out


def _audit(server_id: str, db_id: str, since, until, include_queries: bool) -> list[dict]:
    from .api.deps import get_state

    sec = getattr(get_state(), "security", None)
    if sec is None or not sec.db.exists:
        return []
    sql = "SELECT * FROM audit WHERE server_id = ? AND db_id = ?"
    params: list[Any] = [server_id, db_id]
    if not include_queries:
        sql += " AND COALESCE(category, '') NOT IN ('QUERY', 'TX')"
    if since:
        sql += " AND ts >= ?"
        params.append(since)
    if until:
        sql += " AND ts <= ?"
        params.append(until)
    rows = sec.db.execute(sql + " ORDER BY id DESC LIMIT 2000", params).fetchall()
    out = []
    for r in rows:
        r = dict(r)
        stmt = " ".join((r.get("statement") or "").split())
        out.append(_ev("audit", str(r["id"]), r["ts"], "audit", (r.get("category") or "other").lower(),
                       f"{r.get('category') or 'SQL'}: {stmt[:140] + ('…' if len(stmt) > 140 else '')}",
                       detail=r.get("statement"), status="ok" if r.get("success") else "failed", user=r.get("username"),
                       error=r.get("error"), ref={"type": "audit", "id": r["id"]}))
    return out


def collect(server_id: str, db_id: str, *, since: float | None = None, until: float | None = None,
            categories: list[str] | None = None, q: str | None = None, limit: int = 500,
            with_audit: bool = False, audit_queries: bool = False) -> dict[str, Any]:
    """The database's timeline, newest first."""
    sources: list[tuple[str, Any]] = [
        ("fbadmin", lambda: _local(server_id, db_id, since, until)),
        ("schedules", lambda: _schedules(server_id, db_id, since, until)),
        ("schema_history", lambda: _schema_history(server_id, db_id, since, until)),
        ("alerts", lambda: _alerts(server_id, db_id, since, until)),
    ]
    if with_audit:
        sources.append(("audit", lambda: _audit(server_id, db_id, since, until, audit_queries)))
    events: list[dict] = []
    problems: list[dict] = []
    info: dict[str, Any] = {}
    # first: looking at the database records an engine upgrade in fbadmin's own log, read next
    try:
        evs, info = _database(server_id, db_id, since, until)
        events += evs
    except Exception as exc:  # noqa: BLE001 - the database may be down: the rest still shows
        problems.append({"source": "database", "error": " ".join(str(exc).split()) or exc.__class__.__name__})
    for name, fn in sources:
        try:
            events += fn()
        except Exception as exc:  # noqa: BLE001 - one source failing must not hide the others
            problems.append({"source": name, "error": " ".join(str(exc).split()) or exc.__class__.__name__})
    counts = {c: 0 for c in CATEGORIES}
    words = [w.lower() for w in (q or "").split() if w]
    shown = []
    for e in events:
        if words and not all(w in f"{e['title']}\n{e.get('detail') or ''}\n{e.get('user') or ''}".lower() for w in words):
            continue
        counts[e["category"]] = counts.get(e["category"], 0) + 1
        if categories and e["category"] not in categories:
            continue
        shown.append(e)
    shown.sort(key=lambda e: e["ts"], reverse=True)
    return {"events": shown[:limit], "total": len(shown), "has_more": len(shown) > limit, "counts": counts,
            "problems": problems, "audit": with_audit, **info}
