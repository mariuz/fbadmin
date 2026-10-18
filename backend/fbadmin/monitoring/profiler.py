"""Firebird 5 profiler (``RDB$PROFILER`` package, ``PLG$PROF_*`` tables of the Default_Profiler plugin).

Two ways to profile:

* :func:`profile_script` runs SQL in a fresh attachment between ``START_SESSION`` and
  ``FINISH_SESSION(TRUE)``.
* :func:`start_remote` / :func:`finish_remote` profile another attachment (for example a Query Tool
  tab) by passing its attachment id to the package routines.

Times stored by the plugin are nanoseconds.
"""

from __future__ import annotations

import time
from typing import Any

from ..metadata import Catalog
from ..query import end_transaction
from ..serialize import to_json
from ..sqlsplit import split_sql, strip_comments


# Per-request timings (PLG$PROF_REQUESTS) are only recorded with this plugin option.
OPTIONS = "DETAILED_REQUESTS"


def available(conn) -> bool:
    r = Catalog(conn).one("SELECT COUNT(*) AS n FROM RDB$PACKAGES WHERE RDB$PACKAGE_NAME = 'RDB$PROFILER'")
    return bool(r and r["n"])


def require(conn) -> None:
    if not available(conn):
        raise ValueError("The profiler needs Firebird 5.0 or newer (RDB$PROFILER package not found).")


def _tables_exist(conn) -> bool:
    r = Catalog(conn).one("SELECT COUNT(*) AS n FROM RDB$RELATIONS WHERE RDB$RELATION_NAME = 'PLG$PROF_SESSIONS'", scoped=False)
    return bool(r and r["n"])


def profile_script(conn, sql: str, description: str | None = None, max_rows: int = 100) -> dict[str, Any]:
    """Run ``sql`` (one or more statements) under the profiler in ``conn`` and flush the results.

    ``conn`` should be a dedicated attachment; everything runs in one transaction that is committed
    at the end (so DML is kept, as in the Query Tool with auto-commit).
    """
    require(conn)
    cur = conn.cursor()
    cur.execute(f"SELECT RDB$PROFILER.START_SESSION(?, NULL, NULL, NULL, '{OPTIONS}') FROM RDB$DATABASE",
                [description or "fbadmin"])
    profile_id = cur.fetchone()[0]
    results = []
    error = None
    t0 = time.perf_counter()
    try:
        for stmt in split_sql(sql):
            text = stmt.sql
            if not strip_comments(text).strip():
                continue
            c = conn.cursor()
            s0 = time.perf_counter()
            c.execute(text)
            rows = []
            if c.description:
                rows = c.fetchmany(max_rows) or []
                # drain the cursor so the whole statement is profiled
                while c.fetchmany(1000):
                    pass
            c.close()  # the request is only timed when it ends
            results.append({"sql": text, "elapsed_ms": round((time.perf_counter() - s0) * 1000, 2),
                            "columns": [d[0] for d in c.description] if c.description else [],
                            "rows": to_json([list(r) for r in rows]), "rowcount": c.rowcount})
    except Exception as exc:  # noqa: BLE001 - reported to the user, the session is still flushed
        error = str(exc).strip()
    finally:
        try:
            cur = conn.cursor()
            cur.execute("EXECUTE PROCEDURE RDB$PROFILER.FINISH_SESSION(TRUE)")
        finally:
            end_transaction(conn, commit=error is None)
            if error is not None:
                # the profile data was flushed in the rolled back transaction: flush again in a new one
                cur = conn.cursor()
                cur.execute("EXECUTE PROCEDURE RDB$PROFILER.FLUSH")
                end_transaction(conn, commit=True)
    return {"profile_id": profile_id, "results": results, "error": error,
            "elapsed_ms": round((time.perf_counter() - t0) * 1000, 2)}


def start_remote(conn, attachment_id: int, description: str | None = None) -> int:
    require(conn)
    cur = conn.cursor()
    cur.execute(f"SELECT RDB$PROFILER.START_SESSION(?, NULL, ?, NULL, '{OPTIONS}') FROM RDB$DATABASE",
                [description or f"attachment {attachment_id}", attachment_id])
    pid = cur.fetchone()[0]
    end_transaction(conn, commit=True)
    return pid


def finish_remote(conn, attachment_id: int) -> None:
    require(conn)
    cur = conn.cursor()
    cur.execute("EXECUTE PROCEDURE RDB$PROFILER.FINISH_SESSION(TRUE, ?)", [attachment_id])
    end_transaction(conn, commit=True)


