"""Trace API: start sessions from templates, live event stream, stop, list, save to file."""

from __future__ import annotations

from fastapi import APIRouter
from fastapi.responses import FileResponse
from pydantic import BaseModel

from ..maintenance import trace as T
from .deps import get_state

router = APIRouter(prefix="/api/servers/{server_id}/trace", tags=["trace"])


@router.get("/templates")
def templates(server_id: str):
    return [{"id": k, "label": v["label"], "description": v["description"], "params": v["params"]}
            for k, v in T.TEMPLATES.items()] + [
        {"id": "custom", "label": "Custom", "description": "Write the trace configuration yourself.", "params": {}}]


class TraceIn(BaseModel):
    template: str = "slow_queries"
    database_id: str | None = None  # None = all databases
    name: str | None = None
    time_threshold: int | None = None
    include_filter: str | None = None
    exclude_filter: str | None = None
    max_sql_length: int = 4000
    config: str | None = None  # custom configuration text


class PreviewIn(TraceIn):
    pass


def _config(server_id: str, body: TraceIn) -> tuple[str, str | None]:
    db_path = None
    label = None
    if body.database_id:
        db = get_state().store.get_database(server_id, body.database_id)
        db_path, label = db.path, db.name
    cfg = T.build_config(body.template, db_path, time_threshold=body.time_threshold,
                         include_filter=body.include_filter, exclude_filter=body.exclude_filter,
                         max_sql_length=body.max_sql_length, custom=body.config)
    return cfg, label


@router.post("/preview")
def preview(server_id: str, body: PreviewIn):
    return {"config": _config(server_id, body)[0]}


@router.get("/sessions")
def list_sessions(server_id: str):
    """Sessions started from fbadmin plus every trace session the server reports."""
    local = [s.summary() for s in T.get_trace_manager().list(server_id)]
    with get_state().manager.service(server_id) as svc:
        remote = T.server_sessions(svc)
    return {"local": local, "server": remote}


@router.post("/sessions", status_code=201)
def start(server_id: str, body: TraceIn):
    st = get_state()
    cfg, label = _config(server_id, body)
    st.store.get(server_id)
    st.manager._password(server_id)  # fail early (401) when no password is known
    sess = T.get_trace_manager().start(
        server_id, lambda: st.manager.service(server_id), name=body.name or f"fbadmin {body.template}",
        config=cfg, template=body.template, database=label)
    if sess.status == "failed":
        raise ValueError(f"Trace session failed to start: {sess.error}")
    return sess.summary()


@router.get("/sessions/{local_id}")
def get_session(server_id: str, local_id: str):
    return T.get_trace_manager().get(local_id).summary()


@router.get("/sessions/{local_id}/events")
def events(server_id: str, local_id: str, after: int = 0, q: str | None = None, types: str | None = None,
           limit: int = 1000):
    sess = T.get_trace_manager().get(local_id)
    evs = sess.events_after(after, q, [t for t in (types or "").split(",") if t], limit)
    return {"session": sess.summary(), "events": evs,
            "last_seq": sess.event_count}


@router.post("/sessions/{local_id}/stop")
def stop(server_id: str, local_id: str):
    sess = T.get_trace_manager().get(local_id)
    if sess.session_id is not None and sess.status == "running":
        with get_state().manager.service(server_id) as svc:
            T.stop_session(svc, sess.session_id)
    return sess.summary()


@router.delete("/sessions/{local_id}", status_code=204)
def delete(server_id: str, local_id: str):
    sess = T.get_trace_manager().get(local_id)
    if sess.status == "running" and sess.session_id is not None:
        with get_state().manager.service(server_id) as svc:
            T.stop_session(svc, sess.session_id)
    T.get_trace_manager().remove(local_id)


@router.get("/sessions/{local_id}/download")
def download(server_id: str, local_id: str):
    sess = T.get_trace_manager().get(local_id)
    if not sess.file.exists():
        raise LookupError("No trace output yet")
    return FileResponse(sess.file, media_type="text/plain", filename=f"trace-{sess.id}.log")


@router.post("/server-sessions/{session_id}/stop")
def stop_server_session(server_id: str, session_id: int):
    """Stop any trace session on the server (also ones not started by fbadmin)."""
    with get_state().manager.service(server_id) as svc:
        return {"output": T.stop_session(svc, session_id)}
