"""Data compare API (milestone 1.2): compare table rows of two databases, sync script, apply."""

from __future__ import annotations

import re

from fastapi import APIRouter
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field

from ..connections import PasswordRequired, connect
from ..datacompare import engine
from ..serialize import to_json
from .deps import get_state

router = APIRouter(tags=["datacompare"])


class Endpoint(BaseModel):
    server_id: str
    db_id: str


class PairIn(BaseModel):
    source: Endpoint
    target: Endpoint
    ignore_columns: list[str] = []


class TableIn(BaseModel):
    name: str
    where: str | None = None  # SQL condition applied to both sides
    ignore_columns: list[str] = []


class CompareIn(BaseModel):
    source: Endpoint
    target: Endpoint
    tables: list[TableIn]
    ignore_columns: list[str] = []  # COLUMN or TABLE.COLUMN, in every table
    inserts: bool = True
    updates: bool = True
    deletes: bool = True
    max_differences: int = Field(100, ge=0, le=10000)  # sample rows kept per table and kind


class ApplyIn(BaseModel):
    confirm: bool = False


def _open(ep: Endpoint):
    st = get_state()
    srv = st.store.get(ep.server_id)
    db = st.store.get_database(ep.server_id, ep.db_id)
    pw = st.store.password_for(ep.server_id)
    if not pw:
        raise PasswordRequired(ep.server_id)
    return connect(srv, db.path, pw)


def _name(ep: Endpoint) -> str:
    st = get_state()
    return f"{st.store.get(ep.server_id).name} / {st.store.get_database(ep.server_id, ep.db_id).name}"


def _check_pair(source: Endpoint, target: Endpoint) -> None:
    if (source.server_id, source.db_id) == (target.server_id, target.db_id):
        raise ValueError("Source and target are the same database")


@router.post("/api/datacompare/tables")
def tables(body: PairIn):
    """The tables of both databases and how each would be compared (key, columns) or why not."""
    _check_pair(body.source, body.target)
    src, dst = _open(body.source), _open(body.target)
    try:
        engine.snapshot_readonly(src)
        engine.snapshot_readonly(dst)
        plans = engine.plan_tables(engine.open_catalog(src), engine.open_catalog(dst), set(body.ignore_columns))
    finally:
        for c in (src, dst):
            try:
                c.rollback()
                c.close()
            except Exception:  # noqa: BLE001
                pass
    return {"source_name": _name(body.source), "target_name": _name(body.target),
            "tables": [engine.public_plan(p) for p in plans]}


@router.post("/api/datacompare/compare", status_code=202)
def compare(body: CompareIn):
    _check_pair(body.source, body.target)
    if not body.tables:
        raise ValueError("Choose at least one table")
    directions = {k for k, on in (("insert", body.inserts), ("update", body.updates), ("delete", body.deletes)) if on}
    _open(body.source).close()  # fail early (password, path)
    _open(body.target).close()
    run = engine.Run(source=(body.source.server_id, body.source.db_id),
                     target=(body.target.server_id, body.target.db_id),
                     source_name=_name(body.source), target_name=_name(body.target))
    run.result.update(source_name=run.source_name, target_name=run.target_name, tables=[], skipped=[],
                      totals={}, statements=0, script_bytes=0, applied=False,
                      directions=sorted(directions), max_differences=body.max_differences)

    def job(log):
        try:
            src = _open(body.source)
            try:
                dst = _open(body.target)
                try:
                    engine.compare_tables(src, dst, log, run, tables=[t.model_dump() for t in body.tables],
                                          ignore=set(body.ignore_columns), directions=directions,
                                          max_samples=body.max_differences)
                finally:
                    dst.close()
            finally:
                src.close()
        finally:
            run.finished = True

    j = get_state().jobs.start("datacompare", f"Compare data {run.source_name} → {run.target_name}",
                               body.target.server_id, job)
    run.result["job_id"] = j.id
    engine.runs.add(j.id, run)
    return j.summary()


def _run(job_id: str):
    job = get_state().jobs.get(job_id)  # per-user job scoping in server mode
    return job, engine.runs.get(job_id)


@router.get("/api/datacompare/jobs/{job_id}")
def result(job_id: str):
    job, run = _run(job_id)
    return to_json({**run.result, "status": job.status, "error": job.error, "applied": run.applied})


@router.get("/api/datacompare/jobs/{job_id}/samples")
def samples(job_id: str, table: str):
    _, run = _run(job_id)
    stats = next((t for t in run.result.get("tables", []) if t["name"] == table), None)
    if stats is None:
        raise KeyError(f"Table {table!r} was not compared")
    s = run.samples.get(table, {})
    return {"table": table, "columns": stats["columns"], "key": stats["key"],
            "insert": s.get("insert", []), "update": s.get("update", []), "delete": s.get("delete", [])}


@router.get("/api/datacompare/jobs/{job_id}/script")
def script(job_id: str):
    job, run = _run(job_id)
    if job.status != "finished":
        raise ValueError("The comparison has not finished")
    name = re.sub(r"[^A-Za-z0-9_.-]", "_", run.target_name.split(" / ")[-1]) or "target"
    return StreamingResponse(run.script_chunks(), media_type="application/sql",
                             headers={"Content-Disposition": f'attachment; filename="datasync_{name}.sql"'})


@router.post("/api/servers/{server_id}/databases/{db_id}/datacompare/{job_id}/apply", status_code=202)
def apply(server_id: str, db_id: str, job_id: str, body: ApplyIn):
    """Run the sync script on the target in one transaction (rolled back on any error)."""
    job, run = _run(job_id)
    st = get_state()
    if run.target != (server_id, db_id):
        raise ValueError("This comparison has a different target database")
    if st.store.get(server_id).read_only:
        return JSONResponse(status_code=403, content={"detail": "The target server is registered as read-only",
                                                      "code": "read_only"})
    if not body.confirm:
        raise ValueError("Confirm applying the script to the target database")
    if job.status != "finished":
        raise ValueError("The comparison has not finished")
    if run.applying or run.applied:
        raise ValueError("This script was already applied: compare again")
    if not run.statement_count():
        raise ValueError("The script has no statements")
    ep = Endpoint(server_id=server_id, db_id=db_id)
    _open(ep).close()
    run.applying = True

    def job_fn(log):
        try:
            conn = _open(ep)
            try:
                engine.apply_script(run, conn, log)
                run.applied = True
            finally:
                conn.close()
        finally:
            run.applying = False

    j = st.jobs.start("datasync", f"Apply data sync script to {run.target_name}", server_id, job_fn)
    return j.summary()
