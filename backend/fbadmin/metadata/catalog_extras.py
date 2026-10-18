"""Catalog additions for milestone 0.3: collations, character sets, shadows, BLOB filters,
SQL SECURITY (Firebird 4+), system flags and privileges (RDB$USER_PRIVILEGES).

Mixed into :class:`fbadmin.metadata.catalog.Catalog`; methods rely on ``rows``/``one``/
``has_column`` from there.
"""

from __future__ import annotations

from typing import Any

from .types import quote_ident, sql_string


def sql_security_clause(value: bool | None, prefix: str = " ", suffix: str = "") -> str:
    """``SQL SECURITY DEFINER/INVOKER`` for a RDB$SQL_SECURITY value (NULL = not set)."""
    if value is None:
        return ""
    return f"{prefix}SQL SECURITY {'DEFINER' if value else 'INVOKER'}{suffix}"


# RDB$USER_PRIVILEGES.RDB$PRIVILEGE codes
PRIVILEGE_NAMES = {
    "S": "SELECT", "I": "INSERT", "U": "UPDATE", "D": "DELETE", "R": "REFERENCES",
    "X": "EXECUTE", "G": "USAGE", "M": "MEMBER", "C": "CREATE", "L": "ALTER ANY", "O": "DROP ANY",
}
PRIVILEGE_CODES = {v: k for k, v in PRIVILEGE_NAMES.items()}
# RDB$USER_TYPE / RDB$OBJECT_TYPE codes (obj_type in Firebird sources)
GRANTEE_TYPES = {1: "VIEW", 2: "TRIGGER", 5: "PROCEDURE", 8: "USER", 13: "ROLE", 15: "FUNCTION", 18: "PACKAGE"}
GRANTEE_CODES = {v: k for k, v in GRANTEE_TYPES.items()}
OBJECT_TYPE_CODES = {
    0: "tables", 1: "views", 5: "procedures", 7: "exceptions", 9: "domains", 11: "charsets", 13: "roles",
    14: "sequences", 15: "functions", 17: "collations", 18: "packages",
}

# Which system table/name column holds RDB$SYSTEM_FLAG for an object type.
_SYSTEM_FLAG_SOURCES = {
    "views": ("RDB$RELATIONS", "RDB$RELATION_NAME"),
    "procedures": ("RDB$PROCEDURES", "RDB$PROCEDURE_NAME"),
    "functions": ("RDB$FUNCTIONS", "RDB$FUNCTION_NAME"),
    "packages": ("RDB$PACKAGES", "RDB$PACKAGE_NAME"),
    "triggers": ("RDB$TRIGGERS", "RDB$TRIGGER_NAME"),
    "sequences": ("RDB$GENERATORS", "RDB$GENERATOR_NAME"),
    "domains": ("RDB$FIELDS", "RDB$FIELD_NAME"),
    "exceptions": ("RDB$EXCEPTIONS", "RDB$EXCEPTION_NAME"),
    "indices": ("RDB$INDICES", "RDB$INDEX_NAME"),
    "roles": ("RDB$ROLES", "RDB$ROLE_NAME"),
}
_SECURITY_SOURCES = {
    "procedures": ("RDB$PROCEDURES", "RDB$PROCEDURE_NAME", " AND RDB$PACKAGE_NAME IS NULL"),
    "functions": ("RDB$FUNCTIONS", "RDB$FUNCTION_NAME", " AND RDB$PACKAGE_NAME IS NULL"),
    "packages": ("RDB$PACKAGES", "RDB$PACKAGE_NAME", ""),
    "triggers": ("RDB$TRIGGERS", "RDB$TRIGGER_NAME", ""),
}
SYSTEM_FLAG_NAMES = {0: "User", 1: "System", 2: "QLI", 3: "Check constraint", 4: "Referential constraint",
                     5: "View check", 6: "Identity generator"}

_COLL_PAD, _COLL_CI, _COLL_AI = 1, 2, 4


def _strip_auto_attributes(spec: str | None) -> str:
    """Drop COLL-VERSION / ICU-VERSION, which Firebird adds by itself."""
    parts = [p for p in (spec or "").split(";") if p.strip()]
    keep = [p for p in parts if p.split("=")[0].strip().upper() not in ("COLL-VERSION", "ICU-VERSION")]
    return ";".join(keep)


