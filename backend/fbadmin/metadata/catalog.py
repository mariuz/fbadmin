"""Introspection of a Firebird database through the RDB$/SEC$/MON$ system tables.

Targets Firebird 3.0 and newer (ODS 12+). Features that only exist in later
versions (e.g. partial indices in Firebird 5) are detected at runtime.
"""

from __future__ import annotations

from functools import cached_property
from typing import Any

from .types import format_type, quote_ident, sql_string

# Object types shown in the object explorer, in display order.
OBJECT_TYPES: dict[str, dict[str, str]] = {
    "tables": {"label": "Tables", "singular": "Table"},
    "views": {"label": "Views", "singular": "View"},
    "procedures": {"label": "Procedures", "singular": "Procedure"},
    "functions": {"label": "Functions", "singular": "Function"},
    "packages": {"label": "Packages", "singular": "Package"},
    "triggers": {"label": "Triggers", "singular": "Trigger"},
    "sequences": {"label": "Sequences", "singular": "Sequence"},
    "domains": {"label": "Domains", "singular": "Domain"},
    "exceptions": {"label": "Exceptions", "singular": "Exception"},
    "indices": {"label": "Indices", "singular": "Index"},
    "roles": {"label": "Roles", "singular": "Role"},
    "users": {"label": "Users", "singular": "User"},
    "system_tables": {"label": "System Tables", "singular": "System Table"},
}

# RDB$DEPENDENCIES object type codes.
DEP_TYPES = {
    0: "tables", 1: "views", 2: "triggers", 3: "computed", 4: "validation", 5: "procedures",
    6: "indices", 7: "exceptions", 8: "users", 9: "domains", 10: "indices", 11: "count",
    14: "sequences", 15: "functions", 17: "collations", 18: "packages", 19: "packages",
}

_DDL_EVENTS = {
    1: "CREATE TABLE", 2: "ALTER TABLE", 3: "DROP TABLE", 4: "CREATE PROCEDURE", 5: "ALTER PROCEDURE",
    6: "DROP PROCEDURE", 7: "CREATE FUNCTION", 8: "ALTER FUNCTION", 9: "DROP FUNCTION", 10: "CREATE TRIGGER",
    11: "ALTER TRIGGER", 12: "DROP TRIGGER", 16: "CREATE EXCEPTION", 17: "ALTER EXCEPTION",
    18: "DROP EXCEPTION", 19: "CREATE VIEW", 20: "ALTER VIEW", 21: "DROP VIEW", 22: "CREATE DOMAIN",
    23: "ALTER DOMAIN", 24: "DROP DOMAIN", 25: "CREATE ROLE", 26: "ALTER ROLE", 27: "DROP ROLE",
    28: "CREATE INDEX", 29: "ALTER INDEX", 30: "DROP INDEX", 31: "CREATE SEQUENCE", 32: "ALTER SEQUENCE",
    33: "DROP SEQUENCE", 34: "CREATE USER", 35: "ALTER USER", 36: "DROP USER", 37: "CREATE COLLATION",
    38: "DROP COLLATION", 39: "ALTER CHARACTER SET", 40: "CREATE PACKAGE", 41: "ALTER PACKAGE",
    42: "DROP PACKAGE", 43: "CREATE PACKAGE BODY", 44: "DROP PACKAGE BODY", 45: "CREATE MAPPING",
    46: "ALTER MAPPING", 47: "DROP MAPPING",
}
_DB_TRIGGER_EVENTS = {
    8192: "ON CONNECT", 8193: "ON DISCONNECT", 8194: "ON TRANSACTION START",
    8195: "ON TRANSACTION COMMIT", 8196: "ON TRANSACTION ROLLBACK",
}
_RELATION_TYPES = {
    0: "Persistent", 1: "View", 2: "External", 3: "Virtual",
    4: "Global temporary (on commit preserve rows)", 5: "Global temporary (on commit delete rows)",
}

_TYPE_COLS = """
  f.RDB$FIELD_TYPE AS ftype, f.RDB$FIELD_SUB_TYPE AS subtype, f.RDB$FIELD_LENGTH AS flen,
  f.RDB$FIELD_PRECISION AS fprec, f.RDB$FIELD_SCALE AS fscale, f.RDB$CHARACTER_LENGTH AS clen,
  f.RDB$SEGMENT_LENGTH AS seglen, TRIM(cs.RDB$CHARACTER_SET_NAME) AS charset
"""


def decode_trigger_type(ttype: int) -> tuple[str, str]:
    """Return (kind, event clause) for RDB$TRIGGER_TYPE."""
    if ttype is None:
        return "dml", ""
    if ttype in _DB_TRIGGER_EVENTS:
        return "database", _DB_TRIGGER_EVENTS[ttype]
    if ttype & 0x4000:  # DDL trigger
        when = "AFTER" if ttype & 1 else "BEFORE"
        events = [name for bit, name in _DDL_EVENTS.items() if ttype & (1 << bit)]
        if len(events) == len(_DDL_EVENTS):
            return "ddl", f"{when} ANY DDL STATEMENT"
        return "ddl", f"{when} " + " OR ".join(events)
    when = "AFTER" if (ttype + 1) & 1 else "BEFORE"
    actions = []
    for slot in (1, 2, 3):
        code = ((ttype + 1) >> (slot * 2 - 1)) & 3
        if code:
            actions.append({1: "INSERT", 2: "UPDATE", 3: "DELETE"}[code])
    return "dml", f"{when} " + " OR ".join(actions)


def _is_user_domain(name: str | None) -> bool:
    return bool(name) and not name.startswith("RDB$")


def _is_auto_name(name: str | None) -> bool:
    return not name or name.startswith("INTEG_") or name.startswith("RDB$")


