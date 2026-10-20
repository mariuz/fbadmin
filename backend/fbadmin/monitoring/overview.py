"""Multi-server overview (milestone 2.0): the health of every registered server and database.

* Every server (Services API: version, server-wide attachments) and every database (MON$ snapshot,
  transaction report, size, last backup, replication) is checked in a shared thread pool. The page
  waits at most ``FBADMIN_OVERVIEW_TIMEOUT`` seconds (default 8); a check that takes longer is shown
  as "timed out" and keeps running in the background. While a check of a database is still running,
  later requests wait for that same check instead of starting another one, so a dead server never
  piles up threads or connections.
* Results are cached per fbadmin user for ``FBADMIN_OVERVIEW_TTL`` seconds (default 15).
* Saved passwords are used, never asked for: a server without a known password is reported as
  "password not saved" (or "locked" while the master password vault is locked).
* Only the servers the current user can see are checked (the same registry as ``GET /api/servers``).
* Firing alert rules (alert engine status), failed scheduled jobs and the last scheduled backup come
  from fbadmin's own stores; the database values reuse :mod:`fbadmin.monitoring.probes` and
  :mod:`fbadmin.monitoring.replication`.
"""

from __future__ import annotations

import contextvars
import os
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout
from typing import Any

from ..metadata import Catalog
from . import probes, replication

TTL = float(os.environ.get("FBADMIN_OVERVIEW_TTL", "15"))
TIMEOUT = float(os.environ.get("FBADMIN_OVERVIEW_TIMEOUT", "8"))
LONG_TX = int(os.environ.get("FBADMIN_OVERVIEW_LONG_TX", "3600"))  # seconds
GAP_WARN = int(os.environ.get("FBADMIN_OVERVIEW_GAP_WARN", "10000"))  # transactions

LEVELS = {"ok": 0, "unknown": 1, "warning": 2, "critical": 3}

_pool = ThreadPoolExecutor(max_workers=int(os.environ.get("FBADMIN_OVERVIEW_WORKERS", "16")),
                           thread_name_prefix="fbadmin-overview")
_lock = threading.Lock()
_inflight: dict[tuple, Future] = {}
_cache: dict[Any, tuple[float, dict]] = {}


def worst(levels: list[str]) -> str:
    return max(levels, key=lambda lv: LEVELS.get(lv, 0)) if levels else "ok"


def clear_cache() -> None:
    with _lock:
        _cache.clear()


# ------------------------------------------------------------------------ probes
def _err(exc: BaseException) -> str:
    return " ".join(str(exc).split()) or exc.__class__.__name__


def _minus_own(n: int | None) -> int | None:
    return max(0, n - 1) if n is not None else None


