"""Browser tabs of the UI (issue #9): closing the last tab disconnects everything (see connstate)."""

from __future__ import annotations

from fastapi import APIRouter
from pydantic import BaseModel, Field

from .. import connstate
from .deps import get_state

router = APIRouter(prefix="/api/client", tags=["client"])


class TabIn(BaseModel):
    tab: str = Field(min_length=8, max_length=64)


@router.post("/hello")
def hello(body: TabIn):
    """A tab opened or reloaded."""
    connstate.hello(body.tab)
    return {"disconnect_on_close": connstate.grace()}


@router.post("/bye")
def bye(body: TabIn):
    """A tab is closing (sent with keepalive from pagehide)."""
    connstate.bye(body.tab)
    return {"ok": True}


def _all_closed(who: str) -> None:
    """The last tab of ``who`` closed: everything disconnected; in desktop mode also the connections and the
    passwords typed this session (in server mode they are shared with other users)."""
    servers = connstate.disconnect_all(who)
    if who:
        return
    st = get_state()
    for sid in servers:
        st.manager.disconnect(sid)
        st.store.forget_password(sid)


connstate.on_all_closed(_all_closed)
