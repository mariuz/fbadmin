"""Event monitor sessions: one dedicated attachment + auxiliary connection per browser tab.

A session registers event names in requests of at most 15 names (like ``isc_event_block``),
counts arrivals per name, keeps a bounded log, and re-queues each request after every
notification (the protocol delivers one notification per queued request). The browser polls
``changes(after)`` like the trace viewer does. Sessions end when closed, when the server
connection is lost, or after ``IDLE_TIMEOUT`` seconds without a poll.
"""

from __future__ import annotations

import collections
import logging
import os
import threading
import time
import uuid

from .wire import MAX_NAMES_PER_REQUEST, AuxConnectionError, EventChannel, encode_name

log = logging.getLogger("fbadmin.events")

MAX_LOG = 2000
MAX_NAMES = 500
RATE_WINDOW = 300  # seconds of per-name history kept for the rate sparkline
RATE_BUCKETS = 30
IDLE_TIMEOUT = float(os.environ.get("FBADMIN_EVENTS_IDLE_TIMEOUT", "300"))
MAX_SESSIONS = int(os.environ.get("FBADMIN_EVENTS_MAX_SESSIONS", "20"))


class EventName:
    def __init__(self, name: str):
        self.name = name
        self.server_count: int | None = None  # last count reported by the server (None: no baseline yet)
        self.count = 0  # arrivals since registered / reset
        self.last_at: float | None = None
        self.registered_at = time.time()
        self.history: collections.deque[tuple[float, int]] = collections.deque()

    def summary(self, now: float) -> dict:
        while self.history and self.history[0][0] < now - RATE_WINDOW:
            self.history.popleft()
        step = RATE_WINDOW / RATE_BUCKETS
        buckets = [0] * RATE_BUCKETS
        for t, n in self.history:
            idx = min(RATE_BUCKETS - 1, max(0, int((t - (now - RATE_WINDOW)) // step)))
            buckets[idx] += n
        return {"name": self.name, "count": self.count, "last_at": self.last_at,
                "registered_at": self.registered_at, "listening": self.server_count is not None,
                "rate": buckets}


class EventSession:
    def __init__(self, server_id: str, db_id: str, connect):
        self.id = uuid.uuid4().hex
        self.server_id = server_id
        self.db_id = db_id
        self._connect = connect  # () -> firebirdsql connection (dedicated attachment)
        self.status = "starting"
        self.error: str | None = None
        self.error_code: str | None = None
        self.created_at = time.time()
        self.last_seen = time.time()
        self.names: dict[str, EventName] = {}
        self.requests: dict[int, tuple[str, ...]] = {}
        self.log: collections.deque[dict] = collections.deque(maxlen=MAX_LOG)
        self.seq = 0
        self.notifications = 0
        self._rid = 0
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._conn = None
        self._channel: EventChannel | None = None
        self._thread: threading.Thread | None = None

    # --- lifecycle -----------------------------------------------------------------
    def start(self) -> None:
        conn = self._connect()
        try:
            channel = EventChannel(conn)
        except AuxConnectionError as exc:
            # The server keeps waiting for the auxiliary connection, so a polite detach would
            # hang: drop the main socket instead.
            if exc.code != "aux_busy":  # (a busy request is finished and dropped in the background)
                _drop_socket(conn)
            self._fail(str(exc), exc.code)
            raise
        except Exception:
            _close_conn(conn)
            raise
        with self._lock:
            self._stop = threading.Event()
            self._conn, self._channel = conn, channel
            self.status = "listening"
            names = [n for n in self.names]
            self.requests.clear()
            for n in self.names.values():
                n.server_count = None
            try:
                self._queue_new(names)
            except Exception as exc:
                self._fail(f"Cannot register the events: {str(exc).strip() or exc.__class__.__name__}")
                self.close("error")
                raise
        self._thread = threading.Thread(target=self._run, args=(channel, self._stop),
                                        name=f"events-{self.id[:8]}", daemon=True)
        self._thread.start()

    def close(self, reason: str = "closed") -> None:
        self._stop.set()
        with self._lock:
            channel, conn = self._channel, self._conn
            self._channel = self._conn = None
            if self.status in ("listening", "starting"):
                self.status = reason
        if channel is not None:
            for rid in list(self.requests):
                try:
                    channel.cancel(rid)
                except Exception:  # noqa: BLE001
                    break
            channel.close()
        if conn is not None:
            _close_conn(conn)
        with self._lock:
            self.requests.clear()

    def _fail(self, message: str, code: str | None = None) -> None:
        with self._lock:
            self.status = "error"
            self.error = message
            self.error_code = code

    def _run(self, channel: EventChannel, stop: threading.Event) -> None:
        while not stop.is_set():
            if time.time() - self.last_seen > IDLE_TIMEOUT:
                log.info("event session %s idle, closing", self.id)
                self.close("expired")
                return
            try:
                got = channel.read(1.0)
                if got is not None:
                    with self._lock:
                        if stop.is_set():
                            return
                        self._notified(*got)
            except Exception as exc:  # noqa: BLE001 - connection lost, server shutdown ...
                if stop.is_set() or channel is not self._channel:
                    return
                log.info("event session %s failed: %s", self.id, exc)
                self._fail(f"Event connection lost: {str(exc).strip() or exc.__class__.__name__}", "connection_lost")
                self.close("error")
                return

    # --- registration ----------------------------------------------------------------
    def _next_rid(self) -> int:
        self._rid += 1
        return self._rid

    def _queue_group(self, names: tuple[str, ...]) -> None:
        rid = self._next_rid()
        counts = {n: (self.names[n].server_count or 0) for n in names}
        self._channel.queue(rid, counts)
        self.requests[rid] = names

    def _queue_new(self, names: list[str]) -> None:
        """Queue ``names`` (not yet in any request), filling the last request first."""
        if not names or self._channel is None:
            return
        rid, group = (max(self.requests.items()) if self.requests else (None, ()))
        if rid is not None and len(group) < MAX_NAMES_PER_REQUEST:
            self._channel.cancel(rid)
            del self.requests[rid]
            names = list(group) + names
        for i in range(0, len(names), MAX_NAMES_PER_REQUEST):
            self._queue_group(tuple(names[i:i + MAX_NAMES_PER_REQUEST]))

    def add(self, names: list[str]) -> list[str]:
        clean: list[str] = []
        for raw in names:
            name = raw.strip()
            encode_name(name)
            if name and name not in self.names and name not in clean:
                clean.append(name)
        with self._lock:
            if len(self.names) + len(clean) > MAX_NAMES:
                raise ValueError(f"At most {MAX_NAMES} event names per monitor")
            for n in clean:
                self.names[n] = EventName(n)
            if self.status == "listening":
                self._queue_new(clean)
        return clean

    def remove(self, names: list[str]) -> None:
        with self._lock:
            gone = {n for n in names if n in self.names}
            if not gone:
                return
            for rid, group in list(self.requests.items()):
                if gone & set(group):
                    del self.requests[rid]
                    if self._channel is not None:
                        self._channel.cancel(rid)
                        rest = tuple(n for n in group if n not in gone)
                        if rest:
                            self._queue_group(rest)
            for n in gone:
                del self.names[n]

    def reset(self) -> None:
        with self._lock:
            for n in self.names.values():
                n.count = 0
                n.last_at = None
                n.history.clear()
            self.log.clear()

    # --- notifications ----------------------------------------------------------------
    def _notified(self, rid: int, counts: dict[str, int]) -> None:
        now = time.time()
        with self._lock:
            self.notifications += 1
            for name, server_count in counts.items():
                ev = self.names.get(name)
                if ev is None:
                    continue
                if ev.server_count is None:  # first answer after queueing: the baseline
                    ev.server_count = server_count
                    continue
                delta = server_count - ev.server_count
                if delta <= 0:
                    continue
                ev.server_count = server_count
                ev.count += delta
                ev.last_at = now
                ev.history.append((now, delta))
                self.seq += 1
                self.log.append({"seq": self.seq, "time": now, "name": name, "count": delta, "total": ev.count})
            group = self.requests.pop(rid, None)
            if group is not None and self._channel is not None:
                # The request is finished; queue the same names again (new id) with the new counts.
                self._queue_group(group)

    # --- views -------------------------------------------------------------------------
    def summary(self) -> dict:
        now = time.time()
        with self._lock:
            return {"id": self.id, "server_id": self.server_id, "db_id": self.db_id, "status": self.status,
                    "error": self.error, "error_code": self.error_code, "created_at": self.created_at,
                    "now": now, "requests": len(self.requests), "notifications": self.notifications,
                    "last_seq": self.seq, "rate_window": RATE_WINDOW,
                    "names": [n.summary(now) for n in self.names.values()]}

    def changes(self, after: int = 0, limit: int = 500) -> dict:
        self.last_seen = time.time()
        with self._lock:
            entries = [e for e in self.log if e["seq"] > after][-limit:]
        return {"session": self.summary(), "log": entries}


class EventManager:
    def __init__(self) -> None:
        self.sessions: dict[str, EventSession] = {}
        self._lock = threading.Lock()

    def open(self, server_id: str, db_id: str, connect, names: list[str] | None = None) -> EventSession:
        with self._lock:
            self._prune()
            if len(self.sessions) >= MAX_SESSIONS:
                raise ValueError(f"Too many event monitors are open (at most {MAX_SESSIONS}); close one first")
            sess = EventSession(server_id, db_id, connect)
            self.sessions[sess.id] = sess
        try:
            if names:
                sess.add(names)
            sess.start()
        except Exception:
            self.sessions.pop(sess.id, None)
            sess.close()
            raise
        return sess

    def _prune(self) -> None:
        for sid, s in list(self.sessions.items()):
            if s.status not in ("listening", "starting") and time.time() - s.last_seen > IDLE_TIMEOUT:
                self.sessions.pop(sid, None)

    def get(self, session_id: str, server_id: str | None = None, db_id: str | None = None) -> EventSession:
        sess = self.sessions.get(session_id)
        if sess is None or (server_id and sess.server_id != server_id) or (db_id and sess.db_id != db_id):
            raise KeyError(f"Unknown or expired event monitor session {session_id!r}")
        return sess

    def reconnect(self, sess: EventSession) -> EventSession:
        sess.close("closed")
        sess.status, sess.error, sess.error_code = "starting", None, None
        sess.start()
        return sess

    def close(self, session_id: str) -> None:
        sess = self.sessions.pop(session_id, None)
        if sess:
            sess.close()

    def close_server(self, server_id: str, db_id: str | None = None) -> None:
        for sid, s in list(self.sessions.items()):
            if s.server_id == server_id and (db_id is None or s.db_id == db_id):
                self.close(sid)

    def close_all(self) -> None:
        for sid in list(self.sessions):
            self.close(sid)


def _drop_socket(conn) -> None:
    try:
        conn.sock.close()
    except Exception:  # noqa: BLE001
        pass


def _close_conn(conn) -> None:
    done = threading.Event()

    def _close():
        try:
            conn.close()
        except Exception:  # noqa: BLE001
            pass
        done.set()

    threading.Thread(target=_close, daemon=True).start()
    if not done.wait(5):
        _drop_socket(conn)


_manager = EventManager()


def get_event_manager() -> EventManager:
    return _manager
