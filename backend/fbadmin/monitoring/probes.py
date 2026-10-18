"""Read-only MON$ queries used by the dashboard, the history sampler and the metrics exporter.

Every function takes an open ``firebirdsql`` connection. The caller is responsible for ending the
transaction afterwards (MON$ tables are a snapshot taken at the first MON$ access of a transaction,
so a fresh transaction is needed for fresh numbers).

Features are detected at runtime (by looking at the system tables), not by version number:
Firebird 3.0.x already has MON$TABLE_STATS, for example, while MON$COMPILED_STATEMENTS is new in 5.0.
"""

from __future__ import annotations

import time
from typing import Any

from ..metadata import Catalog

# Cumulative counters (since the database was opened by the first attachment).
COUNTERS = (
    "next_transaction",
    "page_reads",
    "page_writes",
    "page_fetches",
    "page_marks",
    "seq_reads",
    "idx_reads",
    "inserts",
    "updates",
    "deletes",
    "backouts",
    "purges",
    "expunges",
    "record_waits",
    "record_conflicts",
)
# Point-in-time values.
GAUGES = (
    "attachments",
    "transactions",
    "active_statements",
    "stalled_statements",
    "oit",
    "oat",
    "ost",
    "memory_used",
    "memory_allocated",
    "max_memory_used",
)

ATT_STATE = {0: "idle", 1: "active", 2: "stalled"}
ISOLATION = {
    0: "consistency",
    1: "concurrency",
    2: "read committed record version",
    3: "read committed no record version",
    4: "read committed read consistency",
}


def features(conn) -> dict[str, Any]:
    """Which monitoring features this server/database offers."""
    cat = Catalog(conn)
    rels = {
        r["n"]
        for r in cat.rows(
            "SELECT TRIM(RDB$RELATION_NAME) AS n FROM RDB$RELATIONS WHERE RDB$RELATION_NAME IN "
            "('MON$TABLE_STATS', 'MON$COMPILED_STATEMENTS', 'RDB$PUBLICATIONS', 'RDB$CONFIG')"
        )
    }
    prof = cat.one("SELECT COUNT(*) AS n FROM RDB$PACKAGES WHERE RDB$PACKAGE_NAME = 'RDB$PROFILER'")
    return {
        "engine_version": cat.engine_version,
        "engine_major": cat.engine_major,
        "table_stats": "MON$TABLE_STATS" in rels,
        "compiled_statements": "MON$COMPILED_STATEMENTS" in rels,
        "profiler": bool(prof and prof["n"]),
        "replication": "RDB$PUBLICATIONS" in rels,
        "config": "RDB$CONFIG" in rels,
    }


