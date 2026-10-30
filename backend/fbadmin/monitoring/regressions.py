"""Query regression tracking (roadmap 2.2): slow statements and their plans over time, and the ones that got worse.

Observations come from the history sampler (statements running longer than the slow-statement threshold, with
``MON$EXPLAINED_PLAN``; one observation per execution, the longest duration seen) and from the Query Tool
(statements of tracked databases slower than ``regression_query_ms``; their plan is explained in the
background). Statements are grouped by a fingerprint of their text: literals become ``?``, comments and
whitespace go, and case is folded outside quoted names, so ``WHERE ID = 7`` and ``where id=8`` are one
statement.

A statement **regressed** when, compared with its baseline (the runs of the previous ``baseline_days``, before
the recent ``recent_hours``, and after it was last accepted):

* it is **slower**: the median of its recent runs is at least ``factor`` times the baseline median and at
  least ``min_seconds`` more (both with ``min_runs`` runs); or
* its **plan changed** (the latest plan differs from the baseline's usual plan) and it is slower with it.

A changed plan that isn't slower is shown, not flagged. *Accept* makes the current behaviour the new
baseline. The ``query.regression`` alert is sent when a statement regresses (again when its regression
changes) and once when it is gone.
"""

from __future__ import annotations

import hashlib
import re
import statistics
import threading
import time
from collections import Counter
from typing import Any

DEFAULTS: dict[str, Any] = {
    "regression_factor": 2.0,
    "regression_recent_hours": 24,
    "regression_baseline_days": 14,
    "regression_min_runs": 3,
    "regression_min_seconds": 0.5,
    "regression_query_ms": 200,
    "regression_keep_days": 30,
}
MAX_RUNS = 2000  # kept per statement
_INLIST = re.compile(r"\bin\s*\(\s*\?(?:\s*,\s*\?)+\s*\)")
_init_lock = threading.Lock()


# ------------------------------------------------------------------ fingerprints
def normalize(sql: str) -> str:
    """The statement's shape: literals and parameters as ``?``, no comments, single spaces, lower case outside
    quoted names, ``IN (?, ?, ?)`` as ``IN (?)``."""
    s = sql.strip().rstrip(";").strip()
    out: list[str] = []
    i, n = 0, len(s)

    def space() -> None:
        if out and out[-1] != " ":
            out.append(" ")

    while i < n:
        c = s[i]
        if c in "'\"":  # a string literal, or a quoted name (kept as written)
            j = i + 1
            while j < n:
                if s[j] == c:
                    if j + 1 < n and s[j + 1] == c:  # doubled quote
                        j += 2
                        continue
                    break
                j += 1
            out.append("?" if c == "'" else s[i:j + 1])
            i = j + 1
        elif s.startswith("--", i):
            j = s.find("\n", i)
            i = n if j < 0 else j + 1
            space()
        elif s.startswith("/*", i):
            j = s.find("*/", i + 2)
            i = n if j < 0 else j + 2
            space()
        elif c.isspace():
            space()
            i += 1
        elif c.isdigit() and (i == 0 or not (s[i - 1].isalnum() or s[i - 1] in "_$")):
            j = i
            while j < n and (s[j].isdigit() or s[j] == "."):
                j += 1
            if j < n and s[j] in "eE" and j + 1 < n and (s[j + 1].isdigit() or s[j + 1] in "+-"):
                j += 2
                while j < n and s[j].isdigit():
                    j += 1
            out.append("?")
            i = j
        elif c == ":" and i + 1 < n and (s[i + 1].isalpha() or s[i + 1] == "_"):
            j = i + 1
            while j < n and (s[j].isalnum() or s[j] in "_$"):
                j += 1
            out.append("?")
            i = j
        else:
            out.append(c.lower())
            i += 1
    norm = "".join(out).strip()
    # spacing inside brackets, before an opening one and around commas and comparisons doesn't matter
    norm = re.sub(r"\s*([,=<>])\s*", r"\1", norm)
    norm = re.sub(r"\(\s+", "(", re.sub(r"\s+\)", ")", re.sub(r"\s+\(", "(", norm)))
    return _INLIST.sub("in(?)", norm)


