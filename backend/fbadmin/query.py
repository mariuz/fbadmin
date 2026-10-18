"""Execution of SQL scripts for the Query Tool."""

from __future__ import annotations

import re
import time
from typing import Any

from .serialize import to_json, type_name
from .sqlsplit import split_sql, strip_comments

# isql-only commands that are ignored so scripts written for isql run unchanged.
_IGNORED = re.compile(
    r"^(SET\s+(AUTODDL|SQL\s+DIALECT|NAMES|STATS|PLAN|PLANONLY|ECHO|LIST|COUNT|BAIL|HEADING|WIDTH|BLOB|"
    r"TIME|WARNINGS|WNG|ROWCOUNT|EXPLAIN|PER_TABLE_STATS|MAXROWS|KEEP_TRAN_PARAMS|LOCAL_TIMEOUT)\b|"
    r"CONNECT\b|CREATE\s+DATABASE\b|SHOW\b|INPUT\b|OUTPUT\b|QUIT\b|EXIT\b)",
    re.IGNORECASE,
)
_COMMIT = re.compile(r"^COMMIT(\s+WORK)?\s*$", re.IGNORECASE)
_ROLLBACK = re.compile(r"^ROLLBACK(\s+WORK)?\s*$", re.IGNORECASE)


def error_message(exc: Exception) -> str:
    msg = str(exc).strip()
    # firebirdsql puts "SQLCODE ... \n message" - keep it readable
    return msg or exc.__class__.__name__


def explain(conn, sql: str) -> str | None:
    cur = conn.cursor()
    prepared = cur.prep(sql, explain_plan=True)
    try:
        return (prepared.stmt.plan or "").strip() or None
    finally:
        # The statement was only prepared, never opened: free it with DSQL_drop.
        # Closing it would make the server answer "attempt to reclose a closed cursor".
        prepared.stmt._is_open = False
        prepared.stmt.drop()


def run_script(
    conn,
    script: str,
    *,
    autocommit: bool,
    max_rows: int,
    with_plan: bool = False,
    state: dict | None = None,
) -> dict[str, Any]:
    """Execute every statement of ``script`` on ``conn``.

    Stops at the first error. In autocommit mode each statement is committed
    on success; otherwise the transaction stays open until commit/rollback.
    ``state['dirty']`` tracks whether the open transaction has work.
    """
    state = state if state is not None else {}
    results: list[dict[str, Any]] = []
    error = None
    t_start = time.perf_counter()
    for idx, stmt in enumerate(split_sql(script)):
        sql = stmt.sql
        head = strip_comments(sql)
        t0 = time.perf_counter()
        item: dict[str, Any] = {"index": idx, "line": stmt.line, "sql": sql}
        try:
            if _IGNORED.match(head):
                item.update(kind="ignored", message="isql command ignored")
            elif _COMMIT.match(head):
                conn.commit()
                state["dirty"] = False
                item.update(kind="command", message="Transaction committed")
            elif _ROLLBACK.match(head):
                conn.rollback()
                state["dirty"] = False
                item.update(kind="command", message="Transaction rolled back")
            else:
                if with_plan:
                    try:
                        item["plan"] = explain(conn, sql)
                    except Exception:
                        item["plan"] = None
                cur = conn.cursor()
                cur.execute(sql)
                state["dirty"] = True
                if cur.description:
                    cols = [
                        {"name": d[0], "type": type_name(d[1], d[5]), "nullable": d[6]} for d in cur.description
                    ]
                    rows = cur.fetchmany(max_rows + 1) or []
                    truncated = len(rows) > max_rows
                    rows = rows[:max_rows]
                    item.update(
                        kind="select",
                        columns=cols,
                        rows=[[to_json(v) for v in r] for r in rows],
                        row_count=len(rows),
                        truncated=truncated,
                        message=f"{len(rows)}{'+' if truncated else ''} row(s) fetched",
                    )
                    try:
                        cur.close()
                    except Exception:
                        pass
                else:
                    try:
                        rc = cur.rowcount
                    except Exception:
                        rc = -1
                    item.update(kind="command", row_count=rc,
                                message=f"{rc} row(s) affected" if rc is not None and rc >= 0 else "OK")
                if autocommit:
                    conn.commit()
                    state["dirty"] = False
        except Exception as exc:  # noqa: BLE001 - report any driver error to the user
            if autocommit:
                try:
                    conn.rollback()
                    state["dirty"] = False
                except Exception:
                    pass
            error = {"message": error_message(exc), "index": idx, "line": stmt.line, "sql": sql}
            item.update(kind="error", message=error["message"])
        item["elapsed_ms"] = round((time.perf_counter() - t0) * 1000, 2)
        results.append(item)
        if error:
            break
    return {
        "results": results,
        "error": error,
        "elapsed_ms": round((time.perf_counter() - t_start) * 1000, 2),
        "transaction_open": bool(state.get("dirty")),
    }
