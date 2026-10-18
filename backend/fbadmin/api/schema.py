"""Schema tools API (milestone 0.6): extract, diff, ERD, import/export, copy, search, generator, report."""

from __future__ import annotations

import re
import threading
import uuid
from contextlib import contextmanager
from typing import Any

from fastapi import APIRouter, Query, Request
from fastapi.responses import PlainTextResponse, Response, StreamingResponse
from pydantic import BaseModel

from ..connections import PasswordRequired, connect
from ..metadata import Catalog
from ..query import run_script
from ..schema import dataio
from ..schema.copydata import copy_tables
from ..schema.diff import compare
from ..schema.extract import extract_database
from ..schema.generator import generate_rows
from ..schema.model import Snapshot
from ..schema.report import objects_report
from ..schema.search import search_objects
from ..serialize import to_json
from .deps import get_state

router = APIRouter(tags=["schema"])
DB = "/api/servers/{server_id}/databases/{db_id}/schema"

# structured results of schema jobs (import, copy, generate), by job id
_results: dict[str, dict] = {}
_results_lock = threading.Lock()


# ------------------------------------------------------------------ helpers
def _password(server_id: str) -> str:
    pw = get_state().store.password_for(server_id)
    if not pw:
        raise PasswordRequired(server_id)
    return pw


def open_db(server_id: str, db_id: str):
    """A dedicated connection (long operations must not block the shared metadata connection)."""
    st = get_state()
    srv = st.store.get(server_id)
    db = st.store.get_database(server_id, db_id)
    return connect(srv, db.path, _password(server_id))


@contextmanager
def db_conn(server_id: str, db_id: str):
    conn = open_db(server_id, db_id)
    try:
        yield conn
        conn.commit()
    finally:
        try:
            conn.close()
        except Exception:  # noqa: BLE001
            pass


def _start_job(kind: str, description: str, server_id: str, fn) -> dict:
    result: dict[str, Any] = {}
    job = get_state().jobs.start(kind, description, server_id, lambda log: fn(log, result))
    with _results_lock:
        _results[job.id] = result
        if len(_results) > 200:
            for k in list(_results)[:-200]:
                _results.pop(k, None)
    return job.summary()


@router.get("/api/schema/jobs/{job_id}/result")
def job_result(job_id: str):
    job = get_state().jobs.get(job_id)
    with _results_lock:
        res = _results.get(job_id, {})
    return to_json({"status": job.status, "error": job.error, **res})


# ------------------------------------------------------------------ extract
@router.get(DB + "/extract")
def extract(server_id: str, db_id: str, set_term: bool = True, grants: bool = True, comments: bool = True,
            sequence_values: bool = True, header: bool = True, download: bool = False):
    db = get_state().store.get_database(server_id, db_id)
    with db_conn(server_id, db_id) as conn:
        sql = extract_database(conn, set_term=set_term, header=header, grants=grants, comments=comments,
                               sequence_values=sequence_values, path=db.path)
    if download:
        name = re.sub(r"[^A-Za-z0-9_.-]", "_", db.name) or "database"
        return PlainTextResponse(sql, media_type="application/sql",
                                 headers={"Content-Disposition": f'attachment; filename="{name}.sql"'})
    return {"sql": sql}


# ------------------------------------------------------------------ diff
class Side(BaseModel):
    server_id: str
    db_id: str | None = None
    sql: str | None = None  # a DDL script, applied to a temporary database for the comparison
    temp_dir: str | None = None


class DiffIn(BaseModel):
    source: Side
    target: Side
    set_term: bool = True


def _temp_dir(side: Side, other: Side) -> str:
    if side.temp_dir:
        return side.temp_dir.rstrip("/\\")
    st = get_state()
    paths = []
    if other.db_id and other.server_id == side.server_id:
        paths.append(st.store.get_database(other.server_id, other.db_id).path)
    paths += [d.path for d in st.store.get(side.server_id).databases]
    for p in paths:
        m = re.match(r"^(.*)[/\\][^/\\]+$", p)
        if m:
            return m.group(1)
    raise ValueError("Cannot choose a directory for the temporary database: set temp_dir")


@contextmanager
def _snapshot(side: Side, other: Side, charset: str | None = None):
    if side.db_id:
        with db_conn(side.server_id, side.db_id) as conn:
            yield Snapshot(conn), get_state().store.get_database(side.server_id, side.db_id).name
        return
    if not side.sql:
        raise ValueError("Choose a database or a DDL script")
    st = get_state()
    srv = st.store.get(side.server_id)
    if charset:
        srv = srv.model_copy(update={"charset": charset})
    path = f"{_temp_dir(side, other)}/fbadmin_diff_{uuid.uuid4().hex[:10]}.fdb"
    conn = connect(srv, path, _password(side.server_id), create=True)
    try:
        res = run_script(conn, side.sql, autocommit=True, max_rows=1)
        if res["error"]:
            e = res["error"]
            raise ValueError(f"The DDL script failed at line {e['line']}: {e['message']}")
        yield Snapshot(conn), "DDL script"
    finally:
        try:
            conn.commit()
        except Exception:  # noqa: BLE001
            pass
        try:
            conn.drop_database()
        except Exception:  # noqa: BLE001
            pass