def snapshot(conn) -> dict[str, Any]:
    """Database-wide counters and gauges at this moment (one row of the dashboard/history)."""
    cat = Catalog(conn)
    row = cat.one(
        "SELECT d.MON$NEXT_TRANSACTION AS next_transaction, d.MON$OLDEST_TRANSACTION AS oit, "
        "d.MON$OLDEST_ACTIVE AS oat, d.MON$OLDEST_SNAPSHOT AS ost, "
        "io.MON$PAGE_READS AS page_reads, io.MON$PAGE_WRITES AS page_writes, "
        "io.MON$PAGE_FETCHES AS page_fetches, io.MON$PAGE_MARKS AS page_marks, "
        "r.MON$RECORD_SEQ_READS AS seq_reads, r.MON$RECORD_IDX_READS AS idx_reads, "
        "r.MON$RECORD_INSERTS AS inserts, r.MON$RECORD_UPDATES AS updates, r.MON$RECORD_DELETES AS deletes, "
        "r.MON$RECORD_BACKOUTS AS backouts, r.MON$RECORD_PURGES AS purges, r.MON$RECORD_EXPUNGES AS expunges, "
        "r.MON$RECORD_WAITS AS record_waits, r.MON$RECORD_CONFLICTS AS record_conflicts, "
        "m.MON$MEMORY_USED AS memory_used, m.MON$MEMORY_ALLOCATED AS memory_allocated, "
        "m.MON$MAX_MEMORY_USED AS max_memory_used, "
        "(SELECT COUNT(*) FROM MON$ATTACHMENTS a WHERE COALESCE(a.MON$SYSTEM_FLAG, 0) = 0) AS attachments, "
        "(SELECT COUNT(*) FROM MON$TRANSACTIONS) AS transactions, "
        "(SELECT COUNT(*) FROM MON$STATEMENTS s WHERE s.MON$STATE = 1) AS active_statements, "
        "(SELECT COUNT(*) FROM MON$STATEMENTS s WHERE s.MON$STATE = 2) AS stalled_statements "
        "FROM MON$DATABASE d "
        "JOIN MON$IO_STATS io ON io.MON$STAT_ID = d.MON$STAT_ID "
        "JOIN MON$RECORD_STATS r ON r.MON$STAT_ID = d.MON$STAT_ID "
        "JOIN MON$MEMORY_USAGE m ON m.MON$STAT_ID = d.MON$STAT_ID"
    ) or {}
    out: dict[str, Any] = {"ts": time.time()}
    for k in COUNTERS + GAUGES:
        v = row.get(k)
        out[k] = int(v) if v is not None else None
    return out


def rates(prev: dict[str, Any] | None, cur: dict[str, Any]) -> dict[str, float | None]:
    """Per-second rates between two snapshots. ``None`` when a counter went backwards
    (the database was closed by its last attachment and reopened, which resets MON$ counters)."""
    if not prev:
        return {k: None for k in COUNTERS}
    dt = (cur["ts"] - prev["ts"]) or 1e-9
    out: dict[str, float | None] = {}
    for k in COUNTERS:
        a, b = prev.get(k), cur.get(k)
        out[k] = None if a is None or b is None or b < a else (b - a) / dt
    return out


# ------------------------------------------------------------------ table statistics
TABLE_METRICS = ("seq_reads", "idx_reads", "inserts", "updates", "deletes", "backouts", "purges", "expunges",
                 "waits", "conflicts", "locks", "backversion_reads", "fragment_reads")


def table_stats(conn, order_by: str = "reads", limit: int = 50, include_system: bool = False) -> list[dict]:
    """Per-table record counters for the whole database (MON$TABLE_STATS, stat group 0)."""
    cat = Catalog(conn)
    sql = (
        "SELECT TRIM(t.MON$TABLE_NAME) AS table_name, "
        "r.MON$RECORD_SEQ_READS AS seq_reads, r.MON$RECORD_IDX_READS AS idx_reads, "
        "r.MON$RECORD_INSERTS AS inserts, r.MON$RECORD_UPDATES AS updates, r.MON$RECORD_DELETES AS deletes, "
        "r.MON$RECORD_BACKOUTS AS backouts, r.MON$RECORD_PURGES AS purges, r.MON$RECORD_EXPUNGES AS expunges, "
        "r.MON$RECORD_WAITS AS waits, r.MON$RECORD_CONFLICTS AS conflicts, r.MON$RECORD_LOCKS AS locks, "
        "r.MON$BACKVERSION_READS AS backversion_reads, r.MON$FRAGMENT_READS AS fragment_reads "
        "FROM MON$TABLE_STATS t JOIN MON$RECORD_STATS r ON r.MON$STAT_ID = t.MON$RECORD_STAT_ID "
        "WHERE t.MON$STAT_GROUP = 0"
    )
    rows = cat.rows(sql)
    if not include_system:
        rows = [r for r in rows if not r["table_name"].startswith(("RDB$", "MON$", "SEC$", "PLG$"))]
    for r in rows:
        r["reads"] = (r["seq_reads"] or 0) + (r["idx_reads"] or 0)
        r["writes"] = (r["inserts"] or 0) + (r["updates"] or 0) + (r["deletes"] or 0)
    key = order_by if order_by in TABLE_METRICS + ("reads", "writes", "table_name") else "reads"
    rows.sort(key=lambda r: (r[key] or 0) if key != "table_name" else r[key], reverse=key != "table_name")
    return rows[:limit]


