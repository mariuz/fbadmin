"""Login sessions (in memory), the current principal and rate limiting.

Sessions are deliberately kept in memory: the per-user vault key lives in the
session, so a restart logs everyone out and locks every vault.
"""

from __future__ import annotations

import secrets
import threading
import time
from collections import deque
from contextvars import ContextVar
from dataclasses import dataclass, field


@dataclass
class Principal:
    user_id: int
    username: str
    role: str
    auth_source: str = "internal"
    session_id: str | None = None

    @property
    def is_admin(self) -> bool:
        return self.role == "admin"


#: The fbadmin user of the current request (None in desktop mode).
current_principal: ContextVar[Principal | None] = ContextVar("fbadmin_principal", default=None)
#: Client address of the current request (for the audit log).
current_client_ip: ContextVar[str | None] = ContextVar("fbadmin_client_ip", default=None)


@dataclass
class Session:
    id: str
    principal: Principal
    csrf: str
    created: float = field(default_factory=time.time)
    last_seen: float = field(default_factory=time.time)


class SessionStore:
    def __init__(self, lifetime: int, idle: int):
        self.lifetime = lifetime
        self.idle = idle
        self._sessions: dict[str, Session] = {}
        self._lock = threading.Lock()

    def create(self, principal: Principal) -> Session:
        sid = secrets.token_urlsafe(32)
        principal.session_id = sid
        s = Session(id=sid, principal=principal, csrf=secrets.token_urlsafe(32))
        with self._lock:
            self._sessions[sid] = s
        return s

    def get(self, sid: str | None) -> Session | None:
        if not sid:
            return None
        now = time.time()
        with self._lock:
            s = self._sessions.get(sid)
            if s is None:
                return None
            if now - s.created > self.lifetime or now - s.last_seen > self.idle:
                self._sessions.pop(sid, None)
                return None
            s.last_seen = now
            return s

    def remove(self, sid: str) -> Session | None:
        with self._lock:
            return self._sessions.pop(sid, None)

    def remove_user(self, user_id: int) -> list[Session]:
        with self._lock:
            gone = [s for s in self._sessions.values() if s.principal.user_id == user_id]
            for s in gone:
                self._sessions.pop(s.id, None)
            return gone

    def user_has_sessions(self, user_id: int) -> bool:
        now = time.time()
        with self._lock:
            return any(s.principal.user_id == user_id and now - s.created <= self.lifetime
                       and now - s.last_seen <= self.idle for s in self._sessions.values())

    def update_role(self, user_id: int, role: str) -> None:
        with self._lock:
            for s in self._sessions.values():
                if s.principal.user_id == user_id:
                    s.principal.role = role

    def purge(self) -> list[Session]:
        now = time.time()
        with self._lock:
            gone = [s for s in self._sessions.values()
                    if now - s.created > self.lifetime or now - s.last_seen > self.idle]
            for s in gone:
                self._sessions.pop(s.id, None)
            return gone


class RateLimiter:
    """Sliding-window counter: at most ``limit`` hits per ``window`` seconds per key."""

    def __init__(self, limit: int, window: int):
        self.limit = limit
        self.window = window
        self._hits: dict[str, deque[float]] = {}
        self._lock = threading.Lock()

    @property
    def enabled(self) -> bool:
        return self.limit > 0 and self.window > 0

    def _q(self, key: str, now: float) -> deque[float]:
        q = self._hits.setdefault(key, deque())
        while q and now - q[0] > self.window:
            q.popleft()
        return q

    def blocked(self, key: str) -> float:
        """Seconds to wait if ``key`` is over the limit, else 0."""
        if not self.enabled:
            return 0
        now = time.time()
        with self._lock:
            q = self._q(key, now)
            if len(q) >= self.limit:
                return max(1.0, self.window - (now - q[0]))
            return 0

    def hit(self, key: str) -> float:
        """Record one hit; returns seconds to wait if the limit is exceeded (the hit is then not counted)."""
        if not self.enabled:
            return 0
        now = time.time()
        with self._lock:
            q = self._q(key, now)
            if len(q) >= self.limit:
                return max(1.0, self.window - (now - q[0]))
            q.append(now)
            if len(self._hits) > 10000:  # forget idle keys
                for k in [k for k, v in self._hits.items() if not v or now - v[-1] > self.window]:
                    self._hits.pop(k, None)
            return 0

    def reset(self, key: str) -> None:
        with self._lock:
            self._hits.pop(key, None)
