"""Guided performance check-up ("why is it slow?", roadmap 2.3).

One ranked report per database, built from what fbadmin already collects:

* the transaction counters: a stuck oldest active transaction (OAT) and who holds it, long transactions;
* sweep settings: the OIT gap against the sweep interval, automatic sweep off with no scheduled sweep;
* the page cache: its size against the database and the cache miss rate (the history sampler's last hour
  when the database is tracked, otherwise since the database was opened);
* lock waits: statements waiting for a record lock right now, and how often waits and update
  conflicts happen;
* index statistics: selectivity never computed (indices created on an empty table, then loaded), and
  with gstat, stored selectivity that no longer matches the data; inactive indices;
* gstat (optional, it reads the whole database): record versions and version chains, fragmented or
  sparse tables, deep and unselective indices;
* the history store: query regressions and the index advisor's suggestions; nbackup state.

Every finding has a severity, the numbers behind it and fix actions: some run here (recompute
selectivity), the others use existing endpoints (sweep, database properties, history tracking,
disconnecting an attachment) or open the view where the problem is handled. Titles and advice are
English templates with ``{name}`` placeholders, so the UI can translate them (see
``frontend/src/features/checkup/messages.ts``).

A check-up runs as a job; the last report of each database is kept in memory.
"""

from __future__ import annotations

import os
import threading
import time
from typing import Any

from ..metadata import Catalog
from ..metadata.types import quote_ident
from . import probes

GAP_WARN = int(os.environ.get("FBADMIN_CHECKUP_GAP_WARN", os.environ.get("FBADMIN_OVERVIEW_GAP_WARN", "10000")))
LONG_TX = int(os.environ.get("FBADMIN_CHECKUP_LONG_TX", "1800"))  # seconds
MIN_NODES = 100          # ignore tiny indices for the selectivity comparison
SELECTIVITY_FACTOR = 3.0  # stored vs actual selectivity off by at least this much
MISS_WARN = 0.05          # page cache: reads / fetches
MIN_FETCHES = 20000       # need this much activity to judge the cache
SUGGESTED_SWEEP_INTERVAL = 20000

SEVERITY = {"critical": 3, "warning": 2, "info": 1}
# order within a severity: what to look at first
WEIGHT = {
    "oat_stuck": 100, "lock_waits_now": 95, "nbackup_locked": 90, "selectivity_outdated": 85,
    "selectivity_never": 84, "regressions": 80, "record_versions": 75, "page_cache": 70, "oit_stuck": 65,
    "lock_contention": 60, "long_transactions": 55, "inactive_indices": 50, "missing_indices": 45,
    "sweep_off": 30, "storage": 20, "unselective_indices": 15, "nbackup_merge": 12, "history_off": 10,
}

_lock = threading.Lock()
_reports: dict[tuple[str, str], dict[str, Any]] = {}
_jobs: dict[tuple[str, str], str] = {}


def _n(v: Any) -> str:
    return f"{int(v):,}" if isinstance(v, (int, float)) else str(v)


def _mb(b: float | None) -> str:
    if b is None:
        return "?"
    return f"{b / 1024 ** 3:.1f} GB" if b >= 1024 ** 3 else f"{b / 1024 ** 2:.0f} MB"


def _age(sec: float | None) -> str:
    sec = int(sec or 0)
    if sec < 120:
        return f"{sec} s"
    if sec < 7200:
        return f"{sec // 60} min"
    return f"{sec // 3600} h {sec % 3600 // 60} min"


def finding(code: str, severity: str, title: str, advice: str, params: dict | None = None, *,
            evidence: list[dict] | None = None, actions: list[dict] | None = None) -> dict[str, Any]:
    p = {k: v for k, v in (params or {}).items()}
    return {
        "code": code, "severity": severity, "title": title, "advice": advice, "params": p,
        "text": {"title": title.format(**p), "advice": advice.format(**p)},
        "evidence": evidence or [], "actions": actions or [],
    }


def action(kind: str, label: str, confirm: str | None = None, **params: Any) -> dict[str, Any]:
    return {"kind": kind, "label": label, "confirm": confirm, "params": params}


def open_view(label: str, target: str) -> dict[str, Any]:
    return action("open", label, target=target)