# ------------------------------------------------------------------ compiled statements (Firebird 5)
def compiled_statements(conn, limit: int = 200, search: str | None = None) -> list[dict]:
    cat = Catalog(conn)
    where = ""
    params: list = []
    if search:
        where = "WHERE (c.MON$SQL_TEXT CONTAINING ? OR c.MON$OBJECT_NAME CONTAINING ?)"
        params = [search, search]
    rows = cat.rows(
        "SELECT c.MON$COMPILED_STATEMENT_ID AS id, c.MON$SQL_TEXT AS sql_text, "
        "TRIM(c.MON$OBJECT_NAME) AS object_name, c.MON$OBJECT_TYPE AS object_type, "
        "TRIM(c.MON$PACKAGE_NAME) AS package_name, c.MON$EXPLAINED_PLAN AS explained_plan, "
        "(SELECT COUNT(*) FROM MON$STATEMENTS s WHERE s.MON$COMPILED_STATEMENT_ID = c.MON$COMPILED_STATEMENT_ID) "
        "AS statements, "
        "(SELECT COUNT(*) FROM MON$STATEMENTS s WHERE s.MON$COMPILED_STATEMENT_ID = c.MON$COMPILED_STATEMENT_ID "
        "AND s.MON$STATE = 1) AS active, "
        "io.MON$PAGE_READS AS page_reads, io.MON$PAGE_FETCHES AS page_fetches, "
        "r.MON$RECORD_SEQ_READS AS seq_reads, r.MON$RECORD_IDX_READS AS idx_reads, "
        "m.MON$MEMORY_USED AS memory_used "
        "FROM MON$COMPILED_STATEMENTS c "
        "LEFT JOIN MON$IO_STATS io ON io.MON$STAT_ID = c.MON$STAT_ID "
        "LEFT JOIN MON$RECORD_STATS r ON r.MON$STAT_ID = c.MON$STAT_ID "
        "LEFT JOIN MON$MEMORY_USAGE m ON m.MON$STAT_ID = c.MON$STAT_ID "
        f"{where} ORDER BY c.MON$COMPILED_STATEMENT_ID DESC ROWS {int(limit)}",
        params,
    )
    # MON$OBJECT_TYPE uses the RDB$OBJECT_TYPES numbering.
    otypes = {2: "trigger", 5: "procedure", 15: "function", 18: "package body"}
    for r in rows:
        r["object_type_name"] = otypes.get(r["object_type"], None if r["object_type"] is None else str(r["object_type"]))
    return rows


