"""Execution of SQL scripts for the Query Tool."""

from __future__ import annotations

import re
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from firebirdsql.consts import isc_info_sql_explain_plan, isc_info_sql_get_plan
from firebirdsql.fbcore import Statement
from firebirdsql.utils import bytes_to_int

from .serialize import to_json, type_name
from .sqlsplit import bind_params, split_sql, strip_comments

# isql-only commands that are ignored so scripts written for isql run unchanged.
_IGNORED = re.compile(
    r"^(SET\s+(AUTODDL|SQL\s+DIALECT|NAMES|STATS|PLAN|PLANONLY|ECHO|LIST|COUNT|BAIL|HEADING|WIDTH|BLOB|"
    r"TIME|WARNINGS|WNG|ROWCOUNT|EXPLAIN|PER_TABLE_STATS|MAXROWS|KEEP_TRAN_PARAMS|LOCAL_TIMEOUT)\b|"
    r"CONNECT\b|CREATE\s+DATABASE\b|SHOW\b|INPUT\b|OUTPUT\b|QUIT\b|EXIT\b)",
    re.IGNORECASE,
)
_COMMIT = re.compile(r"^COMMIT(\s+WORK)?\s*$", re.IGNORECASE)
_ROLLBACK = re.compile(r"^ROLLBACK(\s+WORK)?\s*$", re.IGNORECASE)
_ERR_POS = re.compile(r"line (-?\d+), column (-?\d+)", re.IGNORECASE)

ISOLATIONS = {
    # name: (tpb bytes, minimum engine major version)
    "read_committed": (bytes([15, 17]), 3),  # isc_tpb_read_committed + rec_version
    "read_committed_no_rec_version": (bytes([15, 18]), 3),
    "read_consistency": (bytes([15, 22]), 4),  # isc_tpb_read_consistency (Firebird 4+)
    "snapshot": (bytes([2]), 3),  # isc_tpb_concurrency
    "snapshot_table_stability": (bytes([1]), 3),  # isc_tpb_consistency
}


@dataclass
class TxOptions:
    isolation: str = "read_committed"
    read_only: bool = False
    wait: bool = True
    lock_timeout: int | None = None  # seconds; only with wait

    def tpb(self) -> bytes:
        if self.isolation not in ISOLATIONS:
            raise ValueError(f"Unknown isolation {self.isolation!r}")
        b = bytes([3, 8 if self.read_only else 9, 6 if self.wait else 7]) + ISOLATIONS[self.isolation][0]
        if self.wait and self.lock_timeout:
            b += bytes([21, 4]) + int(self.lock_timeout).to_bytes(4, "little")
        return b


def apply_tpb(conn, tpb: bytes) -> None:
    """Use ``tpb`` for every transaction this connection starts from now on."""
    conn.cursor()  # makes sure conn._transaction exists
    conn._transaction.transaction_parameter_block = (tpb,) * 5


def end_transaction(conn, commit: bool) -> None:
    """Commit/rollback even if the driver thinks nothing happened (refreshes snapshots)."""
    t = getattr(conn, "_transaction", None)
    if t is None or t._trans_handle is None:
        return
    t.is_dirty = True
    t.commit() if commit else t.rollback()


@dataclass
class QuerySession:
    """State of one Query Tool tab: a dedicated attachment and its transaction."""

    conn: Any
    server_id: str
    db_id: str
    attachment_id: int | None = None
    engine_major: int = 3
    lock: threading.RLock = field(default_factory=threading.RLock)
    last_used: float = field(default_factory=time.time)
    dirty: bool = False  # transaction has uncommitted work
    tx: TxOptions = field(default_factory=TxOptions)
    statement_timeout: int = 0  # milliseconds, Firebird 4+
    force_read_only: bool = False  # server registered as read-only
    # result set left open so the client can fetch more rows
    cursor: Any = None
    cursor_columns: int = 0
    cursor_commit: bool = False  # commit once the cursor is closed (auto-commit mode)
    cursor_pending: list = field(default_factory=list)  # rows read ahead but not yet sent

    def close_cursor(self) -> None:
        if self.cursor is not None:
            try:
                self.cursor.close()
            except Exception:  # noqa: BLE001
                pass
            self.cursor = None
            self.cursor_pending = []
            if self.cursor_commit:
                self.conn.commit()
                self.dirty = False
            self.cursor_commit = False

    def set_options(self, tx: TxOptions, statement_timeout: int | None) -> None:
        if self.dirty:
            raise ValueError("Commit or roll back the open transaction before changing transaction options")
        if self.force_read_only and not tx.read_only:
            raise ValueError("This server is registered as read-only: transactions must be read-only")
        if ISOLATIONS[tx.isolation][1] > self.engine_major:
            raise ValueError(f"{tx.isolation} needs Firebird {ISOLATIONS[tx.isolation][1]} or newer")
        self.close_cursor()
        end_transaction(self.conn, commit=False)
        apply_tpb(self.conn, tx.tpb())
        self.tx = tx
        if statement_timeout is not None and statement_timeout != self.statement_timeout:
            if self.engine_major < 4:
                raise ValueError("Statement timeout needs Firebird 4 or newer")
            cur = self.conn.cursor()
            cur.execute(f"SET STATEMENT TIMEOUT {int(statement_timeout)} MILLISECOND")
            end_transaction(self.conn, commit=True)
            self.statement_timeout = statement_timeout

    def options(self) -> dict:
        return {
            "isolation": self.tx.isolation,
            "read_only": self.tx.read_only,
            "wait": self.tx.wait,
            "lock_timeout": self.tx.lock_timeout,
            "statement_timeout": self.statement_timeout,
            "engine_major": self.engine_major,
            "attachment_id": self.attachment_id,
        }


