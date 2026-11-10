"""Change requests with approval (roadmap 2.4): propose, dry run, approve or reject, apply (see fbadmin/changes.py)."""

from __future__ import annotations

import time
from typing import Any, Literal

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from .. import changes as C
from .. import migrations as M
from ..notebook_runs import actor, check_statements
from ..security.access import READ
from ..security.errors import AccessDenied
from .deps import get_state

router = APIRouter(prefix="/api/changes", tags=["changes"])


class ChangeIn(BaseModel):
    server_id: str
    database_id: str
    title: str = Field(min_length=1, max_length=200)
    description: str = Field(default="", max_length=20000)
    kind: Literal["sql", "migrations"] = "sql"
    sql: str = Field(default="", max_length=2_000_000)
    scripts: list[C.ScriptItem] = Field(default_factory=list, max_length=500)


class ChangeEdit(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=20000)
    sql: str | None = Field(default=None, max_length=2_000_000)
    scripts: list[C.ScriptItem] | None = Field(default=None, max_length=500)


class TextIn(BaseModel):
    text: str = Field(default="", max_length=20000)


class DryRunIn(BaseModel):
    copy_mode: Literal["metadata", "full"] = "metadata"
    scratch: str | None = None  # scratch database path on the server (default: next to the database)


def _check_sql(cr: C.ChangeRequest) -> None:
    """Refuse statements the current user may not run (object rules, a read-only server)."""
    if cr.kind != "sql":
        return
    try:
        check_statements(cr.server_id, cr.database_id, cr.sql, read_only=False)
    except PermissionError as exc:  # an OSError: would be answered as "cannot reach server"
        raise AccessDenied(str(exc)) from None


def _check_content(kind: str, sql: str, scripts: list[C.ScriptItem]) -> None:
    if kind == "sql":
        if not sql.strip():
            raise ValueError("The change has no SQL")
        from ..sqlsplit import split_sql

        if not [s for s in split_sql(sql) if s.sql.strip()]:
            raise ValueError("The change has no statements")
    else:
        if not scripts:
            raise ValueError("Add the migration scripts")
        found, warnings = M.parse([(s.name, s.sql) for s in scripts])
        if not found:
            raise ValueError("No versioned migration scripts (V<version>__<description>.sql): " + "; ".join(warnings))


def _visible(cid: str) -> C.ChangeRequest:
    cr = C.get_store().get(cid)
    if not C.can_see(cr):
        raise KeyError("Unknown change request")
    return cr


def _view(cr: C.ChangeRequest, full: bool = True) -> dict[str, Any]:
    who = actor()
    out = cr.model_dump(exclude={"sql", "scripts", "history", "dry_run", "applied"} if not full else set())
    st = get_state()
    try:
        db = st.store.get_database(cr.server_id, cr.database_id)
        out["database"] = db.name
        out["server"] = st.store.get(cr.server_id).name
    except KeyError:
        out["database"] = out["server"] = None
    out["approvals_needed"] = C.APPROVALS
    out["mine"] = C.is_author(cr, who) and not who.desktop
    out["can"] = C.actions(cr, who)
    out["approve_blocked"] = C.may_approve(cr, who)
    out["statements"] = _count(cr)
    if full and cr.dry_run:
        out["dry_run"] = {**cr.dry_run, "stale": cr.dry_run.get("revision") != cr.revision}
    return out


def _count(cr: C.ChangeRequest) -> int:
    from ..sqlsplit import split_sql

    try:
        return sum(1 for s in split_sql(cr.text()) if s.sql.strip() and not s.sql.strip().startswith("--"))
    except Exception:  # noqa: BLE001
        return 0


@router.get("")
def list_requests(server_id: str | None = None, database_id: str | None = None, status: str | None = None):
    out = []
    for cr in C.get_store().all():
        if server_id and cr.server_id != server_id or database_id and cr.database_id != database_id:
            continue
        if status == "active" and cr.status in C.DONE or status and status != "active" and cr.status != status:
            continue
        if C.can_see(cr):
            out.append(_view(cr, full=False))
    return out


