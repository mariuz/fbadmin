"""Firebird connection handling.

Two kinds of connections exist:

* **Metadata connections** - one per registered database, shared by the
  object explorer. Every call commits right away so new objects show up.
* **Query sessions** - one connection per Query Tool tab, each with its own
  transaction so users can run, commit or roll back independently
  (like pgAdmin's Query Tool).

The ``firebirdsql`` driver is not thread-safe, so each connection is guarded
by a lock.
"""

from __future__ import annotations

import threading
import time
import uuid
from contextlib import contextmanager
from typing import Iterator

import firebirdsql
from firebirdsql import services

from .store import DatabaseEntry, ServerEntry, ServerStore


class PasswordRequired(Exception):
    """Raised when a server has no saved password and none was given."""


class FbConnection:
    def __init__(self, conn: firebirdsql.Connection):
        self.conn = conn
        self.lock = threading.RLock()
        self.last_used = time.time()

    def close(self) -> None:
        with self.lock:
            try:
                self.conn.close()
            except Exception:
                pass


def connect(
    srv: ServerEntry,
    db_path: str,
    password: str,
    *,
    create: bool = False,
    page_size: int = 16384,
) -> firebirdsql.Connection:
    kwargs = dict(
        host=srv.host,
        port=srv.port,
        database=db_path,
        user=srv.user,
        password=password,
        role=srv.role or None,
        charset=srv.charset or "UTF8",
        wire_crypt=srv.wire_crypt,
        timeout=30,
    )
    if create:
        return firebirdsql.create_database(page_size=page_size, **kwargs)
    return firebirdsql.connect(**kwargs)


class ConnectionManager:
    def __init__(self, store: ServerStore):
        self.store = store
        self._lock = threading.RLock()
        self._meta: dict[tuple[str, str], FbConnection] = {}
        self._sessions: dict[str, tuple[str, str, FbConnection, dict]] = {}

    # --- helpers -------------------------------------------------------
    def _password(self, server_id: str) -> str:
        pw = self.store.password_for(server_id)
        if not pw:
            raise PasswordRequired(server_id)
        return pw

    def _open(self, server_id: str, db_id: str) -> FbConnection:
        srv = self.store.get(server_id)
        db: DatabaseEntry = self.store.get_database(server_id, db_id)
        return FbConnection(connect(srv, db.path, self._password(server_id)))

    # --- metadata connections -----------------------------------------
    @contextmanager
    def meta(self, server_id: str, db_id: str) -> Iterator[firebirdsql.Connection]:
        key = (server_id, db_id)
        with self._lock:
            fbc = self._meta.get(key)
            if fbc is None:
                fbc = self._open(server_id, db_id)
                self._meta[key] = fbc
        with fbc.lock:
            fbc.last_used = time.time()
            try:
                yield fbc.conn
                fbc.conn.commit()
            except (firebirdsql.OperationalError, OSError, ConnectionError, BrokenPipeError) as exc:
                # Connection may be dead (server restart, attachment killed): drop it.
                if _is_connection_lost(exc):
                    with self._lock:
                        self._meta.pop(key, None)
                    fbc.close()
                else:
                    _safe_rollback(fbc.conn)
                raise
            except Exception:
                _safe_rollback(fbc.conn)
                raise

    def is_connected(self, server_id: str, db_id: str) -> bool:
        return (server_id, db_id) in self._meta

    def disconnect(self, server_id: str, db_id: str | None = None) -> None:
        with self._lock:
            keys = [k for k in self._meta if k[0] == server_id and (db_id is None or k[1] == db_id)]
            for k in keys:
                self._meta.pop(k).close()
            for sid in [s for s, v in self._sessions.items() if v[0] == server_id and (db_id is None or v[1] == db_id)]:
                self._sessions.pop(sid)[2].close()

    # --- services API --------------------------------------------------
    @contextmanager
    def service(self, server_id: str):
        srv = self.store.get(server_id)
        svc = services.connect(
            host=srv.host,
            port=srv.port,
            user=srv.user,
            password=self._password(server_id),
            wire_crypt=srv.wire_crypt,
            timeout=60,
        )
        try:
            yield svc
        finally:
            try:
                svc.close()
            except Exception:
                pass

    # --- query tool sessions -------------------------------------------
    def open_session(self, server_id: str, db_id: str) -> str:
        fbc = self._open(server_id, db_id)
        sid = uuid.uuid4().hex
        with self._lock:
            self._sessions[sid] = (server_id, db_id, fbc, {"autocommit": True})
        return sid

    def session(self, sid: str) -> tuple[FbConnection, dict]:
        with self._lock:
            try:
                _, _, fbc, opts = self._sessions[sid]
            except KeyError:
                raise KeyError(f"Unknown or expired query session {sid!r}") from None
        fbc.last_used = time.time()
        return fbc, opts

    def close_session(self, sid: str) -> None:
        with self._lock:
            entry = self._sessions.pop(sid, None)
        if entry:
            _safe_rollback(entry[2].conn)
            entry[2].close()

    def reap_idle(self, max_idle: int) -> int:
        now = time.time()
        with self._lock:
            stale = [s for s, v in self._sessions.items() if now - v[2].last_used > max_idle]
        for sid in stale:
            self.close_session(sid)
        return len(stale)

    def close_all(self) -> None:
        with self._lock:
            for fbc in self._meta.values():
                fbc.close()
            for v in self._sessions.values():
                v[2].close()
            self._meta.clear()
            self._sessions.clear()


def _safe_rollback(conn) -> None:
    try:
        conn.rollback()
    except Exception:
        pass


def _is_connection_lost(exc: Exception) -> bool:
    if isinstance(exc, (OSError, ConnectionError, BrokenPipeError)):
        return True
    msg = str(exc).lower()
    return any(s in msg for s in ("connection shutdown", "connection lost", "connection reset", "broken pipe",
                                   "error writing data", "error reading data", "connection rejected"))