class CatalogExtras:
    # provided by Catalog
    rows: Any
    one: Any
    has_column: Any

    # ------------------------------------------------------- SQL SECURITY
    def has_sql_security(self, relation: str = "RDB$RELATIONS") -> bool:
        return self.has_column(relation, "RDB$SQL_SECURITY")

    def sql_security_col(self, relation: str, alias: str = "") -> str:
        """Select-list fragment returning RDB$SQL_SECURITY as ``sql_security`` (NULL before Firebird 4)."""
        if self.has_sql_security(relation):
            return f", {alias}RDB$SQL_SECURITY AS sql_security"
        return ", CAST(NULL AS BOOLEAN) AS sql_security"

    def add_security_property(self, props: dict, r: dict) -> None:
        if self.has_sql_security():
            v = r.get("sql_security")
            props["SQL security"] = None if v is None else ("DEFINER" if v else "INVOKER")

    def extra_properties(self, otype: str, name: str, obj: dict) -> None:
        props = obj.get("properties")
        if not isinstance(props, dict):
            return
        src = _SECURITY_SOURCES.get(otype)
        if src and self.has_sql_security(src[0]) and "SQL security" not in props:
            r = self.one(f"SELECT RDB$SQL_SECURITY AS v FROM {src[0]} WHERE {src[1]} = ?{src[2]}", (name,))
            v = (r or {}).get("v")
            props["SQL security"] = None if v is None else ("DEFINER" if v else "INVOKER")
        src2 = _SYSTEM_FLAG_SOURCES.get(otype)
        if src2 and "System flag" not in props:
            r = self.one(f"SELECT FIRST 1 RDB$SYSTEM_FLAG AS v FROM {src2[0]} WHERE {src2[1]} = ?", (name,))
            if r is not None:
                flag = r.get("v") or 0
                props["System flag"] = f"{flag} ({SYSTEM_FLAG_NAMES.get(flag, 'other')})"

    # ---------------------------------------------------------- collations
    def _list_collations(self):
        return self.rows(
            "SELECT TRIM(co.RDB$COLLATION_NAME) AS name, TRIM(cs.RDB$CHARACTER_SET_NAME) AS charset, "
            "TRIM(co.RDB$BASE_COLLATION_NAME) AS base_collation, co.RDB$SYSTEM_FLAG AS system_flag, "
            "co.RDB$DESCRIPTION AS description FROM RDB$COLLATIONS co "
            "JOIN RDB$CHARACTER_SETS cs ON cs.RDB$CHARACTER_SET_ID = co.RDB$CHARACTER_SET_ID "
            "ORDER BY COALESCE(co.RDB$SYSTEM_FLAG, 0), co.RDB$COLLATION_NAME"
        )

    def collation_row(self, name: str) -> dict | None:
        return self.one(
            "SELECT TRIM(co.RDB$COLLATION_NAME) AS name, co.RDB$COLLATION_ID AS coll_id, "
            "TRIM(cs.RDB$CHARACTER_SET_NAME) AS charset, TRIM(co.RDB$BASE_COLLATION_NAME) AS base, "
            "co.RDB$COLLATION_ATTRIBUTES AS attrs, co.RDB$SPECIFIC_ATTRIBUTES AS spec, "
            "co.RDB$SYSTEM_FLAG AS system_flag, TRIM(co.RDB$OWNER_NAME) AS owner, co.RDB$DESCRIPTION AS description "
            "FROM RDB$COLLATIONS co JOIN RDB$CHARACTER_SETS cs ON cs.RDB$CHARACTER_SET_ID = co.RDB$CHARACTER_SET_ID "
            "WHERE co.RDB$COLLATION_NAME = ?",
            (name,),
        )

    def _get_collations(self, name: str):
        r = self.collation_row(name)
        if not r:
            return None
        attrs = r["attrs"] or 0
        system = bool(r["system_flag"])
        spec = _strip_auto_attributes(r["spec"])
        qn = quote_ident(name)
        if system:
            ddl = f"-- {name} is a built-in collation of character set {r['charset']}\n"
        else:
            ddl = f"CREATE COLLATION {qn} FOR {r['charset']}"
            if r["base"]:
                ddl += f"\n    FROM {quote_ident(r['base'])}"
            ddl += "\n    " + ("PAD SPACE" if attrs & _COLL_PAD else "NO PAD")
            ddl += "\n    " + ("CASE INSENSITIVE" if attrs & _COLL_CI else "CASE SENSITIVE")
            ddl += "\n    " + ("ACCENT INSENSITIVE" if attrs & _COLL_AI else "ACCENT SENSITIVE")
            if spec:
                ddl += f"\n    {sql_string(spec)}"
            ddl += ";\n"
            if r["description"]:
                ddl += f"\nCOMMENT ON COLLATION {qn} IS {sql_string(r['description'])};\n"
        return {
            "properties": {
                "Name": r["name"],
                "Character set": r["charset"],
                "Base collation": r["base"],
                "Pad space": bool(attrs & _COLL_PAD),
                "Case insensitive": bool(attrs & _COLL_CI),
                "Accent insensitive": bool(attrs & _COLL_AI),
                "Specific attributes": spec or None,
                "Collation id": r["coll_id"],
                "Owner": r["owner"],
                "System flag": f"{r['system_flag'] or 0} ({'System' if system else 'User'})",
                "Comment": r["description"],
            },
            "ddl": ddl,
        }

    # -------------------------------------------------------------- charsets
    def _list_charsets(self):
        return self.rows(
            "SELECT TRIM(RDB$CHARACTER_SET_NAME) AS name, RDB$CHARACTER_SET_ID AS id, "
            "RDB$BYTES_PER_CHARACTER AS bytes_per_char, TRIM(RDB$DEFAULT_COLLATE_NAME) AS default_collation, "
            "RDB$SYSTEM_FLAG AS system_flag, RDB$DESCRIPTION AS description FROM RDB$CHARACTER_SETS "
            "ORDER BY RDB$CHARACTER_SET_NAME"
        )

    def _get_charsets(self, name: str):
        r = self.one(
            "SELECT TRIM(RDB$CHARACTER_SET_NAME) AS name, RDB$CHARACTER_SET_ID AS id, "
            "RDB$BYTES_PER_CHARACTER AS bpc, TRIM(RDB$DEFAULT_COLLATE_NAME) AS dflt, RDB$SYSTEM_FLAG AS system_flag, "
            "TRIM(RDB$OWNER_NAME) AS owner, RDB$DESCRIPTION AS description FROM RDB$CHARACTER_SETS "
            "WHERE RDB$CHARACTER_SET_NAME = ?",
            (name,),
        )
        if not r:
            return None
        colls = self.rows(
            "SELECT TRIM(co.RDB$COLLATION_NAME) AS name, co.RDB$SYSTEM_FLAG AS system_flag "
            "FROM RDB$COLLATIONS co WHERE co.RDB$CHARACTER_SET_ID = ? ORDER BY co.RDB$COLLATION_ID",
            (r["id"],),
        )
        qn = quote_ident(name)
        ddl = f"-- {name} is a built-in character set\n"
        if r["dflt"] and r["dflt"] != name:
            ddl += f"ALTER CHARACTER SET {qn} SET DEFAULT COLLATION {quote_ident(r['dflt'])};\n"
        if r["description"]:
            ddl += f"COMMENT ON CHARACTER SET {qn} IS {sql_string(r['description'])};\n"
        return {
            "properties": {
                "Name": r["name"],
                "Id": r["id"],
                "Bytes per character": r["bpc"],
                "Default collation": r["dflt"],
                "Collations": ", ".join(c["name"] for c in colls),
                "Owner": r["owner"],
                "System flag": f"{r['system_flag'] or 0} ({'System' if r['system_flag'] else 'User'})",
                "Comment": r["description"],
            },
            "collations": colls,
            "ddl": ddl,
        }

    # --------------------------------------------------------------- shadows
    def _shadow_files(self, number: int | None = None):
        where = "RDB$SHADOW_NUMBER = ?" if number is not None else "RDB$SHADOW_NUMBER > 0"
        return self.rows(
            "SELECT RDB$SHADOW_NUMBER AS num, TRIM(RDB$FILE_NAME) AS file_name, RDB$FILE_SEQUENCE AS seq, "
            "RDB$FILE_START AS start_page, RDB$FILE_LENGTH AS length_pages, RDB$FILE_FLAGS AS flags "
            "FROM RDB$FILES WHERE " + where + " ORDER BY RDB$SHADOW_NUMBER, RDB$FILE_SEQUENCE",
            (number,) if number is not None else (),
        )

    @staticmethod
    def _shadow_mode(flags: int | None) -> dict:
        flags = flags or 0
        return {"manual": bool(flags & 4), "conditional": bool(flags & 16), "inactive": bool(flags & 2)}

    def _list_shadows(self):
        out: dict[int, dict] = {}
        for f in self._shadow_files():
            if f["num"] not in out:
                m = self._shadow_mode(f["flags"])
                out[f["num"]] = {"name": str(f["num"]), "file_name": f["file_name"], "files": 0,
                                 "mode": "MANUAL" if m["manual"] else "AUTO", "conditional": m["conditional"]}
            out[f["num"]]["files"] += 1
        return list(out.values())

    def _get_shadows(self, name: str):
        try:
            num = int(name)
        except ValueError:
            return None
        files = self._shadow_files(num)
        if not files:
            return None
        m = self._shadow_mode(files[0]["flags"])
        ddl = f"CREATE SHADOW {num} {'MANUAL' if m['manual'] else 'AUTO'}"
        if m["conditional"]:
            ddl += " CONDITIONAL"
        ddl += f" {sql_string(files[0]['file_name'])}"
        if len(files) > 1 and files[0]["length_pages"]:
            ddl += f" LENGTH {files[0]['length_pages']}"
        for f in files[1:]:
            ddl += f"\n    FILE {sql_string(f['file_name'])}"
            if f["start_page"]:
                ddl += f" STARTING AT {f['start_page']}"
            if f["length_pages"]:
                ddl += f" LENGTH {f['length_pages']}"
        ddl += ";\n"
        return {
            "properties": {
                "Shadow number": num,
                "Mode": "MANUAL" if m["manual"] else "AUTO",
                "Conditional": m["conditional"],
                "Inactive": m["inactive"],
                "Files": "\n".join(f["file_name"] for f in files),
            },
            "files": files,
            "ddl": ddl,
        }

    # ----------------------------------------------------------- BLOB filters
    def _list_blob_filters(self):
        return self.rows(
            "SELECT TRIM(RDB$FUNCTION_NAME) AS name, RDB$INPUT_SUB_TYPE AS input_type, "
            "RDB$OUTPUT_SUB_TYPE AS output_type, TRIM(RDB$MODULE_NAME) AS module, RDB$SYSTEM_FLAG AS system_flag, "
            "RDB$DESCRIPTION AS description FROM RDB$FILTERS ORDER BY RDB$FUNCTION_NAME"
        )

    def _get_blob_filters(self, name: str):
        r = self.one(
            "SELECT TRIM(RDB$FUNCTION_NAME) AS name, RDB$INPUT_SUB_TYPE AS input_type, "
            "RDB$OUTPUT_SUB_TYPE AS output_type, TRIM(RDB$MODULE_NAME) AS module, TRIM(RDB$ENTRYPOINT) AS entrypoint, "
            "RDB$SYSTEM_FLAG AS system_flag, RDB$DESCRIPTION AS description FROM RDB$FILTERS "
            "WHERE RDB$FUNCTION_NAME = ?",
            (name,),
        )
        if not r:
            return None
        qn = quote_ident(name)
        ddl = (f"DECLARE FILTER {qn}\n    INPUT_TYPE {r['input_type']} OUTPUT_TYPE {r['output_type']}\n"
               f"    ENTRY_POINT {sql_string(r['entrypoint'] or '')} MODULE_NAME {sql_string(r['module'] or '')};\n")
        if r["description"]:
            ddl += f"\nCOMMENT ON FILTER {qn} IS {sql_string(r['description'])};\n"
        return {
            "properties": {
                "Name": r["name"],
                "Input subtype": r["input_type"],
                "Output subtype": r["output_type"],
                "Module": r["module"],
                "Entry point": r["entrypoint"],
                "System flag": f"{r['system_flag'] or 0} ({'System' if r['system_flag'] else 'User'})",
                "Comment": r["description"],
            },
            "ddl": ddl,
        }

    # ------------------------------------------------------------ privileges
    def privileges(self, *, object_name: str | None = None, grantee: str | None = None,
                   include_system: bool = False) -> list[dict[str, Any]]:
        """Rows of RDB$USER_PRIVILEGES for an object or a grantee, decoded."""
        cond, params = [], []
        if object_name is not None:
            cond.append("p.RDB$RELATION_NAME = ?")
            params.append(object_name)
        if grantee is not None:
            cond.append("p.RDB$USER = ?")
            params.append(grantee)
        cond.append("p.RDB$USER_TYPE IN (1, 2, 5, 8, 13, 15, 18)")
        if not include_system:
            cond.append("p.RDB$GRANTOR IS NOT NULL")
        rows = self.rows(
            "SELECT TRIM(p.RDB$USER) AS grantee, p.RDB$USER_TYPE AS grantee_type, TRIM(p.RDB$GRANTOR) AS grantor, "
            "TRIM(p.RDB$PRIVILEGE) AS priv, p.RDB$GRANT_OPTION AS grant_option, "
            "TRIM(p.RDB$RELATION_NAME) AS object_name, p.RDB$OBJECT_TYPE AS object_type, "
            "TRIM(p.RDB$FIELD_NAME) AS column_name, "
            "(SELECT FIRST 1 IIF(r.RDB$VIEW_BLR IS NULL, 0, 1) FROM RDB$RELATIONS r "
            " WHERE r.RDB$RELATION_NAME = p.RDB$RELATION_NAME) AS is_view "
            "FROM RDB$USER_PRIVILEGES p WHERE " + " AND ".join(cond) +
            " ORDER BY p.RDB$RELATION_NAME, p.RDB$USER, p.RDB$PRIVILEGE, p.RDB$FIELD_NAME",
            params,
        )
        out = []
        for r in rows:
            ot = r["object_type"]
            if ot == 0 and r["is_view"]:
                otype = "views"
            elif ot is not None and ot >= 20:
                otype = "ddl"
            else:
                otype = OBJECT_TYPE_CODES.get(ot, f"type_{ot}")
            out.append({
                "grantee": r["grantee"],
                "grantee_type": GRANTEE_TYPES.get(r["grantee_type"], str(r["grantee_type"])),
                "grantor": r["grantor"],
                "privilege": PRIVILEGE_NAMES.get(r["priv"], r["priv"]),
                "object": r["object_name"],
                "object_type": otype,
                "column": r["column_name"],
                "grant_option": bool(r["grant_option"]),
            })
        return out

    def grantees(self) -> dict[str, list[str]]:
        def names(sql: str) -> list[str]:
            return [r["name"] for r in self.rows(sql)]

        users = set(names("SELECT TRIM(SEC$USER_NAME) AS name FROM SEC$USERS"))
        users |= set(names("SELECT DISTINCT TRIM(RDB$USER) AS name FROM RDB$USER_PRIVILEGES WHERE RDB$USER_TYPE = 8"))
        users |= set(names("SELECT DISTINCT TRIM(RDB$OWNER_NAME) AS name FROM RDB$RELATIONS "
                           "WHERE RDB$OWNER_NAME IS NOT NULL"))
        users.discard("PUBLIC")
        return {
            "users": ["PUBLIC", *sorted(users)],
            "roles": names("SELECT TRIM(RDB$ROLE_NAME) AS name FROM RDB$ROLES ORDER BY 1"),
            "procedures": names("SELECT TRIM(RDB$PROCEDURE_NAME) AS name FROM RDB$PROCEDURES WHERE "
                                "RDB$PACKAGE_NAME IS NULL AND COALESCE(RDB$SYSTEM_FLAG, 0) = 0 ORDER BY 1"),
            "functions": names("SELECT TRIM(RDB$FUNCTION_NAME) AS name FROM RDB$FUNCTIONS WHERE "
                               "RDB$PACKAGE_NAME IS NULL AND COALESCE(RDB$SYSTEM_FLAG, 0) = 0 ORDER BY 1"),
            "packages": names("SELECT TRIM(RDB$PACKAGE_NAME) AS name FROM RDB$PACKAGES "
                              "WHERE COALESCE(RDB$SYSTEM_FLAG, 0) = 0 ORDER BY 1"),
            "triggers": names("SELECT TRIM(RDB$TRIGGER_NAME) AS name FROM RDB$TRIGGERS "
                              "WHERE COALESCE(RDB$SYSTEM_FLAG, 0) = 0 ORDER BY 1"),
            "views": names("SELECT TRIM(RDB$RELATION_NAME) AS name FROM RDB$RELATIONS WHERE RDB$VIEW_BLR IS NOT NULL "
                           "AND COALESCE(RDB$SYSTEM_FLAG, 0) = 0 ORDER BY 1"),
        }

    # ------------------------------------------------- recursive dependents
    def dependents_tree(self, name: str, column: str | None = None, max_depth: int = 6) -> list[dict[str, Any]]:
        """Objects that (transitively) depend on ``name`` (optionally only on one of its columns)."""
        from .catalog import DEP_TYPES

        seen: set[tuple[str, str]] = set()
        out: list[dict[str, Any]] = []
        frontier: list[tuple[str, str | None]] = [(name, column)]
        for depth in range(1, max_depth + 1):
            nxt: list[tuple[str, str | None]] = []
            for obj, col in frontier:
                sql = ("SELECT DISTINCT TRIM(RDB$DEPENDENT_NAME) AS name, RDB$DEPENDENT_TYPE AS dtype "
                       "FROM RDB$DEPENDENCIES WHERE RDB$DEPENDED_ON_NAME = ? AND RDB$DEPENDENT_NAME <> ?")
                params: list = [obj, obj]
                if col:
                    sql += " AND RDB$FIELD_NAME = ?"
                    params.append(col)
                for r in self.rows(sql, params):
                    dtype = DEP_TYPES.get(r["dtype"], "other")
                    key = (r["name"], dtype)
                    if key in seen or (r["name"] == name and depth > 1):
                        continue
                    seen.add(key)
                    item = {"name": r["name"], "type": dtype, "depth": depth, "via": obj}
                    if dtype in ("computed", "validation"):
                        # computed column / CHECK constraint: report the owning relation or domain
                        item["detail"] = self._owner_of_generated(r["name"], dtype)
                    out.append(item)
                    nxt.append((r["name"], None))
            frontier = nxt
            if not frontier:
                break
        return out

    def _owner_of_generated(self, name: str, dtype: str) -> str | None:
        if dtype == "computed":
            r = self.one("SELECT FIRST 1 TRIM(RDB$RELATION_NAME) || '.' || TRIM(RDB$FIELD_NAME) AS o "
                         "FROM RDB$RELATION_FIELDS WHERE RDB$FIELD_SOURCE = ?", (name,))
        else:
            r = self.one("SELECT FIRST 1 TRIM(RDB$RELATION_NAME) AS o FROM RDB$TRIGGERS WHERE RDB$TRIGGER_NAME = ?",
                         (name,))
        return (r or {}).get("o")

    def referencing_foreign_keys(self, table: str) -> list[dict[str, Any]]:
        return self.rows(
            "SELECT TRIM(rc.RDB$CONSTRAINT_NAME) AS name, TRIM(rc.RDB$RELATION_NAME) AS relation "
            "FROM RDB$RELATION_CONSTRAINTS rc "
            "JOIN RDB$REF_CONSTRAINTS ref ON ref.RDB$CONSTRAINT_NAME = rc.RDB$CONSTRAINT_NAME "
            "JOIN RDB$RELATION_CONSTRAINTS uq ON uq.RDB$CONSTRAINT_NAME = ref.RDB$CONST_NAME_UQ "
            "WHERE uq.RDB$RELATION_NAME = ? AND rc.RDB$RELATION_NAME <> ? ORDER BY 2, 1",
            (table, table),
        )