@router.post("", status_code=201)
def create(body: ChangeIn):
    if C.level(body.server_id, body.database_id) < READ:
        raise AccessDenied("Proposing a change needs at least the 'read' level on the database")
    _check_content(body.kind, body.sql, body.scripts)
    who = actor()
    cr = C.ChangeRequest(server_id=body.server_id, database_id=body.database_id, title=body.title.strip(),
                         description=body.description, kind=body.kind,
                         sql=body.sql if body.kind == "sql" else "",
                         scripts=body.scripts if body.kind == "migrations" else [],
                         author=who.username, author_id=who.user_id)
    cr.log(who, "submitted")
    cr = C.get_store().add(cr)
    C.audit(cr, f"submitted: {cr.title}")
    C.notify(cr, "submitted", who, cr.description)
    return _view(cr)


@router.get("/{cid}")
def get_one(cid: str):
    return _view(_visible(cid))


@router.put("/{cid}")
def edit(cid: str, body: ChangeEdit):
    _visible(cid)
    who = actor()

    def fn(cr: C.ChangeRequest) -> None:
        if not C.actions(cr, who)["edit"]:
            raise AccessDenied("Only its author can edit a change request, before it is applied")
        if body.title is not None:
            cr.title = body.title.strip()
        if body.description is not None:
            cr.description = body.description
        new_sql = body.sql if body.sql is not None and cr.kind == "sql" else cr.sql
        new_scripts = body.scripts if body.scripts is not None and cr.kind == "migrations" else cr.scripts
        changed = new_sql != cr.sql or [s.model_dump() for s in new_scripts] != [s.model_dump() for s in cr.scripts]
        if changed:
            _check_content(cr.kind, new_sql, new_scripts)
            cr.sql, cr.scripts = new_sql, new_scripts
            cr.revision += 1
            dropped = len(cr.approvals)
            cr.approvals = []
            cr.status = "open"
            cr.log(who, "revised", f"revision {cr.revision}" + (f"; {dropped} approval(s) cleared" if dropped else ""))
        else:
            cr.log(who, "edited")

    cr = C.get_store().update(cid, fn)
    C.audit(cr, f"edited (revision {cr.revision})")
    return _view(cr)


@router.post("/{cid}/comments")
def comment(cid: str, body: TextIn):
    _visible(cid)
    if not body.text.strip():
        raise ValueError("Write the comment")
    who = actor()
    return _view(C.get_store().update(cid, lambda cr: cr.log(who, "comment", body.text.strip())))


@router.post("/{cid}/withdraw")
def withdraw(cid: str):
    _visible(cid)
    who = actor()

    def fn(cr: C.ChangeRequest) -> None:
        if not C.actions(cr, who)["withdraw"]:
            raise AccessDenied("Only its author can withdraw a change request that is not applied")
        cr.status = "withdrawn"
        cr.log(who, "withdrawn")

    cr = C.get_store().update(cid, fn)
    C.audit(cr, "withdrawn")
    C.notify(cr, "withdrawn", who)
    return _view(cr)


@router.post("/{cid}/approve")
def approve(cid: str, body: TextIn | None = None):
    _visible(cid)
    who = actor()
    note = (body.text if body else "").strip()

    def fn(cr: C.ChangeRequest) -> None:
        why = C.may_approve(cr, who)
        if why:
            raise AccessDenied(why) if "level" in why or "someone else" in why else HTTPException(409, why)
        cr.approvals.append(C.Approval(by=who.username, user_id=who.user_id, comment=note,
                                       changes=cr.dry_run.get("changes_fp", "") if cr.dry_run else ""))
        cr.log(who, "approved", note)
        if len(cr.approvals) >= C.APPROVALS:
            cr.status = "approved"

    cr = C.get_store().update(cid, fn)
    C.audit(cr, "approved" + (f": {note}" if note else ""))
    if cr.status == "approved":
        C.notify(cr, "approved", who, note)
    return _view(cr)


