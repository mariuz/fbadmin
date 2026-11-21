"""Fleet health (roadmap 2.5): every database the user may see, with its newest signals (fbadmin/fleet.py)."""

from __future__ import annotations

from fastapi import APIRouter

from .. import fleet as F

router = APIRouter(prefix="/api/fleet", tags=["monitoring"])


@router.get("")
def fleet_health():
    """One row per database: history, check-up, forecast, backup, drift, change requests, trace, alerts."""
    return F.fleet()
