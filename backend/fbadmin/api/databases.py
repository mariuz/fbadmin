"""Object explorer, data viewer and monitoring for a single database."""

from __future__ import annotations

import re

from fastapi import APIRouter, Query
from pydantic import BaseModel

from .. import schemahistory as schema_history
from ..config import settings
from ..metadata import OBJECT_TYPES, Catalog
from ..metadata.catalog import drop_statement
from ..metadata.types import quote_ident
from ..query import run_script
from ..serialize import to_json, type_name
from .deps import get_state

router = APIRouter(prefix="/api/servers/{server_id}/databases/{db_id}", tags=["databases"])


class ExecuteIn(BaseModel):
    sql: str


class RowChange(BaseModel):
    key: dict[str, object] | None = None  # primary key values (update/delete)
    values: dict[str, object] | None = None  # new values (insert/update)


class DataChanges(BaseModel):
    inserts: list[RowChange] = []
    updates: list[RowChange] = []
    deletes: list[RowChange] = []


@router.get("/info")
def database_info(server_id: str, db_id: str):
    with get_state().manager.meta(server_id, db_id) as conn:
        cat = Catalog(conn)
        return to_json({"info": cat.database_info(), "counts": cat.object_counts()})


@router.post("/connect")
def connect_database(server_id: str, db_id: str):
    with get_state().manager.meta(server_id, db_id) as conn:
        cat = Catalog(conn)
        return {"engine_version": cat.engine_version, "charset": cat.default_charset}


@router.get("/schemas")
def list_schemas(server_id: str, db_id: str):
    """SQL schemas (Firebird 6+); an empty list on older servers."""
    with get_state().manager.meta(server_id, db_id) as conn:
        cat = Catalog(conn, schema="")
        return {"supported": cat.schemas_supported, "schemas": to_json(cat.list_schemas())}


@router.get("/objects")
def object_types(server_id: str, db_id: str):
    return [{"type": k, **v} for k, v in OBJECT_TYPES.items()]


@router.get("/objects/{otype}")
def list_objects(server_id: str, db_id: str, otype: str):
    with get_state().manager.meta(server_id, db_id) as conn:
        return to_json(Catalog(conn).list_objects(otype))


@router.get("/objects/{otype}/{name}")
def get_object(server_id: str, db_id: str, otype: str, name: str):
    with get_state().manager.meta(server_id, db_id) as conn:
        return to_json(Catalog(conn).get_object(otype, name))


@router.get("/objects/{otype}/{name}/script")
def object_script(server_id: str, db_id: str, otype: str, name: str, kind: str = "create"):
    with get_state().manager.meta(server_id, db_id) as conn:
        return {"sql": Catalog(conn).script(otype, name, kind)}


@router.delete("/objects/{otype}/{name}")
def drop_object(server_id: str, db_id: str, otype: str, name: str):
    sql = drop_statement(otype, name)
    with get_state().manager.meta(server_id, db_id) as conn:
        cur = conn.cursor()
        cur.execute(sql)
    return {"sql": sql}


