"""Monitoring and performance API (milestone 0.7)."""

from __future__ import annotations

import time

from fastapi import APIRouter, Query
from pydantic import BaseModel

from ..metadata import Catalog
from ..monitoring import advisor, history, probes, profiler
from ..query import end_transaction
from ..serialize import to_json
from ..sqlsplit import split_sql, strip_comments
from .deps import get_state

router = APIRouter(prefix="/api/servers/{server_id}/databases/{db_id}/monitoring", tags=["monitoring"])
settings_router = APIRouter(prefix="/api/monitoring", tags=["monitoring"])


def _meta(server_id: str, db_id: str):
    return get_state().manager.meta(server_id, db_id)


@router.get("/features")
def features(server_id: str, db_id: str):
    with _meta(server_id, db_id) as conn:
        return probes.features(conn)


@router.get("/snapshot")
def snapshot(server_id: str, db_id: str):
    """Cumulative counters + gauges; the UI computes rates from consecutive snapshots."""
    with _meta(server_id, db_id) as conn:
        snap = probes.snapshot(conn)
        end_transaction(conn, commit=True)
    return snap


@router.get("/tables")
def table_stats(server_id: str, db_id: str, order_by: str = "reads", limit: int = Query(50, ge=1, le=1000),
                include_system: bool = False):
    with _meta(server_id, db_id) as conn:
        f = probes.features(conn)
        if not f["table_stats"]:
            return {"available": False, "reason": "MON$TABLE_STATS is not available on this server "
                                                  f"(Firebird {f['engine_version']}).", "tables": []}
        rows = probes.table_stats(conn, order_by, limit, include_system)
    return to_json({"available": True, "tables": rows})


@router.get("/compiled")
def compiled_statements(server_id: str, db_id: str, search: str | None = None,
                        limit: int = Query(200, ge=1, le=5000)):
    with _meta(server_id, db_id) as conn:
        f = probes.features(conn)
        if not f["compiled_statements"]:
            return {"available": False, "reason": "The compiled statement cache (MON$COMPILED_STATEMENTS) "
                                                  f"needs Firebird 5.0; this is {f['engine_version']}.",
                    "statements": []}
        rows = probes.compiled_statements(conn, limit, search)
    return to_json({"available": True, "statements": rows})


@router.get("/transactions")
def transactions(server_id: str, db_id: str, long_running: int = Query(300, ge=1),
                 gap_warn: int = Query(10000, ge=1)):
    with _meta(server_id, db_id) as conn:
        return to_json(probes.transactions_report(conn, long_running, gap_warn))


@router.get("/locks")
def locks(server_id: str, db_id: str):
    with _meta(server_id, db_id) as conn:
        return to_json(probes.locks_report(conn))


# ------------------------------------------------------------------ history
class TrackingIn(BaseModel):
    enabled: bool


@router.get("/history")
def history_samples(server_id: str, db_id: str, hours: float = Query(6, gt=0, le=24 * 366),
                    max_points: int = Query(500, ge=2, le=5000)):
    store = history.get_store()
    since = time.time() - hours * 3600
    return {
        "tracking": store.is_tracked(server_id, db_id),
        "settings": store.get_settings(),
        "samples": store.samples(server_id, db_id, since=since, max_points=max_points),
    }


@router.put("/history/tracking")
def set_tracking(server_id: str, db_id: str, body: TrackingIn):
    get_state().store.get_database(server_id, db_id)  # 404 for unknown databases
    store = history.get_store()
    from ..security import runas

    db = get_state().store.get_database(server_id, db_id)
    cred = runas.issue(server_id, f"history: {db.name}") if body.enabled else None
    runas.revoke(store.set_tracked(server_id, db_id, body.enabled, run_as=cred))
    if body.enabled:
        try:
            history.sample_database(server_id, db_id)
        except Exception:  # noqa: BLE001 - recorded as last_error
            pass
    return {"tracking": store.is_tracked(server_id, db_id)}


@router.post("/history/sample")
def sample_now(server_id: str, db_id: str):
    return history.sample_database(server_id, db_id)


@router.delete("/history")
def clear_history(server_id: str, db_id: str):
    history.get_store().forget(server_id, db_id)
    return {"ok": True}


@router.get("/history/slow")
def slow_statements(server_id: str, db_id: str, limit: int = Query(200, ge=1, le=5000)):
    return {"statements": history.get_store().slow_statements(server_id, db_id, limit)}


class SettingsIn(BaseModel):
    interval: int | None = None
    retention_hours: int | None = None
    slow_seconds: int | None = None
    metrics_enabled: bool | None = None


@settings_router.get("/settings")
def get_settings():
    store = history.get_store()
    return {"settings": store.get_settings(), "tracked": store.tracked()}


@settings_router.put("/settings")
def put_settings(body: SettingsIn):
    from ..security.core import require_admin

    require_admin()  # sampler interval, retention and the /metrics exporter are server-wide settings
    if body.interval is not None and body.interval < 0:
        raise ValueError("interval must be >= 0 (0 disables the sampler)")
    if body.retention_hours is not None and body.retention_hours < 1:
        raise ValueError("retention_hours must be >= 1")
    store = history.get_store()
    return {"settings": store.set_settings(body.model_dump(exclude_none=True)), "tracked": store.tracked()}


# ------------------------------------------------------------------ profiler
class ProfileRunIn(BaseModel):
    sql: str
    description: str | None = None


class ProfileAttachIn(BaseModel):
    attachment_id: int
    description: str | None = None