# ------------------------------------------------------------------ transactions / OAT
def transactions_report(conn, long_running: int = 300, gap_warn: int = 10000) -> dict[str, Any]:
    """Long-running transactions, OIT/OAT/OST gaps and who is holding the OAT."""
    cat = Catalog(conn)
    db = cat.one(
        "SELECT MON$NEXT_TRANSACTION AS next_transaction, MON$OLDEST_TRANSACTION AS oit, "
        "MON$OLDEST_ACTIVE AS oat, MON$OLDEST_SNAPSHOT AS ost, MON$SWEEP_INTERVAL AS sweep_interval "
        "FROM MON$DATABASE"
    ) or {}
    txs = cat.rows(
        "SELECT t.MON$TRANSACTION_ID AS id, t.MON$ATTACHMENT_ID AS attachment_id, t.MON$STATE AS state, "
        "t.MON$TIMESTAMP AS started_at, t.MON$ISOLATION_MODE AS isolation, t.MON$READ_ONLY AS read_only, "
        "DATEDIFF(MILLISECOND FROM t.MON$TIMESTAMP TO CURRENT_TIMESTAMP) / 1000.0 AS age_seconds, "
        "t.MON$LOCK_TIMEOUT AS lock_timeout, t.MON$OLDEST_ACTIVE AS tx_oldest_active, "
        "TRIM(a.MON$USER) AS username, TRIM(a.MON$REMOTE_ADDRESS) AS remote_address, "
        "TRIM(a.MON$REMOTE_PROCESS) AS remote_process, a.MON$REMOTE_PID AS remote_pid, "
        "a.MON$TIMESTAMP AS connected_at, a.MON$STATE AS attachment_state, "
        "IIF(a.MON$ATTACHMENT_ID = CURRENT_CONNECTION, 1, 0) AS is_self, "
        "COALESCE(a.MON$SYSTEM_FLAG, 0) AS is_system, "
        "r.MON$RECORD_INSERTS AS inserts, r.MON$RECORD_UPDATES AS updates, r.MON$RECORD_DELETES AS deletes "
        "FROM MON$TRANSACTIONS t "
        "JOIN MON$ATTACHMENTS a ON a.MON$ATTACHMENT_ID = t.MON$ATTACHMENT_ID "
        "LEFT JOIN MON$RECORD_STATS r ON r.MON$STAT_ID = t.MON$STAT_ID "
        "ORDER BY t.MON$TRANSACTION_ID"
    )
    stmts = cat.rows(
        "SELECT s.MON$STATEMENT_ID AS id, s.MON$ATTACHMENT_ID AS attachment_id, "
        "s.MON$TRANSACTION_ID AS transaction_id, s.MON$STATE AS state, s.MON$TIMESTAMP AS started_at, "
        "s.MON$SQL_TEXT AS sql_text FROM MON$STATEMENTS s WHERE s.MON$STATE <> 0 OR s.MON$TRANSACTION_ID IS NOT NULL"
    )
    by_tx: dict[int, list[dict]] = {}
    for s in stmts:
        if s["transaction_id"] is not None:
            by_tx.setdefault(s["transaction_id"], []).append(s)
    for t in txs:
        t["age_seconds"] = float(t["age_seconds"]) if t["age_seconds"] is not None else None
        t["isolation_name"] = ISOLATION.get(t["isolation"], str(t["isolation"]))
        t["statements"] = by_tx.get(t["id"], [])
        t["writes"] = (t["inserts"] or 0) + (t["updates"] or 0) + (t["deletes"] or 0)

    nxt, oit, oat, ost = (db.get(k) or 0 for k in ("next_transaction", "oit", "oat", "ost"))
    holders = [t for t in txs if t["id"] == oat]
    if not holders:
        # A read-only read committed transaction does not hold back the OAT on Firebird 4+;
        # otherwise the oldest live transaction is the one pinning it.
        pinning = [t for t in txs if not (t["read_only"] and t["isolation"] in (2, 3, 4)) and not t["is_system"]]
        holders = pinning[:1]
    # fbadmin's own and system (garbage collector, cache writer...) attachments are not reported
    long_txs = [t for t in txs if (t["age_seconds"] or 0) >= long_running and not t["is_self"] and not t["is_system"]]

    alerts: list[dict[str, Any]] = []
    gap_oat = nxt - oat
    gap_oit = oat - oit
    sweep = db.get("sweep_interval") or 0
    if gap_oat >= gap_warn:
        h = holders[0] if holders else None
        who = f" (held by transaction {h['id']} of {h['username']}@{h['remote_address'] or 'local'})" if h else ""
        alerts.append({"level": "critical" if gap_oat >= gap_warn * 10 else "warning",
                       "message": f"Next transaction - OAT gap is {gap_oat:,}{who}. Old record versions cannot be "
                                  "garbage collected while the OAT is stuck."})
    if gap_oit >= gap_warn:
        msg = f"OAT - OIT gap is {gap_oit:,}: a rolled-back or limbo transaction is stuck."
        if sweep and gap_oit > sweep:
            msg += f" It exceeds the sweep interval ({sweep:,}), so automatic sweep should run."
        elif not sweep:
            msg += " Automatic sweep is disabled; run a manual sweep."
        alerts.append({"level": "warning", "message": msg})
    for t in long_txs:
        level = "critical" if (t["age_seconds"] or 0) >= long_running * 12 else "warning"
        alerts.append({"level": level,
                       "message": f"Transaction {t['id']} ({t['username']}, "
                                  f"{(t['remote_process'] or 'unknown process').replace(chr(92), '/').split('/')[-1]}) "
                                  f"has been running for {_fmt_age(t['age_seconds'])}.",
                       "transaction_id": t["id"], "attachment_id": t["attachment_id"]})
    return {
        "database": db,
        "gaps": {"next_oat": gap_oat, "oat_oit": gap_oit, "next_oit": nxt - oit, "next_ost": nxt - ost},
        "oat_holders": holders,
        "long_running": long_txs,
        "transactions": txs,
        "alerts": alerts,
        "thresholds": {"long_running": long_running, "gap_warn": gap_warn},
    }