@router.post("/api/schema/diff")
def schema_diff(body: DiffIn):
    # a DDL script side gets the default character set of the database side
    charset = None
    real = body.source if body.source.db_id else body.target if body.target.db_id else None
    if real:
        with db_conn(real.server_id, real.db_id) as conn:
            charset = Catalog(conn).default_charset
    with _snapshot(body.source, body.target, charset) as (src, src_name), \
            _snapshot(body.target, body.source, charset) as (tgt, tgt_name):
        header = f"/* Migration generated by fbadmin: makes {tgt_name} match {src_name} */\n"
        res = compare(src, tgt, set_term=body.set_term, header=header)
    res["source_name"], res["target_name"] = src_name, tgt_name
    return res


# ------------------------------------------------------------------ ERD
@router.get(DB + "/erd")
def erd(server_id: str, db_id: str):
    with get_state().manager.meta(server_id, db_id) as conn:
        cat = Catalog(conn)
        tables, relations = [], []
        for t in cat.list_objects("tables"):
            cols = cat.columns(t["name"])
            cons = cat.constraints(t["name"])
            fk_cols = {c for k in cons if k["type"] == "FOREIGN KEY" for c in k["columns"]}
            uq_cols = {c for k in cons if k["type"] == "UNIQUE" for c in k["columns"]}
            tables.append({
                "name": t["name"], "comment": t["description"], "relation_type": t["relation_type"],
                "columns": [{"name": c["name"], "type": c["domain"] or c["data_type"],
                             "data_type": c["data_type"], "pk": c["primary_key"], "fk": c["name"] in fk_cols,
                             "unique": c["name"] in uq_cols, "not_null": c["not_null"],
                             "computed": bool(c["computed"])} for c in cols],
            })
            for k in cons:
                if k["type"] == "FOREIGN KEY":
                    relations.append({"name": k["name"], "from_table": t["name"], "from_columns": k["columns"],
                                      "to_table": k["ref_table"], "to_columns": k["ref_columns"],
                                      "on_delete": k.get("on_delete"), "on_update": k.get("on_update")})
        return to_json({"tables": tables, "relations": relations})


# ------------------------------------------------------------------ import
@router.post("/api/schema/uploads", status_code=201)
async def upload(request: Request, filename: str = Query("upload.csv")):
    """Raw request body streamed to disk (no multipart, so any size works)."""
    uid, path = dataio.new_upload_path(filename)
    size = 0
    with open(path, "wb") as f:
        async for chunk in request.stream():
            size += len(chunk)
            f.write(chunk)
    return dataio.upload_info(uid)


@router.delete("/api/schema/uploads/{upload_id}", status_code=204)
def delete_upload(upload_id: str):
    dataio.upload_path(upload_id).unlink(missing_ok=True)


class PreviewIn(BaseModel):
    format: str | None = None
    options: dict[str, Any] = {}


@router.post("/api/schema/uploads/{upload_id}/preview")
def upload_preview(upload_id: str, body: PreviewIn):
    info = dataio.upload_info(upload_id)
    fmt = body.format or info["format"]
    return {**info, **dataio.preview(dataio.upload_path(upload_id), fmt, body.options)}


class ImportIn(BaseModel):
    upload_id: str
    format: str | None = None
    options: dict[str, Any] = {}
    table: str
    create: bool = False
    columns: list[dict[str, Any]]
    truncate: bool = False
    on_error: str = "abort"  # abort | skip
    delete_upload: bool = True


@router.post(DB + "/import", status_code=202)
def import_data(server_id: str, db_id: str, body: ImportIn):
    info = dataio.upload_info(body.upload_id)
    path = dataio.upload_path(body.upload_id)
    fmt = body.format or info["format"]
    open_db(server_id, db_id).close()  # fail early (password, path)

    def run(log, result):
        log(f"Importing {info['filename']} ({fmt}) into {body.table}")
        conn = open_db(server_id, db_id)
        try:
            dataio.run_import(conn, log, path=path, fmt=fmt, options=body.options, table=body.table,
                              create=body.create, columns=body.columns, truncate=body.truncate,
                              on_error=body.on_error, result=result)
        finally:
            conn.close()
            if body.delete_upload:
                path.unlink(missing_ok=True)

    return _start_job("import", f"Import {info['filename']} into {body.table}", server_id, run)


# ------------------------------------------------------------------ export
class ExportIn(BaseModel):
    table: str | None = None
    sql: str | None = None
    format: str = "csv"
    delimiter: str = ","
    header: bool = True
    table_name: str | None = None  # for SQL INSERT output