@router.post("/{cid}/reject")
def reject(cid: str, body: TextIn):
    _visible(cid)
    who = actor()
    if not body.text.strip():
        raise ValueError("Say why the change is rejected: the author reads it")

    def fn(cr: C.ChangeRequest) -> None:
        if not C.actions(cr, who)["reject"]:
            raise AccessDenied("Rejecting needs the 'admin' level on the database, and an open or approved request")
        cr.status = "rejected"
        cr.approvals = []
        cr.log(who, "rejected", body.text.strip())

    cr = C.get_store().update(cid, fn)
    C.audit(cr, f"rejected: {body.text.strip()}")
    C.notify(cr, "rejected", who, body.text.strip())
    return _view(cr)


# ------------------------------------------------------------------ dry run and apply
def _pending(cr: C.ChangeRequest) -> tuple[list[M.Script], str]:
    """The scripts to run and the source ref; for migrations, the ones not applied yet."""
    if cr.kind == "sql":
        return [M.Script(version="1", key=(1,), description=cr.title, name=f"change request #{cr.number}",
                         sql=cr.sql, checksum=M.checksum(cr.sql))], f"change request #{cr.number}"
    from .migrations import FilesIn, ScriptIn, _status

    st, _scripts, ref, _src = _status(cr.server_id, cr.database_id, FilesIn(
        scripts=[ScriptIn(name=s.name, sql=s.sql) for s in cr.scripts], source_ref=f"change request #{cr.number}"))
    if st["errors"]:
        raise ValueError(" ".join(st["errors"]))
    return st["_pending"], ref


@router.post("/{cid}/dry-run", status_code=202)
def dry_run(cid: str, body: DryRunIn | None = None):
    """Run the change on a scratch copy of the database and keep the report (schema changes) on the request."""
    from .migrations import _connector

    cr = _visible(cid)
    who = actor()
    if not C.actions(cr, who)["dry_run"]:
        raise AccessDenied("The dry run needs the 'admin' level on the database (it restores a copy)")
    body = body or DryRunIn()
    st = get_state()
    db = st.store.get_database(cr.server_id, cr.database_id)
    srv = st.store.get(cr.server_id)
    _check_sql(cr)
    pending, ref = _pending(cr)
    scratch = M.scratch_path(db.path, f"cr_{cr.id}", body.scratch)
    revision = cr.revision

    def run(log):
        schema = C.schema_fingerprint(cr.server_id, cr.database_id)
        if not pending:
            log("Nothing pending: every migration script is applied already")
            report: dict[str, Any] = {"ok": True, "scripts": [], "changes": [],
                                      "summary": {"created": 0, "altered": 0, "dropped": 0}, "copy": body.copy_mode,
                                      "seconds": 0, "finished_at": time.time()}
        else:
            report = M.dry_run(pending=pending, db_path=db.path, scratch=scratch, copy=body.copy_mode,
                               svc_factory=lambda: st.manager.service(cr.server_id),
                               connect=_connector(cr.server_id, db.path), registered=[d.path for d in srv.databases],
                               user=who.username, ref=ref, log=log)
        report.update(revision=revision, schema=schema, changes_fp=C.changes_fingerprint(report), by=who.username,
                      pending=[p.name for p in pending])
        detail = (f"{'passed' if report['ok'] else 'FAILED'}: " + ", ".join(
            f"{report['summary'].get(k, 0)} {k}" for k in ("created", "altered", "dropped")))

        def keep(c: C.ChangeRequest) -> None:
            if c.revision != revision:
                return  # edited meanwhile: this report is for an older revision
            approved = {a.changes for a in c.approvals}
            c.dry_run = report
            if c.approvals and approved != {report["changes_fp"]}:
                c.approvals = []
                if c.status == "approved":
                    c.status = "open"
                detail_ = detail + "; the schema changes differ from the approved ones: approvals cleared"
            else:
                detail_ = detail
            c.log(who, "dry_run", detail_)

        C.get_store().update(cr.id, keep)
        C.audit(cr, f"dry run {detail}", report["ok"])
        if not report["ok"]:
            failed = next((s for s in report["scripts"] if not s["ok"]), None)
            raise RuntimeError("The dry run failed" + (f": {failed['error']['message']}" if failed else ""))

    job = st.jobs.start("change-dry-run", f"Change request #{cr.number} dry run: {db.name}", cr.server_id, run)
    C.get_store().update(cr.id, lambda c: setattr(c, "dry_run_job", job.id))
    return job.summary()


