"""Scheduled notebooks and reports (roadmap 2.4): run a notebook's SQL cells on a schedule and send the results.

A schedule of kind ``notebook`` runs as the user who scheduled it (``security/runas.py``), so every statement is
checked against that user's fbadmin permissions exactly as the Query Tool's are (``security.access``): the
server and database must be visible to them, each statement needs its level, hidden objects stay hidden. A
read-only server refuses statements that change data, and so does the schedule itself unless *read only* is
turned off. In server mode, the statements are written to the audit log.

The SQL cells run in order on a fresh attachment, each statement committed (as in a notebook), and the run
stops at the first error. Then:

* the results are saved in the notebook (when its owner, or an administrator, scheduled it), so it shows the
  latest run;
* a report goes to the chosen alert channels (e-mail, Slack, webhook): a summary, the notebook as Markdown
  (notes, SQL, results as tables) and, by e-mail, the report and each result as attachments (Markdown, CSV).
  ``send`` decides when: ``always``, only when a result has ``rows`` (a "tell me when this finds something"
  report), or only on ``error``.
"""

from __future__ import annotations

import csv
import io
import time
from typing import Any, Literal

from pydantic import BaseModel, Field

MAX_ROWS = 1000
SLACK_CHARS = 3500
SUMMARY_ROWS = 5


class NotebookOptions(BaseModel):
    notebook_id: str = Field(min_length=1, max_length=64)
    channels: list[str] = Field(default_factory=list, max_length=20)   # alert channel ids
    send: Literal["always", "rows", "error"] = "always"
    read_only: bool = True        # refuse statements that change data or metadata
    save_results: bool = True     # keep the run's results in the notebook
    attach_csv: bool = True       # e-mail: one CSV per result


def actor():
    from .library import Actor

    try:
        from .security.sessions import current_principal

        p = current_principal.get()
    except Exception:  # noqa: BLE001
        p = None
    return Actor() if p is None else Actor(user_id=p.user_id, username=p.username, is_admin=p.is_admin)


def check_statements(server_id: str, db_id: str, sql: str, read_only: bool) -> None:
    """Refuse the cell like the Query Tool would refuse it for the schedule's owner."""
    from .api.deps import get_state
    from .security import readonly
    from .security.access import READ, Need, _check_need

    st = get_state()
    writes = readonly.write_statements(sql)
    if writes and read_only:
        raise PermissionError("The schedule is read only, and this cell changes data: "
                              + " ".join(writes[0].split())[:120])
    srv = st.store.get(server_id)
    if writes and getattr(srv, "read_only", False):
        raise PermissionError(f"The server {srv.name} is registered read-only: " + " ".join(writes[0].split())[:120])
    sec = getattr(st, "security", None)
    if sec is None:
        return
    acc = sec.access.current()
    if acc.free:
        return
    refusal = _check_need(st, acc, Need(server_id, db_id, READ, sql=sql))
    if refusal is not None:
        raise PermissionError(refusal.detail)


def _read_only_user(server_id: str, db_id: str) -> bool:
    from .api.deps import get_state
    from .security.access import WRITE

    sec = getattr(get_state(), "security", None)
    if sec is None:
        return False
    acc = sec.access.current()
    return not acc.free and acc.level(server_id, db_id) < WRITE


def _audit(server_id: str, db_id: str, result: dict) -> None:
    try:
        from .api.deps import get_state

        sec = getattr(get_state(), "security", None)
        if sec is not None and sec.audit.enabled:
            sec.audit.record_script(server_id, db_id, result)
    except Exception:  # noqa: BLE001 - auditing must not break the run
        pass


