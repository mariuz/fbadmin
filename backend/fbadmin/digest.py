"""Weekly digest per database (roadmap 2.5): one message on a schedule with what happened to a database over the
period: its status, the check-up, the capacity forecast, query regressions, backups, change requests, the
errors the continuous trace saw, alerts and comments. Sent to e-mail, Slack or webhook channels (a ``digest``
schedule, as the user who scheduled it); e-mails also carry the digest as a Markdown file.

Nothing connects to the database: like the fleet page, the digest reads fbadmin's own records.
"""

from __future__ import annotations

import time
from typing import Any

from pydantic import BaseModel, Field

SLACK_CHARS = 3500


class DigestOptions(BaseModel):
    channels: list[str] = Field(default_factory=list, max_length=20)
    days: int = Field(default=7, ge=1, le=31)  # the period the digest covers


def _when(ts: float | None) -> str:
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(ts)) if ts else "never"


def _size(n: float | None) -> str:
    if n is None:
        return "–"
    for unit in ("bytes", "KB", "MB", "GB", "TB"):
        if abs(n) < 1024 or unit == "TB":
            return f"{n:.0f} {unit}" if unit == "bytes" else f"{n:.1f} {unit}"
        n /= 1024
    return str(n)


def collect(server_id: str, db_id: str, days: int, now: float | None = None) -> dict[str, Any]:
    """The facts of the digest (also the JSON of the run's history)."""
    from . import changes as CR
    from . import comments as CM
    from . import fleet
    from .alerts.engine import get_engine
    from .api.deps import get_state
    from .maintenance.scheduler import get_scheduler
    from .monitoring import forecast, history, regressions, trace_errors

    now = now or time.time()
    since = now - days * 86400
    st = get_state()
    srv = st.store.get(server_id)
    db = st.store.get_database(server_id, db_id)
    sch = get_scheduler()
    with sch._lock:
        ids = {s.id for s in sch.schedules.values() if (s.server_id, s.database_id) == (server_id, db_id)}
        hist = [h for h in reversed(sch.history) if h.get("schedule_id") in ids]
    reqs = [c for c in CR.get_store().all() if (c.server_id, c.database_id) == (server_id, db_id)]
    try:
        alerts = [a for a in get_engine().recent(5000) if (a.get("server_id"), a.get("database_id")) == (server_id, db_id)
                  and a.get("time", 0) >= since]
    except Exception:  # noqa: BLE001
        alerts = []
    row = fleet.row(server_id, srv, db, hist=hist, changes=reqs,
                    alerts=[a for a in alerts if a.get("time", 0) >= now - 86400], now=now)
    period = [h for h in hist if (h.get("finished_at") or 0) >= since]
    checkups = [h for h in period if h["kind"] == "checkup"]
    backups = [h for h in period if h["kind"] == "backup"]
    obs = [o for o in forecast.observations(server_id, db_id) if o["ts"] >= since]
    size = [o["pages"] * o["page_size"] for o in obs if o.get("pages") and o.get("page_size")]
    store = history.get_store()
    regs = []
    if store.is_tracked(server_id, db_id):
        try:
            regs = regressions.analyze(store, server_id, db_id, store.get_settings(), only="regressions")
        except Exception:  # noqa: BLE001
            regs = []
    errs = trace_errors.groups(server_id, db_id, hours=days * 24, now=now)
    by_event: dict[str, int] = {}
    for a in alerts:
        by_event[a["event"]] = by_event.get(a["event"], 0) + 1
    new_comments = [c for c in CM.get_store().items.values() if c.created_at >= since and (
        (c.target.server_id, c.target.database_id) == (server_id, db_id))]
    return {
        "server": srv.name, "database": db.name, "days": days, "since": since, "until": now,
        "status": row["status"], "problems": row["problems"],
        "checkup": row["checkup"], "checkup_runs": len(checkups),
        "checkup_new": sorted({t for h in checkups for c, a in (h.get("alerted") or {}).items()
                               if c in (h.get("new") or []) for t in [a["title"]]}),
        "forecast": row["forecast"],
        "size": {"start": size[0] if size else None, "end": size[-1] if size else None},
        "regressions": [{"sql": " ".join((r.get("normalized") or "").split())[:160], "ratio": r.get("ratio")}
                        for r in regs[:5]], "regressions_total": len(regs),
        "backups": {"ok": sum(1 for h in backups if h["status"] == "finished"),
                    "failed": sum(1 for h in backups if h["status"] == "failed"), "newest": row["backup"]},
        "changes": {"proposed": sum(1 for c in reqs if c.created_at >= since),
                    "applied": sum(1 for c in reqs if c.status == "applied" and (c.applied or {}).get("at", 0) >= since),
                    "open": row["changes"]["open"] + row["changes"]["approved"], "failed": row["changes"]["failed"]},
        "errors": {"total": errs["total"], "lock": errs["lock"],
                   "top": [{"message": g["message"], "count": g["count"], "new": g["new"]} for g in errs["groups"][:3]]},
        "alerts": {"total": len(alerts), "by_event": by_event},
        "comments": len(new_comments),
    }