def fetch_more(sess: QuerySession, n: int) -> dict[str, Any]:
    """Next ``n`` rows of the result set left open by ``run_script(keep_cursor=True)``."""
    if sess.cursor is None:
        raise ValueError("No open result set - run the query again")
    rows = list(sess.cursor_pending)
    sess.cursor_pending = []
    need = n + 1 - len(rows)
    if need > 0:
        rows += [[to_json(v) for v in r] for r in (sess.cursor.fetchmany(need) or [])]
    has_more = len(rows) > n
    if has_more:
        sess.cursor_pending = rows[n:]
        rows = rows[:n]
    else:
        sess.close_cursor()
    return {
        "rows": rows,
        "has_more": has_more,
        "transaction_open": sess.dirty and not (sess.cursor is not None and sess.cursor_commit),
    }


def error_message(exc: Exception) -> str:
    msg = str(exc).strip()
    return msg or exc.__class__.__name__


def _prepare_info(conn, sql: str, item: int) -> str | None:
    """Prepare ``sql`` asking for a plan info item; return its text (None if unavailable)."""
    trans = conn.cursor().transaction
    trans.check_trans_handle()
    stmt = Statement(trans)
    old = conn.__dict__.get("buffer_length")
    conn.buffer_length = 65535  # detailed plans of big queries exceed the default 1 KB
    try:
        conn._op_prepare_statement(stmt.handle, trans.trans_handle, sql, option_items=bytes([item]))
        while conn.lazy_response_count:
            conn.lazy_response_count -= 1
            (h, _oid, _buf) = conn._op_response()
            stmt.handle = h
        (h, _oid, buf) = conn._op_response()
    finally:
        if old is None:
            del conn.buffer_length
        else:
            conn.buffer_length = old
        try:
            stmt._is_open = False
            stmt.drop()
        except Exception:  # noqa: BLE001
            pass
    if not buf or buf[0] != item:
        return None  # e.g. isc_info_truncated
    ln = bytes_to_int(buf[1:3])
    return conn.bytes_to_str(buf[3 : 3 + ln]).strip() or None


def explain(conn, sql: str, detailed: bool = False) -> str | None:
    if detailed:
        return _prepare_info(conn, sql, isc_info_sql_explain_plan)
    return _prepare_info(conn, sql, isc_info_sql_get_plan)


def error_position(msg: str, stmt) -> dict | None:
    """Map Firebird's "line X, column Y" (relative to the statement) to script coordinates."""
    m = _ERR_POS.search(msg)
    if not m:
        return None
    line, col = int(m.group(1)), int(m.group(2))
    if line < 1:
        return None
    return {"line": stmt.line + line - 1, "column": (stmt.col + col - 1) if line == 1 else col}


def _delta(before: dict | None, after: dict | None) -> dict | None:
    if not before or not after:
        return None
    return {k: (after[k] or 0) - (before[k] or 0) for k in after}