@router.get("/completion")
def completion(server_id: str, db_id: str):
    """Names for editor autocompletion: relations with columns, procedures, functions, packages."""
    with get_state().manager.meta(server_id, db_id) as conn:
        cat = Catalog(conn)
        rels: dict[str, list[str]] = {}
        for r in cat.rows(
            "SELECT TRIM(rf.RDB$RELATION_NAME) AS rel, TRIM(rf.RDB$FIELD_NAME) AS col FROM RDB$RELATION_FIELDS rf "
            "JOIN RDB$RELATIONS r ON r.RDB$RELATION_NAME = rf.RDB$RELATION_NAME "
            "WHERE COALESCE(r.RDB$SYSTEM_FLAG, 0) = 0 OR r.RDB$RELATION_NAME STARTING WITH 'MON$' "
            "OR r.RDB$RELATION_NAME STARTING WITH 'RDB$' OR r.RDB$RELATION_NAME STARTING WITH 'SEC$' "
            "ORDER BY rf.RDB$RELATION_NAME, rf.RDB$FIELD_POSITION"
        ):
            rels.setdefault(r["rel"], []).append(r["col"])
        procs = [r["name"] for r in cat.rows(
            "SELECT TRIM(RDB$PROCEDURE_NAME) AS name FROM RDB$PROCEDURES "
            "WHERE RDB$PACKAGE_NAME IS NULL AND COALESCE(RDB$SYSTEM_FLAG, 0) = 0")]
        funcs = [r["name"] for r in cat.rows(
            "SELECT TRIM(RDB$FUNCTION_NAME) AS name FROM RDB$FUNCTIONS "
            "WHERE RDB$PACKAGE_NAME IS NULL AND COALESCE(RDB$SYSTEM_FLAG, 0) = 0")]
        pkgs = {}
        for r in cat.rows(
            "SELECT TRIM(RDB$PACKAGE_NAME) AS pkg, TRIM(RDB$PROCEDURE_NAME) AS name FROM RDB$PROCEDURES "
            "WHERE RDB$PACKAGE_NAME IS NOT NULL UNION ALL SELECT TRIM(RDB$PACKAGE_NAME), TRIM(RDB$FUNCTION_NAME) "
            "FROM RDB$FUNCTIONS WHERE RDB$PACKAGE_NAME IS NOT NULL"
        ):
            pkgs.setdefault(r["pkg"], []).append(r["name"])
        return {"relations": rels, "procedures": procs, "functions": funcs, "packages": pkgs}


@router.post("/execute")
def execute(server_id: str, db_id: str, body: ExecuteIn):
    """Run a script in autocommit mode (used by create/alter dialogs)."""
    with get_state().manager.meta(server_id, db_id) as conn:
        res = run_script(conn, body.sql, autocommit=True, max_rows=settings.max_rows)
    schema_history.after_sql(server_id, db_id, body.sql)
    return res


# ------------------------------------------------------------------ data
@router.get("/tables/{name}/data")
def table_data(
    server_id: str,
    db_id: str,
    name: str,
    limit: int = Query(100, ge=1, le=10000),
    offset: int = Query(0, ge=0),
    order_by: str | None = None,
    descending: bool = False,
    where: str | None = None,
):
    """Paged rows of a table or view. ``where`` is raw SQL (the user is the DB admin)."""
    with get_state().manager.meta(server_id, db_id) as conn:
        cat = Catalog(conn)
        cols = cat.columns(name)
        if not cols:
            raise LookupError(f"Relation {name!r} not found")
        col_names = {c["name"] for c in cols}
        pk = [c["name"] for c in cols if c["primary_key"]]
        sql = f"SELECT {', '.join(quote_ident(c['name']) for c in cols)}"
        if not pk:
            sql += ", RDB$DB_KEY AS FBADMIN$DB_KEY"
        sql += f" FROM {quote_ident(name)}"
        if where and where.strip():
            sql += f" WHERE {where}"
        if order_by and order_by in col_names:
            sql += f" ORDER BY {quote_ident(order_by)}{' DESC' if descending else ''}"
        elif pk:
            sql += " ORDER BY " + ", ".join(quote_ident(p) for p in pk)
        sql += f" OFFSET {offset} ROWS FETCH NEXT {limit + 1} ROWS ONLY"
        cur = conn.cursor()
        cur.execute(sql)
        desc = cur.description
        rows = cur.fetchall() or []
        has_more = len(rows) > limit
        rows = rows[:limit]
        return {
            "sql": sql,
            "columns": [
                {"name": d[0], "type": type_name(d[1], d[5]), "nullable": d[6]} for d in desc
            ],
            "column_meta": to_json(cols),
            "primary_key": pk,
            "rows": [[to_json(v) for v in r] for r in rows],
            "offset": offset,
            "limit": limit,
            "has_more": has_more,
        }


@router.get("/tables/{name}/count")
def table_count(server_id: str, db_id: str, name: str):
    with get_state().manager.meta(server_id, db_id) as conn:
        cur = conn.cursor()
        cur.execute(f"SELECT COUNT(*) FROM {quote_ident(name)}")
        return {"count": cur.fetchone()[0]}


