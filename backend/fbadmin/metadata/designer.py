"""Editable specs of existing objects for the object dialogs.

The spec format is the input of :mod:`fbadmin.metadata.ddlgen`: a dialog loads the spec,
lets the user edit a copy and sends both back to get the ALTER script.
"""

from __future__ import annotations

import re
from typing import Any

from .catalog import Catalog
from .ddlgen import SYSTEM_PRIVILEGES, _strip_kw

_WITH_CHECK = re.compile(r"\s+WITH\s+CHECK\s+OPTION\s*;?\s*$", re.IGNORECASE)


def _sec(v) -> str | None:
    return None if v is None else ("DEFINER" if v else "INVOKER")


def _default(v: str | None) -> str | None:
    t = _strip_kw(v, "DEFAULT")
    return t or None


def _rel_row(cat: Catalog, name: str) -> dict | None:
    return cat.one(
        "SELECT TRIM(RDB$RELATION_NAME) AS name, RDB$RELATION_TYPE AS relation_type, "
        "RDB$EXTERNAL_FILE AS external_file, RDB$DESCRIPTION AS description, RDB$VIEW_SOURCE AS view_source"
        + cat.sql_security_col("RDB$RELATIONS") + " FROM RDB$RELATIONS WHERE RDB$RELATION_NAME = ?",
        (name,),
    )


def column_spec(c: dict) -> dict[str, Any]:
    return {
        "orig_name": c["name"],
        "name": c["name"],
        "data_type": c["data_type"],
        "domain": c["domain"],
        "default": _default(c["col_default"]),
        "domain_default": None if c["col_default"] else _default(c["default"]),
        "not_null": c["col_not_null"],
        "domain_not_null": c["not_null"] and not c["col_not_null"],
        "collation": c["collation"] if c["collation"] and c["collation"] != (c["charset"] or "") else None,
        "charset": c["charset"],
        "identity": c["identity"],
        "identity_start": c["identity_start"] if c["identity"] else None,
        "identity_increment": c["identity_increment"] if c["identity"] else None,
        "computed": c["computed"],
        "primary_key": c["primary_key"],
        "comment": c["description"],
    }


def table_spec(cat: Catalog, name: str) -> dict[str, Any]:
    r = _rel_row(cat, name)
    if not r or r["view_source"] is not None:
        raise LookupError(f"Table {name!r} not found")
    cols = cat.columns(name)
    cons = cat.constraints(name)
    idx = cat.indices(relation=name)
    trg = cat.triggers(relation=name)
    rtype = r["relation_type"]
    return {
        "name": name,
        "kind": {4: "gtt_preserve", 5: "gtt_delete"}.get(rtype, "persistent"),
        "external_file": (r["external_file"] or "").strip() or None,
        "sql_security": _sec(r["sql_security"]),
        "comment": r["description"],
        "columns": [column_spec(c) for c in cols],
        "constraints": [
            {
                "orig_name": c["name"],
                "name": c["name"],
                "auto_name": c["name"].startswith("INTEG_") or c["name"].startswith("RDB$"),
                "type": c["type"],
                "columns": c["columns"],
                "ref_table": c.get("ref_table"),
                "ref_columns": c.get("ref_columns") or [],
                "on_update": c.get("on_update"),
                "on_delete": c.get("on_delete"),
                "check": _strip_kw(c.get("source"), "CHECK") or None,
                "index_name": c["index"] if c.get("index") and c["index"] != c["name"]
                and not c["index"].startswith("RDB$") else None,
                "index_desc": c["descending"],
            }
            for c in cons
        ],
        "indices": [index_spec_row(i) for i in idx if not i["constraint_name"]],
        "triggers": [
            {"orig_name": t["name"], "name": t["name"], "active": not t["inactive"], "event": t["event"],
             "position": t["position"]}
            for t in trg
        ],
    }


def index_spec_row(i: dict) -> dict[str, Any]:
    return {
        "orig_name": i["name"],
        "name": i["name"],
        "table": i["relation"],
        "unique": i["is_unique"],
        "descending": i["descending"],
        "columns": i["columns"],
        "expression": i["expr"],
        "condition": _strip_kw(i.get("cond"), "WHERE") or None,
        "active": not i["inactive"],
        "selectivity": i["selectivity"],
        "constraint_name": i["constraint_name"],
        "comment": i["description"],
    }


