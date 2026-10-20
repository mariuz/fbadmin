"""Visual query builder (milestone 2.0): the metadata the builder needs to draw tables and suggest joins.

The SELECT itself is generated in the browser (live preview); these endpoints only list relations
(tables and views, per schema on Firebird 6), their columns and their foreign keys, plus the
server's reserved words so the client quotes identifiers exactly like the backend's ``quote_ident``.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query

from ..metadata import Catalog
from ..metadata.types import RESERVED
from ..schema_ctx import valid_schema
from ..serialize import to_json
from .deps import get_state

router = APIRouter(prefix="/api/servers/{server_id}/databases/{db_id}/query-builder", tags=["query-builder"])


def _catalog(conn, schema: str | None) -> Catalog:
    try:
        cat = Catalog(conn, valid_schema(schema))
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    if cat.schemas_supported and cat.schema is None:
        # Firebird 6 without a schema in the request: list PUBLIC, not every schema mixed together
        cat = Catalog(conn, "PUBLIC")
    return cat


@router.get("/relations")
def relations(server_id: str, db_id: str, schema: str | None = Query(None)):
    """Tables and views of the database (of ``schema``, or the active schema, on Firebird 6)."""
    with get_state().manager.meta(server_id, db_id) as conn:
        cat = _catalog(conn, schema)
        items = [{"name": t["name"], "kind": "table", "description": t["description"]} for t in cat.list_objects("tables")]
        items += [{"name": v["name"], "kind": "view", "description": v["description"]} for v in cat.list_objects("views")]
        items.sort(key=lambda r: r["name"])
        return to_json({
            "engine_major": cat.engine_major,
            "schemas_supported": cat.schemas_supported,
            "schema": cat.schema,
            "schemas": [s["name"] for s in cat.list_schemas()],
            "relations": items,
            "reserved": sorted(RESERVED),
        })


@router.get("/relations/{name}")
def relation(server_id: str, db_id: str, name: str, schema: str | None = Query(None)):
    """Columns and outgoing foreign keys of one table or view."""
    with get_state().manager.meta(server_id, db_id) as conn:
        cat = _catalog(conn, schema)
        cols = cat.columns(name)
        if not cols:
            raise HTTPException(404, f"Table or view {name} not found")
        cons = cat.constraints(name)
        fk_cols = {c for k in cons if k["type"] == "FOREIGN KEY" for c in k["columns"]}
        return to_json({
            "name": name,
            "schema": cat.schema,
            "columns": [{"name": c["name"], "type": c["data_type"], "pk": c["primary_key"], "fk": c["name"] in fk_cols,
                         "not_null": c["not_null"]} for c in cols],
            "foreign_keys": [{"name": k["name"], "columns": k["columns"], "ref_table": k["ref_table"],
                              "ref_columns": k["ref_columns"]} for k in cons if k["type"] == "FOREIGN KEY"],
        })
