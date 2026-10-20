"""Multi-server overview API: health of every registered server and database on one page."""

from __future__ import annotations

from fastapi import APIRouter

from ..monitoring import overview

router = APIRouter(prefix="/api/overview", tags=["overview"])


@router.get("")
def get_overview(refresh: bool = False):
    """Checks run concurrently with a per-check timeout and are cached briefly (see monitoring/overview.py).
    ``refresh=true`` ignores the cache."""
    return overview.build(refresh=refresh)