def render(d: dict[str, Any]) -> str:
    """The digest as Markdown (plain enough for Slack and e-mail)."""
    out = [f"# {d['database']} ({d['server']}): the last {d['days']} day(s)", "",
           f"Status: **{d['status']}**" + (" - " + "; ".join(d["problems"]) if d["problems"] else ""), ""]
    c = d["checkup"]
    if c:
        out.append(f"- Check-up ({_when(c['at'])}): {c.get('critical', 0)} critical, {c.get('warning', 0)} warning(s), "
                   f"{c.get('info', 0)} note(s); {d['checkup_runs']} scheduled run(s) in the period")
        for title in d["checkup_new"][:5]:
            out.append(f"  - new: {title}")
    else:
        out.append("- Check-up: never run")
    f = d["forecast"]
    size = d["size"]
    grow = ""
    if size["start"] is not None and size["end"] is not None:
        grow = f"; size {_size(size['end'])} ({'+' if size['end'] >= size['start'] else ''}{_size(size['end'] - size['start'])})"
    out.append(f"- Capacity: {f['title'] if f and f.get('title') else 'no limit near' if f else 'no forecast yet'}{grow}")
    out.append(f"- Query regressions: {d['regressions_total']}")
    for r in d["regressions"]:
        out.append(f"  - {r['sql']}" + (f" ({r['ratio']}x slower)" if r.get("ratio") else ""))
    b = d["backups"]
    newest = b["newest"]
    out.append(f"- Backups: {b['ok']} made, {b['failed']} failed; newest "
               + (_when(newest.get("at")) if newest and newest.get("at") else "none"))
    ch = d["changes"]
    out.append(f"- Change requests: {ch['proposed']} proposed, {ch['applied']} applied, {ch['open']} open"
               + (f", {ch['failed']} failed" if ch["failed"] else ""))
    e = d["errors"]
    out.append(f"- Errors seen by the trace: {e['total']} ({e['lock']} lock conflict(s))")
    for g in e["top"]:
        out.append(f"  - {g['count']} x {g['message']}" + (" (new)" if g["new"] else ""))
    a = d["alerts"]
    out.append(f"- Alerts: {a['total']}" + (" (" + ", ".join(f"{k}: {v}" for k, v in sorted(a["by_event"].items())) + ")"
                                            if a["by_event"] else ""))
    out.append(f"- Comments: {d['comments']} new")
    return "\n".join(out) + "\n"


def send(schedule, log) -> dict[str, Any]:
    """The scheduler's entry point for kind ``digest``: collect, render, deliver."""
    import uuid

    from .alerts import channels as ch
    from .alerts.store import get_store as alert_store

    opts = schedule.digest or DigestOptions()
    d = collect(schedule.server_id, schedule.database_id, opts.days)
    text = render(d)
    log(f"Digest of {d['database']} over {opts.days} day(s): status {d['status']}")
    now = time.time()
    alert = {
        "id": uuid.uuid4().hex[:12], "event": "database.digest",
        "level": {"critical": "critical", "warning": "warning"}.get(d["status"], "info"),
        "title": f"Digest: {d['database']} ({d['server']}), the last {opts.days} day(s)",
        "message": text if len(text) <= SLACK_CHARS else text[:SLACK_CHARS] + "\n... (truncated: see the attached digest)",
        "server_id": schedule.server_id, "database_id": schedule.database_id, "server": d["server"],
        "database": d["database"], "time": now, "time_text": time.strftime("%Y-%m-%d %H:%M:%S %Z", time.localtime(now)),
        "rule": schedule.name, "details": d, "attachments": [(f"digest-{d['database']}.md", "text/markdown",
                                                              text.encode("utf-8"))],
    }
    store = alert_store()
    sent, errors = [], []
    for cid in opts.channels:
        c = store.channels.get(cid)
        if c is None or not c.enabled:
            errors.append(f"{cid}: no such channel, or it is disabled")
            continue
        try:
            ch.send(c, alert)
            sent.append(c.name)
            log(f"Sent to {c.name}")
        except Exception as exc:  # noqa: BLE001 - one channel failing must not stop the others
            errors.append(f"{c.name}: {' '.join(str(exc).split())}")
            log(f"Could not send to {c.name}: {exc}")
    if not opts.channels:
        log("No channel: the digest is kept in the run's history only")
    return {"status_of_db": d["status"], "sent": sent, "delivery_errors": errors, "digest": text[:20000]}