def index_spec(cat: Catalog, name: str) -> dict[str, Any]:
    rows = cat.indices(name=name)
    if not rows:
        raise LookupError(f"Index {name!r} not found")
    return index_spec_row(rows[0])


def view_spec(cat: Catalog, name: str) -> dict[str, Any]:
    r = _rel_row(cat, name)
    if not r or r["view_source"] is None:
        raise LookupError(f"View {name!r} not found")
    src = (r["view_source"] or "").strip()
    check = bool(_WITH_CHECK.search(src))
    return {
        "name": name,
        "columns": [c["name"] for c in cat.columns(name)],
        "select": _WITH_CHECK.sub("", src).strip(),
        "check_option": check,
        "comment": r["description"],
    }


def _split_params(params: list[dict]) -> tuple[list[dict], list[dict]]:
    ins = [{"name": p["name"], "type": p["data_type"], "comment": p["description"]}
           for p in params if p.get("direction", "IN") == "IN"]
    outs = [{"name": p["name"], "type": p["data_type"], "comment": p["description"]}
            for p in params if p.get("direction") == "OUT"]
    return ins, outs


def procedure_spec(cat: Catalog, name: str) -> dict[str, Any]:
    r = cat.one(
        "SELECT RDB$PROCEDURE_SOURCE AS source, TRIM(RDB$ENGINE_NAME) AS engine, TRIM(RDB$ENTRYPOINT) AS entrypoint, "
        "RDB$DESCRIPTION AS description" + cat.sql_security_col("RDB$PROCEDURES") +
        " FROM RDB$PROCEDURES WHERE RDB$PROCEDURE_NAME = ? AND RDB$PACKAGE_NAME IS NULL",
        (name,),
    )
    if not r:
        raise LookupError(f"Procedure {name!r} not found")
    ins, outs = _split_params(cat.procedure_params(name))
    return {
        "name": name,
        "kind": "external" if r["engine"] else "psql",
        "inputs": ins,
        "outputs": outs,
        "body": None if r["engine"] else (r["source"] or "").strip(),
        "external": {"name": r["entrypoint"], "engine": r["engine"], "body": (r["source"] or "").strip() or None}
        if r["engine"] else None,
        "sql_security": _sec(r["sql_security"]),
        "comment": r["description"],
    }


def function_spec(cat: Catalog, name: str) -> dict[str, Any]:
    r = cat.one(
        "SELECT RDB$FUNCTION_SOURCE AS source, TRIM(RDB$ENGINE_NAME) AS engine, TRIM(RDB$ENTRYPOINT) AS entrypoint, "
        "RDB$DETERMINISTIC_FLAG AS det, RDB$RETURN_ARGUMENT AS ret, RDB$LEGACY_FLAG AS legacy, "
        "RDB$DESCRIPTION AS description" + cat.sql_security_col("RDB$FUNCTIONS") +
        " FROM RDB$FUNCTIONS WHERE RDB$FUNCTION_NAME = ? AND RDB$PACKAGE_NAME IS NULL",
        (name,),
    )
    if not r:
        raise LookupError(f"Function {name!r} not found")
    if r["legacy"]:
        raise ValueError("Legacy UDFs (DECLARE EXTERNAL FUNCTION) cannot be edited with the function dialog")
    args = cat.function_args(name)
    ret_pos = r["ret"] or 0
    return {
        "name": name,
        "kind": "external" if r["engine"] else "psql",
        "inputs": [{"name": a["name"], "type": a["data_type"], "comment": a["description"]}
                   for a in args if a["position"] != ret_pos],
        "returns": next((a["data_type"] for a in args if a["position"] == ret_pos), ""),
        "deterministic": bool(r["det"]),
        "body": None if r["engine"] else (r["source"] or "").strip(),
        "external": {"name": r["entrypoint"], "engine": r["engine"], "body": (r["source"] or "").strip() or None}
        if r["engine"] else None,
        "sql_security": _sec(r["sql_security"]),
        "comment": r["description"],
    }


def trigger_spec(cat: Catalog, name: str) -> dict[str, Any]:
    rows = cat.triggers(name=name)
    if not rows:
        raise LookupError(f"Trigger {name!r} not found")
    t = rows[0]
    return {
        "name": name,
        "relation": t["relation"],
        "trigger_kind": t["kind"],
        "event": t["event"],
        "position": t["position"] or 0,
        "active": not t["inactive"],
        "body": None if t["engine"] else _strip_kw(t["source"], "AS"),
        "external": {"name": t["entrypoint"], "engine": t["engine"],
                     "body": _strip_kw(t["source"], "AS") or None} if t["engine"] else None,
        "kind": "external" if t["engine"] else "psql",
        "sql_security": _sec(t.get("sql_security")),
        "comment": t["description"],
    }


