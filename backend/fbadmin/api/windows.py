"""Maintenance windows (roadmap 2.5; fbadmin/windows.py)."""

from __future__ import annotations

import time

from fastapi import APIRouter

from .. import windows as W
from ..notebook_runs import actor
from ..security.access import ADMIN, require_level
from ..security.core import require_admin
from .deps import get_state

router = APIRouter(prefix="/api/maintenance-windows", tags=["maintenance"])


def _visible(w: W.Window) -> bool:
    if not w.server_id:
        return True
    try:
        st = get_state()
        if w.database_id:
            st.store.get_database(w.server_id, w.database_id)
        else:
            st.store.get(w.server_id)
        return True
    except Exception:  # noqa: BLE001 - hidden or removed
        return False


def _may_change(w: W.Window) -> None:
    """A database's window needs the admin level there; a server's, on the server; a global one, an fbadmin
    administrator."""
    if w.database_id:
        get_state().store.get_database(w.server_id, w.database_id)
        require_level(w.server_id, w.database_id, ADMIN, "a maintenance window")
    elif w.server_id:
        get_state().store.get(w.server_id)
        require_level(w.server_id, None, ADMIN, "a maintenance window for the whole server")
    else:
        require_admin()


@router.get("")
def list_windows(server_id: str | None = None, database_id: str | None = None):
    """Windows visible to the user; with a database, those that cover it."""
    now = time.time()
    out = [W.view(w, now) for w in W.get_store().all() if _visible(w)
           and (not server_id or w.covers(server_id, database_id))]
    return sorted(out, key=lambda v: (v["next_start"] or 9e18, v["name"]))


@router.post("", status_code=201)
def create(body: W.Window):
    _may_change(body)
    w = body.model_copy(update={"created_by": actor().username, "created_at": time.time()})
    return W.view(W.get_store().put(w))


@router.put("/{wid}")
def update(wid: str, body: W.Window):
    cur = W.get_store().get(wid)
    _may_change(cur)
    _may_change(body)
    w = body.model_copy(update={"id": wid, "created_by": cur.created_by, "created_at": cur.created_at})
    return W.view(W.get_store().put(w))


@router.delete("/{wid}", status_code=204)
def delete(wid: str):
    _may_change(W.get_store().get(wid))
    W.get_store().delete(wid)