def sessions(conn) -> list[dict[str, Any]]:
    if not available(conn) or not _tables_exist(conn):
        return []
    return Catalog(conn).rows(
        "SELECT s.PROFILE_ID AS profile_id, s.ATTACHMENT_ID AS attachment_id, TRIM(s.USER_NAME) AS username, "
        "s.DESCRIPTION AS description, s.START_TIMESTAMP AS started_at, s.FINISH_TIMESTAMP AS finished_at, "
        "(SELECT SUM(r.TOTAL_ELAPSED_TIME) FROM PLG$PROF_REQUESTS r WHERE r.PROFILE_ID = s.PROFILE_ID "
        "AND r.CALLER_STATEMENT_ID IS NULL) AS total_ns "
        "FROM PLG$PROF_SESSIONS s ORDER BY s.PROFILE_ID DESC"
    )


def delete_session(conn, profile_id: int | None = None) -> None:
    """Delete one profile (or all with ``None``) via ``RDB$PROFILER.PURGE`` semantics."""
    if profile_id is None:
        Catalog(conn).rows("DELETE FROM PLG$PROF_SESSIONS")
    else:
        Catalog(conn).rows("DELETE FROM PLG$PROF_SESSIONS WHERE PROFILE_ID = ?", [profile_id])


def report(conn, profile_id: int) -> dict[str, Any]:
    """Everything the UI needs for one profile: statements tree, PSQL line stats, record sources."""
    cat = Catalog(conn)
    sess = cat.one(
        "SELECT PROFILE_ID AS profile_id, ATTACHMENT_ID AS attachment_id, TRIM(USER_NAME) AS username, "
        "DESCRIPTION AS description, START_TIMESTAMP AS started_at, FINISH_TIMESTAMP AS finished_at "
        "FROM PLG$PROF_SESSIONS WHERE PROFILE_ID = ?", [profile_id])
    if not sess:
        raise LookupError(f"Profile {profile_id} not found")
    statements = cat.rows(
        "SELECT st.STATEMENT_ID AS statement_id, st.PARENT_STATEMENT_ID AS parent_id, "
        "TRIM(st.STATEMENT_TYPE) AS statement_type, TRIM(st.PACKAGE_NAME) AS package_name, "
        "TRIM(st.ROUTINE_NAME) AS routine_name, st.SQL_TEXT AS sql_text "
        "FROM PLG$PROF_STATEMENTS st WHERE st.PROFILE_ID = ? ORDER BY st.STATEMENT_ID", [profile_id])
    agg = cat.rows(
        "SELECT v.STATEMENT_ID AS statement_id, v.COUNTER AS calls, v.TOTAL_ELAPSED_TIME AS total_ns, "
        "v.MIN_ELAPSED_TIME AS min_ns, v.MAX_ELAPSED_TIME AS max_ns, v.AVG_ELAPSED_TIME AS avg_ns "
        "FROM PLG$PROF_STATEMENT_STATS_VIEW v WHERE v.PROFILE_ID = ?", [profile_id])
    agg_by = {r["statement_id"]: r for r in agg}
    for s in statements:
        r = agg_by.get(s["statement_id"], {})
        for k in ("calls", "total_ns", "min_ns", "max_ns", "avg_ns"):
            s[k] = r.get(k)
        s["children"] = []
    by_id = {s["statement_id"]: s for s in statements}
    roots = []
    for s in statements:
        p = by_id.get(s["parent_id"])
        (p["children"] if p else roots).append(s["statement_id"])
    # Caller -> callee edges (a procedure called from a block is not its child statement).
    edges = cat.rows(
        "SELECT DISTINCT r.CALLER_STATEMENT_ID AS caller, r.STATEMENT_ID AS callee FROM PLG$PROF_REQUESTS r "
        "WHERE r.PROFILE_ID = ? AND r.CALLER_STATEMENT_ID IS NOT NULL", [profile_id])
    callees: dict[int, list[int]] = {}
    for e in edges:
        if e["caller"] != e["callee"]:
            callees.setdefault(e["caller"], []).append(e["callee"])
    called = {e["callee"] for e in edges}
    for s in statements:
        s["calls_to"] = sorted(set(callees.get(s["statement_id"], [])) - set(s["children"]))
    # hide the profiler's own START_SESSION call
    roots = [r for r in roots if r not in called
             and "RDB$PROFILER.START_SESSION" not in (by_id[r]["sql_text"] or "").upper()]

    lines = cat.rows(
        "SELECT p.STATEMENT_ID AS statement_id, TRIM(p.STATEMENT_TYPE) AS statement_type, "
        "TRIM(p.PACKAGE_NAME) AS package_name, TRIM(p.ROUTINE_NAME) AS routine_name, "
        "p.LINE_NUM AS line_num, p.COLUMN_NUM AS column_num, p.COUNTER AS counter, "
        "p.MIN_ELAPSED_TIME AS min_ns, p.MAX_ELAPSED_TIME AS max_ns, p.TOTAL_ELAPSED_TIME AS total_ns, "
        "p.AVG_ELAPSED_TIME AS avg_ns FROM PLG$PROF_PSQL_STATS_VIEW p WHERE p.PROFILE_ID = ? "
        "ORDER BY p.TOTAL_ELAPSED_TIME DESC", [profile_id])
    sources = cat.rows(
        "SELECT v.STATEMENT_ID AS statement_id, v.CURSOR_ID AS cursor_id, TRIM(v.CURSOR_NAME) AS cursor_name, "
        "v.CURSOR_LINE_NUM AS cursor_line, v.CURSOR_COLUMN_NUM AS cursor_column, "
        "v.RECORD_SOURCE_ID AS record_source_id, v.PARENT_RECORD_SOURCE_ID AS parent_record_source_id, "
        "v.\"LEVEL\" AS depth, v.ACCESS_PATH AS access_path, v.OPEN_COUNTER AS open_counter, "
        "v.OPEN_TOTAL_ELAPSED_TIME AS open_total_ns, v.FETCH_COUNTER AS fetch_counter, "
        "v.FETCH_TOTAL_ELAPSED_TIME AS fetch_total_ns, v.OPEN_FETCH_TOTAL_ELAPSED_TIME AS total_ns "
        "FROM PLG$PROF_RECORD_SOURCE_STATS_VIEW v WHERE v.PROFILE_ID = ? "
        "ORDER BY v.STATEMENT_ID, v.CURSOR_ID, v.RECORD_SOURCE_ID", [profile_id])
    routines = {}
    for ln in lines:
        key = (ln["statement_type"], ln["package_name"], ln["routine_name"])
        if ln["routine_name"] and key not in routines:
            routines[key] = routine_source(cat, *key)
    src = []
    for (stype, pkg, name), text in routines.items():
        rl = [ln for ln in lines if (ln["statement_type"], ln["package_name"], ln["routine_name"]) == (stype, pkg, name)]
        src.append({"statement_type": stype, "package_name": pkg, "routine_name": name, "source": text,
                    "line_offset": guess_line_offset(text, [(ln["line_num"], ln["column_num"]) for ln in rl])})
    total = sum((by_id[r]["total_ns"] or 0) for r in roots)
    return to_json({"session": sess, "statements": statements, "roots": roots, "lines": lines,
                    "record_sources": sources, "routines": src, "total_ns": total})