def run_script(
    conn,
    script: str,
    *,
    autocommit: bool,
    max_rows: int,
    with_plan: bool = False,
    state: dict | QuerySession | None = None,
    params: dict[str, Any] | None = None,
    stats: Callable[[], dict | None] | None = None,
    keep_cursor: bool = False,
) -> dict[str, Any]:
    """Execute every statement of ``script`` on ``conn``.

    Stops at the first error. In autocommit mode each statement is committed on
    success; otherwise the transaction stays open until commit/rollback.

    * ``params``: values for ``:name`` / ``?`` markers (``$1``, ``$2``... for ``?``)
    * ``stats``: callable returning cumulative attachment counters; per-statement deltas are reported
    * ``keep_cursor``: when the last statement is a SELECT with more rows than ``max_rows``,
      leave its cursor open in the session so the client can fetch more.
    """
    sess = state if isinstance(state, QuerySession) else None
    st_dict = state if isinstance(state, dict) else {}
    params = params or {}

    def set_dirty(v: bool) -> None:
        if sess:
            sess.dirty = v
        else:
            st_dict["dirty"] = v

    if sess:
        sess.close_cursor()
    statements = split_sql(script)
    counter = [0]
    results: list[dict[str, Any]] = []
    error = None
    cursor_open = False
    t_start = time.perf_counter()
    for idx, stmt in enumerate(statements):
        sql = stmt.sql
        head = strip_comments(sql)
        t0 = time.perf_counter()
        item: dict[str, Any] = {"index": idx, "line": stmt.line, "col": stmt.col, "sql": sql}
        keep_this = False
        try:
            if _IGNORED.match(head):
                item.update(kind="ignored", message="isql command ignored")
            elif _COMMIT.match(head):
                end_transaction(conn, commit=True)
                set_dirty(False)
                item.update(kind="command", message="Transaction committed")
            elif _ROLLBACK.match(head):
                end_transaction(conn, commit=False)
                set_dirty(False)
                item.update(kind="command", message="Transaction rolled back")
            else:
                bound, names = bind_params(sql, counter)
                missing = [n for n in names if n not in params]
                if missing:
                    raise ValueError("No value for parameter(s): " + ", ".join(":" + m for m in dict.fromkeys(missing)))
                values = [params[n] for n in names]
                if with_plan:
                    try:
                        item["plan"] = explain(conn, bound)
                    except Exception:  # noqa: BLE001
                        item["plan"] = None
                before = stats() if stats else None
                cur = conn.cursor()
                cur.execute(bound, values)
                set_dirty(True)
                if cur.description:
                    cols = [
                        {"name": d[0], "type": type_name(d[1], d[5]), "nullable": d[6]} for d in cur.description
                    ]
                    rows = cur.fetchmany(max_rows + 1) or []
                    truncated = len(rows) > max_rows
                    extra = rows[max_rows:]
                    rows = rows[:max_rows]
                    item.update(
                        kind="select",
                        columns=cols,
                        rows=[[to_json(v) for v in r] for r in rows],
                        row_count=len(rows),
                        truncated=truncated,
                        message=f"{len(rows)}{'+' if truncated else ''} row(s) fetched",
                    )
                    if truncated and keep_cursor and sess and idx == len(statements) - 1:
                        # leave open for "fetch more"; the row we peeked is re-sent first
                        keep_this = True
                        sess.cursor = cur
                        sess.cursor_columns = len(cols)
                        sess.cursor_commit = autocommit
                        sess.cursor_pending = [[to_json(v) for v in r] for r in extra]
                    else:
                        try:
                            cur.close()
                        except Exception:  # noqa: BLE001
                            pass
                else:
                    try:
                        rc = cur.rowcount
                    except Exception:  # noqa: BLE001
                        rc = -1
                    item.update(kind="command", row_count=rc,
                                message=f"{rc} row(s) affected" if rc is not None and rc >= 0 else "OK")
                if stats:
                    item["stats"] = _delta(before, stats())
                if autocommit and not keep_this:
                    conn.commit()
                    set_dirty(False)
                cursor_open = cursor_open or keep_this
        except Exception as exc:  # noqa: BLE001 - report any driver error to the user
            if autocommit:
                try:
                    conn.rollback()
                    set_dirty(False)
                except Exception:  # noqa: BLE001
                    pass
            msg = error_message(exc)
            error = {"message": msg, "index": idx, "line": stmt.line, "sql": sql, "position": error_position(msg, stmt)}
            item.update(kind="error", message=msg)
        item["elapsed_ms"] = round((time.perf_counter() - t0) * 1000, 2)
        results.append(item)
        if error:
            break
    dirty = sess.dirty if sess else bool(st_dict.get("dirty"))
    return {
        "results": results,
        "error": error,
        "elapsed_ms": round((time.perf_counter() - t_start) * 1000, 2),
        "transaction_open": dirty and not (cursor_open and autocommit),
        "cursor_open": cursor_open,
    }