def run_cells(nb, server_id: str, db_id: str, opts: NotebookOptions, log) -> tuple[list, dict[str, Any]]:
    """Run the SQL cells; returns the cells with their new outputs and a summary."""
    from . import jobs
    from .api.deps import get_state
    from .connections import connect
    from .notebooks import Output, ResultOut
    from .query import run_script

    st = get_state()
    srv = st.store.get(server_id)
    db = st.store.get_database(server_id, db_id)
    user = actor().username
    pw = st.store.password_for(server_id)  # the schedule owner's saved password (server mode: their own)
    if not pw:
        raise RuntimeError(f"No saved password for {srv.name}: the schedule's owner must save it")
    if _read_only_user(server_id, db_id) and not srv.read_only:
        srv = srv.model_copy(update={"read_only": True})  # Firebird refuses writes too (read-only transactions)
    conn = connect(srv, db.path, pw)
    cells = [c.model_copy(deep=True) for c in nb.cells]
    summary: dict[str, Any] = {"cells": 0, "results": 0, "rows": 0, "error": None, "error_cell": None}
    try:
        for i, c in enumerate(cells, start=1):
            if c.type != "sql" or not c.source.strip():
                continue
            jobs.check()
            summary["cells"] += 1
            started = time.time()
            try:
                check_statements(server_id, db_id, c.source, opts.read_only)
                res = run_script(conn, c.source, autocommit=True, max_rows=MAX_ROWS)
                _audit(server_id, db_id, res)
                err = res["error"]["message"] if res.get("error") else None
                results = [ResultOut(kind=r["kind"], message=r.get("message") or "", columns=r.get("columns") or [],
                                     rows=r.get("rows") or [], row_count=r.get("row_count"),
                                     truncated=bool(r.get("truncated")), elapsed_ms=r.get("elapsed_ms"))
                           for r in res["results"] if r["kind"] != "ignored"]
            except PermissionError as exc:
                err, results = str(exc), []
            except Exception as exc:  # noqa: BLE001 - reported like a statement error
                err, results = " ".join(str(exc).split()) or exc.__class__.__name__, []
            c.output = Output(ran_at=time.time(), ran_by=user, elapsed_ms=round((time.time() - started) * 1000, 2),
                              error=err, results=results)
            rows = sum(len(r.rows) for r in results if r.kind == "select")
            summary["results"] += sum(1 for r in results if r.kind == "select")
            summary["rows"] += rows
            log(f"Cell {i}: " + (f"error: {err}" if err else f"{rows} row(s)"))
            if err:
                summary["error"], summary["error_cell"] = err, i
                break
    finally:
        try:
            conn.rollback()
        except Exception:  # noqa: BLE001
            pass
        conn.close()
    return cells, summary


# ===================================================================== the report
def _csv(result) -> bytes:
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow([c.name for c in result.columns])
    for row in result.rows:
        w.writerow(["" if v is None else v for v in row])
    return buf.getvalue().encode("utf-8")


def _summary_text(nb, summary: dict, db_label: str) -> str:
    head = (f"Notebook \"{nb.name}\" on {db_label}: {summary['cells']} SQL cell(s), {summary['results']} result(s), "
            f"{summary['rows']} row(s).")
    if summary["error"]:
        head += f"\nStopped at cell {summary['error_cell']}: {summary['error']}"
    return head


def _first_rows(cells) -> str:
    """The first rows of each result as plain text (Slack, the e-mail body)."""
    out = []
    for i, c in enumerate(cells, start=1):
        o = c.output
        if c.type != "sql" or o is None:
            continue
        for r in o.results:
            if r.kind != "select":
                continue
            out.append(f"Cell {i} - {len(r.rows)} row(s)")
            cols = [x.name for x in r.columns]
            out.append(" | ".join(cols))
            for row in r.rows[:SUMMARY_ROWS]:
                out.append(" | ".join("NULL" if v is None else str(v) for v in row))
            if len(r.rows) > SUMMARY_ROWS:
                out.append(f"... {len(r.rows) - SUMMARY_ROWS} more")
            out.append("")
    return "\n".join(out).strip()


