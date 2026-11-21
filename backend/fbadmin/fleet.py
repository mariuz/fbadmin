"""Fleet health (roadmap 2.5): one row per database the user may see, from what fbadmin already keeps.

Opening the page connects to no database: every value comes from fbadmin's own stores (the history sampler,
the last check-up, the forecast, the scheduler's run history, change requests, the alert history, the
continuous trace). A value that was never recorded is ``None`` and shown as such.

Each row gets a ``status`` (``critical`` > ``warning`` > ``ok`` > ``unknown``) from its worst signal, and the
``problems`` that set it, so the table can be sorted and filtered by what needs attention.
"""

from __future__ import annotations

import time
from typing import Any

BACKUP_WARN_HOURS = 36    # newest backup older than this: warning
BACKUP_CRIT_HOURS = 24 * 7
SAMPLE_STALE = 3600       # tracked, but no sample for an hour: warning
RANK = {"critical": 3, "warning": 2, "ok": 1, "unknown": 0}


def _checkup(sid: str, dbid: str, hist: list[dict]) -> dict[str, Any] | None:
    from .monitoring import checkup

    rep = (checkup.last(sid, dbid) or {}).get("report")
    best = None
    if rep:
        best = {"at": rep["generated_at"], **rep["summary"], "source": "scheduled" if rep.get("schedule") else "manual"}
    run = next((h for h in hist if h["kind"] == "checkup" and h["status"] in ("findings", "finished")), None)
    if run and run.get("summary") and (best is None or (run.get("finished_at") or 0) > best["at"]):
        best = {"at": run.get("finished_at"), **run["summary"], "source": "scheduled"}
    return best


def _backup(hist: list[dict], now: float) -> dict[str, Any] | None:
    ok = next((h for h in hist if h["kind"] == "backup" and h["status"] == "finished"), None)
    failed = next((h for h in hist if h["kind"] == "backup" and h["status"] == "failed"), None)
    if not ok and not failed:
        return None
    at = ok.get("finished_at") if ok else None
    return {"at": at, "age_hours": round((now - at) / 3600, 1) if at else None, "file": (ok or {}).get("file"),
            "last_failed_at": failed.get("finished_at") if failed and (not ok or failed["finished_at"] > ok["finished_at"])
            else None, "error": failed.get("error") if failed and (not ok or failed["finished_at"] > ok["finished_at"])
            else None}


def _drift(hist: list[dict]) -> dict[str, Any] | None:
    run = next((h for h in hist if h["kind"] == "schema_drift" and h["status"] in ("drift", "finished", "failed")), None)
    if not run:
        return None
    return {"at": run.get("finished_at"), "drift": run["status"] == "drift", "failed": run["status"] == "failed",
            "summary": run.get("summary")}


