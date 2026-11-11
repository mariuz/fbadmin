"""Continuous trace capture (roadmap 2.4): the always-on trace session of a database (monitoring/trace_capture.py)."""

from __future__ import annotations

from fastapi import APIRouter
from pydantic import BaseModel, Field

from ..monitoring import history
from ..monitoring import trace_capture as TC
from ..security import runas
from ..security.access import ADMIN, require_level
from .deps import get_state

DB = "/api/servers/{server_id}/databases/{db_id}/monitoring/trace-capture"
router = APIRouter(tags=["monitoring"])


class CaptureIn(BaseModel):
    enabled: bool
    threshold_ms: int = Field(default=TC.DEFAULT_THRESHOLD_MS, ge=1, le=3_600_000)


def _view(server_id: str, db_id: str) -> dict:
    cfg = TC.config(server_id, db_id)
    return {"enabled": cfg["enabled"], "threshold_ms": cfg["threshold_ms"], "updated_at": cfg["updated_at"],
            "tracked": bool(history.get_store().is_tracked(server_id, db_id)),
            "per_minute": TC.PER_MINUTE, **TC.status(server_id, db_id)}


@router.get(DB)
def get_capture(server_id: str, db_id: str):
    """The settings and the state of the continuous trace session."""
    get_state().store.get_database(server_id, db_id)
    return _view(server_id, db_id)


@router.put(DB)
def set_capture(server_id: str, db_id: str, body: CaptureIn):
    """Turn the capture on or off (it needs the database's history to be recorded) and change its threshold."""
    db = get_state().store.get_database(server_id, db_id)
    require_level(server_id, db_id, ADMIN, "a continuous trace session")
    if body.enabled:
        if not history.get_store().is_tracked(server_id, db_id):
            raise ValueError("Record the history of this database first: the trace feeds it")
        get_state().manager._password(server_id)  # fail early (401) when no password is known
    cred = runas.issue(server_id, f"continuous trace: {db.name}") if body.enabled else None
    runas.revoke(TC.save(server_id, db_id, body.enabled, body.threshold_ms, cred))
    TC.sync(server_id, db_id)
    return _view(server_id, db_id)