def sql_hash(sql: str) -> str:
    return hashlib.sha1(normalize(sql).encode("utf-8")).hexdigest()[:20]


def plan_hash(plan: str | None) -> str | None:
    if not plan or not plan.strip():
        return None
    norm = "\n".join(line.strip() for line in plan.strip().splitlines() if line.strip())
    return hashlib.sha1(norm.encode("utf-8")).hexdigest()[:16]


# ------------------------------------------------------------------ storage (in the history store's SQLite file)
def _ensure(store) -> None:
    if getattr(store, "_regressions_ready", False):
        return
    with _init_lock, store._lock:
        store._db.executescript(
            """
            CREATE TABLE IF NOT EXISTS stmt_texts (server_id TEXT NOT NULL, db_id TEXT NOT NULL, sql_hash TEXT NOT NULL,
                normalized TEXT, example TEXT, first_seen REAL, last_seen REAL, accepted_at REAL, alerted TEXT,
                PRIMARY KEY (server_id, db_id, sql_hash));
            CREATE TABLE IF NOT EXISTS stmt_runs (server_id TEXT NOT NULL, db_id TEXT NOT NULL, sql_hash TEXT NOT NULL,
                ts REAL NOT NULL, seconds REAL NOT NULL, plan_hash TEXT, source TEXT, exec_key TEXT);
            CREATE INDEX IF NOT EXISTS stmt_runs_db ON stmt_runs (server_id, db_id, sql_hash, ts);
            CREATE UNIQUE INDEX IF NOT EXISTS stmt_runs_exec ON stmt_runs (server_id, db_id, exec_key)
                WHERE exec_key IS NOT NULL;
            CREATE TABLE IF NOT EXISTS stmt_plans (server_id TEXT NOT NULL, db_id TEXT NOT NULL, sql_hash TEXT NOT NULL,
                plan_hash TEXT NOT NULL, plan TEXT, first_seen REAL, last_seen REAL,
                PRIMARY KEY (server_id, db_id, sql_hash, plan_hash));
            """
        )
        store._regressions_ready = True


def record(store, server_id: str, db_id: str, sql: str, seconds: float, *, plan: str | None = None,
           source: str = "sampler", exec_key: str | None = None, ts: float | None = None) -> str:
    """Store one execution (``exec_key``: the same execution seen again only raises its duration)."""
    _ensure(store)
    now = ts or time.time()
    h = sql_hash(sql)
    ph = plan_hash(plan)
    with store._lock:
        db = store._db
        db.execute("INSERT INTO stmt_texts (server_id, db_id, sql_hash, normalized, example, first_seen, last_seen) "
                   "VALUES (?, ?, ?, ?, ?, ?, ?) ON CONFLICT (server_id, db_id, sql_hash) DO UPDATE SET "
                   "last_seen = MAX(last_seen, excluded.last_seen)",
                   (server_id, db_id, h, normalize(sql)[:20000], sql[:20000], now, now))
        if ph:
            db.execute("INSERT INTO stmt_plans (server_id, db_id, sql_hash, plan_hash, plan, first_seen, last_seen) "
                       "VALUES (?, ?, ?, ?, ?, ?, ?) ON CONFLICT (server_id, db_id, sql_hash, plan_hash) DO UPDATE SET "
                       "last_seen = MAX(last_seen, excluded.last_seen)", (server_id, db_id, h, ph, plan, now, now))
        if exec_key:
            cur = db.execute("UPDATE stmt_runs SET seconds = MAX(seconds, ?), plan_hash = COALESCE(plan_hash, ?) "
                             "WHERE server_id = ? AND db_id = ? AND exec_key = ?", (seconds, ph, server_id, db_id, exec_key))
            if cur.rowcount:
                return h
        db.execute("INSERT INTO stmt_runs (server_id, db_id, sql_hash, ts, seconds, plan_hash, source, exec_key) "
                   "VALUES (?, ?, ?, ?, ?, ?, ?, ?)", (server_id, db_id, h, now, seconds, ph, source, exec_key))
    return h