def routine_source(cat: Catalog, stype: str | None, package: str | None, name: str) -> str | None:
    if package:
        r = cat.one("SELECT RDB$PACKAGE_BODY_SOURCE AS src FROM RDB$PACKAGES WHERE RDB$PACKAGE_NAME = ?", [package])
    elif stype == "PROCEDURE":
        r = cat.one("SELECT RDB$PROCEDURE_SOURCE AS src FROM RDB$PROCEDURES WHERE RDB$PROCEDURE_NAME = ? "
                    "AND RDB$PACKAGE_NAME IS NULL", [name])
    elif stype == "FUNCTION":
        r = cat.one("SELECT RDB$FUNCTION_SOURCE AS src FROM RDB$FUNCTIONS WHERE RDB$FUNCTION_NAME = ? "
                    "AND RDB$PACKAGE_NAME IS NULL", [name])
    elif stype == "TRIGGER":
        r = cat.one("SELECT RDB$TRIGGER_SOURCE AS src FROM RDB$TRIGGERS WHERE RDB$TRIGGER_NAME = ?", [name])
    else:
        return None
    return (r or {}).get("src")


def guess_line_offset(source: str | None, positions: list[tuple[int, int]]) -> int:
    """Line numbers of PSQL stats are relative to the original CREATE statement, while RDB$xxx_SOURCE
    starts after ``AS``. Find the offset that puts most profiled (line, column) pairs at the start of
    a token in the stored source."""
    if not source or not positions:
        return 0
    src_lines = source.split("\n")
    best, best_score = 0, -1
    for off in range(0, 60):
        score = 0
        for line, col in positions:
            i = (line or 0) - 1 - off
            if 0 <= i < len(src_lines) and col and col >= 1:
                text = src_lines[i]
                j = col - 1
                if j < len(text) and not text[j].isspace() and (j == 0 or not (text[j - 1].isalnum() or text[j - 1] == "_")):
                    score += 1
        if score > best_score:
            best, best_score = off, score
    return best