# DATE, TIME, TIMESTAMP, TIME / TIMESTAMP WITH TIME ZONE (RDB$FIELD_TYPE)
_TEMPORAL_TYPES = {12, 13, 35, 28, 29}
_ISO_T = re.compile(r"^(\d{4}-\d{2}-\d{2})T(?=\d)")
_LONG_FRACTION = re.compile(r"(\d{2}:\d{2}:\d{2}\.\d{4})\d+")


def _temporal_text(s: str) -> str:
    """Grid text of a date/time value in a form Firebird parses: the JSON of plain TIMESTAMPs is
    ISO 8601 (``2024-07-01T10:11:12.345600``) but Firebird wants a space and at most 4 fraction
    digits. The WITH TIME ZONE values already are Firebird's text (``... Europe/Bucharest``)."""
    return _LONG_FRACTION.sub(r"\1", _ISO_T.sub(r"\1 ", s.strip()))


def _param(v):
    """Grid values: {"$hex": "..."} is binary data (file upload); everything else is passed as-is."""
    if isinstance(v, dict) and "$hex" in v:
        return bytes.fromhex(str(v["$hex"]))
    return v


@router.post("/tables/{name}/data")
def save_table_data(server_id: str, db_id: str, name: str, body: DataChanges):
    """Apply grid edits in one transaction. Rows are identified by primary key (or DB_KEY)."""
    statements = []
    qn = quote_ident(name)
    temporal: set[str] = set()

    def val(col: str, v):
        v = _param(v)
        return _temporal_text(v) if col in temporal and isinstance(v, str) else v

    def where(key: dict) -> tuple[str, list]:
        parts, params = [], []
        for k, v in key.items():
            if k == "FBADMIN$DB_KEY":
                parts.append("RDB$DB_KEY = ?")
                params.append(bytes.fromhex(str(v)[2:]) if str(v).startswith("0x") else v)
            elif v is None:
                parts.append(f"{quote_ident(k)} IS NULL")
            else:
                parts.append(f"{quote_ident(k)} = ?")
                params.append(val(k, v))
        if not parts:
            raise ValueError("Row key is required for update/delete")
        return " AND ".join(parts), params

    affected = 0
    with get_state().manager.meta(server_id, db_id) as conn:
        temporal.update(c["name"] for c in Catalog(conn).columns(name) if c.get("field_type") in _TEMPORAL_TYPES)
        for d in body.deletes:
            w, p = where(d.key or {})
            statements.append((f"DELETE FROM {qn} WHERE {w}", p))
        for u in body.updates:
            if not u.values:
                continue
            w, p = where(u.key or {})
            sets = ", ".join(f"{quote_ident(k)} = ?" for k in u.values)
            statements.append((f"UPDATE {qn} SET {sets} WHERE {w}", [val(k, x) for k, x in u.values.items()] + p))
        for i in body.inserts:
            vals = i.values or {}
            if vals:
                cols = ", ".join(quote_ident(k) for k in vals)
                marks = ", ".join("?" for _ in vals)
                statements.append((f"INSERT INTO {qn} ({cols}) VALUES ({marks})", [val(k, x) for k, x in vals.items()]))
            else:
                statements.append((f"INSERT INTO {qn} DEFAULT VALUES", []))

        cur = conn.cursor()
        for sql, params in statements:
            cur.execute(sql, params)
            rc = cur.rowcount
            if rc == 0 and not sql.startswith("INSERT"):
                raise ValueError(f"Row not found (it may have been changed by another user): {sql}")
            affected += max(rc, 0)
    return {"statements": len(statements), "affected": affected}


