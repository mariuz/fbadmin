"""Scheduled performance check-up (roadmap 2.4): run the check-up on a cron schedule and alert only on what is
new since the previous run - a finding that appears, gets more severe or names objects it did not name before
(a stuck OAT, selectivity outdated after a load). One *resolved* message follows when the findings are gone.

Optionally the run recomputes the selectivity the check-up finds missing or outdated, the usual fix after a
bulk load."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

from ..alerts import emit
from . import checkup

SELECTIVITY_CODES = ("selectivity_never", "selectivity_outdated")


class CheckupOptions(BaseModel):
    gstat: bool = True  # also read the database with gstat (record versions, actual index selectivity)
    min_severity: Literal["critical", "warning"] = "warning"  # alert on findings at least this severe
    fix_selectivity: bool = False  # recompute missing or outdated selectivity at once
    ignore: list[str] = Field(default_factory=list, max_length=50)  # finding codes never alerted on


def _labels(f: dict) -> list[str]:
    """The objects a finding names (indices, transactions, tables), from its evidence."""
    return sorted({str(e.get("label")) for e in f.get("evidence") or [] if e.get("label")})[:50]


def state(findings: list[dict], opts: CheckupOptions) -> dict[str, dict]:
    """The findings to alert on, by code: ``{code: {severity, labels, title}}``."""
    floor = checkup.SEVERITY[opts.min_severity]
    return {f["code"]: {"severity": f["severity"], "labels": _labels(f), "title": f["text"]["title"]}
            for f in findings if checkup.SEVERITY.get(f["severity"], 0) >= floor and f["code"] not in opts.ignore}


def news(prev: dict[str, dict], cur: dict[str, dict]) -> list[str]:
    """Codes that are new, more severe, or name objects they did not name in the previous run."""
    out = []
    for code, c in cur.items():
        p = prev.get(code)
        if (p is None or checkup.SEVERITY[c["severity"]] > checkup.SEVERITY.get(p.get("severity"), 0)
                or set(c["labels"]) - set(p.get("labels") or [])):
            out.append(code)
    return out


def _fix(server_id: str, db_id: str, findings: list[dict], log) -> list[str]:
    from ..api.deps import get_state
    from ..query import end_transaction

    items: dict[tuple, dict] = {}
    for f in findings:
        if f["code"] in SELECTIVITY_CODES:
            for a in f["actions"]:
                if a["kind"] == "recompute_statistics":
                    for i in a["params"]["indices"]:
                        items[(i.get("schema"), i["name"])] = i
    if not items:
        return []
    log(f"Recomputing the selectivity of {len(items)} index(es)")
    with get_state().manager.meta(server_id, db_id) as conn:
        try:
            return checkup.recompute(conn, list(items.values()), log=log)
        finally:
            end_transaction(conn, commit=False)


def run(s, db, prev: dict | None, log) -> dict[str, Any]:
    """Run the check-up for schedule ``s``; ``prev`` is the schedule's previous history entry. Returns the
    history extras (``status`` is the history status: ``findings`` while there is something to alert on)."""
    opts = s.checkup or CheckupOptions()
    report = checkup.run(s.server_id, s.database_id, with_gstat=opts.gstat, log=log)
    report["schedule"] = {"id": s.id, "name": s.name}
    findings = report["findings"]
    fixed: list[str] = []
    if opts.fix_selectivity and any(f["code"] in SELECTIVITY_CODES for f in findings):
        try:
            fixed = _fix(s.server_id, s.database_id, findings, log)
        except Exception as exc:  # noqa: BLE001 - the findings are still reported
            log(f"Could not recompute the selectivity: {exc}")
        if fixed:
            findings = [f for f in findings if f["code"] not in SELECTIVITY_CODES]
            report["fixed"] = fixed
    cur = state(findings, opts)
    before = (prev or {}).get("alerted") or {}
    new = news(before, cur)
    errors = [c["name"] for c in report["checks"] if c["status"] == "error"]
    if new:
        level = "critical" if any(cur[c]["severity"] == "critical" for c in new) else "warning"
        lines = [f"- {cur[c]['title']} ({cur[c]['severity']})" for c in new]
        still = [c for c in cur if c not in new]
        msg = (f"The scheduled check-up \"{s.name}\" found {len(new)} new finding(s) in {db.name}:\n"
               + "\n".join(lines)
               + (f"\n\n{len(still)} earlier finding(s) are still there." if still else "")
               + (f"\n\nSelectivity recomputed for {len(fixed)} index(es)." if fixed else "")
               + "\n\nOpen Performance check-up… on the database for the evidence and the fixes.")
        emit("checkup.findings", level, f"Check-up of {db.name}: {cur[new[0]]['title']}"
             + (f" and {len(new) - 1} more" if len(new) > 1 else ""), msg,
             server_id=s.server_id, database_id=s.database_id, key=f"checkup:{s.id}",
             details={"schedule_id": s.id, "new": new, "summary": report["summary"], "fixed": fixed},
             force=True)
    elif before and not cur:
        emit("checkup.findings", "resolved", f"Resolved: check-up of {db.name}",
             f"The scheduled check-up \"{s.name}\" finds nothing to report in {db.name} any more.",
             server_id=s.server_id, database_id=s.database_id, key=f"checkup:{s.id}",
             details={"schedule_id": s.id}, force=True)
    log(f"{len(findings)} finding(s), {len(new)} new" + (f", selectivity recomputed for {len(fixed)}" if fixed else ""))
    return {"status": "findings" if cur else "finished", "summary": report["summary"], "alerted": cur,
            "new": new, "fixed": fixed, "check_errors": errors}
