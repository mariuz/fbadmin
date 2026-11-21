"""Unused index finder (roadmap 2.5): indices that appear in no plan fbadmin recorded for N days.

The plans come from query regression tracking (``regressions.py``): the continuous trace (every statement above its
threshold), the history sampler (statements running long enough to be sampled) and slow Query Tool statements.
Both plan formats are read: the explained one (``Index "IX_ORDERS_DAY" Range Scan``) and the legacy one
(``PLAN (ORDERS INDEX (IX_ORDERS_DAY))``, ``ORDER IX_...``).

An index is a **candidate to drop** when it is active, appears in none of the plans of the period, and does not
enforce a constraint: primary key, unique and foreign key indices are kept (Firebird uses them to check the
constraint, not only in plans). The evidence is only as good as what was recorded: without the continuous
trace, fast statements are never seen, so the report says how many plans and days it is based on.

The cost of keeping an index: its table's inserts, updates and deletes since the database was opened
(``MON$TABLE_STATS``; each one also maintains the index) and, when the last check-up read gstat, its entries
and depth.
"""

from __future__ import annotations

import re
import time
from typing import Any

from ..metadata import Catalog
from ..metadata.types import quote_ident

_EXPLAINED = re.compile(r'Index\s+((?:"(?:[^"]|"")+"\.)?"(?:[^"]|"")+")')
_LEGACY_INDEX = re.compile(r"\bINDEX\s*\(([^)]*)\)", re.I)
_LEGACY_ORDER = re.compile(r"\bORDER\s+([A-Z0-9_$\"]+)", re.I)


def _unquote(name: str) -> str:
    name = name.strip()
    if "." in name and name.startswith('"'):
        name = name.rsplit(".", 1)[1]
    return name[1:-1].replace('""', '"') if name.startswith('"') and name.endswith('"') else name.upper()


def indices_in_plan(plan: str | None) -> set[str]:
    """Index names a plan uses (without schema)."""
    out: set[str] = set()
    if not plan:
        return out
    for m in _EXPLAINED.finditer(plan):
        out.add(_unquote(m.group(1)))
    for m in _LEGACY_INDEX.finditer(plan):
        out |= {_unquote(x) for x in m.group(1).split(",") if x.strip()}
    for m in _LEGACY_ORDER.finditer(plan):
        out.add(_unquote(m.group(1)))
    return out


def evidence(server_id: str, db_id: str, days: float, now: float) -> dict[str, Any]:
    """Which indices the recorded plans of the period used, and how much was recorded."""
    from . import history, regressions, trace_capture

    store = history.get_store()
    regressions._ensure(store)
    since = now - days * 86400
    with store._lock:
        plans = store._db.execute("SELECT plan, last_seen, first_seen FROM stmt_plans WHERE server_id = ? AND db_id = ? "
                                  "AND last_seen >= ?", (server_id, db_id, since)).fetchall()
        sources = dict(store._db.execute("SELECT source, COUNT(*) FROM stmt_runs WHERE server_id = ? AND db_id = ? "
                                         "AND ts >= ? GROUP BY source", (server_id, db_id, since)).fetchall())
        first = store._db.execute("SELECT MIN(first_seen) FROM stmt_plans WHERE server_id = ? AND db_id = ?",
                                  (server_id, db_id)).fetchone()[0]
    used: dict[str, float] = {}
    for plan, last_seen, _first in plans:
        for name in indices_in_plan(plan):
            used[name] = max(used.get(name, 0), last_seen or 0)
    span = min(days, (now - first) / 86400) if first else 0
    return {"used": used, "plans": len(plans), "runs": sum(sources.values()), "sources": sources,
            "since": since, "evidence_days": round(span, 1), "trace": trace_capture.active(server_id, db_id),
            "tracked": bool(store.is_tracked(server_id, db_id))}


