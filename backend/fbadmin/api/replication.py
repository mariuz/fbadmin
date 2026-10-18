"""Replication API (milestone 0.8, Firebird 4+)."""

from __future__ import annotations

import re
from typing import Any

from fastapi import APIRouter
from pydantic import BaseModel

from ..monitoring import replication
from ..query import end_transaction
from ..serialize import to_json
from .deps import get_state

router = APIRouter(prefix="/api/servers/{server_id}/databases/{db_id}/replication", tags=["replication"])
conf_router = APIRouter(prefix="/api/replication", tags=["replication"])


def _status(server_id: str, db_id: str) -> dict[str, Any]:
    with get_state().manager.meta(server_id, db_id) as conn:
        out = replication.status(conn)
        end_transaction(conn, commit=True)
    return out


def _require(st: dict) -> None:
    if not st["supported"]:
        raise ValueError(f"Replication needs Firebird 4.0 or newer (this server runs {st['engine_version']}).")


@router.get("")
def replication_status(server_id: str, db_id: str, peer_server: str | None = None, peer_db: str | None = None):
    """Publication, published tables, replica mode and replication sequence.

    With ``peer_server``/``peer_db`` (the other side, registered in fbadmin) the replication sequence of
    both databases is compared, which approximates the replication lag in journal segments.
    """
    st = _status(server_id, db_id)
    if peer_server and peer_db and st["supported"]:
        try:
            peer = _status(peer_server, peer_db)
            st["peer"] = {k: peer.get(k) for k in ("database_name", "replica_mode", "replication_sequence",
                                                    "publication_enabled", "guid", "engine_version")}
            a, b = st.get("replication_sequence"), peer.get("replication_sequence")
            if a is not None and b is not None:
                primary_seq, replica_seq = (a, b) if st["replica_mode"] == "none" else (b, a)
                st["peer"]["segments_behind"] = max(0, primary_seq - replica_seq)
        except Exception as exc:  # noqa: BLE001
            st["peer"] = {"error": str(exc).strip()}
    return to_json(st)


class PublicationIn(BaseModel):
    enabled: bool


@router.post("/publication")
def set_publication(server_id: str, db_id: str, body: PublicationIn):
    _require(_status(server_id, db_id))
    with get_state().manager.meta(server_id, db_id) as conn:
        sql = replication.set_publication(conn, body.enabled)
    return {"sql": sql, "status": to_json(_status(server_id, db_id))}


class TablesIn(BaseModel):
    action: str  # include | exclude
    tables: list[str] = []
    all: bool = False


@router.post("/tables")
def change_tables(server_id: str, db_id: str, body: TablesIn):
    _require(_status(server_id, db_id))
    with get_state().manager.meta(server_id, db_id) as conn:
        sql = replication.change_tables(conn, body.action, body.tables, body.all)
    return {"sql": sql, "status": to_json(_status(server_id, db_id))}


class ReplicaModeIn(BaseModel):
    mode: str  # none | read_only | read_write


@router.post("/replica-mode")
def set_replica_mode(server_id: str, db_id: str, body: ReplicaModeIn):
    """Switch replica mode (gfix -replica). The engine needs exclusive access to the database: fbadmin
    closes its own connections to it (metadata and Query Tool tabs); other attachments must be gone."""
    _require(_status(server_id, db_id))
    st = get_state()
    db = st.store.get_database(server_id, db_id)
    mgr = st.manager
    with mgr._lock:
        own = {s.attachment_id for s in mgr._sessions.values() if s.server_id == server_id and s.db_id == db_id}
    with mgr.meta(server_id, db_id) as conn:
        cur = conn.cursor()
        cur.execute("SELECT MON$ATTACHMENT_ID, TRIM(MON$USER), TRIM(MON$REMOTE_PROCESS) FROM MON$ATTACHMENTS "
                    "WHERE MON$ATTACHMENT_ID <> CURRENT_CONNECTION AND COALESCE(MON$SYSTEM_FLAG, 0) = 0")
        others = [r for r in cur.fetchall() if r[0] not in own]
    if others:
        who = ", ".join(f"{a} ({u}, {(p or '?').split('/')[-1]})" for a, u, p in others[:10])
        raise ValueError("Changing the replica mode needs exclusive access to the database. Other attachments: "
                         f"{who}. Disconnect them (Monitor) or shut the database down first.")
    mgr.disconnect(server_id, db_id)
    with mgr.service(server_id) as svc:
        replication.set_replica_mode(svc, db.path, body.mode)
    return {"mode": body.mode, "status": to_json(_status(server_id, db_id))}


@router.get("/log")
def replication_log(server_id: str, db_id: str, limit: int = 100):
    """Replication-related entries of firebird.log (errors are also written to replication.log on the
    server, which is not readable remotely)."""
    with get_state().manager.service(server_id) as svc:
        log = svc.getLog() or ""
    entries = [e.strip() for e in re.split(r"\n\s*\n", log) if e.strip()]
    hits = [e for e in entries if re.search(r"replic|journal|segment", e, re.I)]
    return {"entries": hits[-limit:], "total_entries": len(entries)}


class ConfIn(BaseModel):
    model_config = {"extra": "allow"}
    role: str = "primary"
    database: str


@conf_router.post("/conf")
def generate_conf(body: ConfIn):
    return replication.generate_conf(body.model_dump())