# ------------------------------------------------------------ monitoring
@router.get("/monitor")
def monitor(server_id: str, db_id: str):
    with get_state().manager.meta(server_id, db_id) as conn:
        cat = Catalog(conn)
        attachments = cat.rows(
            "SELECT a.MON$ATTACHMENT_ID AS id, TRIM(a.MON$USER) AS username, TRIM(a.MON$ROLE) AS role_name, "
            "TRIM(a.MON$REMOTE_ADDRESS) AS remote_address, TRIM(a.MON$REMOTE_PROCESS) AS remote_process, "
            "TRIM(a.MON$REMOTE_PROTOCOL) AS protocol, a.MON$STATE AS state, a.MON$TIMESTAMP AS connected_at, "
            "TRIM(a.MON$AUTH_METHOD) AS auth_method, TRIM(a.MON$CLIENT_VERSION) AS client_version, "
            "TRIM(cs.RDB$CHARACTER_SET_NAME) AS charset, "
            "IIF(a.MON$ATTACHMENT_ID = CURRENT_CONNECTION, 1, 0) AS is_self, "
            "io.MON$PAGE_READS AS page_reads, io.MON$PAGE_WRITES AS page_writes, io.MON$PAGE_FETCHES AS fetches "
            "FROM MON$ATTACHMENTS a "
            "LEFT JOIN RDB$CHARACTER_SETS cs ON cs.RDB$CHARACTER_SET_ID = a.MON$CHARACTER_SET_ID "
            "LEFT JOIN MON$IO_STATS io ON io.MON$STAT_ID = a.MON$STAT_ID "
            "WHERE COALESCE(a.MON$SYSTEM_FLAG, 0) = 0 ORDER BY a.MON$ATTACHMENT_ID"
        )
        transactions = cat.rows(
            "SELECT t.MON$TRANSACTION_ID AS id, t.MON$ATTACHMENT_ID AS attachment_id, t.MON$STATE AS state, "
            "t.MON$TIMESTAMP AS started_at, t.MON$ISOLATION_MODE AS isolation, t.MON$READ_ONLY AS read_only, "
            "t.MON$LOCK_TIMEOUT AS lock_timeout FROM MON$TRANSACTIONS t ORDER BY t.MON$TRANSACTION_ID"
        )
        statements = cat.rows(
            "SELECT s.MON$STATEMENT_ID AS id, s.MON$ATTACHMENT_ID AS attachment_id, "
            "s.MON$TRANSACTION_ID AS transaction_id, s.MON$STATE AS state, s.MON$TIMESTAMP AS started_at, "
            "s.MON$SQL_TEXT AS sql_text FROM MON$STATEMENTS s WHERE s.MON$STATE <> 0 "
            "AND s.MON$ATTACHMENT_ID <> CURRENT_CONNECTION ORDER BY s.MON$TIMESTAMP"
        )
        db = cat.one(
            "SELECT MON$NEXT_TRANSACTION AS next_transaction, MON$OLDEST_TRANSACTION AS oit, "
            "MON$OLDEST_ACTIVE AS oat, MON$OLDEST_SNAPSHOT AS ost, MON$PAGE_BUFFERS AS page_buffers, "
            "MON$PAGES AS pages, MON$PAGE_SIZE AS page_size FROM MON$DATABASE"
        )
        io = cat.one(
            "SELECT io.MON$PAGE_READS AS reads, io.MON$PAGE_WRITES AS writes, io.MON$PAGE_FETCHES AS fetches, "
            "io.MON$PAGE_MARKS AS marks FROM MON$DATABASE d JOIN MON$IO_STATS io ON io.MON$STAT_ID = d.MON$STAT_ID"
        )
        return to_json(
            {
                "database": db,
                "io": io,
                "attachments": attachments,
                "transactions": transactions,
                "statements": statements,
            }
        )


@router.delete("/monitor/attachments/{attachment_id}")
def kill_attachment(server_id: str, db_id: str, attachment_id: int):
    with get_state().manager.meta(server_id, db_id) as conn:
        cur = conn.cursor()
        cur.execute("DELETE FROM MON$ATTACHMENTS WHERE MON$ATTACHMENT_ID = ? AND MON$ATTACHMENT_ID <> "
                    "CURRENT_CONNECTION", [attachment_id])
    return {"ok": True}


@router.delete("/monitor/statements/{statement_id}")
def cancel_statement(server_id: str, db_id: str, statement_id: int):
    with get_state().manager.meta(server_id, db_id) as conn:
        cur = conn.cursor()
        cur.execute("DELETE FROM MON$STATEMENTS WHERE MON$STATEMENT_ID = ?", [statement_id])
    return {"ok": True}
