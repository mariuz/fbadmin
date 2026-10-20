"""Event monitor API: register POST_EVENT names, poll arrivals, suggest names, post test events."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from ..connections import connect
from ..events import scan
from ..events.monitor import get_event_manager
from ..events.wire import AuxConnectionError, encode_name
from .deps import get_state

router = APIRouter(prefix="/api/servers/{server_id}/databases/{db_id}/event-monitor", tags=["events"])


class EventNamesIn(BaseModel):
    names: list[str] = Field(default_factory=list, max_length=500)


class EventPostIn(BaseModel):
    name: str
    count: int = Field(default=1, ge=1, le=100)


def _connector(server_id: str, db_id: str):
    st = get_state()
    srv = st.store.get(server_id)
    db = st.store.get_database(server_id, db_id)
    password = st.manager._password(server_id)  # 401 before anything is opened
    return lambda: connect(srv, db.path, password)


@router.get("/suggestions")
def suggestions(server_id: str, db_id: str):
    """Event names found in POST_EVENT calls of triggers, procedures, functions and packages."""
    with get_state().manager.meta(server_id, db_id) as conn:
        return scan.scan_sources(conn)


@router.post("/sessions", status_code=201)
def open_session(server_id: str, db_id: str, body: EventNamesIn):
    try:
        sess = get_event_manager().open(server_id, db_id, _connector(server_id, db_id), body.names)
    except AuxConnectionError as exc:
        return _aux_error(exc)
    return sess.changes(0)


def _aux_error(exc: AuxConnectionError) -> JSONResponse:
    return JSONResponse(status_code=502, content={"detail": str(exc), "code": exc.code})


@router.get("/sessions/{session_id}")
def poll(server_id: str, db_id: str, session_id: str, after: int = 0):
    return get_event_manager().get(session_id, server_id, db_id).changes(after)


@router.post("/sessions/{session_id}/register")
def register(server_id: str, db_id: str, session_id: str, body: EventNamesIn):
    sess = get_event_manager().get(session_id, server_id, db_id)
    added = sess.add(body.names)
    return {"added": added, **sess.changes(0)}


@router.post("/sessions/{session_id}/unregister")
def unregister(server_id: str, db_id: str, session_id: str, body: EventNamesIn):
    sess = get_event_manager().get(session_id, server_id, db_id)
    sess.remove(body.names)
    return sess.changes(0)


@router.post("/sessions/{session_id}/reset")
def reset(server_id: str, db_id: str, session_id: str):
    sess = get_event_manager().get(session_id, server_id, db_id)
    sess.reset()
    return sess.changes(0)


@router.post("/sessions/{session_id}/reconnect")
def reconnect(server_id: str, db_id: str, session_id: str):
    mgr = get_event_manager()
    sess = mgr.get(session_id, server_id, db_id)
    sess._connect = _connector(server_id, db_id)  # pick up changed server settings
    try:
        return mgr.reconnect(sess).changes(0)
    except AuxConnectionError as exc:
        return _aux_error(exc)


@router.delete("/sessions/{session_id}", status_code=204)
def close_session(server_id: str, db_id: str, session_id: str):
    get_event_manager().get(session_id, server_id, db_id)
    get_event_manager().close(session_id)


@router.post("/post")
def post_event(server_id: str, db_id: str, body: EventPostIn):
    """Post an event in its own committed transaction (EXECUTE BLOCK ... POST_EVENT)."""
    st = get_state()
    if st.store.get(server_id).read_only:
        raise HTTPException(403, "Server is registered as read-only: posting events is not allowed")
    name = body.name.strip()
    encode_name(name)
    literal = "'" + name.replace("'", "''") + "'"
    sql = "EXECUTE BLOCK AS BEGIN " + " ".join(f"POST_EVENT {literal};" for _ in range(body.count)) + " END"
    with st.manager.meta(server_id, db_id) as conn:
        conn.cursor().execute(sql)
    return {"name": name, "count": body.count}