@router.post("/{cid}/apply", status_code=202)
def apply(cid: str):
    """Apply an approved change (as a job). Refused when the schema changed since the dry run."""
    from .. import schemahistory as schema_history
    from ..query import run_script
    from .migrations import _connector

    cr = _visible(cid)
    who = actor()
    if not C.actions(cr, who)["apply"]:
        raise AccessDenied("Applying needs the 'admin' level on the database and an approved request")
    st = get_state()
    db = st.store.get_database(cr.server_id, cr.database_id)
    if st.store.get(cr.server_id).read_only:
        raise ValueError("The server is read-only in fbadmin")
    why = C.dry_run_problem(cr)
    if why:
        raise HTTPException(409, why)
    if C.schema_fingerprint(cr.server_id, cr.database_id) != cr.dry_run.get("schema"):
        raise HTTPException(409, "The database's schema changed since the dry run: run the dry run again")
    _check_sql(cr)
    connect_ = _connector(cr.server_id, db.path)

    def started(c: C.ChangeRequest) -> None:
        if c.status != "approved":
            raise HTTPException(409, f"The request is {c.status}")
        c.status = "applying"
        c.log(who, "applying")

    C.get_store().update(cr.id, started)

    def finish(ok: bool, results: list, error: str | None, job_id: str | None) -> None:
        def fn(c: C.ChangeRequest) -> None:
            c.status = "applied" if ok else "failed"
            c.applied = {"at": time.time(), "by": who.username, "ok": ok, "results": results, "error": error,
                         "job_id": job_id}
            c.log(who, "applied" if ok else "failed", error or "")

        done = C.get_store().update(cr.id, fn)
        C.audit(done, "applied" if ok else "apply failed", ok, error)
        C.notify(done, "applied" if ok else "failed", who, error or "")

    job_ref: dict[str, str] = {}

    def run(log):
        try:
            if cr.kind == "sql":
                conn = connect_()
                try:
                    log(f"Applying change request #{cr.number} to {db.name}")
                    res = run_script(conn, cr.sql, autocommit=True, max_rows=10)
                finally:
                    conn.close()
                sec = getattr(st, "security", None)
                if sec is not None and sec.audit.enabled:
                    sec.audit.record_script(cr.server_id, cr.database_id, res)
                results = [{"sql": i.get("sql", "")[:2000], "kind": i["kind"], "message": i.get("message") or "",
                            "row_count": i.get("row_count")} for i in res["results"] if i["kind"] != "ignored"]
                err = None
                if res["error"]:
                    e = res["error"]
                    err = f"line {e['line']}: {e['message']}"
                    log(f"FAILED at {err}")
                else:
                    log(f"Applied {len(results)} statement(s)")
                schema_history.after_sql(cr.server_id, cr.database_id, cr.sql)
            else:
                scripts, _w = M.parse([(s.name, s.sql) for s in cr.scripts])
                rep = M.apply(connect_, scripts, key=f"{cr.server_id}/{cr.database_id}", user=who.username,
                              ref=f"change request #{cr.number}", log=log)
                results = rep["results"]
                f = rep["failed"]
                err = f"{f['script']} line {f['error']['line']}: {f['error']['message']}" if f else None
            finish(err is None, results, err, job_ref.get("id"))
        except Exception as exc:
            msg = " ".join(str(exc).split()) or exc.__class__.__name__
            finish(False, [], msg, job_ref.get("id"))
            raise
        if err:
            raise RuntimeError(err)

    job = st.jobs.start("change-apply", f"Change request #{cr.number}: {db.name}", cr.server_id, run)
    job_ref["id"] = job.id
    C.get_store().update(cr.id, lambda c: setattr(c, "apply_job", job.id))
    return job.summary()