def collect(conn) -> dict[str, Any]:
    """Health values of one database (one transaction, so the MON$ snapshot is consistent)."""
    cat = Catalog(conn)
    snap = probes.snapshot(conn)
    rep = probes.transactions_report(conn, long_running=LONG_TX, gap_warn=GAP_WARN)
    info = cat.one(
        "SELECT MON$PAGE_SIZE AS db_page_size, MON$PAGES AS db_pages, MON$ODS_MAJOR AS ods_major, "
        "MON$ODS_MINOR AS ods_minor, MON$BACKUP_STATE AS nbackup_state, MON$SWEEP_INTERVAL AS sweep_interval, "
        "MON$READ_ONLY AS db_read_only FROM MON$DATABASE", scoped=False) or {}
    # the oldest attachment: in SuperServer the cache writer and garbage collector attach when the
    # database is opened, so this is how long the database has been open
    opened = cat.one(
        "SELECT DATEDIFF(SECOND FROM CAST(MIN(a.MON$TIMESTAMP) AS TIMESTAMP) TO CAST(CURRENT_TIMESTAMP AS TIMESTAMP)) "
        "AS open_seconds FROM MON$ATTACHMENTS a", scoped=False) or {}
    nb = cat.one(
        "SELECT FIRST 1 DATEDIFF(SECOND FROM CAST(h.RDB$TIMESTAMP AS TIMESTAMP) TO CAST(CURRENT_TIMESTAMP AS TIMESTAMP)) "
        "AS backup_age, h.RDB$BACKUP_LEVEL AS backup_level FROM RDB$BACKUP_HISTORY h "
        "ORDER BY h.RDB$TIMESTAMP DESC", scoped=False)
    repl = None
    try:
        r = replication.status(conn)
        if r.get("supported"):
            repl = {"replica_mode": r.get("replica_mode"), "publication_enabled": r.get("publication_enabled"),
                    "sequence": r.get("replication_sequence"),
                    "published_tables": sum(1 for t in r.get("tables", []) if t.get("published"))}
    except Exception:  # noqa: BLE001 - replication details are optional
        repl = None
    now = time.time()
    longs = rep["long_running"]
    page_size, pages = info.get("db_page_size"), info.get("db_pages")
    open_s = opened.get("open_seconds")
    out = {
        "engine_version": cat.engine_version,
        "ods": f"{info.get('ods_major')}.{info.get('ods_minor')}" if info.get("ods_major") is not None else None,
        # not counting fbadmin's own attachment, transaction and statement of this check
        "attachments": _minus_own(snap.get("attachments")),
        "transactions": _minus_own(snap.get("transactions")),
        "active_statements": _minus_own(snap.get("active_statements")),
        "next_transaction": snap.get("next_transaction"),
        "oit": snap.get("oit"), "oat": snap.get("oat"), "ost": snap.get("ost"),
        "gaps": rep["gaps"],
        "long_transactions": len(longs),
        "oldest_transaction_age": max((t["age_seconds"] or 0 for t in longs), default=None),
        "oat_holder": ({k: rep["oat_holders"][0].get(k) for k in
                        ("id", "username", "remote_address", "remote_process", "age_seconds")}
                       if rep["oat_holders"] and not rep["oat_holders"][0].get("is_self") else None),
        "page_size": page_size,
        "pages": pages,
        "size_bytes": page_size * pages if page_size and pages else None,
        "memory_used": snap.get("memory_used"),
        "sweep_interval": info.get("sweep_interval"),
        "nbackup_state": {0: "normal", 1: "stalled", 2: "merge"}.get(info.get("nbackup_state")),
        "read_only": bool(info.get("db_read_only")),
        "open_since": now - float(open_s) if open_s is not None else None,
        "nbackup_last": ({"at": now - float(nb["backup_age"]), "level": nb["backup_level"]}
                         if nb and nb.get("backup_age") is not None else None),
        "replication": repl,
    }
    return out


def _probe_database(mgr, srv, db, password: str) -> dict[str, Any]:
    from ..connections import _close_quietly, connect

    if mgr.is_connected(srv.id, db.id):  # reuse the Object Explorer's connection
        with mgr.meta(srv.id, db.id) as conn:
            return collect(conn)
    conn = connect(srv, db.path, password)  # a short-lived attachment: the tree stays "disconnected"
    try:
        return collect(conn)
    finally:
        _close_quietly(conn)


def _probe_server(mgr, srv) -> dict[str, Any]:
    with mgr.service(srv.id) as svc:
        out: dict[str, Any] = {"server_version": svc.getServerVersion()}
        try:
            out["architecture"] = svc.getArchitecture()
        except Exception:  # noqa: BLE001
            out["architecture"] = None
        try:
            count, names = svc._getSvrDbInfo()
            out["server_attachments"], out["attached_databases"] = count, len(names)
        except Exception:  # noqa: BLE001
            out["server_attachments"] = out["attached_databases"] = None
        return out


# ------------------------------------------------------------------ fbadmin data
def _alerts_by_db() -> dict[tuple[str, str], list[dict]]:
    """Firing (or failing) condition rules of the alert engine, per database."""
    out: dict[tuple[str, str], list[dict]] = {}
    try:
        from ..alerts.engine import get_engine

        eng = get_engine()
        rules = list(eng.store.rules.values())
        status = dict(eng.status)
    except Exception:  # noqa: BLE001
        return out
    for r in rules:
        st = status.get(r.id)
        if not r.enabled or not st or st.get("state") not in ("firing", "error") or not r.server_id:
            continue
        item = {"rule_id": r.id, "rule": r.name, "event": r.event, "state": st["state"],
                "message": st.get("message") or st.get("error"), "value": st.get("value"),
                "checked_at": st.get("checked_at")}
        targets = [(r.server_id, r.database_id or "")]
        if r.event == "replication_lag" and r.peer_server_id:
            targets.append((r.peer_server_id, r.peer_database_id or ""))
        for t in targets:
            out.setdefault(t, []).append(item)
    return out