def package_spec(cat: Catalog, name: str) -> dict[str, Any]:
    r = cat.one(
        "SELECT RDB$PACKAGE_HEADER_SOURCE AS header, RDB$PACKAGE_BODY_SOURCE AS body, RDB$DESCRIPTION AS description"
        + cat.sql_security_col("RDB$PACKAGES") + " FROM RDB$PACKAGES WHERE RDB$PACKAGE_NAME = ?",
        (name,),
    )
    if not r:
        raise LookupError(f"Package {name!r} not found")
    return {"name": name, "header": (r["header"] or "").strip(), "body": (r["body"] or "").strip() or None,
            "sql_security": _sec(r["sql_security"]), "comment": r["description"]}


def sequence_spec(cat: Catalog, name: str) -> dict[str, Any]:
    r = cat.one(
        "SELECT RDB$INITIAL_VALUE AS start_value, RDB$GENERATOR_INCREMENT AS inc, RDB$SYSTEM_FLAG AS system_flag, "
        "RDB$DESCRIPTION AS description FROM RDB$GENERATORS WHERE RDB$GENERATOR_NAME = ?",
        (name,),
    )
    if not r:
        raise LookupError(f"Sequence {name!r} not found")
    cur = cat.one(f"SELECT GEN_ID({_qi(name)}, 0) AS v FROM RDB$DATABASE")
    return {"name": name, "start": r["start_value"], "increment": r["inc"], "current_value": (cur or {}).get("v"),
            "system_flag": r["system_flag"], "comment": r["description"]}


def domain_spec(cat: Catalog, name: str) -> dict[str, Any]:
    obj = cat.get_object("domains", name)
    p = obj["properties"]
    return {
        "name": name,
        "data_type": p["Data type"],
        "default": _default(p["Default"]),
        "not_null": p["Not null"],
        "check": _strip_kw(p["Check"], "CHECK") or None,
        "collation": p["Collation"] if p["Collation"] and p["Collation"] != _charset_of(p["Data type"]) else None,
        "comment": p["Comment"],
        "used_by": obj["dependencies"]["dependents"],
    }


def _charset_of(dtype: str) -> str | None:
    m = re.search(r"CHARACTER SET (\w+)", dtype or "")
    return m.group(1) if m else None


def exception_spec(cat: Catalog, name: str) -> dict[str, Any]:
    r = cat.one("SELECT RDB$MESSAGE AS message, RDB$EXCEPTION_NUMBER AS num, RDB$DESCRIPTION AS description "
                "FROM RDB$EXCEPTIONS WHERE RDB$EXCEPTION_NAME = ?", (name,))
    if not r:
        raise LookupError(f"Exception {name!r} not found")
    return {"name": name, "message": r["message"], "number": r["num"], "comment": r["description"]}


def role_system_privileges(cat: Catalog, name: str) -> list[str] | None:
    if not cat.has_column("RDB$ROLES", "RDB$SYSTEM_PRIVILEGES"):
        return None
    r = cat.one("SELECT HEX_ENCODE(RDB$SYSTEM_PRIVILEGES) AS h FROM RDB$ROLES WHERE RDB$ROLE_NAME = ?", (name,))
    h = (r or {}).get("h")
    if not h:
        return []
    bits = int.from_bytes(bytes.fromhex(h), "little")
    return [p for i, p in enumerate(SYSTEM_PRIVILEGES, start=1) if bits & (1 << i)]


def role_spec(cat: Catalog, name: str) -> dict[str, Any]:
    r = cat.one("SELECT RDB$DESCRIPTION AS description FROM RDB$ROLES WHERE RDB$ROLE_NAME = ?", (name,))
    if not r:
        raise LookupError(f"Role {name!r} not found")
    members = [
        {"name": g["grantee"], "type": g["grantee_type"], "admin": g["grant_option"]}
        for g in cat.privileges(object_name=name) if g["privilege"] == "MEMBER"
    ]
    return {"name": name, "members": members, "system_privileges": role_system_privileges(cat, name),
            "comment": r["description"]}