def row(sid: str, srv, db, *, hist: list[dict], changes: list, alerts: list[dict], now: float) -> dict[str, Any]:
    from .monitoring import forecast, history, trace_capture

    tracked = history.get_store().is_tracked(sid, db.id)
    cfg = trace_capture.config(sid, db.id)
    r: dict[str, Any] = {
        "server_id": sid, "server": srv.name, "database_id": db.id, "database": db.name,
        "history": {"tracked": bool(tracked), "last_sample": (tracked or {}).get("last_sample"),
                    "error": (tracked or {}).get("last_error")},
        "checkup": _checkup(sid, db.id, hist),
        "forecast": forecast.summary(sid, db.id),
        "backup": _backup(hist, now),
        "drift": _drift(hist),
        "changes": {"open": sum(1 for c in changes if c.status == "open"),
                    "approved": sum(1 for c in changes if c.status == "approved"),
                    "failed": sum(1 for c in changes if c.status == "failed")},
        "trace": {"enabled": cfg["enabled"], **{k: v for k, v in trace_capture.status(sid, db.id).items()
                                                if k in ("state", "error", "captured")}},
        "alerts_24h": {"total": len(alerts), "critical": sum(1 for a in alerts if a["level"] == "critical"),
                       "last": alerts[0]["title"] if alerts else None},
        "schedules": sorted({h["kind"] for h in hist}),
    }
    problems: list[tuple[str, str]] = []
    c = r["checkup"]
    if c and c.get("critical"):
        problems.append(("critical", f"check-up: {c['critical']} critical finding(s)"))
    elif c and c.get("warning"):
        problems.append(("warning", f"check-up: {c['warning']} warning(s)"))
    f = r["forecast"]
    if f and f["severity"] in ("critical", "warning"):
        problems.append((f["severity"], f"forecast: {f['title']}"))
    b = r["backup"]
    if b and b.get("error"):
        problems.append(("critical", "the last backup failed"))
    elif b and b.get("age_hours") is not None and b["age_hours"] > BACKUP_CRIT_HOURS:
        problems.append(("critical", f"newest backup {round(b['age_hours'] / 24)} days old"))
    elif b and b.get("age_hours") is not None and b["age_hours"] > BACKUP_WARN_HOURS:
        problems.append(("warning", f"newest backup {round(b['age_hours'])} h old"))
    d = r["drift"]
    if d and d["drift"]:
        problems.append(("warning", "schema drift"))
    if r["changes"]["failed"]:
        problems.append(("warning", f"{r['changes']['failed']} change request(s) failed"))
    h = r["history"]
    if h["tracked"] and h["error"]:
        problems.append(("warning", f"sampling fails: {h['error']}"))
    elif h["tracked"] and h["last_sample"] and now - h["last_sample"] > SAMPLE_STALE:
        problems.append(("warning", "no recent history sample"))
    if r["trace"]["enabled"] and r["trace"]["state"] != "running":
        problems.append(("warning", "the continuous trace is not running"))
    if r["alerts_24h"]["critical"]:
        problems.append(("critical", f"{r['alerts_24h']['critical']} critical alert(s) in 24 h"))
    known = any(r[k] for k in ("checkup", "forecast", "backup", "drift")) or h["tracked"]
    status = max((p[0] for p in problems), key=RANK.get, default="ok" if known else "unknown")
    r["status"] = status
    r["problems"] = [p[1] for p in sorted(problems, key=lambda p: -RANK[p[0]])]
    return r


def fleet(now: float | None = None) -> dict[str, Any]:
    from . import changes as CR
    from .alerts.engine import get_engine
    from .api.deps import get_state
    from .links import may_read
    from .maintenance.scheduler import get_scheduler

    now = now or time.time()
    st = get_state()
    sch = get_scheduler()
    with sch._lock:
        by_db: dict[tuple[str, str], list[str]] = {}
        for s in sch.schedules.values():
            by_db.setdefault((s.server_id, s.database_id), []).append(s.id)
        history = list(reversed(sch.history))  # newest first
    reqs = CR.get_store().all()
    try:
        recent = [a for a in get_engine().recent(2000) if a.get("time", 0) >= now - 86400]
    except Exception:  # noqa: BLE001
        recent = []
    rows = []
    for srv in st.store.list():
        for db in srv.databases:
            if not may_read(srv.id, db.id):
                continue
            ids = set(by_db.get((srv.id, db.id), []))
            hist = [h for h in history if h.get("schedule_id") in ids]
            rows.append(row(srv.id, srv, db, hist=hist, now=now,
                            changes=[c for c in reqs if (c.server_id, c.database_id) == (srv.id, db.id)],
                            alerts=[a for a in recent if (a.get("server_id"), a.get("database_id")) == (srv.id, db.id)]))
    rows.sort(key=lambda r: (-RANK[r["status"]], r["server"].lower(), r["database"].lower()))
    counts = {k: sum(1 for r in rows if r["status"] == k) for k in RANK}
    return {"generated_at": now, "databases": rows, "counts": counts,
            "thresholds": {"backup_warn_hours": BACKUP_WARN_HOURS, "backup_crit_hours": BACKUP_CRIT_HOURS}}
