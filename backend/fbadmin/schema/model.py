"""A complete, normalized snapshot of a database schema.

The snapshot is the common ground of the full DDL extract (``extract.py``) and the schema diff
(``diff.py``): every object is rendered to deterministic DDL fragments, so two databases with the
same schema produce identical text. Per-object DDL comes from :class:`Catalog`; this module only adds
what a whole-database script needs (stubs for PSQL, deferred computed columns, grants, comments,
dependency order).
"""

from __future__ import annotations

import re
from collections import defaultdict
from typing import Any

from ..metadata.catalog import Catalog, _routine_body
from ..metadata.types import quote_ident, sql_string

PSQL_KINDS = ("procedures", "functions", "triggers", "packages")

_COLL_ATTR_DROP = re.compile(r"^(COLL-VERSION|ICU-VERSION)=", re.IGNORECASE)

# RDB$USER_PRIVILEGES codes
_PRIVS = {"S": "SELECT", "I": "INSERT", "U": "UPDATE", "D": "DELETE", "R": "REFERENCES", "X": "EXECUTE",
          "G": "USAGE"}
_OBJ_KW = {0: "", 5: "PROCEDURE ", 15: "FUNCTION ", 18: "PACKAGE ", 7: "EXCEPTION ", 14: "SEQUENCE ",
           9: "DOMAIN "}
_GRANTEE_KW = {8: "", 13: "ROLE ", 5: "PROCEDURE ", 15: "FUNCTION ", 18: "PACKAGE ", 2: "TRIGGER ",
               1: "VIEW ", 0: "TABLE "}


def column_fragment(c: dict, placeholder: bool = False) -> str:
    """Column definition as written inside CREATE TABLE (mirrors Catalog._table_ddl)."""
    s = f"{quote_ident(c['name'])} "
    if c["computed"]:
        if placeholder:
            return s + f"{c['data_type']} COMPUTED BY (CAST(NULL AS {c['data_type']}))"
        return s + f"COMPUTED BY {c['computed']}"
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
    return s


def strip_comment_lines(ddl: str) -> str:
    return "\n".join(ln for ln in ddl.splitlines() if not ln.startswith("COMMENT ON ")).strip()


def topo_sort(names: list[str], deps: dict[str, set[str]]) -> list[str]:
    """Order ``names`` so that every name comes after its dependencies (alphabetical tie-break).
    Cycles are broken by emitting the remaining names alphabetically."""
    remaining = set(names)
    out: list[str] = []
    while remaining:
        ready = sorted(n for n in remaining if not (deps.get(n, set()) & remaining))
        if not ready:
            ready = [sorted(remaining)[0]]
        for n in ready:
            out.append(n)
            remaining.discard(n)
    return out


def load_collations(cat: Catalog) -> dict[str, dict]:
    """User-defined collations as CREATE COLLATION statements."""
    out = {}
    for r in cat.rows(
        "SELECT TRIM(c.RDB$COLLATION_NAME) AS name, TRIM(cs.RDB$CHARACTER_SET_NAME) AS charset, "
        "TRIM(c.RDB$BASE_COLLATION_NAME) AS base, c.RDB$COLLATION_ATTRIBUTES AS attrs, "
        "c.RDB$SPECIFIC_ATTRIBUTES AS spec, c.RDB$DESCRIPTION AS description FROM RDB$COLLATIONS c "
        "JOIN RDB$CHARACTER_SETS cs ON cs.RDB$CHARACTER_SET_ID = c.RDB$CHARACTER_SET_ID "
        "WHERE COALESCE(c.RDB$SYSTEM_FLAG, 0) = 0 ORDER BY 1"
    ):
        s = f"CREATE COLLATION {quote_ident(r['name'])} FOR {r['charset']}"
        s += f" FROM {quote_ident(r['base'])}" if r["base"] else " FROM EXTERNAL ('" + r["name"] + "')"
        attrs = r["attrs"] or 0
        s += " PAD SPACE" if attrs & 1 else " NO PAD"
        s += " CASE INSENSITIVE" if attrs & 2 else " CASE SENSITIVE"
        s += " ACCENT INSENSITIVE" if attrs & 4 else " ACCENT SENSITIVE"
        spec = ";".join(p for p in (r["spec"] or "").split(";") if p and not _COLL_ATTR_DROP.match(p))
        if spec:
            s += " " + sql_string(spec)
        out[r["name"]] = {"ddl": s, "description": r["description"]}
    return out


