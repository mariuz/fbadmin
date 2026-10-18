"""Query Tool sessions: one connection + transaction per editor tab."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter
from pydantic import BaseModel

from ..config import settings
from ..query import ISOLATIONS, TxOptions, end_transaction, explain, fetch_more, run_script
from ..sqlsplit import script_params
from .deps import get_state

router = APIRouter(prefix="/api/query", tags=["query"])


class SessionIn(BaseModel):
    server_id: str
    database_id: str


class ExecuteIn(BaseModel):
    sql: str
    autocommit: bool = True
    with_plan: bool = False
    with_stats: bool = False
    max_rows: int | None = None
    params: dict[str, Any] | None = None
    keep_cursor: bool = True


class ExplainIn(BaseModel):
    sql: str


class FetchIn(BaseModel):
    rows: int = 1000


class OptionsIn(BaseModel):
    isolation: str = "read_committed"
    read_only: bool = False
    wait: bool = True
    lock_timeout: int | None = None
    statement_timeout: int | None = None  # ms, 0 = none
    search_schema: str | None = None  # Firebird 6: search path schema


@router.get("/isolations")
def isolations():
    return [{"name": k, "min_version": v[1]} for k, v in ISOLATIONS.items()]


@router.post("/params")
def params(body: ExplainIn):
    """Parameter markers (:name and ?) the script needs values for."""
    return {"params": script_params(body.sql)}


@router.post("/sessions", status_code=201)
def open_session(body: SessionIn):
    sid, sess = get_state().manager.open_session(body.server_id, body.database_id)
    with sess.lock:
        return {"session_id": sid, "options": {**sess.options(), "search_path": sess.current_search_path()}}


@router.get("/sessions/{sid}/options")
def get_options(sid: str):
    return get_state().manager.session(sid).options()


@router.put("/sessions/{sid}/options")
def set_options(sid: str, body: OptionsIn):
    sess = get_state().manager.session(sid)
    with sess.lock:
        sess.set_options(
            TxOptions(isolation=body.isolation, read_only=body.read_only, wait=body.wait,
                      lock_timeout=body.lock_timeout),
            body.statement_timeout,
        )
        if body.search_schema:
            sess.set_search_path(body.search_schema)
        return {**sess.options(), "search_path": sess.current_search_path()}


@router.post("/sessions/{sid}/execute")
def execute(sid: str, body: ExecuteIn):
    st = get_state()
    sess = st.manager.session(sid)
    with sess.lock:
        # switching to autocommit with pending work keeps the transaction open
        autocommit = body.autocommit and not sess.dirty
        max_rows = min(body.max_rows or settings.max_rows, 100000)
        stats = None
        if body.with_stats and sess.attachment_id is not None:
            def stats():
                return st.manager.attachment_stats(sess.server_id, sess.db_id, sess.attachment_id)
        return run_script(sess.conn, body.sql, autocommit=autocommit, max_rows=max_rows,
                          with_plan=body.with_plan, state=sess, params=body.params, stats=stats,
                          keep_cursor=body.keep_cursor)


@router.post("/sessions/{sid}/fetch")
def fetch(sid: str, body: FetchIn):
    sess = get_state().manager.session(sid)
    with sess.lock:
        return fetch_more(sess, max(1, min(body.rows, 100000)))


@router.post("/sessions/{sid}/cancel")
def cancel(sid: str):
    """Cancel the running statement. Does not wait for the session lock (the statement holds it)."""
    return {"cancelled": get_state().manager.cancel(sid)}


@router.post("/sessions/{sid}/explain")
def explain_plan(sid: str, body: ExplainIn):
    from ..sqlsplit import bind_params

    sess = get_state().manager.session(sid)
    sql, _ = bind_params(body.sql.strip().rstrip(";"), [0])
    with sess.lock:
        sess.close_cursor()
        plan = explain(sess.conn, sql)
        try:
            detailed = explain(sess.conn, sql, detailed=True)
        except Exception:  # noqa: BLE001
            detailed = None
        if not sess.dirty:
            end_transaction(sess.conn, commit=False)  # don't keep a snapshot open just for a plan
        return {"plan": plan, "detailed": detailed}


@router.post("/sessions/{sid}/commit")
def commit(sid: str):
    sess = get_state().manager.session(sid)
    with sess.lock:
        sess.cursor_commit = False
        sess.close_cursor()
        end_transaction(sess.conn, commit=True)
        sess.dirty = False
    return {"transaction_open": False}


@router.post("/sessions/{sid}/rollback")
def rollback(sid: str):
    sess = get_state().manager.session(sid)
    with sess.lock:
        sess.cursor_commit = False
        sess.close_cursor()
        end_transaction(sess.conn, commit=False)
        sess.dirty = False
    return {"transaction_open": False}


@router.delete("/sessions/{sid}", status_code=204)
def close_session(sid: str):
    get_state().manager.close_session(sid)
