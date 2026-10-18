"""Shared application state."""

from __future__ import annotations

from ..config import settings
from ..connections import ConnectionManager
from ..jobs import JobManager
from ..store import ServerStore


class State:
    def __init__(self) -> None:
        self.store = ServerStore(settings.servers_file)
        self.manager = ConnectionManager(self.store)
        self.jobs = JobManager()
        from ..security import attach  # master password, server mode (per-user registries)

        attach(self)


_state: State | None = None


def get_state() -> State:
    global _state
    if _state is None:
        _state = State()
    return _state


def reset_state() -> State:
    """Used by tests to start from a clean registry."""
    global _state
    if _state is not None:
        _state.manager.close_all()
    _state = State()
    return _state
