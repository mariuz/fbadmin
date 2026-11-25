"""Connected / disconnected state of servers and databases as the browser UI sees them (issue #9).

**Disconnect** in the UI closes the connections and also *ends access* until the user chooses **Connect**:
requests from the UI that would open a connection to a disconnected server or database are refused with
HTTP 409 ``{"code": "disconnected", "server_id", "database_id"}`` instead of silently reconnecting. The UI
answers that with a Connect dialog.

- Every server starts disconnected when fbadmin starts. A server becomes connected for the UI when the user
  connects it (``POST /api/servers/{id}/connect``, or the Connect dialog), connects one of its databases
  (``POST .../databases/{db}/connect``) or registers it.
- Disconnecting a server also forgets its password typed for this session (a saved password stays saved).
- Closing the last fbadmin browser tab disconnects everything after ``FBADMIN_DISCONNECT_ON_CLOSE`` seconds
  (default 15; ``0`` turns it off). A reload says hello again in time, so it does not disconnect.

Only requests from the browser UI are checked (they carry ``X-Fbadmin-Client: ui``): background work
(scheduler, alerts, monitoring history, continuous trace) and the command line / API tokens are not affected.
In server mode the state is kept per fbadmin user; in desktop mode there is one user.
"""

from __future__ import annotations

import os
import threading
import time
from contextvars import ContextVar
from collections.abc import Callable

#: True while handling a request from the browser UI.
ui_request: ContextVar[bool] = ContextVar("fbadmin_ui_request", default=False)

UI_HEADER = "x-fbadmin-client"

_lock = threading.RLock()
_connected: dict[str, set[str]] = {}               # owner -> connected server ids
_db_off: dict[str, set[tuple[str, str]]] = {}      # owner -> disconnected (server, database)
_tabs: dict[str, dict[str, float]] = {}            # owner -> open browser tab id -> last hello
_timers: dict[str, threading.Timer] = {}


class Disconnected(Exception):
    """The UI asked for a server or database the user disconnected (HTTP 409, code "disconnected")."""

    def __init__(self, server_id: str, db_id: str | None = None):
        super().__init__(f"Disconnected from {'the database' if db_id else 'the server'}: connect again to continue")
        self.server_id = server_id
        self.db_id = db_id


def owner() -> str:
    """The fbadmin user of the current request ("" in desktop mode)."""
    from .security.sessions import current_principal

    p = current_principal.get()
    return p.username if p is not None else ""


def grace() -> float:
    try:
        return float(os.environ.get("FBADMIN_DISCONNECT_ON_CLOSE", "15"))
    except ValueError:
        return 15.0


# --- state ------------------------------------------------------------------------------
def is_connected(server_id: str, db_id: str | None = None, who: str | None = None) -> bool:
    who = owner() if who is None else who
    with _lock:
        if server_id not in _connected.get(who, set()):
            return False
        return db_id is None or (server_id, db_id) not in _db_off.get(who, set())


def check(server_id: str | None, db_id: str | None = None) -> None:
    """Raise Disconnected when the current UI request needs a server or database the user disconnected."""
    if not server_id or not ui_request.get():
        return
    who = owner()
    with _lock:
        if server_id not in _connected.get(who, set()):
            raise Disconnected(server_id, None)
        if db_id is not None and (server_id, db_id) in _db_off.get(who, set()):
            raise Disconnected(server_id, db_id)


def connect(server_id: str, db_id: str | None = None) -> None:
    """An explicit Connect: the server (and the database) may be used again."""
    who = owner()
    with _lock:
        _connected.setdefault(who, set()).add(server_id)
        if db_id is not None:
            _db_off.get(who, set()).discard((server_id, db_id))


def disconnect(server_id: str, db_id: str | None = None) -> None:
    who = owner()
    with _lock:
        if db_id is None:
            _connected.get(who, set()).discard(server_id)
            _db_off[who] = {k for k in _db_off.get(who, set()) if k[0] != server_id}
        else:
            _db_off.setdefault(who, set()).add((server_id, db_id))


def disconnect_all(who: str) -> list[str]:
    """Everything of ``who`` disconnected; returns the server ids that were connected."""
    with _lock:
        was = sorted(_connected.pop(who, set()))
        _db_off.pop(who, None)
        return was


def database_of(srv, db_path: str) -> str | None:
    """The registered database id of ``db_path`` on ``srv`` (None for an unregistered path)."""
    for d in getattr(srv, "databases", []) or []:
        if d.path == db_path:
            return d.id
    return None


# --- browser tabs -------------------------------------------------------------------------
_on_all_closed: Callable[[str], None] | None = None


def on_all_closed(fn: Callable[[str], None]) -> None:
    """Called (in a timer thread) with the owner when their last tab closed and did not come back."""
    global _on_all_closed
    _on_all_closed = fn


def hello(tab: str) -> None:
    who = owner()
    with _lock:
        _tabs.setdefault(who, {})[tab] = time.time()
        t = _timers.pop(who, None)
    if t is not None:
        t.cancel()


def bye(tab: str) -> None:
    who = owner()
    wait = grace()
    with _lock:
        tabs = _tabs.get(who, {})
        tabs.pop(tab, None)
        if tabs or wait <= 0:
            return
        old = _timers.pop(who, None)
        timer = threading.Timer(wait, _closed, args=(who,))
        timer.daemon = True
        _timers[who] = timer
    if old is not None:
        old.cancel()
    timer.start()


def _closed(who: str) -> None:
    with _lock:
        if _tabs.get(who) or _timers.get(who) is None:
            return  # a tab came back (a reload)
        _timers.pop(who, None)
    if _on_all_closed is not None:
        try:
            _on_all_closed(who)
        except Exception:  # noqa: BLE001 - a timer thread must not raise
            pass


def reset() -> None:
    """Forget everything (tests)."""
    with _lock:
        _connected.clear()
        _db_off.clear()
        _tabs.clear()
        for t in _timers.values():
            t.cancel()
        _timers.clear()
