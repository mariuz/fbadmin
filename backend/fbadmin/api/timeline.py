"""Database timeline (roadmap 2.3): one history per database of schema changes, maintenance, backups,
alerts, upgrades and (for administrators) audit entries - "what changed before it broke?"."""

from __future__ import annotations

from fastapi import APIRouter, Query

from .. import timeline
from ..serialize import to_json
from .deps import get_state
from .library import actor

router = APIRouter(tags=["timeline"])


@router.get("/api/servers/{server_id}/databases/{db_id}/timeline")
def database_timeline(server_id: str, db_id: str, since: float | None = None, until: float | None = None,
                      category: str | None = None, q: str | None = None,
                      limit: int = Query(500, ge=1, le=5000), audit_queries: bool = False):
    """Events newest first. ``since`` / ``until``: unix seconds; ``category``: comma-separated
    (schema, data, backup, maintenance, upgrade, alert, audit). Audit entries are only included for fbadmin
    administrators (``audit_queries`` adds SELECTs and transaction statements)."""
    get_state().store.get_database(server_id, db_id)  # 404 for unknown databases
    who = actor()
    cats = [c for c in (category or "").split(",") if c in timeline.CATEGORIES] or None
    out = timeline.collect(server_id, db_id, since=since, until=until, categories=cats, q=q, limit=limit,
                           with_audit=who.is_admin, audit_queries=audit_queries)
    return to_json(out)
