"""Object management dialogs (milestone 0.3): editable specs, DDL generation, drop preview
with dependencies, and privileges (RDB$USER_PRIVILEGES).

DDL is generated here (not in the browser) so the same code is unit-tested against
real servers; dialogs show the SQL and run it through ``POST .../execute``.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter
from pydantic import BaseModel

from ..metadata import Catalog
from ..metadata import designer
from ..metadata.ddlgen import generate
from ..serialize import to_json
from .deps import get_state

router = APIRouter(prefix="/api/servers/{server_id}/databases/{db_id}", tags=["object dialogs"])


class DDLIn(BaseModel):
    kind: str
    spec: dict[str, Any]
    original: dict[str, Any] | None = None


@router.get("/designer/lookups")
def designer_lookups(server_id: str, db_id: str):
    with get_state().manager.meta(server_id, db_id) as conn:
        return to_json(designer.lookups(Catalog(conn)))


@router.get("/designer/relations/{table}")
def relation_info(server_id: str, db_id: str, table: str):
    """Columns and PK/UNIQUE keys of a table: used by the FK referenced-table/column pickers."""
    with get_state().manager.meta(server_id, db_id) as conn:
        cat = Catalog(conn)
        cols = designer.table_columns(cat, table)
        if not cols:
            raise LookupError(f"Relation {table!r} not found")
        return {"columns": cols, "unique_keys": designer.unique_keys(cat, table)}


@router.get("/designer/comments/{otype}/{name}")
def comments_spec(server_id: str, db_id: str, otype: str, name: str):
    with get_state().manager.meta(server_id, db_id) as conn:
        return to_json(designer.comments_spec(Catalog(conn), otype, name))


@router.get("/designer/{otype}/{name}")
def object_spec(server_id: str, db_id: str, otype: str, name: str):
    with get_state().manager.meta(server_id, db_id) as conn:
        cat = Catalog(conn)
        return to_json({"spec": designer.object_spec(cat, otype, name), "engine_major": cat.engine_major})


@router.post("/ddl")
def make_ddl(server_id: str, db_id: str, body: DDLIn):
    """Generate the DDL for a dialog. Nothing is executed."""
    with get_state().manager.meta(server_id, db_id) as conn:
        engine = Catalog(conn).engine_major
    return generate(body.kind, body.spec, body.original, engine).as_dict()


@router.get("/objects/{otype}/{name}/drop-preview")
def drop_preview(server_id: str, db_id: str, otype: str, name: str, column: str | None = None):
    """The DROP statement plus everything that depends on the object (transitively)."""
    with get_state().manager.meta(server_id, db_id) as conn:
        cat = Catalog(conn)
        if column:
            spec = {"otype": "columns", "table": name, "name": column}
        else:
            spec = {"otype": otype, "name": name}
        sql = generate("drop", spec).sql
        out: dict[str, Any] = {"sql": sql, "dependents": [], "foreign_keys": [], "columns": [], "grants": []}
        if otype in ("users",):
            return out
        if column:
            out["dependents"] = cat.dependents_tree(name, column)
            out["constraints"] = [
                c["name"] for c in cat.constraints(name) if column in c["columns"]
            ]
            out["indices"] = [i["name"] for i in cat.indices(relation=name)
                              if column in i["columns"] and not i["constraint_name"]]
            return to_json(out)
        out["dependents"] = cat.dependents_tree(name)
        if otype == "tables":
            out["foreign_keys"] = cat.referencing_foreign_keys(name)
        if otype == "domains":
            out["columns"] = cat.rows(
                "SELECT TRIM(RDB$RELATION_NAME) AS relation, TRIM(RDB$FIELD_NAME) AS name FROM RDB$RELATION_FIELDS "
                "WHERE RDB$FIELD_SOURCE = ? ORDER BY 1, 2",
                (name,),
            )
        if otype in ("tables", "views", "procedures", "functions", "packages", "sequences", "exceptions", "roles"):
            out["grants"] = cat.privileges(object_name=name)
        return to_json(out)


@router.get("/privileges")
def privileges(server_id: str, db_id: str, object: str | None = None, grantee: str | None = None):
    """Grants on ``object`` or held by ``grantee`` (user, role, procedure, ...)."""
    if not object and not grantee:
        raise ValueError("Pass object or grantee")
    with get_state().manager.meta(server_id, db_id) as conn:
        cat = Catalog(conn)
        rows = cat.privileges(object_name=object, grantee=grantee)
        out: dict[str, Any] = {"grants": rows}
        if object:
            cols = designer.table_columns(cat, object)
            out["columns"] = cols
        return to_json(out)


@router.get("/privileges/grantees")
def grantees(server_id: str, db_id: str):
    with get_state().manager.meta(server_id, db_id) as conn:
        return Catalog(conn).grantees()


@router.get("/privileges/objects")
def grantable_objects(server_id: str, db_id: str):
    """Objects privileges can be granted on, by type (for the per-user editor)."""
    with get_state().manager.meta(server_id, db_id) as conn:
        cat = Catalog(conn)
        out = {}
        for t in ("tables", "views", "procedures", "functions", "packages", "sequences", "exceptions", "roles"):
            out[t] = [r["name"] for r in cat.list_objects(t)]
        return out