def set_plan(store, server_id: str, db_id: str, h: str, ts: float, plan: str | None) -> None:
    """The plan of a Query Tool run, explained after it ran."""
    ph = plan_hash(plan)
    if not ph:
        return
    _ensure(store)
    with store._lock:
        store._db.execute("INSERT INTO stmt_plans (server_id, db_id, sql_hash, plan_hash, plan, first_seen, last_seen) "
                          "VALUES (?, ?, ?, ?, ?, ?, ?) ON CONFLICT (server_id, db_id, sql_hash, plan_hash) DO UPDATE "
                          "SET last_seen = MAX(last_seen, excluded.last_seen)", (server_id, db_id, h, ph, plan, ts, ts))
        store._db.execute("UPDATE stmt_runs SET plan_hash = ? WHERE server_id = ? AND db_id = ? AND sql_hash = ? "
                          "AND ts = ? AND plan_hash IS NULL", (ph, server_id, db_id, h, ts))


def prune(store, keep_days: float) -> None:
    _ensure(store)
    cutoff = time.time() - keep_days * 86400
    with store._lock:
        db = store._db
        db.execute("DELETE FROM stmt_runs WHERE ts < ?", (cutoff,))
        db.execute("DELETE FROM stmt_runs WHERE rowid IN (SELECT rowid FROM (SELECT rowid, ROW_NUMBER() OVER "
                   "(PARTITION BY server_id, db_id, sql_hash ORDER BY ts DESC) AS n FROM stmt_runs) WHERE n > ?)",
                   (MAX_RUNS,))
        db.execute("DELETE FROM stmt_texts WHERE last_seen < ?", (cutoff,))
        db.execute("DELETE FROM stmt_plans WHERE last_seen < ?", (cutoff,))


def forget(store, server_id: str, db_id: str) -> None:
    _ensure(store)
    with store._lock:
        for t in ("stmt_runs", "stmt_texts", "stmt_plans"):
            store._db.execute(f"DELETE FROM {t} WHERE server_id = ? AND db_id = ?", (server_id, db_id))


def accept(store, server_id: str, db_id: str, h: str, now: float | None = None) -> None:
    _ensure(store)
    with store._lock:
        cur = store._db.execute("UPDATE stmt_texts SET accepted_at = ? WHERE server_id = ? AND db_id = ? AND sql_hash = ?",
                                (now or time.time(), server_id, db_id, h))
        if not cur.rowcount:
            raise KeyError(f"Unknown statement {h}")


# ------------------------------------------------------------------ analysis
def _median(xs: list[float]) -> float | None:
    return statistics.median(xs) if xs else None


def analyze(store, server_id: str, db_id: str, s: dict[str, Any], *, now: float | None = None,
            only: str | None = None) -> list[dict[str, Any]]:
    """Every statement with its baseline and recent runs, its plans and its flags; regressions first.
    ``only``: "regressions" or "flagged" (regressions and changed plans)."""
    _ensure(store)
    now = now or time.time()
    recent_start = now - float(s["regression_recent_hours"]) * 3600
    base_start = recent_start - float(s["regression_baseline_days"]) * 86400
    with store._lock:
        texts = {r[0]: r for r in store._db.execute(
            "SELECT sql_hash, normalized, example, first_seen, last_seen, accepted_at, alerted FROM stmt_texts "
            "WHERE server_id = ? AND db_id = ?", (server_id, db_id))}
        runs = store._db.execute("SELECT sql_hash, ts, seconds, plan_hash FROM stmt_runs WHERE server_id = ? AND "
                                 "db_id = ? AND ts >= ? ORDER BY ts", (server_id, db_id, base_start)).fetchall()
    by: dict[str, list[tuple]] = {}
    for r in runs:
        by.setdefault(r[0], []).append(r)
    out = []
    factor, min_runs, min_s = float(s["regression_factor"]), int(s["regression_min_runs"]), float(s["regression_min_seconds"])
    for h, rs in by.items():
        t = texts.get(h)
        if not t:
            continue
        accepted = t[5] or 0
        base = [r for r in rs if max(base_start, accepted) <= r[1] < recent_start]
        recent = [r for r in rs if r[1] >= max(recent_start, accepted)]
        bm, rm = _median([r[2] for r in base]), _median([r[2] for r in recent])
        base_plans = Counter(r[3] for r in base if r[3])
        base_plan = base_plans.most_common(1)[0][0] if base_plans else None
        recent_plan = next((r[3] for r in reversed(recent) if r[3]), None)
        plan_changed = bool(base_plan and recent_plan and recent_plan != base_plan)
        slower = (len(base) >= min_runs and len(recent) >= min_runs and bm is not None and rm is not None
                  and rm >= factor * bm and rm - bm >= min_s)
        worse_plan = plan_changed and bm is not None and rm is not None and rm > bm * 1.2 and rm - bm >= min_s
        flags = [f for f, on in (("slower", slower), ("plan_changed", plan_changed)) if on]
        regression = slower or worse_plan
        item = {"sql_hash": h, "normalized": t[1], "example": t[2], "first_seen": t[3], "last_seen": t[4],
                "accepted_at": t[5], "runs": len(rs), "baseline_runs": len(base), "recent_runs": len(recent),
                "baseline_median": round(bm, 3) if bm is not None else None,
                "recent_median": round(rm, 3) if rm is not None else None,
                "ratio": round(rm / bm, 2) if bm and rm is not None else None,
                "max_seconds": round(max(r[2] for r in rs), 3), "baseline_plan": base_plan, "recent_plan": recent_plan,
                "flags": flags, "regression": regression, "alerted": t[6]}
        if only == "regressions" and not regression or only == "flagged" and not flags:
            continue
        out.append(item)
    out.sort(key=lambda i: (not i["regression"], "plan_changed" not in i["flags"], -(i["ratio"] or 0), -i["max_seconds"]))
    return out


