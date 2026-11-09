"""Capacity and transaction-ID forecasting (roadmap 2.4): project growth and warn before a limit is reached.

fbadmin records an *observation* of a database at most once an hour (``MIN_INTERVAL``): its size (pages x page
size), the next transaction number and the value of every sequence (identity columns included). Observations
come from the history sampler (tracked databases) and from opening the forecast; they are kept for
``KEEP_DAYS``. A least-squares line through the observations of the last ``WINDOW_DAYS`` days gives the growth
per day, and from it:

* **size**: the size in 30, 90 and 365 days, and the days until a *space budget* set for the database (the
  space set aside on its disk; Firebird cannot tell how much is free);
* **transactions**: transactions per day and the time until the transaction counter's limit. Firebird 3 and
  later count transactions in 48 bits (2^48 - 1, about 281 trillion), so this is reassuring rather than urgent;
  Firebird 2.5 stopped at 2^31 - 1, which is why the limit was once a real outage risk;
* **sequences**: each sequence against the column it feeds: identity columns exactly, and sequences used in a
  table's triggers ("probably feeds") for a single-column integer primary key. An INTEGER column stops at
  2,147,483,647 however large the sequence (BIGINT) can grow;
* **table formats**: Firebird keeps at most 255 formats per table and each ``ALTER TABLE`` that changes its
  columns adds one; at the limit the table cannot be changed until a backup and restore. Shown as used / 255.

A finding is *critical* within 30 days (or at 95 % of a limit), a *warning* within 180 days (or at 80 %); new
or worse findings raise the ``capacity.forecast`` alert.
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
from typing import Any

from ..metadata import Catalog
from ..metadata.types import quote_ident

MIN_INTERVAL = float(os.environ.get("FBADMIN_FORECAST_INTERVAL", "3600"))   # seconds between observations
WINDOW_DAYS = float(os.environ.get("FBADMIN_FORECAST_WINDOW_DAYS", "30"))   # observations used for the trend
KEEP_DAYS = float(os.environ.get("FBADMIN_FORECAST_KEEP_DAYS", "730"))
MIN_SPAN = 6 * 3600          # a trend needs observations at least this far apart
TX_LIMIT_48 = 2**48 - 1       # Firebird 3+ (ODS 12+)
TX_LIMIT_31 = 2**31 - 1       # Firebird 2.5 and earlier (ODS 11)
FORMAT_LIMIT = 255
TYPE_LIMITS = {7: ("SMALLINT", 2**15 - 1), 8: ("INTEGER", 2**31 - 1), 16: ("BIGINT", 2**63 - 1),
               26: ("INT128", 2**127 - 1)}
CRIT_DAYS, WARN_DAYS = 30, 180
CRIT_PCT, WARN_PCT = 95.0, 80.0

_init_lock = threading.Lock()
_last: dict[tuple[str, str], float] = {}
_due_lock = threading.Lock()


# ===================================================================== storage (monitoring.sqlite)
def _ensure(store) -> None:
    if getattr(store, "_forecast_ready", False):
        return
    with _init_lock, store._lock:
        store._db.executescript(
            """
            CREATE TABLE IF NOT EXISTS forecast_obs (server_id TEXT NOT NULL, db_id TEXT NOT NULL, ts REAL NOT NULL,
                pages INTEGER, page_size INTEGER, next_tx INTEGER, seqs TEXT);
            CREATE INDEX IF NOT EXISTS forecast_obs_db ON forecast_obs (server_id, db_id, ts);
            CREATE TABLE IF NOT EXISTS forecast_settings (server_id TEXT NOT NULL, db_id TEXT NOT NULL,
                budget_bytes INTEGER, alerted TEXT, PRIMARY KEY (server_id, db_id));
            """
        )
        store._forecast_ready = True


def _store():
    from . import history

    s = history.get_store()
    _ensure(s)
    return s


def observations(server_id: str, db_id: str, since: float | None = None) -> list[dict[str, Any]]:
    s = _store()
    with s._lock:
        rows = s._db.execute("SELECT ts, pages, page_size, next_tx, seqs FROM forecast_obs WHERE server_id = ? "
                             "AND db_id = ? AND ts >= ? ORDER BY ts", (server_id, db_id, since or 0)).fetchall()
    return [{"ts": r[0], "pages": r[1], "page_size": r[2], "next_tx": r[3], "seqs": json.loads(r[4] or "{}")}
            for r in rows]


def add_observation(server_id: str, db_id: str, obs: dict[str, Any], ts: float | None = None) -> None:
    s = _store()
    now = ts or time.time()
    with s._lock:
        s._db.execute("INSERT INTO forecast_obs (server_id, db_id, ts, pages, page_size, next_tx, seqs) "
                      "VALUES (?, ?, ?, ?, ?, ?, ?)", (server_id, db_id, now, obs.get("pages"), obs.get("page_size"),
                                                        obs.get("next_tx"), json.dumps(obs.get("seqs") or {})))
        s._db.execute("DELETE FROM forecast_obs WHERE ts < ?", (now - KEEP_DAYS * 86400,))
    key = (server_id, db_id)
    _last[key] = max(_last.get(key, 0), now)  # an older observation (imported history) does not reset the hour


def forget(server_id: str, db_id: str) -> None:
    s = _store()
    with s._lock:
        s._db.execute("DELETE FROM forecast_obs WHERE server_id = ? AND db_id = ?", (server_id, db_id))
        s._db.execute("DELETE FROM forecast_settings WHERE server_id = ? AND db_id = ?", (server_id, db_id))
    _last.pop((server_id, db_id), None)


def get_budget(server_id: str, db_id: str) -> int | None:
    s = _store()
    with s._lock:
        r = s._db.execute("SELECT budget_bytes FROM forecast_settings WHERE server_id = ? AND db_id = ?",
                          (server_id, db_id)).fetchone()
    return r[0] if r else None


def set_budget(server_id: str, db_id: str, budget: int | None) -> None:
    s = _store()
    with s._lock:
        s._db.execute("INSERT INTO forecast_settings (server_id, db_id, budget_bytes) VALUES (?, ?, ?) "
                      "ON CONFLICT (server_id, db_id) DO UPDATE SET budget_bytes = excluded.budget_bytes",
                      (server_id, db_id, budget))


def _alerted(server_id: str, db_id: str) -> dict[str, str]:
    s = _store()
    with s._lock:
        r = s._db.execute("SELECT alerted FROM forecast_settings WHERE server_id = ? AND db_id = ?",
                          (server_id, db_id)).fetchone()
    return json.loads(r[0]) if r and r[0] else {}


def _set_alerted(server_id: str, db_id: str, state: dict[str, str]) -> None:
    s = _store()
    with s._lock:
        s._db.execute("INSERT INTO forecast_settings (server_id, db_id, alerted) VALUES (?, ?, ?) "
                      "ON CONFLICT (server_id, db_id) DO UPDATE SET alerted = excluded.alerted",
                      (server_id, db_id, json.dumps(state)))


# ===================================================================== reading the database
def _gen_name(name: str, schema: str | None) -> str:
    return (quote_ident(schema) + "." if schema else "") + quote_ident(name)


def read(conn) -> dict[str, Any]:
    """Size, next transaction, ODS and sequence values of the database ``conn`` is attached to."""
    cat = Catalog(conn)
    d = cat.one("SELECT MON$PAGES AS pages, MON$PAGE_SIZE AS page_size, MON$NEXT_TRANSACTION AS next_tx, "
                "MON$ODS_MAJOR AS ods_major FROM MON$DATABASE", scoped=False) or {}
    schemas = cat.schemas_supported
    gens = cat.rows(("SELECT TRIM(g.RDB$SCHEMA_NAME) AS schema_name, " if schemas else "SELECT ")
                    + "TRIM(g.RDB$GENERATOR_NAME) AS name FROM RDB$GENERATORS g "
                    "WHERE COALESCE(g.RDB$SYSTEM_FLAG, 0) IN (0, 6) ORDER BY 1", scoped=False)
    seqs: dict[str, int] = {}
    for i in range(0, len(gens), 50):
        part = gens[i:i + 50]
        names = [_gen_name(g["name"], g.get("schema_name") if schemas and g.get("schema_name") != "PUBLIC" else None)
                 for g in part]
        try:
            row = cat.one("SELECT " + ", ".join(f"GEN_ID({n}, 0) AS g{j}" for j, n in enumerate(names))
                          + " FROM RDB$DATABASE", scoped=False) or {}
        except Exception:  # noqa: BLE001 - a sequence without the USAGE privilege
            row = {}
        for j, g in enumerate(part):
            v = row.get(f"g{j}")
            if v is not None:
                key = g["name"] if not schemas or g.get("schema_name") in (None, "PUBLIC") else f"{g['schema_name']}.{g['name']}"
                seqs[key] = int(v)
    return {"pages": d.get("pages"), "page_size": d.get("page_size"), "next_tx": d.get("next_tx"),
            "ods_major": d.get("ods_major"), "seqs": seqs}


def observe(conn, server_id: str, db_id: str, force: bool = False) -> dict[str, Any] | None:
    """Record an observation unless one was recorded less than ``MIN_INTERVAL`` ago."""
    key = (server_id, db_id)
    with _due_lock:  # the history sampler and the page may ask at the same moment: only one records
        if key not in _last:
            last = observations(server_id, db_id, since=time.time() - MIN_INTERVAL)
            _last[key] = last[-1]["ts"] if last else 0
        if not force and time.time() - _last[key] < MIN_INTERVAL:
            return None
        before = _last[key]
        _last[key] = time.time()  # reserved; reading the database happens outside the lock
    try:
        obs = read(conn)
    except Exception:
        with _due_lock:
            _last[key] = before
        raise
    add_observation(server_id, db_id, obs)
    return obs


def sequence_targets(conn) -> dict[str, dict[str, Any]]:
    """The column each sequence feeds: identity columns (exact) and single-column integer primary keys of tables
    whose triggers use the sequence (probable)."""
    cat = Catalog(conn)
    out: dict[str, dict[str, Any]] = {}
    has_identity = cat.has_column("RDB$RELATION_FIELDS", "RDB$GENERATOR_NAME")
    if has_identity:
        for r in cat.rows("SELECT TRIM(rf.RDB$GENERATOR_NAME) AS gen, TRIM(rf.RDB$RELATION_NAME) AS table_name, "
                          "TRIM(rf.RDB$FIELD_NAME) AS column_name, f.RDB$FIELD_TYPE AS ftype, f.RDB$FIELD_SCALE AS fscale "
                          "FROM RDB$RELATION_FIELDS rf JOIN RDB$FIELDS f ON f.RDB$FIELD_NAME = rf.RDB$FIELD_SOURCE "
                          "WHERE rf.RDB$GENERATOR_NAME IS NOT NULL", scoped=False):
            if r["ftype"] in TYPE_LIMITS and not r["fscale"]:
                out[r["gen"]] = {"table": r["table_name"], "column": r["column_name"],
                                 "type": TYPE_LIMITS[r["ftype"]][0], "limit": TYPE_LIMITS[r["ftype"]][1], "how": "identity"}
    pks = {r["table_name"]: r for r in cat.rows(
        "SELECT TRIM(rc.RDB$RELATION_NAME) AS table_name, MIN(TRIM(s.RDB$FIELD_NAME)) AS column_name, "
        "COUNT(*) AS n, MIN(f.RDB$FIELD_TYPE) AS ftype, MIN(f.RDB$FIELD_SCALE) AS fscale "
        "FROM RDB$RELATION_CONSTRAINTS rc JOIN RDB$INDEX_SEGMENTS s ON s.RDB$INDEX_NAME = rc.RDB$INDEX_NAME "
        "JOIN RDB$RELATION_FIELDS rf ON rf.RDB$RELATION_NAME = rc.RDB$RELATION_NAME AND rf.RDB$FIELD_NAME = s.RDB$FIELD_NAME "
        "JOIN RDB$FIELDS f ON f.RDB$FIELD_NAME = rf.RDB$FIELD_SOURCE "
        "WHERE rc.RDB$CONSTRAINT_TYPE = 'PRIMARY KEY' GROUP BY rc.RDB$RELATION_NAME", scoped=False)}
    for r in cat.rows("SELECT TRIM(t.RDB$RELATION_NAME) AS table_name, t.RDB$TRIGGER_SOURCE AS src FROM RDB$TRIGGERS t "
                      "WHERE COALESCE(t.RDB$SYSTEM_FLAG, 0) = 0 AND t.RDB$RELATION_NAME IS NOT NULL", scoped=False):
        pk = pks.get(r["table_name"])
        if not pk or pk["n"] != 1 or pk["ftype"] not in TYPE_LIMITS or pk["fscale"]:
            continue
        src = (r["src"] or "").upper()
        for m in re.finditer(r'(?:GEN_ID\s*\(\s*|NEXT\s+VALUE\s+FOR\s+)"?([A-Z0-9_$]+)"?', src):
            out.setdefault(m.group(1), {"table": r["table_name"], "column": pk["column_name"],
                                        "type": TYPE_LIMITS[pk["ftype"]][0], "limit": TYPE_LIMITS[pk["ftype"]][1],
                                        "how": "trigger"})
    return out


def table_formats(conn) -> list[dict[str, Any]]:
    cat = Catalog(conn)
    return cat.rows("SELECT TRIM(r.RDB$RELATION_NAME) AS table_name, MAX(f.RDB$FORMAT) AS formats "
                    "FROM RDB$FORMATS f JOIN RDB$RELATIONS r ON r.RDB$RELATION_ID = f.RDB$RELATION_ID "
                    "WHERE COALESCE(r.RDB$SYSTEM_FLAG, 0) = 0 GROUP BY r.RDB$RELATION_NAME ORDER BY 2 DESC", scoped=False)


# ===================================================================== the forecast
def trend(points: list[tuple[float, float]]) -> float | None:
    """Least-squares slope per day of ``(ts, value)`` points, or None without enough data."""
    pts = [(t, v) for t, v in points if v is not None]
    if len(pts) < 2 or pts[-1][0] - pts[0][0] < MIN_SPAN:
        return None
    n = len(pts)
    mt = sum(t for t, _ in pts) / n
    mv = sum(v for _, v in pts) / n
    den = sum((t - mt) ** 2 for t, _ in pts)
    if den == 0:
        return None
    return sum((t - mt) * (v - mv) for t, v in pts) / den * 86400


def _days_left(current: float, limit: float, per_day: float | None) -> float | None:
    if per_day is None or per_day <= 0:
        return None
    return max(0.0, (limit - current) / per_day)


def _severity(days: float | None, pct: float | None) -> str:
    if (days is not None and days < CRIT_DAYS) or (pct is not None and pct >= CRIT_PCT):
        return "critical"
    if (days is not None and days < WARN_DAYS) or (pct is not None and pct >= WARN_PCT):
        return "warning"
    return "ok"


def analyze(server_id: str, db_id: str, conn, now: float | None = None) -> dict[str, Any]:
    now = now or time.time()
    obs = observations(server_id, db_id, since=now - WINDOW_DAYS * 86400)
    every = observations(server_id, db_id)
    cur = read(conn)
    targets = sequence_targets(conn)
    formats = table_formats(conn)
    out: dict[str, Any] = {"observations": len(every), "window_observations": len(obs),
                           "first": every[0]["ts"] if every else None, "last": every[-1]["ts"] if every else None,
                           "window_days": WINDOW_DAYS, "enough": False, "findings": []}
    if len(obs) >= 2 and obs[-1]["ts"] - obs[0]["ts"] >= MIN_SPAN:
        out["enough"] = True

    # size
    size_now = (cur["pages"] or 0) * (cur["page_size"] or 0)
    per_day = trend([(o["ts"], (o["pages"] or 0) * (o["page_size"] or 0)) for o in obs])
    budget = get_budget(server_id, db_id)
    days = _days_left(size_now, budget, per_day) if budget else None
    pct = round(size_now * 100 / budget, 1) if budget else None
    out["size"] = {"bytes": size_now, "per_day": per_day, "budget": budget, "budget_pct": pct, "days_left": days,
                   "in_30": size_now + per_day * 30 if per_day is not None else None,
                   "in_90": size_now + per_day * 90 if per_day is not None else None,
                   "in_365": size_now + per_day * 365 if per_day is not None else None,
                   "series": [[o["ts"], (o["pages"] or 0) * (o["page_size"] or 0)] for o in every[-500:]],
                   "severity": _severity(days, pct) if budget else "ok"}

    # transactions
    limit = TX_LIMIT_48 if (cur.get("ods_major") or 12) >= 12 else TX_LIMIT_31
    tx_day = trend([(o["ts"], o["next_tx"]) for o in obs])
    tx_days = _days_left(cur["next_tx"] or 0, limit, tx_day)
    out["transactions"] = {"next": cur["next_tx"], "limit": limit, "per_day": tx_day, "days_left": tx_days,
                           "pct": round((cur["next_tx"] or 0) * 100 / limit, 6),
                           "severity": _severity(tx_days, (cur["next_tx"] or 0) * 100 / limit)}

    # sequences
    seq_rows = []
    for name, value in sorted(cur["seqs"].items()):
        tgt = targets.get(name.split(".")[-1]) or {}
        lim = tgt.get("limit") or TYPE_LIMITS[16][1]
        sd = trend([(o["ts"], o["seqs"].get(name)) for o in obs])
        dl = _days_left(value, lim, sd)
        p = round(max(value, 0) * 100 / lim, 4)
        seq_rows.append({"name": name, "value": value, "per_day": sd, "limit": lim, "pct": p, "days_left": dl,
                         "table": tgt.get("table"), "column": tgt.get("column"), "type": tgt.get("type") or "BIGINT",
                         "how": tgt.get("how"), "severity": _severity(dl, p)})
    seq_rows.sort(key=lambda r: ({"critical": 0, "warning": 1, "ok": 2}[r["severity"]], -(r["pct"] or 0)))
    out["sequences"] = seq_rows

    # table formats
    out["formats"] = [{"table": f["table_name"], "formats": f["formats"], "limit": FORMAT_LIMIT,
                       "severity": _severity(None, f["formats"] * 100 / FORMAT_LIMIT)} for f in formats[:50]]

    # findings, most urgent first
    findings = []
    if out["size"]["severity"] != "ok":
        findings.append({"key": "size", "severity": out["size"]["severity"],
                         "title": f"The database reaches its space budget in {_fmt_days(days)}" if days is not None
                         else f"The database uses {pct} % of its space budget"})
    if out["transactions"]["severity"] != "ok":
        findings.append({"key": "transactions", "severity": out["transactions"]["severity"],
                         "title": f"The transaction counter reaches its limit in {_fmt_days(tx_days)}"})
    for r in seq_rows:
        if r["severity"] != "ok":
            col = f"{r['table']}.{r['column']} ({r['type']})" if r["table"] else r["type"]
            findings.append({"key": f"seq:{r['name']}", "severity": r["severity"],
                             "title": f"Sequence {r['name']} feeding {col} is at {r['pct']} % of its limit"
                             + (f" and reaches it in {_fmt_days(r['days_left'])}" if r["days_left"] is not None else "")})
    for f in out["formats"]:
        if f["severity"] != "ok":
            findings.append({"key": f"fmt:{f['table']}", "severity": f["severity"],
                             "title": f"Table {f['table']} has used {f['formats']} of its {FORMAT_LIMIT} formats: "
                                      "back up and restore before it can no longer be altered"})
    findings.sort(key=lambda x: 0 if x["severity"] == "critical" else 1)
    out["findings"] = findings
    return out


def _fmt_days(d: float | None) -> str:
    if d is None:
        return "never at the current rate"
    if d < 1:
        return "less than a day"
    if d < 60:
        return f"{int(d)} days"
    if d < 730:
        return f"{int(d / 30.4)} months"
    years = d / 365.25
    return f"{int(years):,} years" if years < 1e6 else "more than a million years"


def check_alerts(server_id: str, db_id: str, result: dict[str, Any]) -> list[str]:
    """Raise ``capacity.forecast`` for findings that are new or got worse; returns their keys."""
    from .. import alerts

    state = _alerted(server_id, db_id)
    now_state = {f["key"]: f["severity"] for f in result["findings"]}
    raised = []
    for f in result["findings"]:
        prev = state.get(f["key"])
        if prev == f["severity"] or (prev == "critical" and f["severity"] == "warning"):
            continue
        alerts.emit("capacity.forecast", f["severity"], f["title"], f["title"] + ".",
                    server_id=server_id, database_id=db_id, key=f"forecast:{server_id}:{db_id}:{f['key']}",
                    details={"finding": f["key"]}, force=True)
        raised.append(f["key"])
    if now_state != state:
        _set_alerted(server_id, db_id, now_state)
    return raised


def after_sample(conn, server_id: str, db_id: str) -> None:
    """Called by the history sampler: an observation an hour, then the alert check. Never raises."""
    try:
        if observe(conn, server_id, db_id) is not None:
            check_alerts(server_id, db_id, analyze(server_id, db_id, conn))
    except Exception:  # noqa: BLE001 - forecasting must not break sampling
        pass
