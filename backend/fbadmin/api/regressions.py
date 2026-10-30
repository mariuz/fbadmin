"""Query regression tracking (roadmap 2.2): statements that got slower or changed their plan."""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Query

from ..monitoring import history, regressions as R
from .deps import get_state

DB = "/api/servers/{server_id}/databases/{db_id}/monitoring/regressions"
router = APIRouter(tags=["monitoring"])


@router.get(DB)
def list_statements(server_id: str, db_id: str, only: Literal["all", "flagged", "regressions"] = "all",
                    limit: int = Query(500, ge=1, le=5000)):
    """Every tracked statement with its baseline and recent medians, plans and flags; regressions first."""
    get_state().store.get_database(server_id, db_id)
    store = history.get_store()
    s = store.get_settings()
    items = R.analyze(store, server_id, db_id, s, only=None if only == "all" else only)
    return {"tracking": bool(store.is_tracked(server_id, db_id)),
            "settings": {k: s[k] for k in R.DEFAULTS} | {"slow_seconds": s["slow_seconds"]},
            "regressions": sum(1 for i in items if i["regression"]), "statements": items[:limit]}


@router.get(DB + "/{sql_hash}")
def statement(server_id: str, db_id: str, sql_hash: str):
    """One statement: its runs over time (duration, plan, source) and its plans with their medians."""
    get_state().store.get_database(server_id, db_id)
    store = history.get_store()
    return R.detail(store, server_id, db_id, sql_hash, store.get_settings())


@router.post(DB + "/{sql_hash}/accept")
def accept(server_id: str, db_id: str, sql_hash: str):
    """The statement's current behaviour (its plan and its duration) is the new normal: the baseline restarts."""
    get_state().store.get_database(server_id, db_id)
    store = history.get_store()
    R.accept(store, server_id, db_id, sql_hash)
    try:
        get_state().security.audit.record(server_id=server_id, db_id=db_id, success=True, cat="ACTION",
                                          statement=f"-- query regressions: accepted {sql_hash} as the new baseline")
    except Exception:  # noqa: BLE001
        pass
    return {"ok": True}