def detail(store, server_id: str, db_id: str, h: str, s: dict[str, Any], limit: int = 500) -> dict[str, Any]:
    _ensure(store)
    items = [i for i in analyze(store, server_id, db_id, s) if i["sql_hash"] == h]
    with store._lock:
        t = store._db.execute("SELECT normalized, example, first_seen, last_seen, accepted_at FROM stmt_texts WHERE "
                              "server_id = ? AND db_id = ? AND sql_hash = ?", (server_id, db_id, h)).fetchone()
        if not t:
            raise KeyError(f"Unknown statement {h}")
        runs = store._db.execute("SELECT ts, seconds, plan_hash, source FROM stmt_runs WHERE server_id = ? AND db_id = ? "
                                 "AND sql_hash = ? ORDER BY ts DESC LIMIT ?", (server_id, db_id, h, limit)).fetchall()
        plans = store._db.execute("SELECT plan_hash, plan, first_seen, last_seen FROM stmt_plans WHERE server_id = ? AND "
                                  "db_id = ? AND sql_hash = ? ORDER BY first_seen", (server_id, db_id, h)).fetchall()
    per_plan: dict[str, list[float]] = {}
    for _ts, sec, ph, _src in runs:
        if ph:
            per_plan.setdefault(ph, []).append(sec)
    return {"sql_hash": h, "normalized": t[0], "example": t[1], "first_seen": t[2], "last_seen": t[3], "accepted_at": t[4],
            "analysis": items[0] if items else None,
            "runs": [{"ts": r[0], "seconds": r[1], "plan_hash": r[2], "source": r[3]} for r in reversed(runs)],
            "plans": [{"plan_hash": p[0], "plan": p[1], "first_seen": p[2], "last_seen": p[3],
                       "runs": len(per_plan.get(p[0], [])), "median": round(_median(per_plan.get(p[0], [])) or 0, 3)
                       if per_plan.get(p[0]) else None} for p in plans]}


# ------------------------------------------------------------------ alerts
def _signature(i: dict[str, Any]) -> str:
    return f"{i['recent_plan'] or '-'}|{','.join(i['flags'])}"


