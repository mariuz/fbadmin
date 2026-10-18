"""DDL generation for the object dialogs (milestone 0.3).

Every generator takes a JSON-like *spec* (the dialog's form state) and, for ALTER, the
*original* spec as returned by :mod:`fbadmin.metadata.designer`. It returns a
:class:`Script`: the statements to run plus warnings about changes Firebird cannot make
in place. The functions are pure (no database access) so they are easy to test; the
engine major version decides which syntax is available.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from .catalog_extras import PRIVILEGE_NAMES
from .types import quote_ident as q
from .types import sql_string as s

Spec = dict[str, Any]


@dataclass
class Script:
    statements: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def add(self, stmt: str) -> None:
        self.statements.append(stmt.rstrip().rstrip(";"))

    def warn(self, msg: str) -> None:
        self.warnings.append(msg)

    def extend(self, other: Script) -> None:
        self.statements += other.statements
        self.warnings += other.warnings

    @property
    def sql(self) -> str:
        return "".join(st + ";\n" for st in self.statements)

    def as_dict(self) -> dict[str, Any]:
        return {"sql": self.sql, "statements": self.statements, "warnings": self.warnings}


class DDLError(ValueError):
    """Raised for specs that cannot produce valid DDL (missing names, ...)."""


# ------------------------------------------------------------------ helpers
def _name(spec: Spec, key: str = "name", what: str = "Name") -> str:
    v = (spec.get(key) or "").strip()
    if not v:
        raise DDLError(f"{what} is required")
    return v


def _txt(v: Any) -> str:
    return ("" if v is None else str(v)).strip()


def _norm(v: Any) -> str:
    """Normalise SQL text for comparisons (case of keywords kept, whitespace collapsed)."""
    return re.sub(r"\s+", " ", _txt(v))


def _strip_kw(text: Any, kw: str) -> str:
    t = _txt(text)
    m = re.match(rf"^{kw}\b\s*", t, re.IGNORECASE)
    return t[m.end():].strip() if m else t


def _wrapped(expr: str) -> bool:
    if not (expr.startswith("(") and expr.endswith(")")):
        return False
    depth = 0
    in_str = False
    for i, ch in enumerate(expr):
        if ch == "'":
            in_str = not in_str
        elif not in_str:
            if ch == "(":
                depth += 1
            elif ch == ")":
                depth -= 1
                if depth == 0 and i != len(expr) - 1:
                    return False
    return True


def paren(expr: Any) -> str:
    e = _txt(expr)
    return e if _wrapped(e) else f"({e})"


def _cols(cols: list[str] | None) -> str:
    return ", ".join(q(c) for c in cols or [])


def _comment_value(text: Any) -> str:
    t = _txt(text)
    return s(t) if t else "NULL"


def _security(v: Any) -> str | None:
    if v in (None, "", "DEFAULT"):
        return None
    v = str(v).upper()
    if v not in ("DEFINER", "INVOKER"):
        raise DDLError(f"Invalid SQL SECURITY {v!r}")
    return v


def _need_fb4(engine: int, script: Script, what: str) -> bool:
    if engine < 4:
        script.warn(f"{what} requires Firebird 4 or newer; skipped.")
        return False
    return True


COMMENT_KEYWORDS = {
    "tables": "TABLE", "system_tables": "TABLE", "views": "VIEW", "procedures": "PROCEDURE",
    "functions": "FUNCTION", "packages": "PACKAGE", "triggers": "TRIGGER", "sequences": "SEQUENCE",
    "domains": "DOMAIN", "exceptions": "EXCEPTION", "indices": "INDEX", "roles": "ROLE", "users": "USER",
    "collations": "COLLATION", "charsets": "CHARACTER SET", "blob_filters": "FILTER",
}


def comment_on(otype: str, name: str, text: Any, sub: str | None = None, package: str | None = None) -> str:
    """``COMMENT ON`` for an object, a column (``sub`` with tables/views) or a parameter."""
    if otype == "database":
        return f"COMMENT ON DATABASE IS {_comment_value(text)}"
    if sub:
        if otype in ("tables", "views", "system_tables"):
            return f"COMMENT ON COLUMN {q(name)}.{q(sub)} IS {_comment_value(text)}"
        if otype in ("procedures", "functions"):
            owner = f"{q(package)}.{q(name)}" if package else q(name)
            return f"COMMENT ON PARAMETER {owner}.{q(sub)} IS {_comment_value(text)}"
        raise DDLError(f"{otype} have no sub-objects to comment on")
    kw = COMMENT_KEYWORDS.get(otype)
    if not kw:
        raise DDLError(f"COMMENT ON is not available for {otype}")
    return f"COMMENT ON {kw} {q(name)} IS {_comment_value(text)}"


# ------------------------------------------------------------------ columns
def column_type(c: Spec) -> str:
    dom = _txt(c.get("domain"))
    t = q(dom) if dom else _txt(c.get("data_type"))
    if not t:
        raise DDLError(f"Column {c.get('name')!r} needs a data type or domain")
    return t


def column_def(c: Spec, engine: int, script: Script | None = None) -> str:
    """Column definition as used in CREATE TABLE / ALTER TABLE ADD."""
    out = q(_name(c, what="Column name")) + " "
    if _txt(c.get("computed")):
        return out + f"COMPUTED BY {paren(c['computed'])}"
    out += column_type(c)
    ident = c.get("identity")
    if ident:
        ident = str(ident).upper()
        if ident == "ALWAYS" and engine < 4:
            if script:
                script.warn("GENERATED ALWAYS needs Firebird 4; using BY DEFAULT.")
            ident = "BY DEFAULT"
        out += f" GENERATED {ident} AS IDENTITY"
        opts = []
        if c.get("identity_start") not in (None, ""):
            opts.append(f"START WITH {int(c['identity_start'])}")
        if c.get("identity_increment") not in (None, "", 1, "1"):
            if engine >= 4:
                opts.append(f"INCREMENT BY {int(c['identity_increment'])}")
            elif script:
                script.warn("Identity INCREMENT BY needs Firebird 4; skipped.")
        if opts:
            out += " (" + " ".join(opts) + ")"
    elif _txt(c.get("default")):
        out += " DEFAULT " + _strip_kw(c["default"], "DEFAULT")
    if c.get("not_null"):
        out += " NOT NULL"
    if _txt(c.get("check")):
        out += " CHECK " + paren(_strip_kw(c["check"], "CHECK"))
    if _txt(c.get("collation")):
        out += f" COLLATE {q(c['collation'])}"
    return out


# -------------------------------------------------------------- constraints
_RULES = {"", "RESTRICT", "NO ACTION"}


def _rule(v: Any) -> str:
    v = _txt(v).upper()
    return "RESTRICT" if v in _RULES else v


def constraint_def(c: Spec, keep_auto_name: bool = True) -> str:
    ctype = _txt(c.get("type")).upper()
    name = _txt(c.get("name"))
    auto = c.get("auto_name") and not keep_auto_name
    head = f"CONSTRAINT {q(name)} " if name and not auto else ""
    if ctype == "CHECK":
        cond = _strip_kw(c.get("check"), "CHECK")
        if not cond:
            raise DDLError("CHECK constraint needs a condition")
        return f"{head}CHECK {paren(cond)}"
    if not c.get("columns"):
        raise DDLError(f"{ctype or 'Constraint'} {name} needs at least one column")
    if ctype == "FOREIGN KEY":
        ref = _name(c, "ref_table", "Referenced table")
        out = f"{head}FOREIGN KEY ({_cols(c['columns'])}) REFERENCES {q(ref)}"
        if c.get("ref_columns"):
            out += f" ({_cols(c['ref_columns'])})"
        for kw in ("on_update", "on_delete"):
            r = _rule(c.get(kw))
            if r != "RESTRICT":
                out += f" ON {kw[3:].upper()} {r}"
    elif ctype in ("PRIMARY KEY", "UNIQUE"):
        out = f"{head}{ctype} ({_cols(c['columns'])})"
    else:
        raise DDLError(f"Unknown constraint type {ctype!r}")
    idx = _txt(c.get("index_name"))
    if idx or c.get("index_desc"):
        out += " USING " + ("DESC " if c.get("index_desc") else "") + "INDEX " + q(idx or name or "IX_" + ctype[:2])
    return out


def _constraint_key(c: Spec, renames: dict[str, str]) -> tuple:
    def cols(v):
        return tuple(renames.get(x, x) for x in v or [])

    return (
        _txt(c.get("type")).upper(), cols(c.get("columns")), _txt(c.get("ref_table")),
        tuple(c.get("ref_columns") or []), _rule(c.get("on_update")), _rule(c.get("on_delete")),
        _norm(paren(_strip_kw(c.get("check"), "CHECK"))) if _txt(c.get("check")) else "",
        _txt(c.get("index_name")), bool(c.get("index_desc")),
    )


# ------------------------------------------------------------------ indices
def index_create(i: Spec, table: str, engine: int, script: Script | None = None) -> str:
    name = _name(i, what="Index name")
    out = "CREATE " + ("UNIQUE " if i.get("unique") else "") + ("DESCENDING " if i.get("descending") else "")
    out += f"INDEX {q(name)} ON {q(table)}"
    if _txt(i.get("expression")):
        out += f" COMPUTED BY {paren(i['expression'])}"
    else:
        if not i.get("columns"):
            raise DDLError(f"Index {name} needs columns or an expression")
        out += f" ({_cols(i['columns'])})"
    cond = _strip_kw(i.get("condition"), "WHERE")
    if cond:
        if engine >= 5:
            out += f" WHERE {cond}"
        elif script:
            script.warn("Partial indices (WHERE) need Firebird 5; condition ignored.")
    return out


def _index_key(i: Spec, renames: dict[str, str]) -> tuple:
    return (
        bool(i.get("unique")), bool(i.get("descending")),
        tuple(renames.get(c, c) for c in i.get("columns") or []),
        _norm(paren(i["expression"])) if _txt(i.get("expression")) else "",
        _norm(_strip_kw(i.get("condition"), "WHERE")),
    )


def index_script(spec: Spec, original: Spec | None, engine: int) -> Script:
    """Standalone index dialog (CREATE / re-create / activate / statistics / comment)."""
    sc = Script()
    table = _name(spec, "table", "Table")
    name = _name(spec)
    if original:
        same = _index_key(spec, {}) == _index_key(original, {}) and name == original.get("name")
        if not same:
            sc.add(f"DROP INDEX {q(original['name'])}")
            sc.add(index_create(spec, table, engine, sc))
            if spec.get("active") is False:
                sc.add(f"ALTER INDEX {q(name)} INACTIVE")
        elif bool(spec.get("active", True)) != bool(original.get("active", True)):
            sc.add(f"ALTER INDEX {q(name)} {'ACTIVE' if spec.get('active', True) else 'INACTIVE'}")
    else:
        sc.add(index_create(spec, table, engine, sc))
        if spec.get("active") is False:
            sc.add(f"ALTER INDEX {q(name)} INACTIVE")
    if spec.get("recompute_selectivity"):
        sc.add(f"SET STATISTICS INDEX {q(name)}")
    if _txt(spec.get("comment")) != _txt((original or {}).get("comment")):
        sc.add(comment_on("indices", name, spec.get("comment")))
    return sc


# -------------------------------------------------------------------- tables
_KINDS = {"persistent", "gtt_preserve", "gtt_delete"}


def table_create(spec: Spec, engine: int) -> Script:
    sc = Script()
    name = _name(spec, what="Table name")
    kind = spec.get("kind") or "persistent"
    if kind not in _KINDS:
        raise DDLError(f"Unknown table kind {kind!r}")
    cols = spec.get("columns") or []
    if not cols:
        raise DDLError("A table needs at least one column")
    head = "CREATE GLOBAL TEMPORARY TABLE" if kind.startswith("gtt") else "CREATE TABLE"
    out = f"{head} {q(name)}"
    ext = _txt(spec.get("external_file"))
    if ext:
        if kind != "persistent":
            raise DDLError("External tables cannot be global temporary tables")
        out += f" EXTERNAL FILE {s(ext)}"
    lines = [column_def(c, engine, sc) for c in cols]
    for c in spec.get("constraints") or []:
        lines.append(constraint_def(c))
    out += " (\n    " + ",\n    ".join(lines) + "\n)"
    if kind == "gtt_preserve":
        out += " ON COMMIT PRESERVE ROWS"
    elif kind == "gtt_delete":
        out += " ON COMMIT DELETE ROWS"
    sec = _security(spec.get("sql_security"))
    if sec and _need_fb4(engine, sc, "SQL SECURITY"):
        out += f" SQL SECURITY {sec}"
    sc.add(out)
    for i in spec.get("indices") or []:
        sc.add(index_create(i, name, engine, sc))
        if i.get("active") is False:
            sc.add(f"ALTER INDEX {q(i['name'])} INACTIVE")
    if _txt(spec.get("comment")):
        sc.add(comment_on("tables", name, spec["comment"]))
    for c in cols:
        if _txt(c.get("comment")):
            sc.add(comment_on("tables", name, c["comment"], sub=c["name"]))
    return sc


def _type_key(c: Spec) -> str:
    dom = _txt(c.get("domain"))
    return ("D:" + dom) if dom else "T:" + _norm(c.get("data_type")).upper()


def _alter_column(t: str, old: Spec, new: Spec, engine: int, sc: Script) -> None:
    cn = q(new["name"])
    pre = f"ALTER TABLE {t} ALTER COLUMN {cn}"
    was_comp, is_comp = bool(_txt(old.get("computed"))), bool(_txt(new.get("computed")))
    if was_comp != is_comp:
        sc.warn(f"Column {new['name']}: Firebird cannot switch between computed and stored; "
                "the column is dropped and re-added (its data is lost).")
        sc.add(f"ALTER TABLE {t} DROP {cn}")
        sc.add(f"ALTER TABLE {t} ADD {column_def(new, engine, sc)}")
        return
    if is_comp:
        if _norm(paren(old["computed"])) != _norm(paren(new["computed"])):
            sc.add(f"{pre} COMPUTED BY {paren(new['computed'])}")
        return
    if _type_key(old) != _type_key(new):
        sc.add(f"{pre} TYPE {column_type(new)}")
    od, nd = _strip_kw(old.get("default"), "DEFAULT"), _strip_kw(new.get("default"), "DEFAULT")
    if _norm(od) != _norm(nd) and not new.get("identity"):
        sc.add(f"{pre} SET DEFAULT {nd}" if nd else f"{pre} DROP DEFAULT")
    if bool(old.get("not_null")) != bool(new.get("not_null")):
        sc.add(f"{pre} {'SET' if new.get('not_null') else 'DROP'} NOT NULL")
    if _txt(old.get("collation")) != _txt(new.get("collation")):
        sc.warn(f"Column {new['name']}: Firebird cannot change the collation of an existing column "
                "(use a domain, or add a new column and copy the data).")
    oi, ni = old.get("identity"), new.get("identity")
    if oi and not ni:
        if _need_fb4(engine, sc, "DROP IDENTITY"):
            sc.add(f"{pre} DROP IDENTITY")
    elif ni and not oi:
        sc.warn(f"Column {new['name']}: an existing column cannot become an identity column "
                "(add a new identity column instead).")
    elif ni and oi:
        if str(ni).upper() != str(oi).upper() and _need_fb4(engine, sc, "SET GENERATED"):
            sc.add(f"{pre} SET GENERATED {str(ni).upper()}")
        if new.get("identity_increment") not in (None, "") and \
                int(new["identity_increment"]) != int(old.get("identity_increment") or 1) and \
                _need_fb4(engine, sc, "SET INCREMENT BY"):
            sc.add(f"{pre} SET INCREMENT BY {int(new['identity_increment'])}")
        if new.get("identity_restart") not in (None, ""):
            sc.add(f"{pre} RESTART WITH {int(new['identity_restart'])}")


def table_alter(spec: Spec, original: Spec, engine: int) -> Script:
    """ALTER script turning ``original`` into ``spec``. Items are matched by ``orig_name``."""
    sc = Script()
    name = original["name"]
    t = q(name)
    if _txt(spec.get("name")) and spec["name"] != name:
        sc.warn("Firebird cannot rename tables; the name change is ignored.")
    if (spec.get("kind") or "persistent") != (original.get("kind") or "persistent"):
        sc.warn("The table type (persistent / global temporary) cannot be changed; recreate the table.")
    if _txt(spec.get("external_file")) != _txt(original.get("external_file")):
        sc.warn("The external file of a table cannot be changed; recreate the table.")

    old_cols = {c["name"]: c for c in original.get("columns") or []}
    new_cols = spec.get("columns") or []
    renames: dict[str, str] = {}
    for c in new_cols:
        on = c.get("orig_name")
        if on and on in old_cols and on != c["name"]:
            renames[on] = c["name"]
    kept = {c.get("orig_name") for c in new_cols if c.get("orig_name") in old_cols}
    new_names = {c["name"] for c in new_cols}

    def follow(cols: list[str] | None) -> list[str]:
        # references to a renamed column's old name follow the rename
        return [renames[x] if x in renames and x not in new_names else x for x in cols or []]

    spec = dict(spec)
    spec["constraints"] = [{**c, "columns": follow(c.get("columns"))} for c in spec.get("constraints") or []]
    spec["indices"] = [{**i, "columns": follow(i.get("columns"))} for i in spec.get("indices") or []]
    dropped_cols = [n for n in old_cols if n not in kept]

    # triggers: drops first (they may reference dropped columns)
    old_trg = {x["name"]: x for x in original.get("triggers") or []}
    new_trg = {x.get("orig_name") or x["name"]: x for x in spec.get("triggers") or []}
    for n in old_trg:
        if n not in new_trg:
            sc.add(f"DROP TRIGGER {q(n)}")

    # constraints
    old_cons = {c["name"]: c for c in original.get("constraints") or []}
    new_cons = spec.get("constraints") or []
    matched = {c.get("orig_name") for c in new_cons if c.get("orig_name") in old_cons}
    drop_cons, add_cons = [], []
    for n, c in old_cons.items():
        if n not in matched:
            drop_cons.append(c)
    def mentions(text: Any, col: str) -> bool:
        return bool(re.search(rf'(?<![\w$"]){re.escape(col)}(?![\w$])', _txt(text)))

    for c in new_cons:
        on = c.get("orig_name")
        if on in old_cons:
            o = old_cons[on]
            # Firebird refuses to rename a column used by a constraint: drop it first, re-add it after
            touches = [x for x in renames if x in (o.get("columns") or []) or mentions(o.get("check"), x)]
            if _constraint_key(o, renames) != _constraint_key(c, {}) or _txt(c.get("name")) != on or touches:
                drop_cons.append(o)
                if _txt(c.get("check")):
                    chk = c["check"]
                    for x in touches:
                        chk = re.sub(rf'(?<![\w$"]){re.escape(x)}(?![\w$])', q(renames[x]), chk)
                    c = {**c, "check": chk}
                add_cons.append(c)
        else:
            add_cons.append(c)
    order = {"FOREIGN KEY": 0, "CHECK": 1, "UNIQUE": 2, "PRIMARY KEY": 3}
    for c in sorted(drop_cons, key=lambda c: order.get(_txt(c.get("type")).upper(), 9)):
        sc.add(f"ALTER TABLE {t} DROP CONSTRAINT {q(c['name'])}")

    # indices
    old_idx = {i["name"]: i for i in original.get("indices") or []}
    new_idx = spec.get("indices") or []
    matched_i = {i.get("orig_name") for i in new_idx if i.get("orig_name") in old_idx}
    create_idx = []
    toggle_idx = []
    for n in old_idx:
        if n not in matched_i:
            sc.add(f"DROP INDEX {q(n)}")
    for i in new_idx:
        on = i.get("orig_name")
        if on in old_idx:
            o = old_idx[on]
            if _index_key(o, renames) != _index_key(i, {}) or i["name"] != on:
                sc.add(f"DROP INDEX {q(on)}")
                create_idx.append(i)
            elif bool(i.get("active", True)) != bool(o.get("active", True)):
                toggle_idx.append(i)
        else:
            create_idx.append(i)

    # columns
    for n in dropped_cols:
        sc.add(f"ALTER TABLE {t} DROP {q(n)}")
    for old_n, new_n in renames.items():
        sc.add(f"ALTER TABLE {t} ALTER COLUMN {q(old_n)} TO {q(new_n)}")
    added = []
    for c in new_cols:
        on = c.get("orig_name")
        if on in old_cols:
            _alter_column(t, old_cols[on], c, engine, sc)
        else:
            added.append(c)
    for c in added:
        sc.add(f"ALTER TABLE {t} ADD {column_def(c, engine, sc)}")

    # column order
    actual = [renames.get(n, n) for n in old_cols if n not in dropped_cols] + [c["name"] for c in added]
    desired = [c["name"] for c in new_cols]
    if actual != desired and sorted(actual) == sorted(desired):
        cur = list(actual)
        for pos, cname in enumerate(desired):
            if cur[pos] != cname:
                cur.remove(cname)
                cur.insert(pos, cname)
                sc.add(f"ALTER TABLE {t} ALTER COLUMN {q(cname)} POSITION {pos + 1}")

    for c in sorted(add_cons, key=lambda c: 3 - order.get(_txt(c.get("type")).upper(), 0)):
        keep = not (c.get("auto_name") and c.get("orig_name") == c.get("name"))
        sc.add(f"ALTER TABLE {t} ADD {constraint_def(c, keep_auto_name=keep)}")
    for i in create_idx:
        sc.add(index_create(i, name, engine, sc))
        if i.get("active") is False:
            sc.add(f"ALTER INDEX {q(i['name'])} INACTIVE")
    for i in toggle_idx:
        sc.add(f"ALTER INDEX {q(i['name'])} {'ACTIVE' if i.get('active', True) else 'INACTIVE'}")
    for n, x in new_trg.items():
        if n in old_trg and bool(x.get("active", True)) != bool(old_trg[n].get("active", True)):
            sc.add(f"ALTER TRIGGER {q(n)} {'ACTIVE' if x.get('active', True) else 'INACTIVE'}")

    osec, nsec = _security(original.get("sql_security")), _security(spec.get("sql_security"))
    if osec != nsec and _need_fb4(engine, sc, "SQL SECURITY"):
        sc.add(f"ALTER TABLE {t} ALTER SQL SECURITY {nsec}" if nsec else f"ALTER TABLE {t} DROP SQL SECURITY")

    # comments
    if _txt(spec.get("comment")) != _txt(original.get("comment")):
        sc.add(comment_on("tables", name, spec.get("comment")))
    for c in new_cols:
        on = c.get("orig_name")
        before = _txt(old_cols[on].get("comment")) if on in old_cols else ""
        if _txt(c.get("comment")) != before:
            sc.add(comment_on("tables", name, c.get("comment"), sub=c["name"]))
    for i in new_idx:
        on = i.get("orig_name")
        before = _txt(old_idx[on].get("comment")) if on in old_idx else ""
        if _txt(i.get("comment")) != before and (before or _txt(i.get("comment"))):
            sc.add(comment_on("indices", i["name"], i.get("comment")))
    return sc


# --------------------------------------------------------------------- views
def view_script(spec: Spec, original: Spec | None, engine: int) -> Script:
    sc = Script()
    name = _name(spec, what="View name")
    if original and original.get("name") != name:
        sc.warn("Firebird cannot rename views; a new view is created and the old one is kept.")
    select = _txt(spec.get("select")).rstrip(";").strip()
    if not select:
        raise DDLError("The view needs a SELECT statement")
    select = re.sub(r"\s+WITH\s+CHECK\s+OPTION\s*$", "", select, flags=re.IGNORECASE)
    out = f"CREATE OR ALTER VIEW {q(name)}"
    cols = [c for c in spec.get("columns") or [] if _txt(c)]
    if cols:
        out += " (" + ", ".join(q(c.strip()) for c in cols) + ")"
    out += f"\nAS\n{select}"
    if spec.get("check_option"):
        out += "\nWITH CHECK OPTION"
    sc.add(out)
    if _txt(spec.get("comment")) != _txt((original or {}).get("comment")):
        sc.add(comment_on("views", name, spec.get("comment")))
    return sc


# ------------------------------------------------------------------ routines
def _params(ps: list[Spec]) -> str:
    return ",\n    ".join(f"{q(_name(p, what='Parameter name'))} {_name(p, 'type', 'Parameter type')}" for p in ps)


def _body(spec: Spec, sc: Script, engine: int) -> str:
    ext = spec.get("external") or {}
    if _txt(ext.get("name")) or spec.get("kind") == "external":
        out = f"EXTERNAL NAME {s(_txt(ext.get('name')))} ENGINE {_txt(ext.get('engine')) or 'UDR'}"
        if _txt(ext.get("body")):
            out += f"\nAS {s(_txt(ext['body']))}"
        return out
    body = _strip_kw(spec.get("body"), "AS")
    if not body:
        raise DDLError("The routine needs a body (BEGIN ... END)")
    return "AS\n" + body


def _routine_security(spec: Spec, sc: Script, engine: int) -> str:
    sec = _security(spec.get("sql_security"))
    if sec and _need_fb4(engine, sc, "SQL SECURITY"):
        return f"\nSQL SECURITY {sec}"
    return ""


def _param_comments(otype: str, name: str, spec: Spec, original: Spec | None, sc: Script) -> None:
    before = {p["name"]: _txt(p.get("comment")) for p in ((original or {}).get("inputs") or []) +
              ((original or {}).get("outputs") or [])}
    for p in (spec.get("inputs") or []) + (spec.get("outputs") or []):
        if _txt(p.get("comment")) != before.get(p.get("name"), ""):
            sc.add(comment_on(otype, name, p.get("comment"), sub=p["name"]))


def procedure_script(spec: Spec, original: Spec | None, engine: int) -> Script:
    sc = Script()
    name = _name(spec, what="Procedure name")
    out = f"CREATE OR ALTER PROCEDURE {q(name)}"
    if spec.get("inputs"):
        out += " (\n    " + _params(spec["inputs"]) + ")"
    if spec.get("outputs"):
        out += "\nRETURNS (\n    " + _params(spec["outputs"]) + ")"
    out += _routine_security(spec, sc, engine)
    out += "\n" + _body(spec, sc, engine)
    sc.add(out)
    if _txt(spec.get("comment")) != _txt((original or {}).get("comment")):
        sc.add(comment_on("procedures", name, spec.get("comment")))
    _param_comments("procedures", name, spec, original, sc)
    return sc


def function_script(spec: Spec, original: Spec | None, engine: int) -> Script:
    sc = Script()
    name = _name(spec, what="Function name")
    out = f"CREATE OR ALTER FUNCTION {q(name)}"
    out += " (\n    " + _params(spec["inputs"]) + ")" if spec.get("inputs") else " ()"
    out += f"\nRETURNS {_name(spec, 'returns', 'Return type')}"
    if spec.get("deterministic"):
        out += " DETERMINISTIC"
    out += _routine_security(spec, sc, engine)
    out += "\n" + _body(spec, sc, engine)
    sc.add(out)
    if _txt(spec.get("comment")) != _txt((original or {}).get("comment")):
        sc.add(comment_on("functions", name, spec.get("comment")))
    _param_comments("functions", name, spec, original, sc)
    return sc


def trigger_script(spec: Spec, original: Spec | None, engine: int) -> Script:
    sc = Script()
    name = _name(spec, what="Trigger name")
    event = _txt(spec.get("event"))
    if not event:
        raise DDLError("Choose the trigger event")
    out = f"CREATE OR ALTER TRIGGER {q(name)}"
    if _txt(spec.get("relation")):
        out += f" FOR {q(spec['relation'])}"
    out += f"\n{'ACTIVE' if spec.get('active', True) else 'INACTIVE'} {event} POSITION {int(spec.get('position') or 0)}"
    out += _routine_security(spec, sc, engine)
    out += "\n" + _body(spec, sc, engine)
    sc.add(out)
    if _txt(spec.get("comment")) != _txt((original or {}).get("comment")):
        sc.add(comment_on("triggers", name, spec.get("comment")))
    return sc


def package_script(spec: Spec, original: Spec | None, engine: int) -> Script:
    sc = Script()
    name = _name(spec, what="Package name")
    header = _strip_kw(spec.get("header"), "AS")
    if not header:
        raise DDLError("The package needs a header (BEGIN ... END)")
    out = f"CREATE OR ALTER PACKAGE {q(name)}" + _routine_security(spec, sc, engine) + f"\nAS\n{header}"
    sc.add(out)
    body = _strip_kw(spec.get("body"), "AS")
    if body:
        sc.add(f"RECREATE PACKAGE BODY {q(name)}\nAS\n{body}")
    if _txt(spec.get("comment")) != _txt((original or {}).get("comment")):
        sc.add(comment_on("packages", name, spec.get("comment")))
    return sc


# ------------------------------------------------------- simple object types
def sequence_script(spec: Spec, original: Spec | None, engine: int) -> Script:
    sc = Script()
    name = _name(spec, what="Sequence name")
    if not original:
        out = f"CREATE SEQUENCE {q(name)}"
        if spec.get("start") not in (None, ""):
            out += f" START WITH {int(spec['start'])}"
        if spec.get("increment") not in (None, "", 1, "1"):
            out += f" INCREMENT BY {int(spec['increment'])}"
        sc.add(out)
    else:
        if name != original["name"]:
            sc.warn("Firebird cannot rename sequences (ALTER SEQUENCE has no rename); the name change is ignored.")
            name = original["name"]
        parts = []
        if spec.get("restart_with") not in (None, ""):
            parts.append(f"RESTART WITH {int(spec['restart_with'])}")
        if spec.get("increment") not in (None, "") and int(spec["increment"]) != int(original.get("increment") or 1):
            parts.append(f"INCREMENT BY {int(spec['increment'])}")
        if parts:
            sc.add(f"ALTER SEQUENCE {q(name)} " + " ".join(parts))
        if spec.get("set_value") not in (None, ""):
            sc.add(f"SET GENERATOR {q(name)} TO {int(spec['set_value'])}")
    if _txt(spec.get("comment")) != _txt((original or {}).get("comment")):
        sc.add(comment_on("sequences", name, spec.get("comment")))
    return sc


def domain_script(spec: Spec, original: Spec | None, engine: int) -> Script:
    sc = Script()
    name = _name(spec, what="Domain name")
    dtype = _name(spec, "data_type", "Data type")
    dflt = _strip_kw(spec.get("default"), "DEFAULT")
    check = _strip_kw(spec.get("check"), "CHECK")
    if not original:
        out = f"CREATE DOMAIN {q(name)} AS {dtype}"
        if dflt:
            out += f"\n    DEFAULT {dflt}"
        if spec.get("not_null"):
            out += "\n    NOT NULL"
        if check:
            out += f"\n    CHECK {paren(check)}"
        if _txt(spec.get("collation")):
            out += f"\n    COLLATE {q(spec['collation'])}"
        sc.add(out)
    else:
        old = original["name"]
        if name != old:
            sc.add(f"ALTER DOMAIN {q(old)} TO {q(name)}")
        qn = q(name)
        if _norm(dtype).upper() != _norm(original.get("data_type")).upper():
            sc.add(f"ALTER DOMAIN {qn} TYPE {dtype}")
        if _norm(dflt) != _norm(_strip_kw(original.get("default"), "DEFAULT")):
            sc.add(f"ALTER DOMAIN {qn} SET DEFAULT {dflt}" if dflt else f"ALTER DOMAIN {qn} DROP DEFAULT")
        if bool(spec.get("not_null")) != bool(original.get("not_null")):
            sc.add(f"ALTER DOMAIN {qn} {'SET' if spec.get('not_null') else 'DROP'} NOT NULL")
        old_check = _strip_kw(original.get("check"), "CHECK")
        if _norm(paren(check) if check else "") != _norm(paren(old_check) if old_check else ""):
            if old_check:
                sc.add(f"ALTER DOMAIN {qn} DROP CONSTRAINT")
            if check:
                sc.add(f"ALTER DOMAIN {qn} ADD CONSTRAINT CHECK {paren(check)}")
        if _txt(spec.get("collation")) != _txt(original.get("collation")):
            sc.warn("Firebird cannot change the collation of a domain; recreate it.")
    if _txt(spec.get("comment")) != _txt((original or {}).get("comment")):
        sc.add(comment_on("domains", name, spec.get("comment")))
    return sc


def exception_script(spec: Spec, original: Spec | None, engine: int) -> Script:
    sc = Script()
    name = _name(spec, what="Exception name")
    if original and original["name"] != name:
        sc.warn("Firebird cannot rename exceptions; a new exception is created.")
    if not original or _txt(spec.get("message")) != _txt(original.get("message")) or original["name"] != name:
        sc.add(f"CREATE OR ALTER EXCEPTION {q(name)} {s(_txt(spec.get('message')))}")
    if _txt(spec.get("comment")) != _txt((original or {}).get("comment")):
        sc.add(comment_on("exceptions", name, spec.get("comment")))
    return sc


SYSTEM_PRIVILEGES = [
    "USER_MANAGEMENT", "READ_RAW_PAGES", "CREATE_USER_TYPES", "USE_NBACKUP_UTILITY", "CHANGE_SHUTDOWN_MODE",
    "TRACE_ANY_ATTACHMENT", "MONITOR_ANY_ATTACHMENT", "ACCESS_SHUTDOWN_DATABASE", "CREATE_DATABASE",
    "DROP_DATABASE", "USE_GBAK_UTILITY", "USE_GSTAT_UTILITY", "USE_GFIX_UTILITY", "IGNORE_DB_TRIGGERS",
    "CHANGE_HEADER_SETTINGS", "SELECT_ANY_OBJECT_IN_DATABASE", "ACCESS_ANY_OBJECT_IN_DATABASE",
    "MODIFY_ANY_OBJECT_IN_DATABASE", "CHANGE_MAPPING_RULES", "USE_GRANTED_BY_CLAUSE",
    "GRANT_REVOKE_ON_ANY_OBJECT", "GRANT_REVOKE_ANY_DDL_RIGHT", "CREATE_PRIVILEGED_ROLES", "GET_DBCRYPT_INFO",
    "MODIFY_EXT_CONN_POOL", "REPLICATE_INTO_DATABASE", "PROFILE_ANY_ATTACHMENT",
]  # bit n+1 of RDB$ROLES.RDB$SYSTEM_PRIVILEGES


def role_script(spec: Spec, original: Spec | None, engine: int) -> Script:
    sc = Script()
    name = _name(spec, what="Role name")
    qn = q(name)
    privs = [p for p in spec.get("system_privileges") or [] if p]
    if original and original["name"] != name:
        sc.warn("Firebird cannot rename roles; the name change is ignored.")
        name, qn = original["name"], q(original["name"])
    if not original:
        out = f"CREATE ROLE {qn}"
        if privs and _need_fb4(engine, sc, "System privileges"):
            out += " SET SYSTEM PRIVILEGES TO " + ", ".join(privs)
        sc.add(out)
    elif sorted(privs) != sorted(original.get("system_privileges") or []) and \
            _need_fb4(engine, sc, "System privileges"):
        sc.add(f"ALTER ROLE {qn} SET SYSTEM PRIVILEGES TO " + ", ".join(privs) if privs
               else f"ALTER ROLE {qn} DROP SYSTEM PRIVILEGES")
    before = {(m.get("type") or "USER", m["name"]): bool(m.get("admin")) for m in (original or {}).get("members") or []}
    after = {(m.get("type") or "USER", _txt(m["name"])): bool(m.get("admin")) for m in spec.get("members") or []
             if _txt(m.get("name"))}
    for (gt, gn), adm in before.items():
        if (gt, gn) not in after:
            sc.add(f"REVOKE {qn} FROM {_grantee(gt, gn)}")
        elif adm and not after[(gt, gn)]:
            sc.add(f"REVOKE ADMIN OPTION FOR {qn} FROM {_grantee(gt, gn)}")
    for (gt, gn), adm in after.items():
        if (gt, gn) not in before or (adm and not before[(gt, gn)]):
            sc.add(f"GRANT {qn} TO {_grantee(gt, gn)}" + (" WITH ADMIN OPTION" if adm else ""))
    if _txt(spec.get("comment")) != _txt((original or {}).get("comment")):
        sc.add(comment_on("roles", name, spec.get("comment")))
    return sc


def collation_script(spec: Spec, original: Spec | None, engine: int) -> Script:
    sc = Script()
    name = _name(spec, what="Collation name")
    if original:
        sc.warn("Collations cannot be altered; drop and re-create it to change its definition.")
    else:
        out = f"CREATE COLLATION {q(name)} FOR {_name(spec, 'charset', 'Character set')}"
        if _txt(spec.get("external")):
            out += f"\n    FROM EXTERNAL ({s(spec['external'])})"
        elif _txt(spec.get("base")):
            out += f"\n    FROM {q(spec['base'])}"
        if spec.get("pad_space") is not None:
            out += "\n    " + ("PAD SPACE" if spec["pad_space"] else "NO PAD")
        if spec.get("case_insensitive") is not None:
            out += "\n    CASE " + ("INSENSITIVE" if spec["case_insensitive"] else "SENSITIVE")
        if spec.get("accent_insensitive") is not None:
            out += "\n    ACCENT " + ("INSENSITIVE" if spec["accent_insensitive"] else "SENSITIVE")
        if _txt(spec.get("attributes")):
            out += f"\n    {s(_txt(spec['attributes']))}"
        sc.add(out)
    if _txt(spec.get("comment")) != _txt((original or {}).get("comment")):
        sc.add(comment_on("collations", name, spec.get("comment")))
    return sc


# ------------------------------------------------------------------ comments
def comments_script(spec: Spec, original: Spec | None, engine: int) -> Script:
    """``spec``: {otype, name, package?, comment, items: [{name, comment}]} (columns / parameters)."""
    sc = Script()
    otype, name = spec["otype"], _name(spec)
    orig = original or {}
    if _txt(spec.get("comment")) != _txt(orig.get("comment")):
        sc.add(comment_on(otype, name, spec.get("comment")))
    before = {i["name"]: _txt(i.get("comment")) for i in orig.get("items") or []}
    for i in spec.get("items") or []:
        if _txt(i.get("comment")) != before.get(i["name"], ""):
            sc.add(comment_on(otype, name, i.get("comment"), sub=i["name"], package=spec.get("package")))
    return sc


# -------------------------------------------------------------------- rename
RENAME_SUPPORT = {
    "columns": "ALTER TABLE … ALTER COLUMN … TO",
    "domains": "ALTER DOMAIN … TO",
    "indices": "re-created under the new name (DROP INDEX + CREATE INDEX)",
}
RENAME_UNSUPPORTED = {
    "tables": "Firebird has no ALTER TABLE … RENAME. Create a new table, copy the data and drop the old one.",
    "views": "Views cannot be renamed. Create the view under the new name and drop the old one.",
    "sequences": "ALTER SEQUENCE cannot rename. Create a new sequence (SET GENERATOR to the current value) "
                 "and update the code that uses it.",
    "procedures": "Routines cannot be renamed; create a copy under the new name.",
    "functions": "Routines cannot be renamed; create a copy under the new name.",
    "packages": "Packages cannot be renamed; create a copy under the new name.",
    "triggers": "Triggers cannot be renamed; create a copy and drop the old one.",
    "exceptions": "Exceptions cannot be renamed; create a new one.",
    "roles": "Roles cannot be renamed.",
    "users": "Users cannot be renamed.",
    "collations": "Collations cannot be renamed.",
}


def rename_script(spec: Spec, original: Spec | None, engine: int) -> Script:
    """``spec``: {otype, name, new_name, table? (columns), index?: index spec (indices)}."""
    sc = Script()
    otype, name, new = spec["otype"], _name(spec), _name(spec, "new_name", "New name")
    if otype == "columns":
        sc.add(f"ALTER TABLE {q(_name(spec, 'table', 'Table'))} ALTER COLUMN {q(name)} TO {q(new)}")
    elif otype == "domains":
        sc.add(f"ALTER DOMAIN {q(name)} TO {q(new)}")
    elif otype == "indices":
        idx = dict(spec.get("index") or {})
        if not idx:
            raise DDLError("Index definition missing")
        if idx.get("constraint_name"):
            raise DDLError("This index belongs to a constraint; rename the constraint's index by re-creating "
                           "the constraint in the table designer (USING INDEX).")
        sc.add(f"DROP INDEX {q(name)}")
        idx["name"] = new
        sc.add(index_create(idx, idx["table"], engine, sc))
        if idx.get("active") is False:
            sc.add(f"ALTER INDEX {q(new)} INACTIVE")
        if _txt(idx.get("comment")):
            sc.add(comment_on("indices", new, idx["comment"]))
        sc.warn("The index is dropped and rebuilt; this can take a while on big tables.")
    else:
        raise DDLError(RENAME_UNSUPPORTED.get(otype, f"{otype} cannot be renamed."))
    return sc


# -------------------------------------------------------------------- grants
_OBJECT_CLAUSE = {
    "tables": "TABLE", "views": "", "procedures": "PROCEDURE", "functions": "FUNCTION", "packages": "PACKAGE",
    "sequences": "SEQUENCE", "exceptions": "EXCEPTION",
}
_GRANTEE_KW = {"USER", "ROLE", "PROCEDURE", "FUNCTION", "PACKAGE", "TRIGGER", "VIEW"}
COLUMN_PRIVILEGES = {"UPDATE", "REFERENCES"}


def _grantee(gtype: str, name: str) -> str:
    gtype = (gtype or "USER").upper()
    if name.upper() == "PUBLIC":
        return "PUBLIC"
    if gtype not in _GRANTEE_KW:
        raise DDLError(f"Unknown grantee type {gtype!r}")
    return f"{gtype} {q(name)}"


def _on(otype: str, obj: str) -> str:
    if otype not in _OBJECT_CLAUSE:
        raise DDLError(f"Privileges on {otype} cannot be edited here")
    kw = _OBJECT_CLAUSE[otype]
    return f"{kw} {q(obj)}" if kw else q(obj)


def _grant_key(g: Spec) -> tuple:
    return ((g.get("grantee_type") or "USER").upper(), g["grantee"], g["privilege"].upper(), g["object"],
            g["object_type"], g.get("column") or None)


def grants_script(spec: Spec, original: Spec | None, engine: int) -> Script:
    """Diff two grant lists (rows as returned by ``Catalog.privileges``) into GRANT/REVOKE statements.

    ``spec`` = {"grants": [...]} (desired) and ``original`` = {"grants": [...]} (current).
    """
    sc = Script()
    before = {_grant_key(g): bool(g.get("grant_option")) for g in (original or {}).get("grants") or []}
    after = {_grant_key(g): bool(g.get("grant_option")) for g in spec.get("grants") or []}
    for k in after:
        if k[2] not in PRIVILEGE_NAMES.values() or k[2] in ("CREATE", "ALTER ANY", "DROP ANY"):
            raise DDLError(f"Unsupported privilege {k[2]!r}")
        if k[2] != "MEMBER" and k[5] and k[2] not in COLUMN_PRIVILEGES:
            raise DDLError(f"{k[2]} cannot be granted on single columns")
    revokes: dict[tuple, list] = {}
    revoke_opt: dict[tuple, list] = {}
    grants: dict[tuple, list] = {}
    for k, opt in before.items():
        if k not in after:
            revokes.setdefault((k[0], k[1], k[3], k[4], False), []).append(k)
        elif opt and not after[k]:
            revoke_opt.setdefault((k[0], k[1], k[3], k[4], False), []).append(k)
    for k, opt in after.items():
        if k not in before:
            grants.setdefault((k[0], k[1], k[3], k[4], opt), []).append(k)
        elif opt and not before[k]:
            grants.setdefault((k[0], k[1], k[3], k[4], True), []).append(k)

    def priv_list(keys: list[tuple]) -> str:
        whole: list[str] = []
        cols: dict[str, list[str]] = {}
        for k in keys:
            if k[5]:
                cols.setdefault(k[2], []).append(k[5])
            elif k[2] not in whole:
                whole.append(k[2])
        order = ["SELECT", "INSERT", "UPDATE", "DELETE", "REFERENCES", "EXECUTE", "USAGE"]
        parts = [p for p in order if p in whole]
        for p in order:
            if p in cols and p not in whole:
                parts.append(f"{p} ({_cols(sorted(set(cols[p])))})")
        return ", ".join(parts)

    def emit(groups: dict, verb: str, prefix: str = "") -> None:
        for (gtype, gname, obj, otype, opt), keys in sorted(groups.items(), key=lambda kv: str(kv[0])):
            members = [k for k in keys if k[2] == "MEMBER"]
            others = [k for k in keys if k[2] != "MEMBER"]
            to = "TO" if verb == "GRANT" else "FROM"
            if members:
                pre = "ADMIN OPTION FOR " if prefix else ""
                st = f"{verb} {pre}{q(obj)} {to} {_grantee(gtype, gname)}"
                if verb == "GRANT" and opt:
                    st += " WITH ADMIN OPTION"
                sc.add(st)
            if others:
                st = f"{verb} {prefix}{priv_list(others)} ON {_on(otype, obj)} {to} {_grantee(gtype, gname)}"
                if verb == "GRANT" and opt:
                    st += " WITH GRANT OPTION"
                sc.add(st)

    emit(revokes, "REVOKE")
    emit(revoke_opt, "REVOKE", "GRANT OPTION FOR ")
    emit(grants, "GRANT")
    return sc


# ---------------------------------------------------------------------- drop
def drop_script(spec: Spec, original: Spec | None, engine: int) -> Script:
    from .catalog import drop_statement

    sc = Script()
    otype = spec["otype"]
    if otype == "columns":
        sc.add(f"ALTER TABLE {q(_name(spec, 'table', 'Table'))} DROP {q(_name(spec))}")
    elif otype == "constraints":
        sc.add(f"ALTER TABLE {q(_name(spec, 'table', 'Table'))} DROP CONSTRAINT {q(_name(spec))}")
    elif otype == "packages" and spec.get("body_only"):
        sc.add(f"DROP PACKAGE BODY {q(_name(spec))}")
    else:
        sc.add(drop_statement(otype, _name(spec)))
    return sc


# ------------------------------------------------------------------ dispatch
def _table(spec: Spec, original: Spec | None, engine: int) -> Script:
    return table_alter(spec, original, engine) if original else table_create(spec, engine)


GENERATORS = {
    "table": _table,
    "index": index_script,
    "view": view_script,
    "procedure": procedure_script,
    "function": function_script,
    "trigger": trigger_script,
    "package": package_script,
    "sequence": sequence_script,
    "domain": domain_script,
    "exception": exception_script,
    "role": role_script,
    "collation": collation_script,
    "comments": comments_script,
    "rename": rename_script,
    "grants": grants_script,
    "drop": drop_script,
}


def generate(kind: str, spec: Spec, original: Spec | None = None, engine: int = 5) -> Script:
    fn = GENERATORS.get(kind)
    if fn is None:
        raise DDLError(f"Unknown DDL kind {kind!r}")
    return fn(spec, original, engine)