def _schedules_by_db() -> dict[tuple[str, str], dict]:
    """Last scheduled backup and failed scheduled jobs, per database."""
    out: dict[tuple[str, str], dict] = {}
    try:
        from ..maintenance.scheduler import get_scheduler

        sch = get_scheduler()
        with sch._lock:
            schedules = dict(sch.schedules)
            history = list(sch.history)
    except Exception:  # noqa: BLE001
        return out
    last_err: dict[str, dict] = {}
    for h in history:
        if h.get("status") == "failed":
            last_err[h["schedule_id"]] = h
    for h in history:
        s = schedules.get(h.get("schedule_id"))
        if s is None or h.get("kind") != "backup" or h.get("status") != "finished":
            continue
        d = out.setdefault((s.server_id, s.database_id), {"backup": None, "failures": [], "schedules": 0})
        if d["backup"] is None or (h.get("finished_at") or 0) > d["backup"]["at"]:
            d["backup"] = {"at": h.get("finished_at") or h.get("started_at"), "schedule": s.name}
    for s in schedules.values():
        d = out.setdefault((s.server_id, s.database_id), {"backup": None, "failures": [], "schedules": 0})
        d["schedules"] += 1
        if s.last_status == "failed":
            h = last_err.get(s.id, {})
            d["failures"].append({"schedule_id": s.id, "schedule": s.name, "kind": s.kind, "at": s.last_run,
                                  "error": h.get("error")})
    return out


# ------------------------------------------------------------------- evaluation
def _db_issues(d: dict[str, Any]) -> list[dict[str, Any]]:
    """Problems found in the values of a reachable database (codes; the UI words them)."""
    issues: list[dict[str, Any]] = []
    gaps = d.get("gaps") or {}
    g = gaps.get("next_oat") or 0
    if g >= GAP_WARN:
        issues.append({"code": "oat_gap", "level": "critical" if g >= GAP_WARN * 10 else "warning", "value": g})
    g = gaps.get("oat_oit") or 0
    if g >= GAP_WARN:
        issues.append({"code": "oit_gap", "level": "warning", "value": g})
    if d.get("long_transactions"):
        issues.append({"code": "long_transactions", "level": "warning", "value": d["long_transactions"],
                       "age": d.get("oldest_transaction_age")})
    if d.get("nbackup_state") == "stalled":
        issues.append({"code": "nbackup_stalled", "level": "warning"})
    return issues


def _principal_key() -> tuple[Any, Any]:
    from ..security.sessions import current_principal

    p = current_principal.get()
    return (p.user_id if p else None), p


def _password_state(st, srv) -> tuple[str, str | None]:
    """("ok", password) | ("locked", None) | ("missing", None) - never prompts."""
    try:
        pw = st.store.password_for(srv.id)
    except Exception:  # noqa: BLE001
        pw = None
    if pw:
        return "ok", pw
    sec = getattr(st, "security", None)
    try:
        if sec is not None and sec.needs_unlock(srv.id):
            return "locked", None
    except Exception:  # noqa: BLE001
        pass
    return "missing", None


def _submit(key: tuple, fn, *args) -> Future:
    """Start ``fn`` unless the same check is still running from an earlier request."""
    with _lock:
        fut = _inflight.get(key)
        if fut is None or fut.done():
            ctx = contextvars.copy_context()  # the fbadmin user (server mode) follows the check
            fut = _pool.submit(ctx.run, fn, *args)
            _inflight[key] = fut
        return fut


def _result(fut: Future, deadline: float) -> tuple[str, Any]:
    try:
        return "ok", fut.result(timeout=max(0.0, deadline - time.monotonic()))
    except FutureTimeout:
        return "timeout", None
    except Exception as exc:  # noqa: BLE001
        return "error", exc