def check_alerts(store, server_id: str, db_id: str, s: dict[str, Any], db_name: str) -> list[dict[str, Any]]:
    """Alert for statements that regressed (or regressed differently), and once when a regression is gone."""
    from .. import alerts

    items = analyze(store, server_id, db_id, s)
    sent = []
    for i in items:
        if i["regression"]:
            sig = _signature(i)
            if i["alerted"] == sig:
                continue
            what = []
            if "slower" in i["flags"]:
                what.append(f"{i['ratio']}x slower (median {i['recent_median']} s, was {i['baseline_median']} s)")
            if "plan_changed" in i["flags"]:
                what.append("with a different plan")
            alerts.emit("query.regression", "warning", f"Query regression in {db_name}",
                        f"A statement in {db_name} got worse: {' '.join(what)}. {i['normalized'][:300]}",
                        server_id=server_id, database_id=db_id, key=f"regression:{server_id}:{db_id}:{i['sql_hash']}",
                        details={k: i[k] for k in ("sql_hash", "ratio", "baseline_median", "recent_median", "flags")},
                        force=True)
            _set_alerted(store, server_id, db_id, i["sql_hash"], sig)
            sent.append(i)
        elif i["alerted"]:
            alerts.emit("query.regression", "resolved", f"Resolved: query regression in {db_name}",
                        f"The statement is back to normal in {db_name}: {i['normalized'][:300]}",
                        server_id=server_id, database_id=db_id, key=f"regression:{server_id}:{db_id}:{i['sql_hash']}",
                        details={"sql_hash": i["sql_hash"]}, force=True)
            _set_alerted(store, server_id, db_id, i["sql_hash"], None)
    return sent


def _set_alerted(store, server_id: str, db_id: str, h: str, sig: str | None) -> None:
    with store._lock:
        store._db.execute("UPDATE stmt_texts SET alerted = ? WHERE server_id = ? AND db_id = ? AND sql_hash = ?",
                          (sig, server_id, db_id, h))


# ------------------------------------------------------------------ the Query Tool
_queue: list[tuple] = []
_qlock = threading.Lock()
_worker: threading.Thread | None = None
_RECORDED = re.compile(r"^\s*(?:select|with|update|delete|insert|merge|update\s+or|execute\s+procedure)\b", re.I)


def after_sql(server_id: str, db_id: str, result: dict[str, Any]) -> None:
    """Record the Query Tool's slow statements of a tracked database; their plans are explained afterwards."""
    try:
        from . import history

        store = history.get_store()
        if not store.is_tracked(server_id, db_id):
            return
        s = store.get_settings()
        min_ms = float(s["regression_query_ms"])
        now = time.time()
        for item in result.get("results") or []:
            sql = item.get("sql") or ""
            if item.get("kind") not in ("select", "command") or (item.get("elapsed_ms") or 0) < min_ms:
                continue
            if not _RECORDED.match(sql):
                continue
            h = record(store, server_id, db_id, sql, item["elapsed_ms"] / 1000, plan=item.get("plan"),
                       source="query_tool", ts=now)
            if not item.get("plan"):
                _explain_later(server_id, db_id, h, now, sql)
    except Exception:  # noqa: BLE001 - never breaks the Query Tool
        pass


def _explain_later(server_id: str, db_id: str, h: str, ts: float, sql: str) -> None:
    global _worker
    with _qlock:
        if len(_queue) < 200:
            _queue.append((server_id, db_id, h, ts, sql))
        if _worker is None or not _worker.is_alive():
            _worker = threading.Thread(target=_explain_worker, name="fbadmin-regressions", daemon=True)
            _worker.start()


def _explain_worker() -> None:
    from ..api.deps import get_state
    from ..query import end_transaction, explain
    from ..sqlsplit import bind_params
    from . import history

    while True:
        with _qlock:
            if not _queue:
                return
            server_id, db_id, h, ts, sql = _queue.pop(0)
        try:
            bound, _ = bind_params(sql.strip().rstrip(";"), [0])
            with get_state().manager.meta(server_id, db_id) as conn:
                try:
                    plan = explain(conn, bound, detailed=True)
                except Exception:  # noqa: BLE001 - Firebird 2.5-style plan
                    plan = explain(conn, bound)
                end_transaction(conn, commit=False)
            set_plan(history.get_store(), server_id, db_id, h, ts, plan)
        except Exception:  # noqa: BLE001
            pass


def wait_idle(timeout: float = 10) -> None:
    """Tests: until queued plans are explained."""
    end = time.time() + timeout
    while time.time() < end:
        with _qlock:
            busy = bool(_queue) or (_worker is not None and _worker.is_alive())
        if not busy:
            return
        time.sleep(0.05)