def _fmt_age(sec: float | None) -> str:
    if sec is None:
        return "?"
    sec = int(sec)
    if sec < 120:
        return f"{sec} s"
    if sec < 7200:
        return f"{sec // 60} min"
    return f"{sec // 3600} h {sec % 3600 // 60} min"


# ------------------------------------------------------------------ locks
def locks_report(conn) -> dict[str, Any]:
    """What is waiting for a record lock and which transactions are likely blocking it.

    Firebird has no remote equivalent of ``fb_lock_print``; this uses the record wait/conflict counters
    of MON$RECORD_STATS per statement and MON$TABLE_STATS per transaction to infer the waits.
    """
    cat = Catalog(conn)
    stmts = cat.rows(
        "SELECT s.MON$STATEMENT_ID AS id, s.MON$ATTACHMENT_ID AS attachment_id, "
        "s.MON$TRANSACTION_ID AS transaction_id, s.MON$STATE AS state, s.MON$TIMESTAMP AS started_at, "
        "s.MON$SQL_TEXT AS sql_text, s.MON$STAT_ID AS stat_id, "
        "DATEDIFF(MILLISECOND FROM s.MON$TIMESTAMP TO CURRENT_TIMESTAMP) / 1000.0 AS age_seconds, "
        "r.MON$RECORD_WAITS AS waits, r.MON$RECORD_CONFLICTS AS conflicts, r.MON$RECORD_LOCKS AS locks, "
        "TRIM(a.MON$USER) AS username, TRIM(a.MON$REMOTE_PROCESS) AS remote_process, "
        "t.MON$LOCK_TIMEOUT AS lock_timeout "
        "FROM MON$STATEMENTS s "
        "JOIN MON$ATTACHMENTS a ON a.MON$ATTACHMENT_ID = s.MON$ATTACHMENT_ID "
        "LEFT JOIN MON$TRANSACTIONS t ON t.MON$TRANSACTION_ID = s.MON$TRANSACTION_ID "
        "LEFT JOIN MON$RECORD_STATS r ON r.MON$STAT_ID = s.MON$STAT_ID "
        "WHERE s.MON$STATE <> 0 AND s.MON$ATTACHMENT_ID <> CURRENT_CONNECTION"
    )
    has_table_stats = bool(cat.one(
        "SELECT COUNT(*) AS n FROM RDB$RELATIONS WHERE RDB$RELATION_NAME = 'MON$TABLE_STATS'")["n"])
    tstats: list[dict] = []
    if has_table_stats:
        tstats = cat.rows(
            "SELECT ts.MON$STAT_GROUP AS grp, ts.MON$STAT_ID AS stat_id, TRIM(ts.MON$TABLE_NAME) AS table_name, "
            "r.MON$RECORD_WAITS AS waits, r.MON$RECORD_CONFLICTS AS conflicts, r.MON$RECORD_LOCKS AS locks, "
            "r.MON$RECORD_INSERTS AS inserts, r.MON$RECORD_UPDATES AS updates, r.MON$RECORD_DELETES AS deletes "
            "FROM MON$TABLE_STATS ts JOIN MON$RECORD_STATS r ON r.MON$STAT_ID = ts.MON$RECORD_STAT_ID "
            "WHERE ts.MON$STAT_GROUP IN (2, 3)"
        )
    txs = cat.rows(
        "SELECT t.MON$TRANSACTION_ID AS id, t.MON$ATTACHMENT_ID AS attachment_id, t.MON$STAT_ID AS stat_id, "
        "t.MON$TIMESTAMP AS started_at, t.MON$LOCK_TIMEOUT AS lock_timeout, TRIM(a.MON$USER) AS username, "
        "TRIM(a.MON$REMOTE_PROCESS) AS remote_process, TRIM(a.MON$REMOTE_ADDRESS) AS remote_address "
        "FROM MON$TRANSACTIONS t JOIN MON$ATTACHMENTS a ON a.MON$ATTACHMENT_ID = t.MON$ATTACHMENT_ID"
    )
    tx_by_stat = {t["stat_id"]: t for t in txs}
    tx_tables: dict[int, dict[str, dict]] = {}
    stmt_tables: dict[int, list[dict]] = {}
    for r in tstats:
        if r["grp"] == 2 and r["stat_id"] in tx_by_stat:
            tx_tables.setdefault(tx_by_stat[r["stat_id"]]["id"], {})[r["table_name"]] = r
        elif r["grp"] == 3:
            stmt_tables.setdefault(r["stat_id"], []).append(r)

    waiting = []
    for s in stmts:
        if not (s["waits"] or 0):
            continue
        tables = sorted({r["table_name"] for r in stmt_tables.get(s["stat_id"], []) if (r["waits"] or 0) > 0})
        blockers = []
        for t in txs:
            if t["id"] == s["transaction_id"]:
                continue
            touched = tx_tables.get(t["id"], {})
            hit = [n for n in tables if n in touched and
                   ((touched[n]["updates"] or 0) + (touched[n]["deletes"] or 0) + (touched[n]["inserts"] or 0)
                    + (touched[n]["locks"] or 0)) > 0]
            if hit:
                blockers.append({**{k: t[k] for k in ("id", "attachment_id", "username", "remote_process",
                                                       "remote_address", "started_at")}, "tables": hit})
        s["tables"] = tables
        s["blockers"] = blockers
        waiting.append(s)

    conflicts = [s for s in stmts if (s["conflicts"] or 0) > 0]
    config: list[dict] = []
    has_config = bool(cat.one("SELECT COUNT(*) AS n FROM RDB$RELATIONS WHERE RDB$RELATION_NAME = 'RDB$CONFIG'")["n"])
    if has_config:
        config = cat.rows(
            "SELECT TRIM(RDB$CONFIG_NAME) AS name, RDB$CONFIG_VALUE AS config_value, "
            "RDB$CONFIG_DEFAULT AS default_value, RDB$CONFIG_IS_SET AS is_set FROM RDB$CONFIG "
            "WHERE RDB$CONFIG_NAME IN ('DeadlockTimeout', 'LockMemSize', 'LockHashSlots', 'LockAcquireSpins', "
            "'ServerMode', 'StatementTimeout', 'ConnectionIdleTimeout')"
        )
    lock_timeouts = [
        {"id": t["id"], "attachment_id": t["attachment_id"], "username": t["username"],
         "lock_timeout": t["lock_timeout"]}
        for t in txs
    ]
    return {
        "waiting": waiting,
        "conflicts": conflicts,
        "running": [{k: v for k, v in s.items() if k != "stat_id"} for s in stmts],
        "transactions": lock_timeouts,
        "config": config,
        "table_stats_available": has_table_stats,
        "lock_print_available": False,
    }