class Catalog:
    """Metadata reader bound to a single open connection."""

    def __init__(self, conn):
        self.conn = conn
        self._seg_cache: dict[str, list[str]] = {}
        self._col_cache: dict[tuple[str, str], bool] = {}

    # ----------------------------------------------------------------- utils
    def rows(self, sql: str, params: tuple | list = ()) -> list[dict[str, Any]]:
        cur = self.conn.cursor()
        cur.execute(sql, list(params))
        if cur.description is None:
            return []
        names = [d[0].lower() for d in cur.description]
        data = cur.fetchall() or []
        return [dict(zip(names, r)) for r in data]

    def one(self, sql: str, params: tuple | list = ()) -> dict[str, Any] | None:
        r = self.rows(sql, params)
        return r[0] if r else None

    def has_column(self, relation: str, column: str) -> bool:
        key = (relation, column)
        if key not in self._col_cache:
            r = self.one(
                "SELECT COUNT(*) AS n FROM RDB$RELATION_FIELDS WHERE RDB$RELATION_NAME = ? AND RDB$FIELD_NAME = ?",
                (relation, column),
            )
            self._col_cache[key] = bool(r and r["n"])
        return self._col_cache[key]

    @cached_property
    def default_charset(self) -> str:
        r = self.one("SELECT TRIM(RDB$CHARACTER_SET_NAME) AS cs FROM RDB$DATABASE")
        return (r or {}).get("cs") or "NONE"

    @cached_property
    def engine_version(self) -> str:
        r = self.one("SELECT RDB$GET_CONTEXT('SYSTEM', 'ENGINE_VERSION') AS v FROM RDB$DATABASE")
        return (r or {}).get("v") or ""

    @cached_property
    def engine_major(self) -> int:
        try:
            return int(self.engine_version.split(".")[0])
        except ValueError:
            return 3

    def _type(self, r: dict) -> str:
        return format_type(
            r.get("ftype"), r.get("subtype"), r.get("flen"), r.get("fprec"), r.get("fscale"),
            r.get("clen"), r.get("seglen"), r.get("charset"), self.default_charset,
        )

    def index_segments(self, index_name: str) -> list[str]:
        if index_name not in self._seg_cache:
            self._seg_cache[index_name] = [
                r["fld"]
                for r in self.rows(
                    "SELECT TRIM(RDB$FIELD_NAME) AS fld FROM RDB$INDEX_SEGMENTS "
                    "WHERE RDB$INDEX_NAME = ? ORDER BY RDB$FIELD_POSITION",
                    (index_name,),
                )
            ]
        return self._seg_cache[index_name]

    # ------------------------------------------------------------ database
    def database_info(self) -> dict[str, Any]:
        info: dict[str, Any] = {}
        mon = self.one("SELECT * FROM MON$DATABASE") or {}
        for k, v in mon.items():
            info[k.replace("mon$", "")] = v.strip() if isinstance(v, str) else v
        extra = self.one(
            "SELECT TRIM(RDB$CHARACTER_SET_NAME) AS charset, CURRENT_USER AS cur_user, CURRENT_ROLE AS cur_role, "
            "RDB$GET_CONTEXT('SYSTEM','ENGINE_VERSION') AS engine_version, "
            "RDB$GET_CONTEXT('SYSTEM','NETWORK_PROTOCOL') AS protocol, "
            "RDB$GET_CONTEXT('SYSTEM','WIRE_ENCRYPTED') AS wire_encrypted, "
            "(SELECT COUNT(*) FROM MON$ATTACHMENTS) AS attachments "
            "FROM RDB$DATABASE"
        ) or {}
        info.update({k: (v.strip() if isinstance(v, str) else v) for k, v in extra.items()})
        return info

    def object_counts(self) -> dict[str, int]:
        out = {}
        for t in OBJECT_TYPES:
            if t in ("users", "system_tables"):
                continue
            try:
                out[t] = len(self.list_objects(t))
            except Exception:
                out[t] = 0
        return out

    # -------------------------------------------------------------- listing
    def list_objects(self, otype: str) -> list[dict[str, Any]]:
        fn = getattr(self, f"_list_{otype}", None)
        if fn is None:
            raise ValueError(f"Unknown object type {otype!r}")
        return fn()

    def _list_tables(self, system: bool = False):
        return self.rows(
            "SELECT TRIM(RDB$RELATION_NAME) AS name, RDB$RELATION_TYPE AS relation_type, "
            "TRIM(RDB$OWNER_NAME) AS owner, RDB$DESCRIPTION AS description FROM RDB$RELATIONS "
            "WHERE RDB$VIEW_BLR IS NULL AND COALESCE(RDB$SYSTEM_FLAG, 0) " + ("<> 0" if system else "= 0") +
            " ORDER BY RDB$RELATION_NAME"
        )

    def _list_system_tables(self):
        return self._list_tables(system=True)

    def _list_views(self):
        return self.rows(
            "SELECT TRIM(RDB$RELATION_NAME) AS name, TRIM(RDB$OWNER_NAME) AS owner, RDB$DESCRIPTION AS description "
            "FROM RDB$RELATIONS WHERE RDB$VIEW_BLR IS NOT NULL AND COALESCE(RDB$SYSTEM_FLAG, 0) = 0 "
            "ORDER BY RDB$RELATION_NAME"
        )

    def _list_procedures(self):
        return self.rows(
            "SELECT TRIM(RDB$PROCEDURE_NAME) AS name, RDB$PROCEDURE_TYPE AS procedure_type, "
            "RDB$VALID_BLR AS valid, TRIM(RDB$OWNER_NAME) AS owner, RDB$DESCRIPTION AS description "
            "FROM RDB$PROCEDURES WHERE COALESCE(RDB$SYSTEM_FLAG, 0) = 0 AND RDB$PACKAGE_NAME IS NULL "
            "ORDER BY RDB$PROCEDURE_NAME"
        )

    def _list_functions(self):
        return self.rows(
            "SELECT TRIM(RDB$FUNCTION_NAME) AS name, RDB$LEGACY_FLAG AS legacy, RDB$VALID_BLR AS valid, "
            "TRIM(RDB$OWNER_NAME) AS owner, RDB$DESCRIPTION AS description FROM RDB$FUNCTIONS "
            "WHERE COALESCE(RDB$SYSTEM_FLAG, 0) = 0 AND RDB$PACKAGE_NAME IS NULL ORDER BY RDB$FUNCTION_NAME"
        )

    def _list_packages(self):
        return self.rows(
            "SELECT TRIM(RDB$PACKAGE_NAME) AS name, RDB$VALID_BODY_FLAG AS valid, TRIM(RDB$OWNER_NAME) AS owner, "
            "RDB$DESCRIPTION AS description FROM RDB$PACKAGES WHERE COALESCE(RDB$SYSTEM_FLAG, 0) = 0 "
            "ORDER BY RDB$PACKAGE_NAME"
        )

    def _list_triggers(self):
        rows = self.rows(
            "SELECT TRIM(t.RDB$TRIGGER_NAME) AS name, TRIM(t.RDB$RELATION_NAME) AS relation, "
            "t.RDB$TRIGGER_TYPE AS trigger_type, t.RDB$TRIGGER_INACTIVE AS inactive, "
            "t.RDB$TRIGGER_SEQUENCE AS pos, t.RDB$DESCRIPTION AS description FROM RDB$TRIGGERS t "
            "WHERE COALESCE(t.RDB$SYSTEM_FLAG, 0) = 0 AND NOT EXISTS (SELECT 1 FROM RDB$CHECK_CONSTRAINTS cc "
            "WHERE cc.RDB$TRIGGER_NAME = t.RDB$TRIGGER_NAME) ORDER BY t.RDB$TRIGGER_NAME"
        )
        for r in rows:
            r["kind"], r["event"] = decode_trigger_type(r["trigger_type"])
            r["position"] = r.pop("pos")
        return rows

    def _list_sequences(self):
        return self.rows(
            "SELECT TRIM(RDB$GENERATOR_NAME) AS name, RDB$INITIAL_VALUE AS initial_value, "
            "RDB$GENERATOR_INCREMENT AS increment, RDB$DESCRIPTION AS description FROM RDB$GENERATORS "
            "WHERE COALESCE(RDB$SYSTEM_FLAG, 0) = 0 ORDER BY RDB$GENERATOR_NAME"
        )

    def _list_domains(self):
        rows = self.rows(
            "SELECT TRIM(f.RDB$FIELD_NAME) AS name, " + _TYPE_COLS + ", f.RDB$DESCRIPTION AS description "
            "FROM RDB$FIELDS f LEFT JOIN RDB$CHARACTER_SETS cs ON cs.RDB$CHARACTER_SET_ID = f.RDB$CHARACTER_SET_ID "
            "WHERE f.RDB$FIELD_NAME NOT STARTING WITH 'RDB$' AND COALESCE(f.RDB$SYSTEM_FLAG, 0) = 0 "
            "ORDER BY f.RDB$FIELD_NAME"
        )
        return [{"name": r["name"], "data_type": self._type(r), "description": r["description"]} for r in rows]

    def _list_exceptions(self):
        return self.rows(
            "SELECT TRIM(RDB$EXCEPTION_NAME) AS name, RDB$EXCEPTION_NUMBER AS number, RDB$MESSAGE AS message, "
            "RDB$DESCRIPTION AS description FROM RDB$EXCEPTIONS WHERE COALESCE(RDB$SYSTEM_FLAG, 0) = 0 "
            "ORDER BY RDB$EXCEPTION_NAME"
        )

    def _list_indices(self):
        return self.rows(
            "SELECT TRIM(i.RDB$INDEX_NAME) AS name, TRIM(i.RDB$RELATION_NAME) AS relation, "
            "i.RDB$UNIQUE_FLAG AS is_unique, i.RDB$INDEX_TYPE AS descending, i.RDB$INDEX_INACTIVE AS inactive, "
            "i.RDB$STATISTICS AS selectivity FROM RDB$INDICES i "
            "JOIN RDB$RELATIONS r ON r.RDB$RELATION_NAME = i.RDB$RELATION_NAME "
            "WHERE COALESCE(r.RDB$SYSTEM_FLAG, 0) = 0 ORDER BY i.RDB$INDEX_NAME"
        )

    def _list_roles(self):
        return self.rows(
            "SELECT TRIM(RDB$ROLE_NAME) AS name, TRIM(RDB$OWNER_NAME) AS owner, RDB$DESCRIPTION AS description "
            "FROM RDB$ROLES WHERE COALESCE(RDB$SYSTEM_FLAG, 0) = 0 ORDER BY RDB$ROLE_NAME"
        )

    def _list_users(self):
        return self.rows(
            "SELECT TRIM(SEC$USER_NAME) AS name, TRIM(SEC$FIRST_NAME) AS first_name, "
            "TRIM(SEC$MIDDLE_NAME) AS middle_name, TRIM(SEC$LAST_NAME) AS last_name, SEC$ACTIVE AS active, "
            "SEC$ADMIN AS is_admin, TRIM(SEC$PLUGIN) AS plugin, SEC$DESCRIPTION AS description "
            "FROM SEC$USERS ORDER BY SEC$USER_NAME"
        )

    # ------------------------------------------------------------- columns
    def columns(self, relation: str) -> list[dict[str, Any]]:
        rows = self.rows(
            "SELECT TRIM(rf.RDB$FIELD_NAME) AS name, rf.RDB$FIELD_POSITION AS pos, "
            "TRIM(rf.RDB$FIELD_SOURCE) AS field_source, " + _TYPE_COLS + ", "
            "TRIM(co.RDB$COLLATION_NAME) AS collation, rf.RDB$COLLATION_ID AS col_collation_id, "
            "rf.RDB$NULL_FLAG AS col_not_null, f.RDB$NULL_FLAG AS dom_not_null, "
            "rf.RDB$DEFAULT_SOURCE AS col_default, f.RDB$DEFAULT_SOURCE AS dom_default, "
            "f.RDB$COMPUTED_SOURCE AS computed, rf.RDB$IDENTITY_TYPE AS identity_type, "
            "g.RDB$INITIAL_VALUE AS id_start, g.RDB$GENERATOR_INCREMENT AS id_increment, "
            "rf.RDB$DESCRIPTION AS description "
            "FROM RDB$RELATION_FIELDS rf JOIN RDB$FIELDS f ON f.RDB$FIELD_NAME = rf.RDB$FIELD_SOURCE "
            "LEFT JOIN RDB$CHARACTER_SETS cs ON cs.RDB$CHARACTER_SET_ID = f.RDB$CHARACTER_SET_ID "
            "LEFT JOIN RDB$COLLATIONS co ON co.RDB$COLLATION_ID = COALESCE(rf.RDB$COLLATION_ID, f.RDB$COLLATION_ID) "
            "  AND co.RDB$CHARACTER_SET_ID = f.RDB$CHARACTER_SET_ID "
            "LEFT JOIN RDB$GENERATORS g ON g.RDB$GENERATOR_NAME = rf.RDB$GENERATOR_NAME "
            "WHERE rf.RDB$RELATION_NAME = ? ORDER BY rf.RDB$FIELD_POSITION",
            (relation,),
        )
        pk_cols = set()
        for c in self.constraints(relation):
            if c["type"] == "PRIMARY KEY":
                pk_cols.update(c["columns"])
        out = []
        for r in rows:
            user_domain = _is_user_domain(r["field_source"])
            out.append(
                {
                    "name": r["name"],
                    "position": r["pos"],
                    "data_type": self._type(r),
                    "domain": r["field_source"] if user_domain else None,
                    "not_null": bool(r["col_not_null"] or r["dom_not_null"]),
                    "col_not_null": bool(r["col_not_null"]),
                    "default": (r["col_default"] or r["dom_default"] or "").strip() or None,
                    "col_default": (r["col_default"] or "").strip() or None,
                    "computed": (r["computed"] or "").strip() or None,
                    "identity": {0: "ALWAYS", 1: "BY DEFAULT"}.get(r["identity_type"]),
                    "identity_start": r["id_start"],
                    "identity_increment": r["id_increment"],
                    "charset": r["charset"],
                    "collation": r["collation"] if r["col_collation_id"] is not None else None,
                    "primary_key": r["name"] in pk_cols,
                    "description": r["description"],
                    # raw type info for the data editor
                    "field_type": r["ftype"],
                    "field_sub_type": r["subtype"],
                }
            )
        return out

    # --------------------------------------------------------- constraints
    def constraints(self, relation: str) -> list[dict[str, Any]]:
        rows = self.rows(
            "SELECT TRIM(rc.RDB$CONSTRAINT_NAME) AS name, TRIM(rc.RDB$CONSTRAINT_TYPE) AS ctype, "
            "TRIM(rc.RDB$INDEX_NAME) AS index_name, i.RDB$INDEX_TYPE AS index_desc, "
            "TRIM(refc.RDB$UPDATE_RULE) AS update_rule, TRIM(refc.RDB$DELETE_RULE) AS delete_rule, "
            "TRIM(i2.RDB$RELATION_NAME) AS ref_table, TRIM(i2.RDB$INDEX_NAME) AS ref_index "
            "FROM RDB$RELATION_CONSTRAINTS rc "
            "LEFT JOIN RDB$INDICES i ON i.RDB$INDEX_NAME = rc.RDB$INDEX_NAME "
            "LEFT JOIN RDB$REF_CONSTRAINTS refc ON refc.RDB$CONSTRAINT_NAME = rc.RDB$CONSTRAINT_NAME "
            "LEFT JOIN RDB$RELATION_CONSTRAINTS rc2 ON rc2.RDB$CONSTRAINT_NAME = refc.RDB$CONST_NAME_UQ "
            "LEFT JOIN RDB$INDICES i2 ON i2.RDB$INDEX_NAME = rc2.RDB$INDEX_NAME "
            "WHERE rc.RDB$RELATION_NAME = ? AND rc.RDB$CONSTRAINT_TYPE IN "
            "('PRIMARY KEY', 'UNIQUE', 'FOREIGN KEY', 'CHECK') "
            "ORDER BY CASE TRIM(rc.RDB$CONSTRAINT_TYPE) WHEN 'PRIMARY KEY' THEN 1 WHEN 'UNIQUE' THEN 2 "
            "WHEN 'FOREIGN KEY' THEN 3 ELSE 4 END, rc.RDB$CONSTRAINT_NAME",
            (relation,),
        )
        out = []
        for r in rows:
            item = {
                "name": r["name"],
                "type": r["ctype"],
                "index": r["index_name"],
                "descending": bool(r["index_desc"]),
                "columns": self.index_segments(r["index_name"]) if r["index_name"] else [],
            }
            if r["ctype"] == "FOREIGN KEY":
                item.update(
                    ref_table=r["ref_table"],
                    ref_columns=self.index_segments(r["ref_index"]) if r["ref_index"] else [],
                    on_update=r["update_rule"],
                    on_delete=r["delete_rule"],
                )
            if r["ctype"] == "CHECK":
                src = self.one(
                    "SELECT FIRST 1 t.RDB$TRIGGER_SOURCE AS src FROM RDB$CHECK_CONSTRAINTS cc "
                    "JOIN RDB$TRIGGERS t ON t.RDB$TRIGGER_NAME = cc.RDB$TRIGGER_NAME "
                    "WHERE cc.RDB$CONSTRAINT_NAME = ?",
                    (r["name"],),
                )
                item["source"] = (src or {}).get("src", "").strip()
            out.append(item)
        return out

    def _constraint_ddl(self, c: dict) -> str:
        head = "" if _is_auto_name(c["name"]) else f"CONSTRAINT {quote_ident(c['name'])} "
        cols = ", ".join(quote_ident(x) for x in c["columns"])
        if c["type"] == "CHECK":
            return head + c.get("source", "")
        if c["type"] == "FOREIGN KEY":
            refcols = ", ".join(quote_ident(x) for x in c["ref_columns"])
            s = f"{head}FOREIGN KEY ({cols}) REFERENCES {quote_ident(c['ref_table'])} ({refcols})"
            if c.get("on_update") and c["on_update"] != "RESTRICT":
                s += f" ON UPDATE {c['on_update']}"
            if c.get("on_delete") and c["on_delete"] != "RESTRICT":
                s += f" ON DELETE {c['on_delete']}"
        else:
            s = f"{head}{c['type']} ({cols})"
        idx = c.get("index")
        if idx and idx != c["name"] and not idx.startswith("RDB$"):
            s += f" USING {'DESC ' if c.get('descending') else ''}INDEX {quote_ident(idx)}"
        return s

    # ------------------------------------------------------------- indices
    def indices(self, relation: str | None = None, name: str | None = None) -> list[dict[str, Any]]:
        cond_col = ", i.RDB$CONDITION_SOURCE AS cond" if self.has_column("RDB$INDICES", "RDB$CONDITION_SOURCE") else ""
        where = "i.RDB$RELATION_NAME = ?" if relation else "i.RDB$INDEX_NAME = ?"
        rows = self.rows(
            "SELECT TRIM(i.RDB$INDEX_NAME) AS name, TRIM(i.RDB$RELATION_NAME) AS relation, "
            "i.RDB$UNIQUE_FLAG AS is_unique, i.RDB$INDEX_TYPE AS descending, i.RDB$INDEX_INACTIVE AS inactive, "
            "i.RDB$EXPRESSION_SOURCE AS expr, i.RDB$STATISTICS AS selectivity, "
            "TRIM(rc.RDB$CONSTRAINT_NAME) AS constraint_name, TRIM(rc.RDB$CONSTRAINT_TYPE) AS constraint_type, "
            "i.RDB$DESCRIPTION AS description" + cond_col + " FROM RDB$INDICES i "
            "LEFT JOIN RDB$RELATION_CONSTRAINTS rc ON rc.RDB$INDEX_NAME = i.RDB$INDEX_NAME "
            "WHERE " + where + " ORDER BY i.RDB$INDEX_NAME",
            (relation or name,),
        )
        for r in rows:
            r["columns"] = self.index_segments(r["name"])
            r["is_unique"] = bool(r["is_unique"])
            r["descending"] = bool(r["descending"])
            r["inactive"] = bool(r["inactive"])
            r["expr"] = (r.get("expr") or "").strip() or None
            r["cond"] = (r.get("cond") or "").strip() or None
        return rows

    def _index_ddl(self, i: dict) -> str:
        s = "CREATE " + ("UNIQUE " if i["is_unique"] else "") + ("DESCENDING " if i["descending"] else "")
        s += f"INDEX {quote_ident(i['name'])} ON {quote_ident(i['relation'])}"
        if i["expr"]:
            s += f" COMPUTED BY {i['expr']}"
        else:
            s += " (" + ", ".join(quote_ident(c) for c in i["columns"]) + ")"
        if i.get("cond"):
            s += f" {i['cond']}"
        s += ";"
        if i["inactive"]:
            s += f"\nALTER INDEX {quote_ident(i['name'])} INACTIVE;"
        return s

    # ------------------------------------------------------------ triggers
    def triggers(self, relation: str | None = None, name: str | None = None) -> list[dict[str, Any]]:
        where = "t.RDB$RELATION_NAME = ?" if relation else "t.RDB$TRIGGER_NAME = ?"
        rows = self.rows(
            "SELECT TRIM(t.RDB$TRIGGER_NAME) AS name, TRIM(t.RDB$RELATION_NAME) AS relation, "
            "t.RDB$TRIGGER_TYPE AS trigger_type, t.RDB$TRIGGER_SEQUENCE AS pos, "
            "t.RDB$TRIGGER_INACTIVE AS inactive, t.RDB$TRIGGER_SOURCE AS source, "
            "TRIM(t.RDB$ENGINE_NAME) AS engine, TRIM(t.RDB$ENTRYPOINT) AS entrypoint, "
            "t.RDB$DESCRIPTION AS description FROM RDB$TRIGGERS t WHERE " + where +
            " AND COALESCE(t.RDB$SYSTEM_FLAG, 0) = 0 AND NOT EXISTS (SELECT 1 FROM RDB$CHECK_CONSTRAINTS cc "
            "WHERE cc.RDB$TRIGGER_NAME = t.RDB$TRIGGER_NAME) ORDER BY t.RDB$TRIGGER_SEQUENCE, t.RDB$TRIGGER_NAME",
            (relation or name,),
        )
        for r in rows:
            r["kind"], r["event"] = decode_trigger_type(r["trigger_type"])
            r["inactive"] = bool(r["inactive"])
            r["position"] = r.pop("pos")
        return rows

    def _trigger_ddl(self, t: dict) -> str:
        s = f"CREATE OR ALTER TRIGGER {quote_ident(t['name'])}"
        if t["relation"]:
            s += f" FOR {quote_ident(t['relation'])}"
        s += f"\n{'INACTIVE' if t['inactive'] else 'ACTIVE'} {t['event']} POSITION {t['position'] or 0}\n"
        s += _routine_body(t["source"], t.get("engine"), t.get("entrypoint"), source_has_as=True)
        return s

    # ---------------------------------------------------------- parameters
    def _param_decl(self, r: dict) -> str:
        if r.get("mech") == 1 and r.get("rel"):
            t = f"TYPE OF COLUMN {quote_ident(r['rel'])}.{quote_ident(r['fld'])}"
        elif _is_user_domain(r.get("field_source")):
            t = ("TYPE OF " if r.get("mech") == 1 else "") + quote_ident(r["field_source"])
        else:
            t = self._type(r)
        if r.get("not_null"):
            t += " NOT NULL"
        if r.get("collation") and r.get("collation") != r.get("charset"):
            t += f" COLLATE {r['collation']}"
        if r.get("dflt"):
            t += " " + r["dflt"].strip()
        return t

    def procedure_params(self, name: str, package: str | None = None) -> list[dict[str, Any]]:
        pkg = "pp.RDB$PACKAGE_NAME = ?" if package else "pp.RDB$PACKAGE_NAME IS NULL"
        rows = self.rows(
            "SELECT TRIM(pp.RDB$PARAMETER_NAME) AS name, pp.RDB$PARAMETER_TYPE AS ptype, "
            "pp.RDB$PARAMETER_NUMBER AS num, TRIM(pp.RDB$FIELD_SOURCE) AS field_source, "
            "pp.RDB$PARAMETER_MECHANISM AS mech, TRIM(pp.RDB$RELATION_NAME) AS rel, TRIM(pp.RDB$FIELD_NAME) AS fld, "
            "pp.RDB$DEFAULT_SOURCE AS dflt, pp.RDB$NULL_FLAG AS not_null, " + _TYPE_COLS + ", "
            "TRIM(co.RDB$COLLATION_NAME) AS collation, pp.RDB$DESCRIPTION AS description "
            "FROM RDB$PROCEDURE_PARAMETERS pp JOIN RDB$FIELDS f ON f.RDB$FIELD_NAME = pp.RDB$FIELD_SOURCE "
            "LEFT JOIN RDB$CHARACTER_SETS cs ON cs.RDB$CHARACTER_SET_ID = f.RDB$CHARACTER_SET_ID "
            "LEFT JOIN RDB$COLLATIONS co ON co.RDB$COLLATION_ID = pp.RDB$COLLATION_ID "
            "  AND co.RDB$CHARACTER_SET_ID = f.RDB$CHARACTER_SET_ID "
            "WHERE pp.RDB$PROCEDURE_NAME = ? AND " + pkg + " ORDER BY pp.RDB$PARAMETER_TYPE, pp.RDB$PARAMETER_NUMBER",
            (name, package) if package else (name,),
        )
        return [
            {
                "name": r["name"],
                "direction": "OUT" if r["ptype"] == 1 else "IN",
                "data_type": self._param_decl(r),
                "description": r["description"],
            }
            for r in rows
        ]

    def function_args(self, name: str, package: str | None = None) -> list[dict[str, Any]]:
        pkg = "fa.RDB$PACKAGE_NAME = ?" if package else "fa.RDB$PACKAGE_NAME IS NULL"
        rows = self.rows(
            "SELECT TRIM(fa.RDB$ARGUMENT_NAME) AS name, fa.RDB$ARGUMENT_POSITION AS num, "
            "TRIM(fa.RDB$FIELD_SOURCE) AS field_source, fa.RDB$ARGUMENT_MECHANISM AS mech, "
            "TRIM(fa.RDB$RELATION_NAME) AS rel, TRIM(fa.RDB$FIELD_NAME) AS fld, fa.RDB$DEFAULT_SOURCE AS dflt, "
            "fa.RDB$NULL_FLAG AS not_null, "
            "COALESCE(f.RDB$FIELD_TYPE, fa.RDB$FIELD_TYPE) AS ftype, "
            "COALESCE(f.RDB$FIELD_SUB_TYPE, fa.RDB$FIELD_SUB_TYPE) AS subtype, "
            "COALESCE(f.RDB$FIELD_LENGTH, fa.RDB$FIELD_LENGTH) AS flen, "
            "COALESCE(f.RDB$FIELD_PRECISION, fa.RDB$FIELD_PRECISION) AS fprec, "
            "COALESCE(f.RDB$FIELD_SCALE, fa.RDB$FIELD_SCALE) AS fscale, "
            "COALESCE(f.RDB$CHARACTER_LENGTH, fa.RDB$CHARACTER_LENGTH) AS clen, f.RDB$SEGMENT_LENGTH AS seglen, "
            "TRIM(cs.RDB$CHARACTER_SET_NAME) AS charset, TRIM(co.RDB$COLLATION_NAME) AS collation, "
            "fa.RDB$DESCRIPTION AS description "
            "FROM RDB$FUNCTION_ARGUMENTS fa LEFT JOIN RDB$FIELDS f ON f.RDB$FIELD_NAME = fa.RDB$FIELD_SOURCE "
            "LEFT JOIN RDB$CHARACTER_SETS cs ON cs.RDB$CHARACTER_SET_ID = "
            "  COALESCE(f.RDB$CHARACTER_SET_ID, fa.RDB$CHARACTER_SET_ID) "
            "LEFT JOIN RDB$COLLATIONS co ON co.RDB$COLLATION_ID = fa.RDB$COLLATION_ID "
            "  AND co.RDB$CHARACTER_SET_ID = f.RDB$CHARACTER_SET_ID "
            "WHERE fa.RDB$FUNCTION_NAME = ? AND " + pkg + " ORDER BY fa.RDB$ARGUMENT_POSITION",
            (name, package) if package else (name,),
        )
        return [
            {
                "name": r["name"],
                "position": r["num"],
                "data_type": self._param_decl(r),
                "description": r["description"],
            }
            for r in rows
        ]

    # ------------------------------------------------------------- details
    def get_object(self, otype: str, name: str) -> dict[str, Any]:
        fn = getattr(self, f"_get_{otype}", None)
        if fn is None:
            raise ValueError(f"Unknown object type {otype!r}")
        obj = fn(name)
        if obj is None:
            raise LookupError(f"{OBJECT_TYPES.get(otype, {}).get('singular', otype)} {name!r} not found")
        obj.setdefault("type", otype)
        obj.setdefault("name", name)
        if otype not in ("users",):
            obj["dependencies"] = self.dependencies(name)
        return obj

    def _get_tables(self, name: str):
        r = self.one(
            "SELECT TRIM(RDB$RELATION_NAME) AS name, RDB$RELATION_TYPE AS relation_type, "
            "TRIM(RDB$OWNER_NAME) AS owner, RDB$EXTERNAL_FILE AS external_file, RDB$DESCRIPTION AS description, "
            "RDB$SYSTEM_FLAG AS system_flag FROM RDB$RELATIONS WHERE RDB$RELATION_NAME = ? AND RDB$VIEW_BLR IS NULL",
            (name,),
        )
        if not r:
            return None
        cols = self.columns(name)
        cons = self.constraints(name)
        idx = self.indices(relation=name)
        trg = self.triggers(relation=name)
        props = {
            "Name": r["name"],
            "Type": _RELATION_TYPES.get(r["relation_type"], r["relation_type"]),
            "Owner": r["owner"],
            "Columns": len(cols),
            "System table": bool(r["system_flag"]),
            "External file": (r["external_file"] or "").strip() or None,
            "Comment": r["description"],
        }
        obj = {"properties": props, "columns": cols, "constraints": cons, "indices": idx, "triggers": trg}
        obj["ddl"] = self._table_ddl(r, cols, cons, idx, trg)
        return obj

    _get_system_tables = _get_tables

    def _table_ddl(self, t, cols, cons, idx, trg) -> str:
        qn = quote_ident(t["name"])
        rtype = t["relation_type"]
        head = "CREATE GLOBAL TEMPORARY TABLE" if rtype in (4, 5) else "CREATE TABLE"
        lines = []
        for c in cols:
            s = f"    {quote_ident(c['name'])} "
            if c["computed"]:
                s += f"COMPUTED BY {c['computed']}"
            else:
                s += quote_ident(c["domain"]) if c["domain"] else c["data_type"]
                if c["identity"]:
                    s += f" GENERATED {c['identity']} AS IDENTITY"
                    opts = []
                    if c["identity_start"] not in (None, 0, 1):
                        opts.append(f"START WITH {c['identity_start']}")
                    if c["identity_increment"] not in (None, 1):
                        opts.append(f"INCREMENT BY {c['identity_increment']}")
                    if opts:
                        s += " (" + " ".join(opts) + ")"
                if c["col_default"]:
                    s += " " + c["col_default"]
                if c["col_not_null"]:
                    s += " NOT NULL"
                if c["collation"] and c["collation"] != (c["charset"] or ""):
                    s += f" COLLATE {c['collation']}"
            lines.append(s)
        inline = [c for c in cons if c["type"] in ("PRIMARY KEY", "UNIQUE")]
        for c in inline:
            lines.append("    " + self._constraint_ddl(c))
        out = f"{head} {qn}"
        if t.get("external_file"):
            out += f" EXTERNAL FILE {sql_string(t['external_file'].strip())}"
        out += " (\n" + ",\n".join(lines) + "\n)"
        if rtype == 4:
            out += " ON COMMIT PRESERVE ROWS"
        elif rtype == 5:
            out += " ON COMMIT DELETE ROWS"
        out += ";\n"
        for c in cons:
            if c["type"] in ("FOREIGN KEY", "CHECK"):
                out += f"\nALTER TABLE {qn} ADD {self._constraint_ddl(c)};"
        user_idx = [i for i in idx if not i["constraint_name"]]
        if user_idx:
            out += "\n" + "\n".join(self._index_ddl(i) for i in user_idx)
        for tr in trg:
            out += "\n\n" + self._trigger_ddl(tr) + ";"
        comments = []
        if t.get("description"):
            comments.append(f"COMMENT ON TABLE {qn} IS {sql_string(t['description'])};")
        for c in cols:
            if c["description"]:
                comments.append(
                    f"COMMENT ON COLUMN {qn}.{quote_ident(c['name'])} IS {sql_string(c['description'])};"
                )
        if comments:
            out += "\n\n" + "\n".join(comments)
        return out.strip() + "\n"

    def _get_views(self, name: str):
        r = self.one(
            "SELECT TRIM(RDB$RELATION_NAME) AS name, TRIM(RDB$OWNER_NAME) AS owner, RDB$VIEW_SOURCE AS source, "
            "RDB$DESCRIPTION AS description FROM RDB$RELATIONS WHERE RDB$RELATION_NAME = ? "
            "AND RDB$VIEW_BLR IS NOT NULL",
            (name,),
        )
        if not r:
            return None
        cols = self.columns(name)
        trg = self.triggers(relation=name)
        qn = quote_ident(name)
        ddl = f"CREATE OR ALTER VIEW {qn} (\n"
        ddl += ",\n".join(f"    {quote_ident(c['name'])}" for c in cols)
        ddl += f")\nAS\n{(r['source'] or '').strip()};\n"
        for tr in trg:
            ddl += "\n" + self._trigger_ddl(tr) + ";\n"
        if r["description"]:
            ddl += f"\nCOMMENT ON VIEW {qn} IS {sql_string(r['description'])};\n"
        return {
            "properties": {"Name": r["name"], "Owner": r["owner"], "Columns": len(cols), "Comment": r["description"]},
            "columns": cols,
            "triggers": trg,
            "source": r["source"],
            "ddl": ddl,
        }

    def _get_procedures(self, name: str):
        r = self.one(
            "SELECT TRIM(RDB$PROCEDURE_NAME) AS name, RDB$PROCEDURE_TYPE AS ptype, RDB$PROCEDURE_SOURCE AS source, "
            "RDB$VALID_BLR AS valid, TRIM(RDB$OWNER_NAME) AS owner, TRIM(RDB$ENGINE_NAME) AS engine, "
            "TRIM(RDB$ENTRYPOINT) AS entrypoint, RDB$DESCRIPTION AS description FROM RDB$PROCEDURES "
            "WHERE RDB$PROCEDURE_NAME = ? AND RDB$PACKAGE_NAME IS NULL",
            (name,),
        )
        if not r:
            return None
        params = self.procedure_params(name)
        ddl = self._procedure_ddl(name, params, r) + ";\n"
        if r["description"]:
            ddl += f"\nCOMMENT ON PROCEDURE {quote_ident(name)} IS {sql_string(r['description'])};\n"
        return {
            "properties": {
                "Name": r["name"],
                "Kind": {1: "Selectable", 2: "Executable"}.get(r["ptype"], "Legacy"),
                "Owner": r["owner"],
                "Valid": bool(r["valid"]),
                "Engine": r["engine"],
                "Comment": r["description"],
            },
            "parameters": params,
            "source": r["source"],
            "ddl": ddl,
        }

    def _procedure_ddl(self, name, params, r, keyword="CREATE OR ALTER PROCEDURE") -> str:
        ins = [p for p in params if p["direction"] == "IN"]
        outs = [p for p in params if p["direction"] == "OUT"]
        s = f"{keyword} {quote_ident(name)}"
        if ins:
            s += " (\n" + ",\n".join(f"    {quote_ident(p['name'])} {p['data_type']}" for p in ins) + ")"
        if outs:
            s += "\nRETURNS (\n" + ",\n".join(f"    {quote_ident(p['name'])} {p['data_type']}" for p in outs) + ")"
        if r is None:
            return s
        return s + "\n" + _routine_body(r.get("source"), r.get("engine"), r.get("entrypoint"))

    def _get_functions(self, name: str):
        r = self.one(
            "SELECT TRIM(RDB$FUNCTION_NAME) AS name, RDB$FUNCTION_SOURCE AS source, RDB$LEGACY_FLAG AS legacy, "
            "RDB$DETERMINISTIC_FLAG AS is_deterministic, RDB$VALID_BLR AS valid, RDB$RETURN_ARGUMENT AS ret, "
            "TRIM(RDB$MODULE_NAME) AS module, TRIM(RDB$ENTRYPOINT) AS entrypoint, TRIM(RDB$ENGINE_NAME) AS engine, "
            "TRIM(RDB$OWNER_NAME) AS owner, RDB$DESCRIPTION AS description FROM RDB$FUNCTIONS "
            "WHERE RDB$FUNCTION_NAME = ? AND RDB$PACKAGE_NAME IS NULL",
            (name,),
        )
        if not r:
            return None
        args = self.function_args(name)
        ddl = self._function_ddl(name, args, r) + ";\n"
        if r["description"]:
            ddl += f"\nCOMMENT ON FUNCTION {quote_ident(name)} IS {sql_string(r['description'])};\n"
        return {
            "properties": {
                "Name": r["name"],
                "Kind": "Legacy UDF" if r["legacy"] else "PSQL" if not r["engine"] else f"External ({r['engine']})",
                "Deterministic": bool(r["is_deterministic"]),
                "Owner": r["owner"],
                "Valid": bool(r["valid"]) if r["valid"] is not None else None,
                "Comment": r["description"],
            },
            "parameters": [a for a in args if a["position"] != (r["ret"] or 0)],
            "returns": next((a["data_type"] for a in args if a["position"] == (r["ret"] or 0)), None),
            "source": r["source"],
            "ddl": ddl,
        }

    def _function_ddl(self, name, args, r, keyword="CREATE OR ALTER FUNCTION") -> str:
        ret_pos = (r or {}).get("ret") or 0
        ins = [a for a in args if a["position"] != ret_pos]
        ret = next((a["data_type"] for a in args if a["position"] == ret_pos), "INTEGER")
        if r and r.get("legacy"):
            s = f"DECLARE EXTERNAL FUNCTION {quote_ident(name)}\n"
            s += ",\n".join(f"    {a['data_type']}" for a in ins)
            s += f"\nRETURNS {ret}\nENTRY_POINT {sql_string(r['entrypoint'])} MODULE_NAME {sql_string(r['module'])}"
            return s
        s = f"{keyword} {quote_ident(name)}"
        s += " (\n" + ",\n".join(f"    {quote_ident(a['name'])} {a['data_type']}" for a in ins) + ")" if ins else " ()"
        s += f"\nRETURNS {ret}"
        if r is None:
            return s
        if r.get("is_deterministic"):
            s += " DETERMINISTIC"
        return s + "\n" + _routine_body(r.get("source"), r.get("engine"), r.get("entrypoint"))

    def _get_packages(self, name: str):
        r = self.one(
            "SELECT TRIM(RDB$PACKAGE_NAME) AS name, RDB$PACKAGE_HEADER_SOURCE AS header, "
            "RDB$PACKAGE_BODY_SOURCE AS body, RDB$VALID_BODY_FLAG AS valid, TRIM(RDB$OWNER_NAME) AS owner, "
            "RDB$DESCRIPTION AS description FROM RDB$PACKAGES WHERE RDB$PACKAGE_NAME = ?",
            (name,),
        )
        if not r:
            return None
        qn = quote_ident(name)
        procs = self.rows(
            "SELECT TRIM(RDB$PROCEDURE_NAME) AS name FROM RDB$PROCEDURES WHERE RDB$PACKAGE_NAME = ? ORDER BY 1",
            (name,),
        )
        funcs = self.rows(
            "SELECT TRIM(RDB$FUNCTION_NAME) AS name FROM RDB$FUNCTIONS WHERE RDB$PACKAGE_NAME = ? ORDER BY 1",
            (name,),
        )
        ddl = f"CREATE OR ALTER PACKAGE {qn}\nAS\n{(r['header'] or '').strip()};\n"
        if r["body"]:
            ddl += f"\nRECREATE PACKAGE BODY {qn}\nAS\n{r['body'].strip()};\n"
        if r["description"]:
            ddl += f"\nCOMMENT ON PACKAGE {qn} IS {sql_string(r['description'])};\n"
        return {
            "properties": {
                "Name": r["name"],
                "Owner": r["owner"],
                "Body valid": bool(r["valid"]),
                "Procedures": ", ".join(p["name"] for p in procs) or None,
                "Functions": ", ".join(f["name"] for f in funcs) or None,
                "Comment": r["description"],
            },
            "source": r["header"],
            "body": r["body"],
            "ddl": ddl,
        }

    def _get_triggers(self, name: str):
        rows = self.triggers(name=name)
        if not rows:
            return None
        t = rows[0]
        return {
            "properties": {
                "Name": t["name"],
                "Table": t["relation"],
                "Kind": t["kind"].upper(),
                "Event": t["event"],
                "Position": t["position"],
                "Active": not t["inactive"],
                "Comment": t["description"],
            },
            "source": t["source"],
            "ddl": self._trigger_ddl(t) + ";\n",
        }

    def _get_sequences(self, name: str):
        r = self.one(
            "SELECT TRIM(RDB$GENERATOR_NAME) AS name, RDB$INITIAL_VALUE AS initial_value, "
            "RDB$GENERATOR_INCREMENT AS increment, RDB$SYSTEM_FLAG AS system_flag, RDB$DESCRIPTION AS description "
            "FROM RDB$GENERATORS WHERE RDB$GENERATOR_NAME = ?",
            (name,),
        )
        if not r:
            return None
        cur = self.one(f"SELECT GEN_ID({quote_ident(name)}, 0) AS v FROM RDB$DATABASE")
        qn = quote_ident(name)
        ddl = f"CREATE SEQUENCE {qn}"
        if r["initial_value"]:
            ddl += f" START WITH {r['initial_value']}"
        if r["increment"] not in (None, 1):
            ddl += f" INCREMENT BY {r['increment']}"
        ddl += ";\n"
        # The value a fresh sequence reports before first use differs by version:
        # Firebird 4+ reports START WITH - INCREMENT, Firebird 3 reports START WITH.
        initial = r["initial_value"] or 0
        baseline = initial - (r["increment"] or 1) if self.engine_major >= 4 else initial
        if cur and cur["v"] != baseline:
            ddl += f"SET GENERATOR {qn} TO {cur['v']};\n"
        if r["description"]:
            ddl += f"\nCOMMENT ON SEQUENCE {qn} IS {sql_string(r['description'])};\n"
        return {
            "properties": {
                "Name": r["name"],
                "Current value": cur["v"] if cur else None,
                "Initial value": r["initial_value"],
                "Increment": r["increment"],
                "Identity column sequence": r["system_flag"] == 6,
                "Comment": r["description"],
            },
            "ddl": ddl,
        }

    def _get_domains(self, name: str):
        r = self.one(
            "SELECT TRIM(f.RDB$FIELD_NAME) AS name, " + _TYPE_COLS + ", f.RDB$NULL_FLAG AS not_null, "
            "f.RDB$DEFAULT_SOURCE AS dflt, f.RDB$VALIDATION_SOURCE AS validation, "
            "TRIM(co.RDB$COLLATION_NAME) AS collation, f.RDB$DESCRIPTION AS description "
            "FROM RDB$FIELDS f LEFT JOIN RDB$CHARACTER_SETS cs ON cs.RDB$CHARACTER_SET_ID = f.RDB$CHARACTER_SET_ID "
            "LEFT JOIN RDB$COLLATIONS co ON co.RDB$COLLATION_ID = f.RDB$COLLATION_ID "
            "  AND co.RDB$CHARACTER_SET_ID = f.RDB$CHARACTER_SET_ID WHERE f.RDB$FIELD_NAME = ?",
            (name,),
        )
        if not r:
            return None
        dtype = self._type(r)
        qn = quote_ident(name)
        ddl = f"CREATE DOMAIN {qn} AS {dtype}"
        if r["dflt"]:
            ddl += f"\n    {r['dflt'].strip()}"
        if r["not_null"]:
            ddl += "\n    NOT NULL"
        if r["validation"]:
            ddl += f"\n    {r['validation'].strip()}"
        if r["collation"] and r["collation"] != r["charset"]:
            ddl += f"\n    COLLATE {r['collation']}"
        ddl += ";\n"
        if r["description"]:
            ddl += f"\nCOMMENT ON DOMAIN {qn} IS {sql_string(r['description'])};\n"
        return {
            "properties": {
                "Name": r["name"],
                "Data type": dtype,
                "Not null": bool(r["not_null"]),
                "Default": (r["dflt"] or "").strip() or None,
                "Check": (r["validation"] or "").strip() or None,
                "Collation": r["collation"],
                "Comment": r["description"],
            },
            "ddl": ddl,
        }

    def _get_exceptions(self, name: str):
        r = self.one(
            "SELECT TRIM(RDB$EXCEPTION_NAME) AS name, RDB$EXCEPTION_NUMBER AS number, RDB$MESSAGE AS message, "
            "RDB$DESCRIPTION AS description FROM RDB$EXCEPTIONS WHERE RDB$EXCEPTION_NAME = ?",
            (name,),
        )
        if not r:
            return None
        qn = quote_ident(name)
        ddl = f"CREATE OR ALTER EXCEPTION {qn} {sql_string(r['message'])};\n"
        if r["description"]:
            ddl += f"\nCOMMENT ON EXCEPTION {qn} IS {sql_string(r['description'])};\n"
        return {
            "properties": {"Name": r["name"], "Number": r["number"], "Message": r["message"],
                           "Comment": r["description"]},
            "ddl": ddl,
        }

    def _get_indices(self, name: str):
        rows = self.indices(name=name)
        if not rows:
            return None
        i = rows[0]
        return {
            "properties": {
                "Name": i["name"],
                "Table": i["relation"],
                "Columns": ", ".join(i["columns"]) or None,
                "Expression": i["expr"],
                "Condition": i["cond"],
                "Unique": i["is_unique"],
                "Descending": i["descending"],
                "Active": not i["inactive"],
                "Selectivity": i["selectivity"],
                "Constraint": i["constraint_name"],
                "Comment": i["description"],
            },
            "ddl": (
                f"-- Index is managed by constraint {i['constraint_name']}\n"
                if i["constraint_name"]
                else self._index_ddl(i) + "\n"
            ),
        }

    def _get_roles(self, name: str):
        r = self.one(
            "SELECT TRIM(RDB$ROLE_NAME) AS name, TRIM(RDB$OWNER_NAME) AS owner, RDB$DESCRIPTION AS description "
            "FROM RDB$ROLES WHERE RDB$ROLE_NAME = ?",
            (name,),
        )
        if not r:
            return None
        members = self.rows(
            "SELECT DISTINCT TRIM(RDB$USER) AS name FROM RDB$USER_PRIVILEGES "
            "WHERE RDB$RELATION_NAME = ? AND RDB$PRIVILEGE = 'M' ORDER BY 1",
            (name,),
        )
        ddl = f"CREATE ROLE {quote_ident(name)};\n"
        for m in members:
            ddl += f"GRANT {quote_ident(name)} TO {quote_ident(m['name'])};\n"
        return {
            "properties": {
                "Name": r["name"],
                "Owner": r["owner"],
                "Members": ", ".join(m["name"] for m in members) or None,
                "Comment": r["description"],
            },
            "ddl": ddl,
        }

    def _get_users(self, name: str):
        r = self.one(
            "SELECT TRIM(SEC$USER_NAME) AS name, TRIM(SEC$FIRST_NAME) AS first_name, "
            "TRIM(SEC$MIDDLE_NAME) AS middle_name, TRIM(SEC$LAST_NAME) AS last_name, SEC$ACTIVE AS active, "
            "SEC$ADMIN AS is_admin, TRIM(SEC$PLUGIN) AS plugin, SEC$DESCRIPTION AS description "
            "FROM SEC$USERS WHERE SEC$USER_NAME = ?",
            (name,),
        )
        if not r:
            return None
        ddl = f"CREATE USER {quote_ident(name)} PASSWORD '********'"
        for k, kw in (("first_name", "FIRSTNAME"), ("middle_name", "MIDDLENAME"), ("last_name", "LASTNAME")):
            if r[k]:
                ddl += f" {kw} {sql_string(r[k])}"
        ddl += " ACTIVE" if r["active"] else " INACTIVE"
        if r["is_admin"]:
            ddl += " GRANT ADMIN ROLE"
        if r["plugin"]:
            ddl += f" USING PLUGIN {r['plugin']}"
        ddl += ";\n"
        return {
            "properties": {
                "Name": r["name"],
                "First name": r["first_name"],
                "Middle name": r["middle_name"],
                "Last name": r["last_name"],
                "Active": bool(r["active"]),
                "Admin": bool(r["is_admin"]),
                "Plugin": r["plugin"],
                "Comment": r["description"],
            },
            "ddl": ddl,
        }

    # -------------------------------------------------------- dependencies
    def dependencies(self, name: str) -> dict[str, list[dict[str, Any]]]:
        uses = self.rows(
            "SELECT DISTINCT TRIM(RDB$DEPENDED_ON_NAME) AS name, RDB$DEPENDED_ON_TYPE AS dtype, "
            "TRIM(RDB$FIELD_NAME) AS field FROM RDB$DEPENDENCIES WHERE RDB$DEPENDENT_NAME = ? "
            "AND RDB$DEPENDED_ON_NAME <> ?",
            (name, name),
        )
        used_by = self.rows(
            "SELECT DISTINCT TRIM(RDB$DEPENDENT_NAME) AS name, RDB$DEPENDENT_TYPE AS dtype, "
            "TRIM(RDB$FIELD_NAME) AS field FROM RDB$DEPENDENCIES WHERE RDB$DEPENDED_ON_NAME = ? "
            "AND RDB$DEPENDENT_NAME <> ?",
            (name, name),
        )
        for r in uses + used_by:
            r["type"] = DEP_TYPES.get(r.pop("dtype"), "other")
        return {"depends_on": uses, "dependents": used_by}

    # ------------------------------------------------------------- scripts
    def script(self, otype: str, name: str, kind: str) -> str:
        """Generate a helper script (SELECT/INSERT/UPDATE/DELETE/EXECUTE/DROP/CREATE)."""
        qn = quote_ident(name)
        if kind == "create":
            return self.get_object(otype, name)["ddl"]
        if kind == "drop":
            return drop_statement(otype, name) + ";\n"
        if otype in ("tables", "views", "system_tables"):
            cols = self.columns(name)
            names = [quote_ident(c["name"]) for c in cols]
            pk = [quote_ident(c["name"]) for c in cols if c["primary_key"]] or names[:1]
            writable = [quote_ident(c["name"]) for c in cols if not c["computed"]]
            if kind == "select":
                return "SELECT\n    " + ",\n    ".join(names) + f"\nFROM {qn}\nFETCH FIRST 100 ROWS ONLY;\n"
            if kind == "insert":
                return (
                    f"INSERT INTO {qn} (\n    " + ",\n    ".join(writable) + "\n) VALUES (\n    "
                    + ",\n    ".join(f":{c.strip(chr(34))}" for c in writable) + "\n);\n"
                )
            if kind == "update":
                sets = ",\n    ".join(f"{c} = :{c.strip(chr(34))}" for c in writable if c not in pk)
                where = " AND ".join(f"{c} = :{c.strip(chr(34))}" for c in pk)
                return f"UPDATE {qn} SET\n    {sets}\nWHERE {where};\n"
            if kind == "delete":
                where = " AND ".join(f"{c} = :{c.strip(chr(34))}" for c in pk)
                return f"DELETE FROM {qn}\nWHERE {where};\n"
        if otype == "procedures" and kind in ("execute", "select"):
            params = self.procedure_params(name)
            ins = [p for p in params if p["direction"] == "IN"]
            args = ", ".join(f":{p['name']}" for p in ins)
            call = f"{qn}({args})" if ins else qn
            if kind == "select":
                return f"SELECT * FROM {call};\n"
            return f"EXECUTE PROCEDURE {call};\n"
        if otype == "functions" and kind in ("execute", "select"):
            args = ", ".join(f":{a['name']}" for a in self.function_args(name) if a["name"])
            return f"SELECT {qn}({args}) FROM RDB$DATABASE;\n"
        raise ValueError(f"Script {kind!r} not available for {otype}")


def _routine_body(source: str | None, engine: str | None = None, entrypoint: str | None = None,
                  source_has_as: bool = False) -> str:
    if engine:
        s = f"EXTERNAL NAME {sql_string(entrypoint or '')} ENGINE {engine}"
        if source and source.strip():
            s += f"\nAS {sql_string(source.strip())}"
        return s
    body = (source or "").strip()
    if source_has_as or body[:2].upper() == "AS" and (len(body) == 2 or not body[2].isalnum()):
        return body
    return "AS\n" + body


_DROP_KEYWORDS = {
    "tables": "TABLE", "system_tables": "TABLE", "views": "VIEW", "procedures": "PROCEDURE",
    "functions": "FUNCTION", "packages": "PACKAGE", "triggers": "TRIGGER", "sequences": "SEQUENCE",
    "domains": "DOMAIN", "exceptions": "EXCEPTION", "indices": "INDEX", "roles": "ROLE", "users": "USER",
}


def drop_statement(otype: str, name: str) -> str:
    kw = _DROP_KEYWORDS.get(otype)
    if not kw:
        raise ValueError(f"Cannot drop objects of type {otype!r}")
    return f"DROP {kw} {quote_ident(name)}"