def build(refresh: bool = False, timeout: float | None = None) -> dict[str, Any]:
    from ..api.deps import get_state
    from ..serialize import to_json

    uid, _p = _principal_key()
    now = time.time()
    ttl = TTL
    with _lock:
        hit = _cache.get(uid)
    if hit and not refresh and now - hit[0] < ttl:
        return {**hit[1], "cached": True}

    st = get_state()
    mgr = st.manager.for_user(uid) if hasattr(st.manager, "for_user") and uid is not None else st.manager
    timeout = TIMEOUT if timeout is None else timeout
    started = time.monotonic()
    deadline = started + timeout
    alerts = _alerts_by_db()
    sched = _schedules_by_db()

    plan: list[tuple[Any, str, str | None, Future | None]] = []
    for srv in st.store.list():
        state, pw = _password_state(st, srv)
        if state != "ok":
            plan.append((srv, state, None, None))
            continue
        plan.append((srv, "ok", None, None if srv.embedded else _submit((uid, srv.id, srv.host, srv.port, ""), _probe_server, mgr, srv)))
        for db in srv.databases:
            plan.append((srv, "ok", db.id, _submit((uid, srv.id, srv.host, srv.port, db.id, db.path), _probe_database, mgr, srv, db, pw)))

    servers: dict[str, dict[str, Any]] = {}
    for srv, state, db_id, fut in plan:
        s = servers.get(srv.id)
        if s is None:
            s = servers[srv.id] = {
                "id": srv.id, "name": srv.name, "host": srv.host, "port": srv.port, "group": srv.group,
                "shared": srv.shared, "embedded": srv.embedded, "read_only": srv.read_only,
                "status": "ok", "code": None, "error": None, "server_version": None, "architecture": None,
                "server_attachments": None, "databases": [],
            }
            if state != "ok":
                s.update(status="unknown", code="password_not_saved" if state == "missing" else "locked")
                for db in srv.databases:
                    s["databases"].append(_db_view(srv.id, db, alerts, sched, status="unknown", code=s["code"]))
                continue
        if state != "ok":
            continue
        if db_id is None:
            if fut is None:
                continue
            kind, val = _result(fut, deadline)
            if kind == "ok":
                s.update(val)
            elif kind == "timeout":
                s.update(status="critical", code="timeout", error=f"No answer within {timeout:g} s")
            else:
                s.update(status="critical", code="unreachable", error=_err(val))
            continue
        db = next(d for d in srv.databases if d.id == db_id)
        kind, val = _result(fut, deadline)  # type: ignore[arg-type]
        if kind == "ok":
            s["databases"].append(_db_view(srv.id, db, alerts, sched, values=val))
        elif kind == "timeout":
            s["databases"].append(_db_view(srv.id, db, alerts, sched, status="critical", code="timeout",
                                           error=f"No answer within {timeout:g} s"))
        else:
            from ..connections import PasswordRequired

            code = "password_not_saved" if isinstance(val, PasswordRequired) else "unreachable"
            s["databases"].append(_db_view(srv.id, db, alerts, sched, status="critical" if code == "unreachable"
                                           else "unknown", code=code, error=None if code != "unreachable"
                                           else _err(val)))
    out_servers = []
    for s in servers.values():
        if s["server_version"] is None and s["databases"]:
            s["server_version"] = next((d["engine_version"] for d in s["databases"] if d.get("engine_version")), None)
        s["status"] = worst([s["status"]] + [d["status"] for d in s["databases"]])
        out_servers.append(s)
    result = to_json({
        "generated_at": now, "elapsed_ms": round((time.monotonic() - started) * 1000),
        "ttl": ttl, "timeout": timeout, "cached": False,
        "thresholds": {"long_transaction": LONG_TX, "gap_warn": GAP_WARN},
        "servers": out_servers,
    })
    with _lock:
        _cache[uid] = (now, result)
    return result


def _db_view(server_id: str, db, alerts, sched, *, values: dict | None = None, status: str = "ok",
             code: str | None = None, error: str | None = None) -> dict[str, Any]:
    d: dict[str, Any] = {"id": db.id, "name": db.name, "path": db.path, "status": status, "code": code,
                         "error": error, "issues": []}
    if values:
        d.update(values)
        d["issues"] = _db_issues(values)
    rules = alerts.get((server_id, db.id), [])
    d["alerts"] = rules
    for a in rules:
        if a["state"] == "firing":
            d["issues"].append({"code": "alert", "level": "warning", "rule": a["rule"], "message": a["message"]})
        if a["event"] == "replication_lag" and a.get("value") is not None:
            d.setdefault("replication", None)
            if d.get("replication") is not None:
                d["replication"]["lag_segments"] = a["value"]
    sc = sched.get((server_id, db.id)) or {}
    d["last_backup"] = sc.get("backup")
    d["job_failures"] = sc.get("failures", [])
    d["schedules"] = sc.get("schedules", 0)
    for f in d["job_failures"]:
        d["issues"].append({"code": "job_failed", "level": "warning", "schedule": f["schedule"], "kind": f["kind"]})
    if values is not None:
        d["status"] = worst(["ok"] + [i["level"] for i in d["issues"]])
    elif d["status"] == "ok" and d["issues"]:
        d["status"] = worst([i["level"] for i in d["issues"]])
    return d