class Snapshot:
    """Load every user object of the database behind ``conn``."""

    def __init__(self, conn):
        self.cat = cat = Catalog(conn)
        self.engine_major = cat.engine_major
        self.charset = cat.default_charset
        db = cat.one("SELECT RDB$DESCRIPTION AS d FROM RDB$DATABASE") or {}
        self.description: str | None = db.get("d")
        self.deps = self._load_dependencies()
        self.collations = self._load_collations()
        self.domains = self._load_domains()
        self.sequences = self._load_sequences()
        self.exceptions = self._load_exceptions()
        self.roles = {r["name"]: {"ddl": f"CREATE ROLE {quote_ident(r['name'])}", "description": r["description"]}
                      for r in cat.list_objects("roles")}
        self.udfs: dict[str, dict] = {}
        self.procedures = self._load_procedures()
        self.functions = self._load_functions()
        self.packages = self._load_packages()
        self.tables, self.indices = self._load_tables()
        self.views = self._load_views()
        self.view_order = topo_sort(sorted(self.views), {n: v["view_deps"] for n, v in self.views.items()})
        self.triggers = self._load_triggers()
        self.grants = self._load_grants()
        self.comments = self._load_comments()

    # ------------------------------------------------------------ helpers
    def rows(self, sql: str, params=()) -> list[dict[str, Any]]:
        return self.cat.rows(sql, params)

    def _load_dependencies(self) -> list[dict]:
        pkg = ", TRIM(RDB$PACKAGE_NAME) AS pkg" if self.cat.has_column("RDB$DEPENDENCIES", "RDB$PACKAGE_NAME") else ""
        return self.rows(
            "SELECT DISTINCT TRIM(RDB$DEPENDENT_NAME) AS dep, RDB$DEPENDENT_TYPE AS dtype, "
            "TRIM(RDB$DEPENDED_ON_NAME) AS name, RDB$DEPENDED_ON_TYPE AS otype, TRIM(RDB$FIELD_NAME) AS fld" + pkg +
            " FROM RDB$DEPENDENCIES WHERE RDB$DEPENDENT_NAME NOT STARTING WITH 'RDB$' "
            "OR RDB$DEPENDENT_TYPE = 3"
        )

    def depends_on(self, name: str, dtypes: tuple[int, ...]) -> set[str]:
        return {d["name"] for d in self.deps if d["dep"] == name and d["dtype"] in dtypes}

    # ---------------------------------------------------------- loaders
    def _load_collations(self) -> dict[str, dict]:
        return load_collations(self.cat)

    def _load_domains(self) -> dict[str, dict]:
        out = {}
        for d in self.cat.list_objects("domains"):
            obj = self.cat._get_domains(d["name"])
            p = obj["properties"]
            out[d["name"]] = {
                "ddl": strip_comment_lines(obj["ddl"]),
                "type": p["Data type"], "default": p["Default"], "not_null": p["Not null"], "check": p["Check"],
                "collation": p["Collation"], "description": p["Comment"],
            }
        return out

    def _load_sequences(self) -> dict[str, dict]:
        out = {}
        for s in self.cat.list_objects("sequences"):
            lines = strip_comment_lines(self.cat._get_sequences(s["name"])["ddl"]).splitlines()
            out[s["name"]] = {
                "ddl": lines[0].rstrip(";"),
                "value_ddl": lines[1].rstrip(";") if len(lines) > 1 else None,
                "increment": s["increment"], "description": s["description"],
            }
        return out

    def _load_exceptions(self) -> dict[str, dict]:
        out = {}
        for e in self.cat.list_objects("exceptions"):
            out[e["name"]] = {
                "ddl": f"CREATE EXCEPTION {quote_ident(e['name'])} {sql_string(e['message'])}",
                "message": e["message"], "description": e["description"],
            }
        return out

    def _load_procedures(self) -> dict[str, dict]:
        out = {}
        for r in self.rows(
            "SELECT TRIM(RDB$PROCEDURE_NAME) AS name, RDB$PROCEDURE_TYPE AS ptype, RDB$PROCEDURE_SOURCE AS source, "
            "TRIM(RDB$ENGINE_NAME) AS engine, TRIM(RDB$ENTRYPOINT) AS entrypoint, RDB$DESCRIPTION AS description "
            "FROM RDB$PROCEDURES WHERE COALESCE(RDB$SYSTEM_FLAG, 0) = 0 AND RDB$PACKAGE_NAME IS NULL ORDER BY 1"
        ):
            name = r["name"]
            params = self.cat.procedure_params(name)
            header = self.cat._procedure_ddl(name, params, None, "CREATE PROCEDURE")
            external = bool(r["engine"])
            body = "AS\nBEGIN\n  SUSPEND;\nEND" if r["ptype"] == 1 else "AS\nBEGIN\n  EXIT;\nEND"
            full = self.cat._procedure_ddl(name, params, r, "CREATE PROCEDURE")
            out[name] = {
                "kind": "procedures",
                "header": header,
                "create": full,
                "stub": full if external else header + "\n" + body,
                "alter": self.cat._procedure_ddl(name, params, r, "ALTER PROCEDURE"),
                "create_or_alter": self.cat._procedure_ddl(name, params, r, "CREATE OR ALTER PROCEDURE"),
                "stub_alter": None if external else
                self.cat._procedure_ddl(name, params, None, "ALTER PROCEDURE") + "\n" + body,
                "external": external,
                "description": r["description"],
                "params": params,
            }
        return out

    def _load_functions(self) -> dict[str, dict]:
        out = {}
        for r in self.rows(
            "SELECT TRIM(RDB$FUNCTION_NAME) AS name, RDB$FUNCTION_SOURCE AS source, RDB$LEGACY_FLAG AS legacy, "
            "RDB$DETERMINISTIC_FLAG AS is_deterministic, RDB$RETURN_ARGUMENT AS ret, "
            "TRIM(RDB$MODULE_NAME) AS module, TRIM(RDB$ENTRYPOINT) AS entrypoint, TRIM(RDB$ENGINE_NAME) AS engine, "
            "RDB$DESCRIPTION AS description FROM RDB$FUNCTIONS "
            "WHERE COALESCE(RDB$SYSTEM_FLAG, 0) = 0 AND RDB$PACKAGE_NAME IS NULL ORDER BY 1"
        ):
            name = r["name"]
            args = self.cat.function_args(name)
            if r["legacy"]:
                self.udfs[name] = {"ddl": self.cat._function_ddl(name, args, r), "description": r["description"]}
                continue
            header = self.cat._function_ddl(name, args, None, "CREATE FUNCTION")
            external = bool(r["engine"])
            stub_body = "AS\nBEGIN\n  RETURN NULL;\nEND"
            full = self.cat._function_ddl(name, args, r, "CREATE FUNCTION")
            out[name] = {
                "kind": "functions",
                "header": header,
                "create": full,
                "stub": full if external else header + "\n" + stub_body,
                "alter": self.cat._function_ddl(name, args, r, "ALTER FUNCTION"),
                "create_or_alter": self.cat._function_ddl(name, args, r, "CREATE OR ALTER FUNCTION"),
                "stub_alter": None if external else
                self.cat._function_ddl(name, args, None, "ALTER FUNCTION") + "\n" + stub_body,
                "external": external,
                "description": r["description"],
                "params": [a for a in args if a["name"]],
            }
        return out

    def _load_packages(self) -> dict[str, dict]:
        out = {}
        for r in self.rows(
            "SELECT TRIM(RDB$PACKAGE_NAME) AS name, RDB$PACKAGE_HEADER_SOURCE AS header, "
            "RDB$PACKAGE_BODY_SOURCE AS body, RDB$DESCRIPTION AS description FROM RDB$PACKAGES "
            "WHERE COALESCE(RDB$SYSTEM_FLAG, 0) = 0 ORDER BY 1"
        ):
            qn = quote_ident(r["name"])
            out[r["name"]] = {
                "header": f"CREATE PACKAGE {qn}\nAS\n{(r['header'] or '').strip()}",
                "header_cor": f"CREATE OR ALTER PACKAGE {qn}\nAS\n{(r['header'] or '').strip()}",
                "body": f"CREATE PACKAGE BODY {qn}\nAS\n{r['body'].strip()}" if r["body"] else None,
                "description": r["description"],
            }
        return out

    def _load_tables(self) -> tuple[dict[str, dict], dict[str, dict]]:
        cat = self.cat
        # computed columns whose expression uses other objects are created as placeholders first
        external_computed = defaultdict(set)
        for r in self.rows(
            "SELECT DISTINCT TRIM(rf.RDB$RELATION_NAME) AS rel, TRIM(rf.RDB$FIELD_NAME) AS col "
            "FROM RDB$RELATION_FIELDS rf JOIN RDB$DEPENDENCIES d ON d.RDB$DEPENDENT_NAME = rf.RDB$FIELD_SOURCE "
            "AND d.RDB$DEPENDENT_TYPE = 3 WHERE d.RDB$DEPENDED_ON_NAME <> rf.RDB$RELATION_NAME"
        ):
            external_computed[r["rel"]].add(r["col"])
        tables: dict[str, dict] = {}
        indices: dict[str, dict] = {}
        for t in self.rows(
            "SELECT TRIM(RDB$RELATION_NAME) AS name, RDB$RELATION_TYPE AS relation_type, "
            "RDB$EXTERNAL_FILE AS external_file, RDB$DESCRIPTION AS description FROM RDB$RELATIONS "
            "WHERE RDB$VIEW_BLR IS NULL AND COALESCE(RDB$SYSTEM_FLAG, 0) = 0 ORDER BY 1"
        ):
            name = t["name"]
            qn = quote_ident(name)
            cols = cat.columns(name)
            cons = cat.constraints(name)
            deferred_cols = external_computed.get(name, set())

            def con_item(c):
                ddl = cat._constraint_ddl(c)
                auto = c["name"].startswith("INTEG_") or c["name"].startswith("RDB$")
                return {"name": c["name"], "type": c["type"], "auto": auto, "ddl": ddl,
                        "key": ddl if auto else c["name"], "columns": c["columns"],
                        "ref_table": c.get("ref_table"), "ref_columns": c.get("ref_columns")}

            items = [con_item(c) for c in cons]
            sort = lambda c: (c["auto"], c["key"])  # noqa: E731
            pkuk = sorted([c for c in items if c["type"] in ("PRIMARY KEY", "UNIQUE")],
                          key=lambda c: (c["type"] != "PRIMARY KEY", *sort(c)))
            fks = sorted([c for c in items if c["type"] == "FOREIGN KEY"], key=sort)
            checks = sorted([c for c in items if c["type"] == "CHECK"], key=sort)
            rtype = t["relation_type"]
            head = "CREATE GLOBAL TEMPORARY TABLE" if rtype in (4, 5) else "CREATE TABLE"

            def create(placeholders: bool, _cols=cols, _pkuk=pkuk, _deferred=deferred_cols, _t=t, _qn=qn,
                       _head=head, _rtype=rtype):
                lines = ["    " + column_fragment(c, placeholders and c["name"] in _deferred) for c in _cols]
                lines += ["    " + c["ddl"] for c in _pkuk]
                s = f"{_head} {_qn}"
                if _t["external_file"]:
                    s += f" EXTERNAL FILE {sql_string(_t['external_file'].strip())}"
                s += " (\n" + ",\n".join(lines) + "\n)"
                if _rtype == 4:
                    s += " ON COMMIT PRESERVE ROWS"
                elif _rtype == 5:
                    s += " ON COMMIT DELETE ROWS"
                return s

            tables[name] = {
                "name": name,
                "relation_type": rtype,
                "external_file": t["external_file"],
                "description": t["description"],
                "columns": [{**c, "fragment": column_fragment(c)} for c in cols],
                "create": create(False),
                "create_placeholder": create(True),
                "deferred_cols": sorted(deferred_cols),
                "deferred": [
                    f"ALTER TABLE {qn} ALTER {quote_ident(c['name'])} COMPUTED BY {c['computed']}"
                    for c in cols if c["name"] in deferred_cols
                ],
                "pkuk": pkuk,
                "fks": fks,
                "checks": checks,
            }
            for i in cat.indices(relation=name):
                if i["constraint_name"]:
                    continue
                text = cat._index_ddl(i)
                stmts = [s.strip() for s in text.split(";") if s.strip()]
                indices[i["name"]] = {
                    "table": name, "create": stmts[0], "inactive": i["inactive"],
                    "ddl": stmts, "description": i["description"],
                    "active_ddl": cat._index_ddl({**i, "inactive": False}).rstrip(";"),
                }
        return tables, indices

    def _load_views(self) -> dict[str, dict]:
        out = {}
        view_names = {v["name"] for v in self.cat.list_objects("views")}
        for v in self.rows(
            "SELECT TRIM(RDB$RELATION_NAME) AS name, RDB$VIEW_SOURCE AS source, RDB$DESCRIPTION AS description "
            "FROM RDB$RELATIONS WHERE RDB$VIEW_BLR IS NOT NULL AND COALESCE(RDB$SYSTEM_FLAG, 0) = 0 ORDER BY 1"
        ):
            name = v["name"]
            cols = self.cat.columns(name)
            col_list = ",\n".join(f"    {quote_ident(c['name'])}" for c in cols)
            body = f"(\n{col_list})\nAS\n{(v['source'] or '').strip()}"
            out[name] = {
                "ddl": f"CREATE VIEW {quote_ident(name)} {body}",
                "create_or_alter": f"CREATE OR ALTER VIEW {quote_ident(name)} {body}",
                "columns": [c["name"] for c in cols],
                "column_comments": {c["name"]: c["description"] for c in cols if c["description"]},
                "view_deps": self.depends_on(name, (1,)) & view_names - {name},
                "description": v["description"],
            }
        return out

    def _load_triggers(self) -> dict[str, dict]:
        out = {}
        for t in self.cat.list_objects("triggers"):
            tr = self.cat.triggers(name=t["name"])[0]
            cor = self.cat._trigger_ddl(tr)
            out[t["name"]] = {
                "relation": tr["relation"],
                "ddl": "CREATE TRIGGER" + cor[len("CREATE OR ALTER TRIGGER"):],
                "create_or_alter": cor,
                "description": tr["description"],
            }
        return out

    def _load_grants(self) -> list[dict]:
        names = {
            0: set(self.tables) | set(self.views), 5: set(self.procedures), 15: set(self.functions) | set(self.udfs),
            18: set(self.packages), 7: set(self.exceptions), 14: set(self.sequences), 9: set(self.domains),
            13: set(self.roles),
        }
        rows = self.rows(
            "SELECT TRIM(RDB$USER) AS usr, RDB$USER_TYPE AS utype, TRIM(RDB$GRANTOR) AS grantor, "
            "TRIM(RDB$PRIVILEGE) AS priv, RDB$GRANT_OPTION AS opt, TRIM(RDB$RELATION_NAME) AS obj, "
            "TRIM(RDB$FIELD_NAME) AS fld, RDB$OBJECT_TYPE AS otype FROM RDB$USER_PRIVILEGES "
            "WHERE RDB$OBJECT_TYPE IN (0, 5, 7, 9, 13, 14, 15, 18)"
        )
        grouped: dict[tuple, list[str]] = defaultdict(list)
        for r in rows:
            if r["obj"] not in names.get(r["otype"], set()):
                continue
            if r["usr"] == r["grantor"] and r["utype"] == 8 and r["priv"] != "M":
                continue  # the owner's implicit privileges
            grouped[(r["usr"], r["utype"], r["priv"], r["obj"], r["otype"], bool(r["opt"]))].append(r["fld"])
        out = []
        for (usr, utype, priv, obj, otype, opt), flds in grouped.items():
            grantee = _GRANTEE_KW.get(utype, "") + (usr if usr == "PUBLIC" else quote_ident(usr))
            if priv == "M":
                what = quote_ident(obj)
                grant = f"GRANT {what} TO {grantee}" + (" WITH ADMIN OPTION" if opt else "")
                revoke = f"REVOKE {what} FROM {grantee}"
            else:
                p = _PRIVS.get(priv)
                if not p:
                    continue
                cols = sorted(f for f in flds if f)
                if cols:
                    p += " (" + ", ".join(quote_ident(c) for c in cols) + ")"
                on = f"ON {_OBJ_KW.get(otype, '')}{quote_ident(obj)}"
                grant = f"GRANT {p} {on} TO {grantee}" + (" WITH GRANT OPTION" if opt else "")
                revoke = f"REVOKE {p} {on} FROM {grantee}"
            out.append({"object": obj, "ddl": grant, "revoke": revoke})
        out.sort(key=lambda g: (g["object"], g["ddl"]))
        return out

    def _load_comments(self) -> dict[str, str]:
        """COMMENT ON statements keyed by their target (without the text)."""
        out: dict[str, str] = {}

        def add(target: str, text: str | None):
            if text:
                out[target] = f"{target} IS {sql_string(text)}"

        add("COMMENT ON DATABASE", self.description)
        for kind, kw in (("collations", "COLLATION"), ("domains", "DOMAIN"), ("sequences", "SEQUENCE"),
                         ("exceptions", "EXCEPTION"), ("roles", "ROLE"), ("packages", "PACKAGE"),
                         ("triggers", "TRIGGER"), ("indices", "INDEX"), ("udfs", "EXTERNAL FUNCTION")):
            for name, o in getattr(self, kind).items():
                add(f"COMMENT ON {kw} {quote_ident(name)}", o.get("description"))
        for name, t in self.tables.items():
            add(f"COMMENT ON TABLE {quote_ident(name)}", t["description"])
            for c in t["columns"]:
                add(f"COMMENT ON COLUMN {quote_ident(name)}.{quote_ident(c['name'])}", c["description"])
        for name, v in self.views.items():
            add(f"COMMENT ON VIEW {quote_ident(name)}", v["description"])
            for col, d in v["column_comments"].items():
                add(f"COMMENT ON COLUMN {quote_ident(name)}.{quote_ident(col)}", d)
        for kind, kw in (("procedures", "PROCEDURE"), ("functions", "FUNCTION")):
            for name, p in getattr(self, kind).items():
                add(f"COMMENT ON {kw} {quote_ident(name)}", p["description"])
                for prm in p["params"]:
                    add(f"COMMENT ON {kw} PARAMETER {quote_ident(name)}.{quote_ident(prm['name'])}",
                        prm.get("description"))
        return out

    # -------------------------------------------------------- object view
    def comments_for(self, prefixes: list[str]) -> list[str]:
        return [v for k, v in sorted(self.comments.items()) if any(k == p or k.startswith(p + ".") for p in prefixes)]

    def objects(self) -> dict[tuple[str, str], str]:
        """Every object as (type, name) -> display DDL; used by the diff to find differences."""
        out: dict[tuple[str, str], str] = {}

        def text(stmts: list[str]) -> str:
            return "\n".join(s.rstrip().rstrip(";") + ";" for s in stmts if s) + "\n"

        q = quote_ident
        for n, o in self.collations.items():
            out[("collations", n)] = text([o["ddl"], *self.comments_for([f"COMMENT ON COLLATION {q(n)}"])])
        for n, o in self.domains.items():
            out[("domains", n)] = text([o["ddl"], *self.comments_for([f"COMMENT ON DOMAIN {q(n)}"])])
        for n, o in self.sequences.items():
            out[("sequences", n)] = text([o["ddl"], *self.comments_for([f"COMMENT ON SEQUENCE {q(n)}"])])
        for n, o in self.exceptions.items():
            out[("exceptions", n)] = text([o["ddl"], *self.comments_for([f"COMMENT ON EXCEPTION {q(n)}"])])
        for n, o in self.roles.items():
            out[("roles", n)] = text([o["ddl"], *self.comments_for([f"COMMENT ON ROLE {q(n)}"])])
        for n, o in self.udfs.items():
            out[("functions", n)] = text([o["ddl"], *self.comments_for([f"COMMENT ON EXTERNAL FUNCTION {q(n)}"])])
        for n, t in self.tables.items():
            stmts = [t["create"]]
            stmts += [f"ALTER TABLE {q(n)} ADD {c['ddl']}" for c in t["fks"] + t["checks"]]
            stmts += self.comments_for([f"COMMENT ON TABLE {q(n)}", f"COMMENT ON COLUMN {q(n)}"])
            out[("tables", n)] = text(stmts)
        for n, i in self.indices.items():
            out[("indices", n)] = text([*i["ddl"], *self.comments_for([f"COMMENT ON INDEX {q(n)}"])])
        for n, v in self.views.items():
            out[("views", n)] = text([v["ddl"], *self.comments_for([f"COMMENT ON VIEW {q(n)}",
                                                                  f"COMMENT ON COLUMN {q(n)}"])])
        for kind, kw in (("procedures", "PROCEDURE"), ("functions", "FUNCTION")):
            for n, p in getattr(self, kind).items():
                out[(kind, n)] = text([p["create"], *self.comments_for([f"COMMENT ON {kw} {q(n)}",
                                                                        f"COMMENT ON {kw} PARAMETER {q(n)}"])])
        for n, p in self.packages.items():
            out[("packages", n)] = text([p["header"], p["body"], *self.comments_for([f"COMMENT ON PACKAGE {q(n)}"])])
        for n, t in self.triggers.items():
            out[("triggers", n)] = text([t["ddl"], *self.comments_for([f"COMMENT ON TRIGGER {q(n)}"])])
        grants = defaultdict(list)
        for g in self.grants:
            grants[g["object"]].append(g["ddl"])
        for obj, stmts in grants.items():
            out[("grants", obj)] = text(sorted(stmts))
        if self.description:
            out[("database", "comment")] = text([self.comments["COMMENT ON DATABASE"]])
        return out


__all__ = ["Snapshot", "PSQL_KINDS", "topo_sort", "column_fragment", "_routine_body"]