def deliver(nb, cells, summary: dict, opts: NotebookOptions, schedule_name: str, server_id: str, db_id: str,
            log) -> dict[str, Any]:
    """Send the report to the channels (the send policy was checked by the caller)."""
    import uuid

    from .alerts import channels as ch
    from .alerts.store import get_store as alert_store
    from .api.deps import get_state
    from .notebooks import to_markdown

    st = get_state()
    srv = st.store.get(server_id)
    db = st.store.get_database(server_id, db_id)
    report_nb = nb.model_copy(update={"cells": cells})
    markdown = to_markdown(report_nb)
    text = _summary_text(nb, summary, f"{db.name} ({srv.name})")
    rows = _first_rows(cells)
    now = time.time()
    attachments = [(f"{nb.name}.md", "text/markdown", markdown.encode("utf-8"))]
    if opts.attach_csv:
        n = 0
        for i, c in enumerate(cells, start=1):
            for j, r in enumerate((c.output.results if c.output else []), start=1):
                if r.kind == "select" and r.columns and n < 10:
                    n += 1
                    attachments.append((f"cell{i}-result{j}.csv", "text/csv", _csv(r)))
    alert = {
        "id": uuid.uuid4().hex[:12], "event": "notebook.report", "level": "critical" if summary["error"] else "info",
        "title": f"Notebook report: {nb.name}" + (" (failed)" if summary["error"] else ""),
        "message": text + ("\n\n" + rows if rows else ""),
        "server_id": server_id, "database_id": db_id, "server": srv.name, "database": db.name,
        "time": now, "time_text": time.strftime("%Y-%m-%d %H:%M:%S %Z", time.localtime(now)), "rule": schedule_name,
        "details": {"notebook_id": nb.id, "notebook": nb.name, "summary": summary, "markdown": markdown},
        "attachments": attachments,
    }
    if len(alert["message"]) > SLACK_CHARS:
        alert["message"] = alert["message"][:SLACK_CHARS] + "\n... (truncated: see the attached report)"
    store = alert_store()
    sent, errors = [], []
    for cid in opts.channels:
        c = store.channels.get(cid)
        if c is None or not c.enabled:
            errors.append(f"{cid}: no such channel, or it is disabled")
            continue
        try:
            res = ch.send(c, alert)
            sent.append(c.name)
            log(f"Sent to {c.name}: {res}")
        except Exception as exc:  # noqa: BLE001 - one channel failing must not stop the others
            errors.append(f"{c.name}: {' '.join(str(exc).split())}")
            log(f"Could not send to {c.name}: {exc}")
    return {"sent": sent, "delivery_errors": errors}


def run(schedule, log) -> tuple[dict[str, Any], str | None]:
    """The scheduler's entry point for kind ``notebook`` (runs as the schedule's owner)."""
    from . import notebooks

    opts = schedule.notebook or NotebookOptions(notebook_id="")
    who = actor()
    try:
        nb = notebooks.get_store().get(opts.notebook_id, who)
    except KeyError:
        return {}, "The notebook was deleted, or its owner made it private"
    if (nb.server_id, nb.database_id) != (schedule.server_id, schedule.database_id):
        log("The notebook now belongs to another database; it runs on the schedule's database")
    log(f"Running notebook \"{nb.name}\"" + (" (read only)" if opts.read_only else ""))
    cells, summary = run_cells(nb, schedule.server_id, schedule.database_id, opts, log)
    extra: dict[str, Any] = {"notebook": nb.name, "cells": summary["cells"], "rows": summary["rows"],
                             "saved": False}
    if opts.save_results:
        if notebooks.can_edit(nb, who):
            try:
                notebooks.get_store().update(nb.id, {"cells": cells}, who)
                extra["saved"] = True
                log("Saved the results in the notebook")
            except Exception as exc:  # noqa: BLE001 - e.g. too large: the report still goes out
                log(f"Could not save the results in the notebook: {exc}")
        else:
            log("The results are not saved in the notebook: only its owner or an administrator may change it")
    want = {"always": True, "rows": summary["rows"] > 0, "error": bool(summary["error"])}[opts.send]
    if opts.channels and want:
        extra.update(deliver(nb, cells, summary, opts, schedule.name, schedule.server_id, schedule.database_id, log))
    elif opts.channels:
        log({"rows": "No rows: nothing sent", "error": "No error: nothing sent"}.get(opts.send, "Nothing sent"))
        extra["sent"] = []
    failure = f"Cell {summary['error_cell']}: {summary['error']}" if summary["error"] else None
    if not failure and opts.channels and want and not extra.get("sent"):
        failure = "The report could not be sent: " + "; ".join(extra.get("delivery_errors") or [])
    return extra, failure
