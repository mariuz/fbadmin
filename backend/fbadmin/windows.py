"""Maintenance windows (roadmap 2.5): periods when work on a database is expected.

* **Alerts are muted** during a window that covers their database (or server): they are still recorded in the
  alert history, marked *muted*, but not sent to any channel.
* **Approved change requests can wait for the next window**: *Apply in the next maintenance window* stores when
  it opens, and the scheduler applies the change then, as the user who asked for it.

A window covers one database, every database of a server (``database_id`` empty) or everything (``server_id``
empty). It is **recurring** (a cron expression for when it opens, and how long it lasts) or **once** (a start and
an end). Kept in ``<data dir>/maintenance_windows.json`` (mode 0600).
"""

from __future__ import annotations

import json
import os
import secrets
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator

from .config import settings
from .maintenance.cron import Cron


class Window(BaseModel):
    id: str = Field(default_factory=lambda: secrets.token_hex(4))
    name: str = Field(min_length=1, max_length=120)
    server_id: str | None = None      # None: every server
    database_id: str | None = None    # None: every database of the server
    kind: Literal["recurring", "once"] = "recurring"
    cron: str | None = Field(default=None, max_length=120)   # recurring: when it opens
    duration_minutes: int = Field(default=120, ge=5, le=7 * 24 * 60)
    start: float | None = None        # once
    end: float | None = None
    mute_alerts: bool = True
    enabled: bool = True
    created_by: str | None = None
    created_at: float = Field(default_factory=time.time)

    @model_validator(mode="after")
    def _check(self) -> Window:
        if self.database_id and not self.server_id:
            raise ValueError("A window for a database needs its server")
        if self.kind == "recurring":
            if not self.cron:
                raise ValueError("Say when the window opens (a cron expression)")
            Cron(self.cron)  # raises CronError (a ValueError) when invalid
        else:
            if not (self.start and self.end) or self.end <= self.start:
                raise ValueError("A one-off window needs a start before its end")
        return self

    def covers(self, server_id: str | None, db_id: str | None) -> bool:
        if self.server_id and self.server_id != server_id:
            return False
        return not self.database_id or self.database_id == db_id

    def current(self, now: float) -> tuple[float, float] | None:
        """(start, end) of the occurrence open at ``now``, or None."""
        if not self.enabled:
            return None
        if self.kind == "once":
            return (self.start, self.end) if self.start <= now < self.end else None
        dur = self.duration_minutes * 60
        start = Cron(self.cron).next(datetime.fromtimestamp(now - dur) - timedelta(seconds=1)).timestamp()
        return (start, start + dur) if start <= now < start + dur else None

    def next_start(self, now: float) -> tuple[float, float] | None:
        """(start, end) of the next occurrence that has not ended: the open one, or the next one."""
        if not self.enabled:
            return None
        cur = self.current(now)
        if cur:
            return cur
        if self.kind == "once":
            return (self.start, self.end) if self.start > now else None
        start = Cron(self.cron).next(datetime.fromtimestamp(now)).timestamp()
        return start, start + self.duration_minutes * 60


class WindowStore:
    def __init__(self, path: Path):
        self.path = path
        self._lock = threading.RLock()
        self.items: dict[str, Window] = {}
        if path.exists():
            for d in json.loads(path.read_text("utf-8") or "{}").get("windows", []):
                try:
                    w = Window(**d)
                    self.items[w.id] = w
                except Exception:  # noqa: BLE001 - a damaged entry must not hide the others
                    continue

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"windows": [w.model_dump() for w in self.items.values()]}), "utf-8")
        os.chmod(tmp, 0o600)
        os.replace(tmp, self.path)

    def put(self, w: Window) -> Window:
        with self._lock:
            self.items[w.id] = w
            self._save()
        return w

    def get(self, wid: str) -> Window:
        with self._lock:
            w = self.items.get(wid)
            if w is None:
                raise KeyError("Unknown maintenance window")
            return w

    def delete(self, wid: str) -> None:
        with self._lock:
            self.items.pop(wid, None)
            self._save()

    def all(self) -> list[Window]:
        with self._lock:
            return list(self.items.values())


_store: WindowStore | None = None
_store_lock = threading.Lock()


def get_store() -> WindowStore:
    global _store
    path = Path(settings.data_dir) / "maintenance_windows.json"
    with _store_lock:
        if _store is None or _store.path != path:
            _store = WindowStore(path)
        return _store


def open_window(server_id: str | None, db_id: str | None, now: float | None = None,
                purpose: str = "any") -> Window | None:
    """The window open now for this database (``purpose`` "alerts": only windows that mute alerts)."""
    now = now or time.time()
    for w in get_store().all():
        if purpose == "alerts" and not w.mute_alerts:
            continue
        if w.covers(server_id, db_id) and w.current(now):
            return w
    return None


def next_window(server_id: str, db_id: str, now: float | None = None) -> tuple[Window, float, float] | None:
    """The window of this database that opens first (or is open now): (window, start, end)."""
    now = now or time.time()
    best = None
    for w in get_store().all():
        if not w.covers(server_id, db_id):
            continue
        occ = w.next_start(now)
        if occ and (best is None or occ[0] < best[1]):
            best = (w, occ[0], occ[1])
    return best


def view(w: Window, now: float | None = None) -> dict[str, Any]:
    now = now or time.time()
    cur = w.current(now)
    nxt = w.next_start(now)
    return {**w.model_dump(), "open": bool(cur), "open_until": cur[1] if cur else None,
            "next_start": nxt[0] if nxt else None, "next_end": nxt[1] if nxt else None}


# ===================================================================== change requests waiting for a window
def apply_due(now: float | None = None) -> list[str]:
    """Called by the scheduler: start the approved change requests whose window is open (as who scheduled them).
    Returns the ids started."""
    from . import changes as C
    from .security.runas import acting_as, revoke

    now = now or time.time()
    started = []
    for cr in C.get_store().all():
        wait = cr.apply_in_window
        if cr.status != "approved" or not wait or wait.get("at", 0) > now:
            continue
        if open_window(cr.server_id, cr.database_id, now) is None:
            continue  # the window was changed or removed: wait for the next one
        by = C.Actor(username=wait.get("by"))
        try:
            with acting_as(wait.get("run_as"), "applying a change request in a maintenance window"):
                from .api.changes import apply

                job = apply(cr.id)
            started.append(cr.id)
            note, action = f"started in the maintenance window (job {job['id']})", "window_apply"
        except Exception as exc:  # noqa: BLE001 - recorded on the request; it stays approved
            note = " ".join(str(getattr(exc, "detail", "") or exc).split()) or exc.__class__.__name__
            action = "window_failed"

        def done(c, note=note, action=action, by=by) -> None:
            c.apply_in_window = None
            c.log(by, action, note)

        C.get_store().update(cr.id, done)
        revoke(wait.get("run_as"))
    return started