@router.get("/profiler")
def profiler_overview(server_id: str, db_id: str):
    st = get_state()
    with _meta(server_id, db_id) as conn:
        cat = Catalog(conn)
        ok = profiler.available(conn)
        if not ok:
            return {"available": False, "engine_version": cat.engine_version,
                    "reason": f"The profiler needs Firebird 5.0 (this server runs {cat.engine_version}).",
                    "sessions": [], "attachments": []}
        sessions = profiler.sessions(conn)
        atts = cat.rows(
            "SELECT a.MON$ATTACHMENT_ID AS id, TRIM(a.MON$USER) AS username, TRIM(a.MON$REMOTE_PROCESS) AS "
            "remote_process, TRIM(a.MON$REMOTE_ADDRESS) AS remote_address, a.MON$STATE AS state, "
            "IIF(a.MON$ATTACHMENT_ID = CURRENT_CONNECTION, 1, 0) AS is_self "
            "FROM MON$ATTACHMENTS a WHERE COALESCE(a.MON$SYSTEM_FLAG, 0) = 0 ORDER BY a.MON$ATTACHMENT_ID")
        end_transaction(conn, commit=True)
    mgr = st.manager
    with mgr._lock:
        qt = {s.attachment_id for s in mgr._sessions.values() if s.server_id == server_id and s.db_id == db_id}
    for a in atts:
        a["query_tool"] = a["id"] in qt
    return to_json({"available": True, "sessions": sessions, "attachments": [a for a in atts if not a["is_self"]]})


@router.post("/profiler/run")
def profiler_run(server_id: str, db_id: str, body: ProfileRunIn):
    if not strip_comments(body.sql).strip():
        raise ValueError("Nothing to profile")
    mgr = get_state().manager
    fbc = mgr._open(server_id, db_id)
    try:
        return profiler.profile_script(fbc.conn, body.sql, body.description)
    finally:
        fbc.close()


@router.post("/profiler/start")
def profiler_start(server_id: str, db_id: str, body: ProfileAttachIn):
    with _meta(server_id, db_id) as conn:
        pid = profiler.start_remote(conn, body.attachment_id, body.description)
    return {"profile_id": pid, "attachment_id": body.attachment_id}


@router.post("/profiler/finish")
def profiler_finish(server_id: str, db_id: str, body: ProfileAttachIn):
    with _meta(server_id, db_id) as conn:
        profiler.finish_remote(conn, body.attachment_id)
    return {"ok": True}


@router.get("/profiler/{profile_id}")
def profiler_report(server_id: str, db_id: str, profile_id: int):
    with _meta(server_id, db_id) as conn:
        profiler.require(conn)
        return profiler.report(conn, profile_id)


@router.delete("/profiler/{profile_id}")
def profiler_delete(server_id: str, db_id: str, profile_id: int):
    with _meta(server_id, db_id) as conn:
        profiler.delete_session(conn, profile_id)
    return {"ok": True}


@router.delete("/profiler")
def profiler_delete_all(server_id: str, db_id: str):
    with _meta(server_id, db_id) as conn:
        profiler.delete_session(conn, None)
    return {"ok": True}


# ------------------------------------------------------------------ index advisor
class AdvisorIn(BaseModel):
    sources: list[str] = ["running", "history", "compiled"]
    sql: str | None = None  # pasted workload (one or more statements)
    min_rows: int = 1000


@router.post("/advisor")
def index_advisor(server_id: str, db_id: str, body: AdvisorIn):
    workload: list[dict] = []
    if body.sql and body.sql.strip():
        workload += [{"sql": s.sql, "source": "workload"} for s in split_sql(body.sql)
                     if strip_comments(s.sql).strip()]
    if "history" in body.sources:
        for s in history.get_store().slow_statements(server_id, db_id):
            workload.append({"sql": s["sql_text"], "plan": s["plan"], "seconds": s["max_seconds"],
                             "seen": s["seen"], "source": "history"})
    with _meta(server_id, db_id) as conn:
        cat = Catalog(conn)
        if "running" in body.sources:
            for r in cat.rows(
                "SELECT s.MON$SQL_TEXT AS sql_text, s.MON$EXPLAINED_PLAN AS explained_plan, "
                "DATEDIFF(MILLISECOND FROM s.MON$TIMESTAMP TO CURRENT_TIMESTAMP) / 1000.0 AS seconds "
                "FROM MON$STATEMENTS s WHERE s.MON$SQL_TEXT IS NOT NULL "
                "AND s.MON$ATTACHMENT_ID <> CURRENT_CONNECTION"
            ):
                workload.append({"sql": r["sql_text"], "plan": r["explained_plan"],
                                 "seconds": float(r["seconds"]) if r["seconds"] is not None else None,
                                 "source": "running"})
        if "compiled" in body.sources and probes.features(conn)["compiled_statements"]:
            for r in cat.rows("SELECT MON$SQL_TEXT AS sql_text, MON$EXPLAINED_PLAN AS explained_plan "
                              "FROM MON$COMPILED_STATEMENTS WHERE MON$SQL_TEXT IS NOT NULL AND MON$OBJECT_NAME IS NULL"):
                workload.append({"sql": r["sql_text"], "plan": r["explained_plan"], "source": "compiled"})
        # drop fbadmin's own catalog queries and duplicates
        seen: dict[str, dict] = {}
        for w in workload:
            key = " ".join(w["sql"].split())
            upper = key.upper().replace("RDB$DATABASE", "")
            if any(s in upper for s in ("MON$", "RDB$", "PLG$", "SEC$")):
                continue
            if key in seen:
                seen[key]["seen"] = (seen[key].get("seen") or 1) + (w.get("seen") or 1)
                continue
            seen[key] = w
        result = advisor.analyze(conn, list(seen.values()), min_rows=body.min_rows)
        end_transaction(conn, commit=False)
    return to_json(result)