def _indices(conn, cat: Catalog) -> list[dict[str, Any]]:
    schemas = cat.schemas_supported
    sch = "TRIM(i.RDB$SCHEMA_NAME) AS schema_name, " if schemas else ""
    join = " AND r.RDB$SCHEMA_NAME = i.RDB$SCHEMA_NAME" if schemas else ""
    cjoin = " AND c.RDB$SCHEMA_NAME = i.RDB$SCHEMA_NAME" if schemas else ""
    rows = cat.rows(
        f"SELECT {sch}TRIM(i.RDB$INDEX_NAME) AS index_name, TRIM(i.RDB$RELATION_NAME) AS table_name, "
        "COALESCE(i.RDB$UNIQUE_FLAG, 0) AS is_unique, COALESCE(i.RDB$INDEX_INACTIVE, 0) AS inactive, "
        "i.RDB$STATISTICS AS stored, IIF(i.RDB$EXPRESSION_BLR IS NULL, 0, 1) AS is_expression, "
        "TRIM(c.RDB$CONSTRAINT_TYPE) AS constraint_type, TRIM(c.RDB$CONSTRAINT_NAME) AS constraint_name "
        f"FROM RDB$INDICES i JOIN RDB$RELATIONS r ON r.RDB$RELATION_NAME = i.RDB$RELATION_NAME{join} "
        f"LEFT JOIN RDB$RELATION_CONSTRAINTS c ON c.RDB$INDEX_NAME = i.RDB$INDEX_NAME{cjoin} "
        "WHERE COALESCE(r.RDB$SYSTEM_FLAG, 0) = 0 AND r.RDB$VIEW_BLR IS NULL "
        "ORDER BY i.RDB$RELATION_NAME, i.RDB$INDEX_NAME", scoped=False)
    segs: dict[str, list[str]] = {}
    for s in cat.rows("SELECT TRIM(s.RDB$INDEX_NAME) AS index_name, TRIM(s.RDB$FIELD_NAME) AS field_name "
                      "FROM RDB$INDEX_SEGMENTS s ORDER BY s.RDB$INDEX_NAME, s.RDB$FIELD_POSITION", scoped=False):
        segs.setdefault(s["index_name"], []).append(s["field_name"])
    return [{"schema": r.get("schema_name") if schemas else None, "name": r["index_name"], "table": r["table_name"],
             "unique": bool(r["is_unique"]), "inactive": bool(r["inactive"]), "expression": bool(r["is_expression"]),
             "selectivity": float(r["stored"]) if r["stored"] is not None else None,
             "constraint": r["constraint_type"], "constraint_name": r["constraint_name"],
             "columns": segs.get(r["index_name"], [])} for r in rows]


def _table_writes(cat: Catalog) -> dict[str, int] | None:
    try:
        rows = cat.rows("SELECT TRIM(t.MON$TABLE_NAME) AS table_name, r.MON$RECORD_INSERTS AS ins, "
                        "r.MON$RECORD_UPDATES AS upd, r.MON$RECORD_DELETES AS del FROM MON$TABLE_STATS t "
                        "JOIN MON$RECORD_STATS r ON r.MON$STAT_ID = t.MON$RECORD_STAT_ID WHERE t.MON$STAT_GROUP = 0",
                        scoped=False)
    except Exception:  # noqa: BLE001 - MON$TABLE_STATS is missing on early Firebird 3.0 builds
        return None
    return {r["table_name"]: int(r["ins"] or 0) + int(r["upd"] or 0) + int(r["del"] or 0) for r in rows}


def _gstat_sizes(server_id: str, db_id: str) -> dict[str, dict[str, Any]]:
    from . import checkup

    rep = (checkup.last(server_id, db_id) or {}).get("report") or {}
    out = {}
    for t in rep.get("gstat_tables") or []:
        for i in t.get("indices") or []:
            out[i["name"]] = {"nodes": i.get("nodes"), "depth": i.get("depth"), "at": rep.get("generated_at")}
    return out


def report(server_id: str, db_id: str, conn, days: float = 30, now: float | None = None) -> dict[str, Any]:
    now = now or time.time()
    cat = Catalog(conn)
    ev = evidence(server_id, db_id, days, now)
    writes = _table_writes(cat)
    sizes = _gstat_sizes(server_id, db_id)
    rows = []
    for i in _indices(conn, cat):
        last = ev["used"].get(i["name"])
        if i["constraint"] in ("PRIMARY KEY", "UNIQUE", "FOREIGN KEY"):
            status = "constraint"
        elif i["inactive"]:
            status = "inactive"
        else:
            status = "used" if last else "unused"
        qn = (quote_ident(i["schema"]) + "." if i["schema"] else "") + quote_ident(i["name"])
        rows.append({**i, "status": status, "last_used": last, "table_writes": (writes or {}).get(i["table"]),
                     **({"nodes": sizes[i["name"]]["nodes"], "depth": sizes[i["name"]]["depth"]}
                        if i["name"] in sizes else {"nodes": None, "depth": None}),
                     "drop_sql": f"DROP INDEX {qn};" if status == "unused" else None})
    order = {"unused": 0, "used": 1, "inactive": 2, "constraint": 3}
    rows.sort(key=lambda r: (order[r["status"]], -(r["table_writes"] or 0), r["table"], r["name"]))
    unused = [r for r in rows if r["status"] == "unused"]
    if not ev["tracked"]:
        quality = "none"
    elif ev["trace"] and ev["evidence_days"] >= min(days, 7):
        quality = "good"
    elif ev["plans"] >= 10 and ev["evidence_days"] >= 1:
        quality = "partial"
    else:
        quality = "little"
    return {"days": days, "generated_at": now, "evidence": {k: v for k, v in ev.items() if k != "used"},
            "quality": quality, "writes_known": writes is not None, "gstat_at": next(iter(sizes.values()))["at"]
            if sizes else None, "counts": {s: sum(1 for r in rows if r["status"] == s) for s in order},
            "indices": rows, "script": "\n".join(r["drop_sql"] for r in unused)}
