"""Query Tool sessions: one connection + transaction per editor tab."""

from __future__ import annotations

from fastapi import APIRouter
from pydantic import BaseModel

from ..config import settings
from ..query import explain, run_script
from .deps import get_state

router = APIRouter(prefix="/api/query", tags=["query"])


class SessionIn(BaseModel):
    server_id: str
    database_id: str


class ExecuteIn(BaseModel):
    sql: str
    autocommit: bool = True
    with_plan: bool = False
    max_rows: int | None = None


class ExplainIn(BaseModel):
    sql: str


@router.post("/sessions", status_code=201)
def open_session(body: SessionIn):
    sid = get_state().manager.open_session(body.server_id, body.database_id)
    return {"session_id": sid}


@router.post("/sessions/{sid}/execute")
def execute(sid: str, body: ExecuteIn):
    fbc, opts = get_state().manager.session(sid)
    with fbc.lock:
        if opts.get("dirty") and body.autocommit:
            # switching to autocommit with pending work: keep the transaction open
            autocommit = False
        else:
            autocommit = body.autocommit
        max_rows = min(body.max_rows or settings.max_rows, 100000)
        return run_script(fbc.conn, body.sql, autocommit=autocommit, max_rows=max_rows,
                          with_plan=body.with_plan, state=opts)


@router.post("/sessions/{sid}/explain")
def explain_plan(sid: str, body: ExplainIn):
    fbc, _ = get_state().manager.session(sid)
    with fbc.lock:
        return {"plan": explain(fbc.conn, body.sql)}


@router.post("/sessions/{sid}/commit")
def commit(sid: str):
    fbc, opts = get_state().manager.session(sid)
    with fbc.lock:
        fbc.conn.commit()
        opts["dirty"] = False
    return {"transaction_open": False}


@router.post("/sessions/{sid}/rollback")
def rollback(sid: str):
    fbc, opts = get_state().manager.session(sid)
    with fbc.lock:
        fbc.conn.rollback()
        opts["dirty"] = False
    return {"transaction_open": False}


@router.delete("/sessions/{sid}", status_code=204)
def close_session(sid: str):
    get_state().manager.close_session(sid)