# ===================================================================== collection
def collect_database(conn) -> dict[str, Any]:
    """Facts from the database itself (one snapshot transaction)."""
    cat = Catalog(conn)
    db = cat.one(
        "SELECT d.MON$PAGE_SIZE AS page_size, d.MON$PAGES AS pages, d.MON$PAGE_BUFFERS AS page_buffers, "
        "d.MON$SWEEP_INTERVAL AS sweep_interval, d.MON$BACKUP_STATE AS backup_state, "
        "d.MON$READ_ONLY AS db_read_only, io.MON$PAGE_READS AS page_reads, io.MON$PAGE_FETCHES AS page_fetches, "
        "r.MON$RECORD_WAITS AS record_waits, r.MON$RECORD_CONFLICTS AS record_conflicts "
        "FROM MON$DATABASE d JOIN MON$IO_STATS io ON io.MON$STAT_ID = d.MON$STAT_ID "
        "JOIN MON$RECORD_STATS r ON r.MON$STAT_ID = d.MON$STAT_ID", scoped=False) or {}
    opened = cat.one(
        "SELECT DATEDIFF(SECOND FROM CAST(MIN(a.MON$TIMESTAMP) AS TIMESTAMP) TO CAST(CURRENT_TIMESTAMP AS TIMESTAMP)) "
        "AS open_seconds FROM MON$ATTACHMENTS a", scoped=False) or {}
    feats = probes.features(conn)
    config: dict[str, Any] = {}
    if feats["config"]:
        for r in cat.rows("SELECT TRIM(RDB$CONFIG_NAME) AS name, RDB$CONFIG_VALUE AS config_value FROM RDB$CONFIG "
                          "WHERE RDB$CONFIG_NAME IN ('ServerMode', 'DefaultDbCachePages')", scoped=False):
            config[r["name"]] = (r["config_value"] or "").strip()
    tx = probes.transactions_report(conn, long_running=LONG_TX, gap_warn=GAP_WARN)
    locks = probes.locks_report(conn)
    return {
        "engine_version": cat.engine_version,
        "engine_major": cat.engine_major,
        "database": {k: (int(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else v)
                     for k, v in db.items()},
        "open_seconds": opened.get("open_seconds"),
        "config": config,
        "transactions": {"gaps": tx["gaps"], "database": tx["database"],
                         "oat_holders": [_tx_brief(t) for t in tx["oat_holders"]],
                         "long_running": [_tx_brief(t) for t in tx["long_running"]]},
        "locks": {"waiting": [{k: s.get(k) for k in ("id", "attachment_id", "transaction_id", "age_seconds",
                                                    "sql_text", "username", "remote_process", "tables",
                                                    "blockers", "lock_timeout")}
                              for s in locks["waiting"]]},
        "indices": index_statistics(conn, cat),
    }


def _tx_brief(t: dict) -> dict:
    return {"id": t["id"], "attachment_id": t["attachment_id"], "username": t.get("username"),
            "remote_process": t.get("remote_process"), "remote_address": t.get("remote_address"),
            "age_seconds": t.get("age_seconds"), "writes": t.get("writes"), "is_self": bool(t.get("is_self")),
            "is_system": bool(t.get("is_system"))}


def index_statistics(conn, cat: Catalog | None = None, check_rows: int = 500) -> list[dict[str, Any]]:
    """User indices with their stored selectivity; ``has_rows`` for those whose selectivity is 0
    (computed when the table was empty, or never)."""
    cat = cat or Catalog(conn)
    schemas = cat.schemas_supported
    sch = "TRIM(i.RDB$SCHEMA_NAME) AS schema_name, " if schemas else ""
    join = " AND r.RDB$SCHEMA_NAME = i.RDB$SCHEMA_NAME" if schemas else ""
    rows = cat.rows(
        f"SELECT {sch}TRIM(i.RDB$INDEX_NAME) AS index_name, TRIM(i.RDB$RELATION_NAME) AS table_name, "
        "i.RDB$STATISTICS AS stored, COALESCE(i.RDB$INDEX_INACTIVE, 0) AS inactive, "
        "i.RDB$SEGMENT_COUNT AS segments, IIF(i.RDB$EXPRESSION_BLR IS NULL, 0, 1) AS is_expression "
        f"FROM RDB$INDICES i JOIN RDB$RELATIONS r ON r.RDB$RELATION_NAME = i.RDB$RELATION_NAME{join} "
        "WHERE COALESCE(r.RDB$SYSTEM_FLAG, 0) = 0 AND r.RDB$VIEW_BLR IS NULL "
        "AND COALESCE(r.RDB$RELATION_TYPE, 0) IN (0, 2, 3) "
        "ORDER BY i.RDB$RELATION_NAME, i.RDB$INDEX_NAME", scoped=False)
    out = []
    for r in rows:
        out.append({"schema": r.get("schema_name") if schemas else None, "name": r["index_name"],
                    "table": r["table_name"], "stored": float(r["stored"]) if r["stored"] is not None else None,
                    "inactive": bool(r["inactive"]), "segments": r["segments"],
                    "expression": bool(r["is_expression"])})
    # an index with selectivity 0 (or none) on a table that has rows: computed while it was empty
    zero_tables = sorted({(i["schema"], i["table"]) for i in out if not i["inactive"] and not i["stored"]})
    has_rows: dict[tuple, bool] = {}
    cur = conn.cursor()
    try:
        for schema, table in zero_tables[:check_rows]:
            name = (quote_ident(schema) + "." if schema else "") + quote_ident(table)
            try:
                cur.execute(f"SELECT FIRST 1 1 FROM {name}")
                has_rows[(schema, table)] = cur.fetchone() is not None
            except Exception:  # noqa: BLE001 - no privilege, external file missing...
                has_rows[(schema, table)] = False
    finally:
        cur.close()
    for i in out:
        i["has_rows"] = has_rows.get((i["schema"], i["table"]))
    return out


def compare_selectivity(indices: list[dict], stats: dict | None) -> None:
    """Add ``actual`` (1 / distinct keys, from gstat's nodes and total dup) and ``nodes`` to each index."""
    if not stats:
        return
    by: dict[tuple, dict] = {}
    for t in stats.get("tables", []):
        for ix in t.get("indices", []):
            sch, raw = ix.get("schema"), ix["name"]
            if sch and raw.startswith(sch + "."):  # gstat.parse shows non-PUBLIC schemas as SCHEMA.NAME
                raw = raw[len(sch) + 1:]
            by[(sch, raw)] = ix
    for i in indices:
        ix = by.get((i["schema"], i["name"]))
        if not ix:
            continue
        nodes = ix.get("nodes") or 0
        distinct = nodes - (ix.get("total_dup") or 0)
        i["nodes"] = nodes
        i["actual"] = round(1.0 / distinct, 10) if nodes and distinct > 0 else None


def cache_window(server_id: str, db_id: str, hours: float = 1.0) -> dict[str, Any] | None:
    """Reads, fetches, waits and conflicts over the history sampler's last hour (if tracked)."""
    from . import history

    store = history.get_store()
    if not store.is_tracked(server_id, db_id):
        return None
    rows = store.samples(server_id, db_id, since=time.time() - hours * 3600, max_points=0)
    if len(rows) < 2:
        return None
    a, b = rows[0], rows[-1]
    out: dict[str, Any] = {"seconds": (b.get("ts") or 0) - (a.get("ts") or 0)}
    for k in ("page_reads", "page_fetches", "record_waits", "record_conflicts"):
        x, y = a.get(k), b.get(k)
        out[k] = None if x is None or y is None else (y - x if y >= x else y)  # restarted in between
    return out


# ===================================================================== analysis
def analyze(facts: dict[str, Any]) -> list[dict[str, Any]]:
    """Findings, most important first."""
    out: list[dict[str, Any]] = []
    for fn in (_transactions, _sweep, _cache, _locks, _indices, _gstat, _history, _nbackup):
        out += fn(facts)
    # a stuck OAT is the cause of the record versions behind it: never rank it below them
    by = {f["code"]: f for f in out}
    if "oat_stuck" in by and "record_versions" in by:
        sev = max(by["oat_stuck"]["severity"], by["record_versions"]["severity"], key=SEVERITY.get)
        by["oat_stuck"]["severity"] = sev
    out.sort(key=lambda f: (-SEVERITY[f["severity"]], -WEIGHT.get(f["code"], 0)))
    for i, f in enumerate(out, 1):
        f["rank"] = i
    return out


def _who(t: dict) -> str:
    proc = (t.get("remote_process") or "").replace("\\", "/").split("/")[-1]
    return f"{t.get('username') or '?'}" + (f" ({proc})" if proc else "") + \
        (f" from {t['remote_address']}" if t.get("remote_address") else "")


def _transactions(f: dict) -> list[dict]:
    tx = f.get("transactions")
    if not tx:
        return []
    out = []
    gap = tx["gaps"]["next_oat"]
    holder = next((h for h in tx["oat_holders"] if not h["is_self"] and not h["is_system"]), None)
    if gap >= GAP_WARN:
        acts = [open_view("Show transactions", "transactions")]
        ev = []
        if holder:
            ev.append({"label": "Held by", "value": f"transaction {holder['id']} · {_who(holder)} · "
                                                    f"running {_age(holder['age_seconds'])}"})
            acts.append(action("disconnect", "Disconnect attachment {attachment_id}",
                               "Disconnect attachment {attachment_id}? Its transaction is rolled back.",
                               attachment_id=holder["attachment_id"]))
        out.append(finding(
            "oat_stuck", "critical" if gap >= GAP_WARN * 10 else "warning",
            "The oldest active transaction is stuck {gap} transactions behind",
            "Old record versions cannot be garbage collected while a transaction stays open, so reads get "
            "slower and the database grows. Commit or end the transaction that holds it; applications should "
            "not keep snapshot transactions open for long.",
            {"gap": _n(gap)}, evidence=ev, actions=acts))
    longs = [t for t in tx["long_running"] if not holder or t["id"] != holder["id"]]
    if longs:
        out.append(finding(
            "long_transactions", "warning", "{count} transaction(s) running longer than {limit}",
            "Long transactions keep old record versions alive and can hold locks. Find out why they stay "
            "open: a forgotten Query Tool session, a report, or an application that never commits.",
            {"count": len(longs), "limit": _age(LONG_TX)},
            evidence=[{"label": f"transaction {t['id']}", "value": f"{_who(t)} · {_age(t['age_seconds'])}"}
                      for t in longs[:5]],
            actions=[open_view("Show transactions", "transactions")]))
    return out


def _sweep(f: dict) -> list[dict]:
    tx = f.get("transactions")
    db = f.get("database") or {}
    if not tx or "sweep_interval" not in db:
        return []
    out = []
    interval = db.get("sweep_interval") or 0
    gap = tx["gaps"]["oat_oit"]
    if gap >= GAP_WARN:
        acts = [action("sweep", "Sweep now", "Sweep the database now? It reads every page and can take a while.")]
        if not interval:
            acts.append(action("set_sweep_interval", "Turn on automatic sweep (every {value} transactions)",
                               value=SUGGESTED_SWEEP_INTERVAL))
            advice = ("A rolled-back transaction stays \"interesting\" until a sweep. Automatic sweep is off, "
                      "so only a manual or scheduled sweep moves the oldest interesting transaction (OIT) forward.")
        else:
            advice = ("The gap is larger than the sweep interval ({interval}), so automatic sweep should have "
                      "run: it is failing or blocked by the oldest active transaction. Sweep manually and check "
                      "the server log.")
        out.append(finding("oit_stuck", "warning", "The oldest interesting transaction is {gap} transactions behind",
                           advice, {"gap": _n(gap), "interval": _n(interval)}, actions=acts))
    elif not interval and not f.get("sweep_scheduled") and not db.get("db_read_only"):
        out.append(finding(
            "sweep_off", "info", "Automatic sweep is off and no sweep is scheduled",
            "Garbage from rolled-back transactions is then only removed by a sweep. Schedule one (for example "
            "nightly), or turn automatic sweep back on.",
            actions=[open_view("Schedule a sweep", "schedules"),
                     action("set_sweep_interval", "Turn on automatic sweep (every {value} transactions)",
                            value=SUGGESTED_SWEEP_INTERVAL)]))
    return out


def _cache(f: dict) -> list[dict]:
    db = f.get("database") or {}
    size, pages, bufs = db.get("page_size"), db.get("pages"), db.get("page_buffers")
    if not (size and pages and bufs):
        return []
    win = f.get("window") or {}
    if win.get("page_fetches") is not None and (win.get("page_fetches") or 0) >= MIN_FETCHES:
        reads, fetches, when = win.get("page_reads") or 0, win["page_fetches"], "in the last hour"
    else:
        reads, fetches, when = db.get("page_reads") or 0, db.get("page_fetches") or 0, "since the database was opened"
    if fetches < MIN_FETCHES or bufs >= pages:
        return []
    miss = reads / fetches
    if miss < MISS_WARN:
        return []
    mode = (f.get("config") or {}).get("ServerMode", "")
    classic = mode.lower() in ("classic", "superclassic")
    cap = (128 if classic else 2048) * 1024 * 1024 // size
    suggested = int(min(pages * 1.1, max(bufs * 4, 16384), cap))
    acts = []
    if suggested > bufs:
        acts.append(action("set_page_buffers", "Set the cache to {value} pages ({size})",
                           "Write {value} page buffers to the database header? It takes effect when the database "
                           "is next opened.", value=suggested, size=_mb(suggested * size)))
    advice = ("Pages that are not in the cache are read from disk. A larger cache helps when the server has the "
              "memory for it; the value in the database header overrides DefaultDbCachePages and takes effect "
              "when the database is next opened.")
    if classic:
        advice += " In {mode} mode every connection has its own cache, so keep it moderate."
    return [finding(
        "page_cache", "warning" if miss >= MISS_WARN * 2 or bufs * 10 < pages else "info",
        "{miss}% of page fetches miss the cache ({when})", advice,
        {"miss": f"{miss * 100:.1f}", "when": when, "mode": mode or "?"},
        evidence=[{"label": "Cache", "value": f"{_n(bufs)} pages ({_mb(bufs * size)})"},
                  {"label": "Database", "value": f"{_n(pages)} pages ({_mb(pages * size)})"},
                  {"label": "Reads / fetches", "value": f"{_n(reads)} / {_n(fetches)}"}],
        actions=acts)]


def _locks(f: dict) -> list[dict]:
    out = []
    waiting = (f.get("locks") or {}).get("waiting") or []
    if waiting:
        ev, acts = [], [open_view("Show locks", "locks")]
        seen = set()
        for s in waiting[:5]:
            blockers = ", ".join(f"transaction {b['id']} ({b.get('username') or '?'})" for b in s.get("blockers") or [])
            ev.append({"label": f"statement {s['id']}",
                       "value": f"{s.get('username') or '?'} · waiting on {', '.join(s.get('tables') or []) or '?'}"
                                + (f" · blocked by {blockers}" if blockers else ""),
                       "sql": (s.get("sql_text") or "")[:400]})
            for b in s.get("blockers") or []:
                if b["attachment_id"] not in seen and len(seen) < 2:
                    seen.add(b["attachment_id"])
                    acts.append(action("disconnect", "Disconnect attachment {attachment_id}",
                                       "Disconnect attachment {attachment_id}? Its transaction is rolled back.",
                                       attachment_id=b["attachment_id"]))
        out.append(finding(
            "lock_waits_now", "warning", "{count} statement(s) are waiting for a record lock",
            "Another transaction changed the same rows and has not committed. Keep write transactions short; "
            "a lock timeout (WAIT n) stops statements from waiting forever.",
            {"count": len(waiting)}, evidence=ev, actions=acts))
    win = f.get("window") or {}
    db = f.get("database") or {}
    if win.get("record_waits") is not None:
        waits, conflicts, when = win.get("record_waits") or 0, win.get("record_conflicts") or 0, "in the last hour"
        lim_w, lim_c = 100, 10
    else:
        waits, conflicts, when = db.get("record_waits") or 0, db.get("record_conflicts") or 0, \
            "since the database was opened"
        lim_w, lim_c = 1000, 100
    if waits >= lim_w or conflicts >= lim_c:
        out.append(finding(
            "lock_contention", "warning" if conflicts >= lim_c else "info",
            "{waits} lock waits and {conflicts} update conflicts {when}",
            "Transactions often update the same rows at the same time. Look for hot rows (counters, "
            "\"last number\" tables: use a sequence instead) and long write transactions.",
            {"waits": _n(waits), "conflicts": _n(conflicts), "when": when},
            actions=[open_view("Show locks", "locks")]))
    return out


def _qname(i: dict) -> str:
    return (f"{i['schema']}." if i.get("schema") not in (None, "PUBLIC", "SYSTEM") else "") + i["name"]


def _indices(f: dict) -> list[dict]:
    idx = f.get("indices")
    if idx is None:
        return []
    out = []
    active = [i for i in idx if not i["inactive"]]
    never = [i for i in active if not i["stored"] and i.get("has_rows")
             and not (i.get("nodes") is not None and i.get("actual") is None)]
    outdated = []
    for i in active:
        if i in never or i.get("actual") is None or (i.get("nodes") or 0) < MIN_NODES or not i["stored"]:
            continue
        a, s = i["actual"], i["stored"]
        if max(a, s) / min(a, s) >= SELECTIVITY_FACTOR:
            outdated.append(i)

    def recompute(items: list[dict]) -> dict:
        return action("recompute_statistics", "Recompute selectivity of {count} index(es)", count=len(items),
                      indices=[{"name": i["name"], "schema": i["schema"]} for i in items])

    if never:
        out.append(finding(
            "selectivity_never", "warning", "{count} index(es) have no selectivity although their table has rows",
            "The selectivity was computed while the table was empty (or never), so the optimizer thinks these "
            "indices are useless or perfect and may pick bad plans. Recompute it; do this after every bulk load.",
            {"count": len(never)},
            evidence=[{"label": _qname(i), "value": f"table {i['table']}"} for i in never[:10]],
            actions=[recompute(never)]))
    if outdated:
        out.append(finding(
            "selectivity_outdated", "warning", "{count} index(es) have outdated selectivity",
            "The stored selectivity no longer matches the data (measured by gstat), so the optimizer works "
            "from wrong numbers. Recompute it.",
            {"count": len(outdated)},
            evidence=[{"label": _qname(i), "value": f"stored {i['stored']:.6g}, actual {i['actual']:.6g}"}
                      for i in outdated[:10]],
            actions=[recompute(outdated)]))
    inactive = [i for i in idx if i["inactive"]]
    if inactive:
        sql = "\n".join(f"ALTER INDEX {(quote_ident(i['schema']) + '.') if i['schema'] else ''}"
                        f"{quote_ident(i['name'])} ACTIVE;" for i in inactive)
        out.append(finding(
            "inactive_indices", "warning", "{count} index(es) are inactive",
            "Inactive indices are not used by any query (a failed restore or a bulk load can leave them "
            "inactive). Activating one rebuilds it.",
            {"count": len(inactive)},
            evidence=[{"label": _qname(i), "value": f"table {i['table']}"} for i in inactive[:10]],
            actions=[action("query_tool", "Open the ALTER INDEX statements in the Query Tool", sql=sql)]))
    return out


def _gstat(f: dict) -> list[dict]:
    st = f.get("gstat")
    if not st:
        return []
    from ..maintenance import gstat

    att = gstat.attention(st)
    out = []
    versions = [a for a in att if a["issue"] in ("Many record versions", "Long version chains")]
    if versions:
        tables = sorted({a["table"] for a in versions})
        oat_stuck = (f.get("transactions") or {}).get("gaps", {}).get("next_oat", 0) >= GAP_WARN
        advice = ("Back versions make reads slower until garbage collection removes them. A sweep collects them")
        advice += (", but only once the oldest active transaction moves on." if oat_stuck else ".")
        out.append(finding(
            "record_versions", "critical" if any(a["severity"] == "critical" for a in versions) else "warning",
            "{count} table(s) carry many old record versions", advice, {"count": len(tables)},
            evidence=[{"label": a["table"], "value": a["detail"]} for a in versions[:10]],
            actions=[action("sweep", "Sweep now", "Sweep the database now? It reads every page and can take a while."),
                     open_view("Open database statistics", "gstat")]))
    storage = [a for a in att if a["issue"] in ("Fragmented records", "Low page fill", "Deep index")]
    if storage:
        out.append(finding(
            "storage", "info", "{count} table(s) or index(es) are fragmented, sparse or deep",
            "Fragmented records and deep indices mean more page reads per row; a backup and restore compacts "
            "the tables, and a larger page size helps wide records and deep indices.",
            {"count": len(storage)},
            evidence=[{"label": a["index"] or a["table"], "value": f"{a['issue']}: {a['detail']}"}
                      for a in storage[:10]],
            actions=[open_view("Open backup and restore", "backup")]))
    poor = [a for a in att if a["issue"] == "Poor selectivity"]
    if poor:
        out.append(finding(
            "unselective_indices", "info", "{count} index(es) have very few distinct values",
            "An index where most entries share one value rarely helps a query and slows down writes and "
            "garbage collection. Consider dropping it or making it a compound index.",
            {"count": len(poor)},
            evidence=[{"label": a["index"], "value": a["detail"]} for a in poor[:10]]))
    return out


def _history(f: dict) -> list[dict]:
    h = f.get("history")
    if h is None:
        return []
    out = []
    if not h.get("tracked"):
        out.append(finding(
            "history_off", "info", "History isn't recorded for this database",
            "With history on, the check-up also compares cache misses and lock waits over the last hour, and "
            "finds statements that got slower (query regressions) and missing indices.",
            actions=[action("track_history", "Record history for this database")]))
    regs = h.get("regressions") or []
    if regs:
        out.append(finding(
            "regressions", "warning", "{count} statement(s) got slower",
            "Their recent runs are much slower than before, often after a plan change. Compare their plans "
            "over time; outdated index selectivity is a common cause.",
            {"count": len(regs)},
            evidence=[{"label": f"{r.get('ratio') or '?'}× slower", "sql": r.get("normalized") or ""} for r in regs[:5]],
            actions=[open_view("Open query regressions", "regressions")]))
    sugg = h.get("advisor") or []
    if sugg:
        out.append(finding(
            "missing_indices", "warning" if any((s.get("total_seconds") or 0) >= 1 for s in sugg) else "info",
            "The index advisor suggests {count} index(es) for slow statements",
            "Slow statements read whole tables where an index on the filtered columns would do. Check the "
            "suggestion against the plan before creating it.",
            {"count": len(sugg)},
            evidence=[{"label": s.get("table") or "?", "value": ", ".join(s.get("columns") or []), "sql": s.get("ddl")}
                      for s in sugg[:5]],
            actions=[open_view("Open the index advisor", "advisor"),
                     action("query_tool", "Open the CREATE INDEX statements in the Query Tool",
                            sql="\n".join(s["ddl"].rstrip(";") + ";" for s in sugg[:10] if s.get("ddl")))]))
    return out


def _nbackup(f: dict) -> list[dict]:
    state = (f.get("database") or {}).get("backup_state")
    if state == 1:
        return [finding(
            "nbackup_locked", "warning", "The database is locked by nbackup",
            "While it is locked every page write also goes to the delta file, which keeps growing. End the "
            "lock (nbackup -N) when the file copy is done.",
            actions=[open_view("Open incremental backup", "nbackup")])]
    if state == 2:
        return [finding("nbackup_merge", "info", "The nbackup delta file is being merged",
                        "Writes are slower until the merge finishes.")]
    return []


# ===================================================================== running
def run(server_id: str, db_id: str, *, with_gstat: bool = True, log=lambda _line: None) -> dict[str, Any]:
    """Collect everything, analyze it and keep the report as the database's last one."""
    from ..api.deps import get_state
    from ..maintenance import gstat, ops
    from ..query import end_transaction
    from . import history
    from . import regressions as R

    st = get_state()
    db = st.store.get_database(server_id, db_id)
    started = time.time()
    checks: list[dict[str, Any]] = []
    facts: dict[str, Any] = {}

    def step(name: str, fn) -> None:
        log(f"Checking {name}…")
        try:
            fn()
            checks.append({"name": name, "status": "done"})
        except Exception as exc:  # noqa: BLE001 - one failed check must not hide the others
            msg = " ".join(str(exc).split()) or exc.__class__.__name__
            log(f"  {name}: {msg}")
            checks.append({"name": name, "status": "error", "error": msg})

    def database() -> None:
        with st.manager.meta(server_id, db_id) as conn:
            try:
                facts.update(collect_database(conn))
            finally:
                end_transaction(conn, commit=False)

    def schedules() -> None:
        from ..maintenance.scheduler import get_scheduler

        sch = get_scheduler()
        with sch._lock:
            facts["sweep_scheduled"] = any(s.kind == "sweep" and s.enabled and s.server_id == server_id
                                           and s.database_id == db_id for s in sch.schedules.values())

    def stats() -> None:
        lines: list[str] = []
        with st.manager.service(server_id) as svc:
            ops.statistics_text(svc, db.path, record_versions=True, system=False, tables=None, log=lines.append)
        facts["gstat"] = gstat.parse("\n".join(lines))
        compare_selectivity(facts.get("indices") or [], facts["gstat"])

    def hist() -> None:
        store = history.get_store()
        tracked = store.is_tracked(server_id, db_id)
        h: dict[str, Any] = {"tracked": tracked, "regressions": [], "advisor": []}
        facts["history"] = h
        facts["window"] = cache_window(server_id, db_id)
        if tracked:
            h["regressions"] = R.analyze(store, server_id, db_id, store.get_settings(), only="regressions")

    def advisor() -> None:
        from ..api.monitoring import AdvisorIn, index_advisor

        res = index_advisor(server_id, db_id, AdvisorIn(sources=["history", "compiled"]))
        facts.setdefault("history", {"tracked": False})["advisor"] = res.get("suggestions") or []

    step("database", database)
    step("schedules", schedules)
    if with_gstat:
        step("gstat", stats)
    step("history", hist)
    step("advisor", advisor)
    findings = analyze(facts)
    d = facts.get("database") or {}
    gaps = (facts.get("transactions") or {}).get("gaps") or {}
    win = facts.get("window") or {}
    fetch_src = win if (win.get("page_fetches") or 0) >= MIN_FETCHES else d
    report = {
        "server_id": server_id, "database_id": db_id, "database": db.name,
        "generated_at": time.time(), "duration": round(time.time() - started, 2), "gstat": with_gstat,
        "engine_version": facts.get("engine_version"),
        "summary": {s: sum(1 for x in findings if x["severity"] == s) for s in ("critical", "warning", "info")},
        "facts": {
            "page_size": d.get("page_size"), "pages": d.get("pages"),
            "size_bytes": (d.get("page_size") or 0) * (d.get("pages") or 0) or None,
            "page_buffers": d.get("page_buffers"),
            "cache_bytes": (d.get("page_size") or 0) * (d.get("page_buffers") or 0) or None,
            "cache_miss": (round((fetch_src.get("page_reads") or 0) / fetch_src["page_fetches"], 4)
                           if fetch_src.get("page_fetches") else None),
            "sweep_interval": d.get("sweep_interval"), "server_mode": (facts.get("config") or {}).get("ServerMode"),
            "next_oat": gaps.get("next_oat"), "oat_oit": gaps.get("oat_oit"),
            "open_seconds": facts.get("open_seconds"),
            "indices": len(facts.get("indices") or []),
            "tracked": (facts.get("history") or {}).get("tracked"),
        },
        "checks": checks,
        "findings": findings,
    }
    with _lock:
        _reports[(server_id, db_id)] = report
    log(f"Done: {len(findings)} finding(s)")
    return report


def start(server_id: str, db_id: str, *, with_gstat: bool = True) -> dict[str, Any]:
    """Run a check-up as a job (one at a time per database)."""
    from ..api.deps import get_state

    st = get_state()
    db = st.store.get_database(server_id, db_id)
    with _lock:
        jid = _jobs.get((server_id, db_id))
    if jid:
        try:
            job = st.jobs.get(jid)
            if job.status == "running":
                return job.summary()
        except KeyError:
            pass

    def fn(log) -> None:
        run(server_id, db_id, with_gstat=with_gstat, log=log)

    job = st.jobs.start("checkup", f"Performance check-up {db.name}", server_id, fn)
    with _lock:
        _jobs[(server_id, db_id)] = job.id
    return job.summary()


def last(server_id: str, db_id: str) -> dict[str, Any]:
    from ..api.deps import get_state

    with _lock:
        report = _reports.get((server_id, db_id))
        jid = _jobs.get((server_id, db_id))
    job = None
    if jid:
        try:
            j = get_state().jobs.get(jid)
            job = {"id": j.id, "status": j.status, "error": j.error, "log": list(j.log)[-20:]}
        except KeyError:
            job = None
    return {"report": report, "job": job}


def forget(server_id: str, db_id: str) -> None:
    with _lock:
        _reports.pop((server_id, db_id), None)
        _jobs.pop((server_id, db_id), None)


def recompute(conn, indices: list[dict[str, Any]], log=lambda _line: None) -> list[str]:
    """SET STATISTICS INDEX for the given indices (``{name, schema?}``); returns the names done."""
    cat = Catalog(conn)
    schemas = cat.schemas_supported
    known = {(i["schema"], i["name"]) for i in index_statistics(conn, cat, check_rows=0)}
    done = []
    cur = conn.cursor()
    try:
        for i in indices:
            key = (i.get("schema") if schemas else None, i["name"])
            if key not in known:
                raise ValueError(f"Unknown index {i['name']!r}")
            name = (quote_ident(key[0]) + "." if key[0] else "") + quote_ident(key[1])
            cur.execute(f"SET STATISTICS INDEX {name}")
            done.append(_qname({"schema": key[0], "name": key[1]}))
            log(f"Recomputed {done[-1]}")
        conn.commit()
    finally:
        cur.close()
    return done
