"""Capacity and transaction-ID forecasting (roadmap 2.4): growth trends and the limits they run into."""

from __future__ import annotations

from fastapi import APIRouter
from pydantic import BaseModel, Field

from ..monitoring import forecast as F
from ..query import end_transaction
from ..serialize import to_json
from .deps import get_state

DB = "/api/servers/{server_id}/databases/{db_id}/monitoring/forecast"
router = APIRouter(tags=["monitoring"])


class BudgetIn(BaseModel):
    budget_bytes: int | None = Field(default=None, ge=1)


def _forecast(server_id: str, db_id: str, force: bool) -> dict:
    with get_state().manager.meta(server_id, db_id) as conn:
        try:
            F.observe(conn, server_id, db_id, force=force)  # at most one an hour, unless forced
            out = F.analyze(server_id, db_id, conn)
        finally:
            end_transaction(conn, commit=False)
    from ..monitoring import history

    out["tracked"] = bool(history.get_store().is_tracked(server_id, db_id))
    return to_json(out)


@router.get(DB)
def get_forecast(server_id: str, db_id: str):
    """The forecast (records an observation if the last one is more than an hour old)."""
    return _forecast(server_id, db_id, force=False)


@router.post(DB + "/observe")
def observe_now(server_id: str, db_id: str):
    """Record an observation now, then the forecast."""
    return _forecast(server_id, db_id, force=True)


@router.put(DB + "/budget")
def set_budget(server_id: str, db_id: str, body: BudgetIn):
    """The space set aside for the database (bytes; null: none)."""
    get_state().store.get_database(server_id, db_id)
    F.set_budget(server_id, db_id, body.budget_bytes)
    return {"budget_bytes": body.budget_bytes}
