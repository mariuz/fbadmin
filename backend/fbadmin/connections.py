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

import os
import threading
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager

import firebirdsql
from firebirdsql import services

from .query import QuerySession, apply_tpb, end_transaction
from .store import DatabaseEntry, ServerEntry, ServerStore


# Extra (non-primary) metadata connections idle for longer than this are closed.
META_EXTRA_IDLE = 120


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
        self._meta: dict[tuple[str, str], list[FbConnection]] = {}
        self.meta_pool_size = max(1, int(os.environ.get("FBADMIN_META_POOL_SIZE", "3")))
        self._sessions: dict[str, QuerySession] = {}

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
    # Each database has a small pool of metadata connections (FBADMIN_META_POOL_SIZE,
    # default 3) so one slow request (a big DDL extraction, a long MON$ query) does not
    # block the object explorer. The first ("primary") connection is always preferred;
    # extra connections are only opened while it is busy and are closed again by
    # reap_idle() after META_EXTRA_IDLE seconds, so sequential use behaves exactly like
    # the original single shared connection.
    def _acquire_meta(self, key: tuple[str, str]) -> FbConnection:
        """Return a pooled connection for ``key`` with its lock held by the caller."""
        while True:
            with self._lock:
                pool = self._meta.get(key)
                if not pool:
                    fbc = self._open(*key)
                    fbc.lock.acquire()
                    self._meta[key] = [fbc]
                    return fbc
                for fbc in pool:  # primary first; RLock => nested use in one thread reuses it
                    if fbc.lock.acquire(blocking=False):
                        return fbc
                if len(pool) < self.meta_pool_size:
                    fbc = self._open(*key)
                    fbc.lock.acquire()
                    pool.append(fbc)
                    return fbc
            time.sleep(0.01)  # all busy and the pool is full: wait for one to be released

    def _drop_meta(self, key: tuple[str, str], fbc: FbConnection) -> None:
        with self._lock:
            pool = self._meta.get(key)
            if pool and fbc in pool:
                pool.remove(fbc)
                if not pool:
                    self._meta.pop(key, None)

    @contextmanager
    def meta(self, server_id: str, db_id: str) -> Iterator[firebirdsql.Connection]:
        key = (server_id, db_id)
        fbc = self._acquire_meta(key)
        try:
            fbc.last_used = time.time()
            try:
                yield fbc.conn
                fbc.conn.commit()
            except (firebirdsql.OperationalError, OSError, ConnectionError, BrokenPipeError) as exc:
                # Connection may be dead (server restart, attachment killed): drop it.
                if _is_connection_lost(exc):
                    self._drop_meta(key, fbc)
                    fbc.close()
                else:
                    _safe_rollback(fbc.conn)
                raise
            except Exception:
                _safe_rollback(fbc.conn)
                raise
        finally:
            fbc.last_used = time.time()
            fbc.lock.release()

    def is_connected(self, server_id: str, db_id: str) -> bool:
        return bool(self._meta.get((server_id, db_id)))

    def disconnect(self, server_id: str, db_id: str | None = None) -> None:
        with self._lock:
            keys = [k for k in self._meta if k[0] == server_id and (db_id is None or k[1] == db_id)]
            for k in keys:
                for fbc in self._meta.pop(k):
                    fbc.close()
            for sid in [s for s, v in self._sessions.items()
                        if v.server_id == server_id and (db_id is None or v.db_id == db_id)]:
                _close_quietly(self._sessions.pop(sid).conn)

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
    def open_session(self, server_id: str, db_id: str) -> tuple[str, QuerySession]:
        fbc = self._open(server_id, db_id)
        conn = fbc.conn
        cur = conn.cursor()
        cur.execute(
            "SELECT CURRENT_CONNECTION, RDB$GET_CONTEXT('SYSTEM', 'ENGINE_VERSION') FROM RDB$DATABASE"
        )
        att, version = cur.fetchone()
        end_transaction(conn, commit=True)
        sess = QuerySession(conn=conn, server_id=server_id, db_id=db_id, attachment_id=att,
                            engine_major=int(str(version).split(".")[0]))
        apply_tpb(conn, sess.tx.tpb())
        sid = uuid.uuid4().hex
        with self._lock:
            self._sessions[sid] = sess
        return sid, sess

    def session(self, sid: str) -> QuerySession:
        with self._lock:
            try:
                sess = self._sessions[sid]
            except KeyError:
                raise KeyError(f"Unknown or expired query session {sid!r}") from None
        sess.last_used = time.time()
        return sess

    def close_session(self, sid: str) -> None:
        with self._lock:
            sess = self._sessions.pop(sid, None)
        if sess:
            _safe_rollback(sess.conn)
            _close_quietly(sess.conn)

    def reap_idle(self, max_idle: int) -> int:
        now = time.time()
        with self._lock:
            stale = [s for s, v in self._sessions.items() if now - v.last_used > max_idle and not v.lock._is_owned()]
        for sid in stale:
            self.close_session(sid)
        self._reap_meta_extras(META_EXTRA_IDLE)
        return len(stale)

    def _reap_meta_extras(self, max_idle: float) -> None:
        """Close idle non-primary metadata connections (they hold metadata locks)."""
        now = time.time()
        with self._lock:
            for pool in self._meta.values():
                for fbc in pool[1:]:
                    if now - fbc.last_used > max_idle and fbc.lock.acquire(blocking=False):
                        try:
                            pool.remove(fbc)
                            fbc.close()
                        finally:
                            fbc.lock.release()

    def attachment_stats(self, server_id: str, db_id: str, attachment_id: int) -> dict | None:
        """Cumulative I/O and record counters of one attachment (read from MON$ via the metadata connection)."""
        with self.meta(server_id, db_id) as conn:
            cur = conn.cursor()
            cur.execute(
                "SELECT io.MON$PAGE_READS, io.MON$PAGE_WRITES, io.MON$PAGE_FETCHES, io.MON$PAGE_MARKS, "
                "r.MON$RECORD_SEQ_READS, r.MON$RECORD_IDX_READS, r.MON$RECORD_INSERTS, r.MON$RECORD_UPDATES, "
                "r.MON$RECORD_DELETES, r.MON$RECORD_BACKOUTS, r.MON$RECORD_PURGES, r.MON$RECORD_EXPUNGES "
                "FROM MON$ATTACHMENTS a JOIN MON$IO_STATS io ON io.MON$STAT_ID = a.MON$STAT_ID "
                "JOIN MON$RECORD_STATS r ON r.MON$STAT_ID = a.MON$STAT_ID WHERE a.MON$ATTACHMENT_ID = ?",
                [attachment_id],
            )
            row = cur.fetchone()
        if not row:
            return None
        keys = ("reads", "writes", "fetches", "marks", "seq_reads", "idx_reads", "inserts", "updates",
                "deletes", "backouts", "purges", "expunges")
        return dict(zip(keys, row, strict=True))

    def cancel(self, sid: str) -> int:
        """Cancel whatever the session is running, from a different attachment."""
        sess = self.session(sid)
        with self.meta(sess.server_id, sess.db_id) as conn:
            cur = conn.cursor()
            cur.execute(
                "DELETE FROM MON$STATEMENTS WHERE MON$ATTACHMENT_ID = ? AND MON$STATE <> 0", [sess.attachment_id]
            )
            return cur.rowcount

    def close_all(self) -> None:
        with self._lock:
            for pool in self._meta.values():
                for fbc in pool:
                    fbc.close()
            for v in self._sessions.values():
                _close_quietly(v.conn)
            self._meta.clear()
            self._sessions.clear()


def _close_quietly(conn) -> None:
    try:
        conn.close()
    except Exception:  # noqa: BLE001
        pass


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