def collation_spec(cat: Catalog, name: str) -> dict[str, Any]:
    r = cat.collation_row(name)
    if not r:
        raise LookupError(f"Collation {name!r} not found")
    attrs = r["attrs"] or 0
    from .catalog_extras import _strip_auto_attributes

    return {"name": name, "charset": r["charset"], "base": r["base"], "pad_space": bool(attrs & 1),
            "case_insensitive": bool(attrs & 2), "accent_insensitive": bool(attrs & 4),
            "attributes": _strip_auto_attributes(r["spec"]) or None, "system": bool(r["system_flag"]),
            "comment": r["description"]}


def comments_spec(cat: Catalog, otype: str, name: str) -> dict[str, Any]:
    obj = cat.get_object(otype, name)
    comment = obj["properties"].get("Comment")
    items: list[dict] = []
    if otype in ("tables", "views", "system_tables"):
        items = [{"name": c["name"], "kind": "column", "comment": c["description"]} for c in obj.get("columns") or []]
    elif otype in ("procedures", "functions"):
        items = [{"name": p["name"], "kind": "parameter", "comment": p["description"]}
                 for p in obj.get("parameters") or [] if p.get("name")]
    return {"otype": otype, "name": name, "comment": comment, "items": items}


def _qi(name: str) -> str:
    from .types import quote_ident

    return quote_ident(name)


SPECS = {
    "tables": table_spec, "views": view_spec, "indices": index_spec, "procedures": procedure_spec,
    "functions": function_spec, "triggers": trigger_spec, "packages": package_spec, "sequences": sequence_spec,
    "domains": domain_spec, "exceptions": exception_spec, "roles": role_spec, "collations": collation_spec,
}


def object_spec(cat: Catalog, otype: str, name: str) -> dict[str, Any]:
    fn = SPECS.get(otype)
    if fn is None:
        raise ValueError(f"No dialog for {otype}")
    return fn(cat, name)


def lookups(cat: Catalog) -> dict[str, Any]:
    """Names for pickers in the dialogs."""
    def names(sql: str) -> list[str]:
        return [r["name"] for r in cat.rows(sql)]

    tables = names("SELECT TRIM(RDB$RELATION_NAME) AS name FROM RDB$RELATIONS WHERE RDB$VIEW_BLR IS NULL "
                   "AND COALESCE(RDB$SYSTEM_FLAG, 0) = 0 ORDER BY 1")
    charsets = names("SELECT TRIM(RDB$CHARACTER_SET_NAME) AS name FROM RDB$CHARACTER_SETS ORDER BY 1")
    collations = cat.rows(
        "SELECT TRIM(co.RDB$COLLATION_NAME) AS name, TRIM(cs.RDB$CHARACTER_SET_NAME) AS charset FROM RDB$COLLATIONS co "
        "JOIN RDB$CHARACTER_SETS cs ON cs.RDB$CHARACTER_SET_ID = co.RDB$CHARACTER_SET_ID ORDER BY 2, 1"
    )
    return {
        "engine_major": cat.engine_major,
        "engine_version": cat.engine_version,
        "default_charset": cat.default_charset,
        "tables": tables,
        "views": names("SELECT TRIM(RDB$RELATION_NAME) AS name FROM RDB$RELATIONS WHERE RDB$VIEW_BLR IS NOT NULL "
                       "AND COALESCE(RDB$SYSTEM_FLAG, 0) = 0 ORDER BY 1"),
        "domains": names("SELECT TRIM(RDB$FIELD_NAME) AS name FROM RDB$FIELDS WHERE RDB$FIELD_NAME NOT STARTING "
                         "WITH 'RDB$' AND COALESCE(RDB$SYSTEM_FLAG, 0) = 0 ORDER BY 1"),
        "charsets": charsets,
        "collations": collations,
        "system_privileges": SYSTEM_PRIVILEGES[:26] + (SYSTEM_PRIVILEGES[26:] if cat.engine_major >= 5 else []),
    }


def table_columns(cat: Catalog, table: str) -> list[str]:
    return [c["name"] for c in cat.columns(table)]


def unique_keys(cat: Catalog, table: str) -> list[dict[str, Any]]:
    """PK/UNIQUE constraints of ``table`` (candidate targets for a foreign key)."""
    return [{"name": c["name"], "type": c["type"], "columns": c["columns"]}
            for c in cat.constraints(table) if c["type"] in ("PRIMARY KEY", "UNIQUE")]