def _export(server_id: str, db_id: str, body: ExportIn) -> Response:
    from ..metadata.types import quote_ident

    if not body.table and not (body.sql and body.sql.strip()):
        raise ValueError("Choose a table or enter a query")
    sql = body.sql.strip().rstrip(";") if body.sql and body.sql.strip() else f"SELECT * FROM {quote_ident(body.table)}"
    name = body.table_name or body.table or "QUERY_RESULT"
    conn = open_db(server_id, db_id)
    try:
        cur = conn.cursor()
        cur.execute(sql)
        if not cur.description:
            raise ValueError("The statement returns no rows")
    except Exception:
        conn.close()
        raise
    fname = re.sub(r"[^A-Za-z0-9_.-]", "_", name)
    if body.format == "xlsx":
        try:
            f = dataio.export_xlsx(cur, sheet=name)
        finally:
            conn.close()
        return StreamingResponse(
            iter(lambda: f.read(1 << 16), b""),
            media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            headers={"Content-Disposition": f'attachment; filename="{fname}.xlsx"'})
    media = {"csv": "text/csv", "json": "application/json", "sql": "application/sql"}.get(body.format)
    if not media:
        conn.close()
        raise ValueError(f"Unknown export format {body.format!r}")

    def gen():
        try:
            yield from dataio.export_rows(cur, body.format, table_name=name, delimiter=body.delimiter,
                                          header=body.header)
        finally:
            conn.close()

    return StreamingResponse(gen(), media_type=media,
                             headers={"Content-Disposition": f'attachment; filename="{fname}.{body.format}"'})


@router.get(DB + "/export")
def export_get(server_id: str, db_id: str, table: str, format: str = "csv", delimiter: str = ",",
               header: bool = True):
    return _export(server_id, db_id, ExportIn(table=table, format=format, delimiter=delimiter, header=header))


@router.post(DB + "/export")
def export_post(server_id: str, db_id: str, body: ExportIn):
    return _export(server_id, db_id, body)


# ------------------------------------------------------------------ copy data
class Endpoint(BaseModel):
    server_id: str
    db_id: str


class CopyIn(BaseModel):
    source: Endpoint
    target: Endpoint
    tables: list[str]
    create_tables: bool = True
    truncate: bool = False
    batch_size: int = 1000


@router.post("/api/schema/copy", status_code=202)
def copy_data(body: CopyIn):
    if not body.tables:
        raise ValueError("Choose at least one table")
    if (body.source.server_id, body.source.db_id) == (body.target.server_id, body.target.db_id):
        raise ValueError("Source and target are the same database")
    st = get_state()
    sdb = st.store.get_database(body.source.server_id, body.source.db_id)
    tdb = st.store.get_database(body.target.server_id, body.target.db_id)
    open_db(body.source.server_id, body.source.db_id).close()
    open_db(body.target.server_id, body.target.db_id).close()

    def run(log, result):
        src = open_db(body.source.server_id, body.source.db_id)
        dst = open_db(body.target.server_id, body.target.db_id)
        try:
            copy_tables(src, dst, log, tables=body.tables, create_tables=body.create_tables,
                        truncate=body.truncate, batch_size=max(1, body.batch_size), result=result)
        finally:
            src.close()
            dst.close()

    return _start_job("copy", f"Copy {len(body.tables)} table(s) {sdb.name} → {tdb.name}", body.target.server_id, run)


# ------------------------------------------------------------------ search
@router.get(DB + "/search")
def search(server_id: str, db_id: str, q: str, scopes: str = "name,comment,source", types: str = ""):
    with get_state().manager.meta(server_id, db_id) as conn:
        items = search_objects(Catalog(conn), q, set(filter(None, scopes.split(","))),
                               set(filter(None, types.split(","))) or None)
    return {"items": items, "truncated": len(items) >= 500}


# ------------------------------------------------------------------ generator
class GenerateIn(BaseModel):
    table: str
    rows: int = 100
    overrides: dict[str, dict[str, Any]] = {}
    null_ratio: float = 0.1
    seed: int | None = None


@router.post(DB + "/generate", status_code=202)
def generate(server_id: str, db_id: str, body: GenerateIn):
    if not 1 <= body.rows <= 10_000_000:
        raise ValueError("rows must be between 1 and 10,000,000")
    open_db(server_id, db_id).close()

    def run(log, result):
        conn = open_db(server_id, db_id)
        try:
            generate_rows(conn, log, table=body.table, rows=body.rows, overrides=body.overrides,
                          null_ratio=body.null_ratio, seed=body.seed, result=result)
        finally:
            conn.close()

    return _start_job("generate", f"Generate {body.rows} rows in {body.table}", server_id, run)


# ------------------------------------------------------------------ report
@router.get(DB + "/report")
def report(server_id: str, db_id: str):
    with get_state().manager.meta(server_id, db_id) as conn:
        return to_json({"sections": objects_report(Catalog(conn))})
