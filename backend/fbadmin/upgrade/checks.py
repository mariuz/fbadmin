"""Upgrade checks: what in a database needs attention before it moves to a newer Firebird.

Each check reads the source database's metadata (Firebird 3, 4 or 5) and returns findings with a
severity (blocker / warning / info), the affected objects, an explanation and a concrete fix.
Facts about Firebird versions used here were checked on Firebird 3.0, 4.0, 5.0 and a 6.0 snapshot
server (see docs/UPGRADE.md) or taken from the Firebird sources and release notes.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from ..metadata import Catalog
from . import keywords as K

BLOCKER, WARNING, INFO = "blocker", "warning", "info"
SEVERITIES = (BLOCKER, WARNING, INFO)

CAT_RESERVED = "Reserved words"
CAT_FEATURES = "Removed or changed features"
CAT_TYPES = "Data types and SQL behaviour"
CAT_SECURITY = "Security and authentication"
CAT_CONFIG = "Server configuration"
CAT_STORAGE = "Storage and ODS"
CAT_SCHEMAS = "SQL schemas (Firebird 6)"

RELEASE_NOTES = {
    "4.0": "https://firebirdsql.org/file/documentation/release_notes/html/en/4_0/rlsnotes40.html",
    "5.0": "https://firebirdsql.org/file/documentation/release_notes/html/en/5_0/rlsnotes50.html",
    "6.0": "https://github.com/FirebirdSQL/firebird/tree/master/doc",
}


@dataclass
class Ctx:
    cat: Catalog
    source: str            # "3.0", "4.0" or "5.0"
    target: str            # "4.0", "5.0" or "6.0"
    facts: dict[str, Any]
    _sources: list[dict] | None = field(default=None, repr=False)

    def crosses(self, version: str) -> bool:
        """True when the upgrade passes ``version`` (source older than it, target at least it)."""
        return float(self.source) < float(version) <= float(self.target)

    def rows(self, sql: str) -> list[dict[str, Any]]:
        return self.cat.rows(sql, scoped=False)

    def try_rows(self, sql: str) -> list[dict[str, Any]] | None:
        try:
            return self.rows(sql)
        except Exception:  # noqa: BLE001 - optional system table or missing privilege
            try:
                self.cat.conn.rollback()
            except Exception:  # noqa: BLE001
                pass
            return None


def obj(kind: str, name: str, *, parent: str | None = None, link: tuple[str, str] | None = None,
        detail: str | None = None, fix: str | None = None) -> dict[str, Any]:
    """One affected object. ``link`` is the (tree object type, name) to select in the Object Explorer."""
    return {"type": kind, "name": name, "parent": parent,
            "link": {"otype": link[0], "name": link[1]} if link else None, "detail": detail, "fix": fix}


def finding(fid: str, category: str, severity: str, title: str, explanation: str, fix: str,
            objects: list[dict] | None = None, refs: list[str] | None = None) -> dict[str, Any]:
    return {"id": fid, "category": category, "severity": severity, "title": title,
            "explanation": explanation, "fix": fix, "objects": objects or [], "refs": refs or []}


CHECKS: list[Callable[[Ctx], list[dict]]] = []


def check(fn: Callable[[Ctx], list[dict]]) -> Callable[[Ctx], list[dict]]:
    CHECKS.append(fn)
    return fn


# ====================================================================== facts
def database_facts(cat: Catalog) -> dict[str, Any]:
    mon = cat.one("SELECT MON$ODS_MAJOR AS ods_major, MON$ODS_MINOR AS ods_minor, MON$SQL_DIALECT AS dialect, "
                  "MON$PAGE_SIZE AS page_size, MON$PAGES AS pages, TRIM(MON$DATABASE_NAME) AS path "
                  "FROM MON$DATABASE", scoped=False) or {}
    crypt = None
    if cat.has_column("MON$DATABASE", "MON$CRYPT_STATE"):
        r = cat.one("SELECT MON$CRYPT_STATE AS st FROM MON$DATABASE", scoped=False)
        crypt = bool(r and r["st"])
    elif cat.has_column("MON$DATABASE", "MON$CRYPT_PAGE"):
        r = cat.one("SELECT MON$CRYPT_PAGE AS pg FROM MON$DATABASE", scoped=False)
        crypt = bool(r and r["pg"])
    engine = cat.engine_version
    return {
        "engine_version": engine,
        "version": K.version_key(engine or "3"),
        "ods": f"{mon.get('ods_major')}.{mon.get('ods_minor')}",
        "dialect": mon.get("dialect"),
        "page_size": mon.get("page_size"),
        "pages": mon.get("pages"),
        "size_bytes": (mon.get("page_size") or 0) * (mon.get("pages") or 0),
        "file": mon.get("path"),
        "charset": cat.default_charset,
        "encrypted": crypt,
        "counts": {k: v for k, v in cat.object_counts().items() if v},
    }


# =================================================================== analysis
def analyze(cat: Catalog, target: str, *, log: Callable[[str], None] | None = None) -> dict[str, Any]:
    """Run every check against the database behind ``cat`` for an upgrade to ``target``."""
    facts = database_facts(cat)
    source, target = facts["version"], K.version_key(target)
    if float(source) >= float(target):
        raise ValueError(f"The database runs on Firebird {facts['engine_version']}; choose a newer target version "
                         f"than {source}")
    ctx = Ctx(cat=cat, source=source, target=target, facts=facts)
    findings: list[dict] = []
    t0 = time.time()
    for fn in CHECKS:
        try:
            findings.extend(fn(ctx))
        except Exception as e:  # noqa: BLE001 - one broken check must not hide the others
            try:
                cat.conn.rollback()
            except Exception:  # noqa: BLE001
                pass
            findings.append(finding(f"check_failed_{fn.__name__}", CAT_FEATURES, WARNING,
                                    f"Check {fn.__name__} could not run",
                                    f"The check failed with: {e}", "Review this area by hand."))
        if log:
            log(f"{fn.__name__}: done")
    order = {s: i for i, s in enumerate(SEVERITIES)}
    findings.sort(key=lambda f: (order[f["severity"]], f["category"], f["title"]))
    return {
        "source": facts,
        "target": {"version": target, "ods": "{}.{}".format(*K.ODS[target])},
        "findings": findings,
        "counts": {s: sum(1 for f in findings if f["severity"] == s) for s in SEVERITIES},
        "seconds": round(time.time() - t0, 2),
        "keyword_sources": {v: K.KEYWORDS[v]["source"] for v in (source, target)},
    }


# ============================================================ reserved words
_RENAMABLE = {"column", "domain"}
_REFERENCED = {"table", "view", "column", "procedure", "function", "package", "sequence", "exception", "domain",
               "procedure parameter", "function argument"}


def _named_objects(ctx: Ctx) -> list[dict[str, Any]]:
    """Every user object name: (kind, name, parent, tree link)."""
    out: list[dict[str, Any]] = []

    def add(kind, rows, link_type=None, parent_link=None):
        for r in rows:
            name = (r.get("name") or "").strip()
            if not name:
                continue
            parent = (r.get("parent") or "").strip() or None
            if parent_link and parent:
                link = (parent_link(r), parent)
            elif link_type:
                link = (link_type, name)
            else:
                link = None
            out.append({"kind": kind, "name": name, "parent": parent, "link": link})

    rels = ctx.rows("SELECT TRIM(RDB$RELATION_NAME) AS name, CASE WHEN RDB$VIEW_BLR IS NULL THEN 0 ELSE 1 END AS v "
                    "FROM RDB$RELATIONS WHERE COALESCE(RDB$SYSTEM_FLAG, 0) = 0")
    add("table", [r for r in rels if not r["v"]], "tables")
    add("view", [r for r in rels if r["v"]], "views")
    views = {r["name"] for r in rels if r["v"]}
    add("column", ctx.rows(
        "SELECT TRIM(rf.RDB$FIELD_NAME) AS name, TRIM(rf.RDB$RELATION_NAME) AS parent FROM RDB$RELATION_FIELDS rf "
        "JOIN RDB$RELATIONS r ON r.RDB$RELATION_NAME = rf.RDB$RELATION_NAME WHERE COALESCE(r.RDB$SYSTEM_FLAG, 0) = 0"),
        parent_link=lambda r: "views" if r["parent"] in views else "tables")
    add("procedure", ctx.rows("SELECT TRIM(RDB$PROCEDURE_NAME) AS name, TRIM(RDB$PACKAGE_NAME) AS parent "
                              "FROM RDB$PROCEDURES WHERE COALESCE(RDB$SYSTEM_FLAG, 0) = 0"),
        "procedures", parent_link=lambda r: "packages")
    add("procedure parameter", ctx.rows(
        "SELECT TRIM(pp.RDB$PARAMETER_NAME) AS name, TRIM(pp.RDB$PROCEDURE_NAME) AS parent, "
        "TRIM(pp.RDB$PACKAGE_NAME) AS pkg FROM RDB$PROCEDURE_PARAMETERS pp "
        "WHERE COALESCE(pp.RDB$SYSTEM_FLAG, 0) = 0"), parent_link=lambda r: "packages" if r["pkg"] else "procedures")
    add("function", ctx.rows("SELECT TRIM(RDB$FUNCTION_NAME) AS name, TRIM(RDB$PACKAGE_NAME) AS parent "
                             "FROM RDB$FUNCTIONS WHERE COALESCE(RDB$SYSTEM_FLAG, 0) = 0"),
        "functions", parent_link=lambda r: "packages")
    add("function argument", ctx.rows(
        "SELECT TRIM(fa.RDB$ARGUMENT_NAME) AS name, TRIM(fa.RDB$FUNCTION_NAME) AS parent, "
        "TRIM(fa.RDB$PACKAGE_NAME) AS pkg FROM RDB$FUNCTION_ARGUMENTS fa "
        "WHERE COALESCE(fa.RDB$SYSTEM_FLAG, 0) = 0 AND fa.RDB$ARGUMENT_NAME IS NOT NULL"),
        parent_link=lambda r: "packages" if r["pkg"] else "functions")
    add("package", ctx.rows("SELECT TRIM(RDB$PACKAGE_NAME) AS name FROM RDB$PACKAGES "
                            "WHERE COALESCE(RDB$SYSTEM_FLAG, 0) = 0"), "packages")
    add("trigger", ctx.rows(
        "SELECT TRIM(t.RDB$TRIGGER_NAME) AS name FROM RDB$TRIGGERS t WHERE COALESCE(t.RDB$SYSTEM_FLAG, 0) = 0 "
        "AND NOT EXISTS (SELECT 1 FROM RDB$CHECK_CONSTRAINTS cc WHERE cc.RDB$TRIGGER_NAME = t.RDB$TRIGGER_NAME)"),
        "triggers")
    add("domain", ctx.rows("SELECT TRIM(RDB$FIELD_NAME) AS name FROM RDB$FIELDS WHERE COALESCE(RDB$SYSTEM_FLAG, 0) = 0 "
                           "AND RDB$FIELD_NAME NOT STARTING WITH 'RDB$'"), "domains")
    add("sequence", ctx.rows("SELECT TRIM(RDB$GENERATOR_NAME) AS name FROM RDB$GENERATORS "
                             "WHERE COALESCE(RDB$SYSTEM_FLAG, 0) = 0"), "sequences")
    add("exception", ctx.rows("SELECT TRIM(RDB$EXCEPTION_NAME) AS name FROM RDB$EXCEPTIONS "
                              "WHERE COALESCE(RDB$SYSTEM_FLAG, 0) = 0"), "exceptions")
    add("role", ctx.rows("SELECT TRIM(RDB$ROLE_NAME) AS name FROM RDB$ROLES WHERE COALESCE(RDB$SYSTEM_FLAG, 0) = 0"),
        "roles")
    add("index", ctx.rows(
        "SELECT TRIM(i.RDB$INDEX_NAME) AS name, TRIM(i.RDB$RELATION_NAME) AS parent FROM RDB$INDICES i "
        "JOIN RDB$RELATIONS r ON r.RDB$RELATION_NAME = i.RDB$RELATION_NAME "
        "WHERE COALESCE(r.RDB$SYSTEM_FLAG, 0) = 0 AND i.RDB$INDEX_NAME NOT STARTING WITH 'RDB$'"))
    for o in out:
        if o["kind"] == "index":
            o["link"] = ("indices", o["name"])
    add("constraint", ctx.rows(
        "SELECT TRIM(RDB$CONSTRAINT_NAME) AS name, TRIM(RDB$RELATION_NAME) AS parent FROM RDB$RELATION_CONSTRAINTS "
        "WHERE RDB$CONSTRAINT_NAME NOT STARTING WITH 'INTEG_' AND RDB$RELATION_NAME NOT STARTING WITH 'RDB$'"),
        parent_link=lambda r: "tables")
    add("collation", ctx.rows("SELECT TRIM(RDB$COLLATION_NAME) AS name FROM RDB$COLLATIONS "
                              "WHERE COALESCE(RDB$SYSTEM_FLAG, 0) = 0"))
    add("BLOB filter", ctx.rows("SELECT TRIM(RDB$FUNCTION_NAME) AS name FROM RDB$FILTERS "
                                "WHERE COALESCE(RDB$SYSTEM_FLAG, 0) = 0"))
    users = ctx.try_rows("SELECT TRIM(SEC$USER_NAME) AS name FROM SEC$USERS")
    if users:
        add("user", users, "users")
    return out


def _rename_fix(o: dict) -> str:
    q, new = K.qi(o["name"]), K.qi(o["name"] + "_")
    if o["kind"] == "column" and o["link"] and o["link"][0] == "views":
        return (f"Recreate the view with another name for the column in its column list (CREATE OR ALTER VIEW "
                f"{K.qi(o['parent'])} (..., {new}, ...) AS ...), or quote it as {q}.")
    if o["kind"] == "column":
        return f"ALTER TABLE {K.qi(o['parent'])} ALTER COLUMN {q} TO {new};"
    if o["kind"] == "domain":
        return f"ALTER DOMAIN {q} TO {new};"
    if o["kind"] in ("procedure parameter", "function argument"):
        return f"Rename the parameter in {K.qi(o['parent'])} (header and body) with CREATE OR ALTER."
    article = "an" if o["kind"][0] in "aeiou" else "a"
    return f"Write it as {q} in every statement (Firebird cannot rename {article} {o['kind']}), or recreate it under a new name."


@check
def reserved_names(ctx: Ctx) -> list[dict]:
    new = K.new_reserved(ctx.source, ctx.target)
    hits = [o for o in _named_objects(ctx) if o["name"] in new]
    if not hits:
        return []
    out = []
    referenced = [o for o in hits if o["kind"] in _REFERENCED]
    other = [o for o in hits if o["kind"] not in _REFERENCED]

    def items(objs):
        return [obj(o["kind"], o["name"], parent=o["parent"], link=o["link"], fix=_rename_fix(o)) for o in objs]

    if referenced:
        out.append(finding(
            "reserved_names", CAT_RESERVED, BLOCKER,
            f"Names that are reserved words in Firebird {ctx.target}",
            f"These tables, columns, routines and other objects have names that Firebird {ctx.target} reserves "
            f"(they were not reserved in Firebird {ctx.source}). The objects keep working inside the database "
            "after a backup and restore, but every statement that writes the name without double quotes fails "
            "with 'Token unknown': application SQL, scripts, and PSQL code when it is next changed.",
            "Rename columns and domains (the statements are below), or quote the name with double quotes, in "
            "upper case, everywhere it is used. Tables, views, procedures, functions, packages, sequences and "
            "exceptions cannot be renamed in Firebird; quote them or recreate them under a new name.",
            items(referenced), [RELEASE_NOTES.get(ctx.target, "")]))
    if other:
        out.append(finding(
            "reserved_names_ddl", CAT_RESERVED, WARNING,
            f"Triggers, indices, constraints or roles named with Firebird {ctx.target} reserved words",
            "These names are mostly used in DDL (ALTER TRIGGER, ALTER INDEX, GRANT ... TO role). Such statements "
            "must quote them on the new version.",
            "Quote the name in DDL scripts and GRANT statements, or recreate the object under a new name.",
            items(other)))
    return out


def _sources(ctx: Ctx) -> list[dict]:
    """PSQL and SQL source texts stored in the database, with the object they belong to."""
    if ctx._sources is not None:
        return ctx._sources
    out: list[dict] = []

    def add(kind, rows, link_type, parent_link_type=None):
        for r in rows:
            text = r.get("src")
            if not text:
                continue
            parent = (r.get("parent") or "").strip() or None
            link = (parent_link_type, parent) if parent and parent_link_type else (link_type, r["name"]) \
                if link_type else None
            out.append({"kind": kind, "name": r["name"], "parent": parent, "link": link, "text": text})

    add("procedure", ctx.rows("SELECT TRIM(RDB$PROCEDURE_NAME) AS name, TRIM(RDB$PACKAGE_NAME) AS parent, "
                              "RDB$PROCEDURE_SOURCE AS src FROM RDB$PROCEDURES WHERE COALESCE(RDB$SYSTEM_FLAG, 0) = 0 "
                              "AND RDB$PACKAGE_NAME IS NULL"), "procedures")
    add("function", ctx.rows("SELECT TRIM(RDB$FUNCTION_NAME) AS name, RDB$FUNCTION_SOURCE AS src FROM RDB$FUNCTIONS "
                             "WHERE COALESCE(RDB$SYSTEM_FLAG, 0) = 0 AND RDB$PACKAGE_NAME IS NULL"), "functions")
    pk = ctx.rows("SELECT TRIM(RDB$PACKAGE_NAME) AS name, RDB$PACKAGE_HEADER_SOURCE AS h, "
                  "RDB$PACKAGE_BODY_SOURCE AS b FROM RDB$PACKAGES WHERE COALESCE(RDB$SYSTEM_FLAG, 0) = 0")
    add("package header", [{"name": r["name"], "src": r["h"]} for r in pk], "packages")
    add("package body", [{"name": r["name"], "src": r["b"]} for r in pk], "packages")
    add("trigger", ctx.rows(
        "SELECT TRIM(t.RDB$TRIGGER_NAME) AS name, t.RDB$TRIGGER_SOURCE AS src FROM RDB$TRIGGERS t "
        "WHERE COALESCE(t.RDB$SYSTEM_FLAG, 0) = 0 AND NOT EXISTS (SELECT 1 FROM RDB$CHECK_CONSTRAINTS cc "
        "WHERE cc.RDB$TRIGGER_NAME = t.RDB$TRIGGER_NAME)"), "triggers")
    add("view", ctx.rows("SELECT TRIM(RDB$RELATION_NAME) AS name, RDB$VIEW_SOURCE AS src FROM RDB$RELATIONS "
                         "WHERE COALESCE(RDB$SYSTEM_FLAG, 0) = 0 AND RDB$VIEW_BLR IS NOT NULL"), "views")
    add("check constraint", ctx.rows(
        "SELECT TRIM(rc.RDB$CONSTRAINT_NAME) AS name, TRIM(rc.RDB$RELATION_NAME) AS parent, "
        "t.RDB$TRIGGER_SOURCE AS src FROM RDB$RELATION_CONSTRAINTS rc "
        "JOIN RDB$CHECK_CONSTRAINTS cc ON cc.RDB$CONSTRAINT_NAME = rc.RDB$CONSTRAINT_NAME "
        "JOIN RDB$TRIGGERS t ON t.RDB$TRIGGER_NAME = cc.RDB$TRIGGER_NAME AND t.RDB$TRIGGER_TYPE = 1 "
        "WHERE rc.RDB$CONSTRAINT_TYPE = 'CHECK' AND rc.RDB$RELATION_NAME NOT STARTING WITH 'RDB$'"),
        None, "tables")
    add("computed column", ctx.rows(
        "SELECT TRIM(rf.RDB$FIELD_NAME) AS name, TRIM(rf.RDB$RELATION_NAME) AS parent, f.RDB$COMPUTED_SOURCE AS src "
        "FROM RDB$RELATION_FIELDS rf JOIN RDB$FIELDS f ON f.RDB$FIELD_NAME = rf.RDB$FIELD_SOURCE "
        "JOIN RDB$RELATIONS r ON r.RDB$RELATION_NAME = rf.RDB$RELATION_NAME "
        "WHERE COALESCE(r.RDB$SYSTEM_FLAG, 0) = 0 AND f.RDB$COMPUTED_SOURCE IS NOT NULL"), None, "tables")
    add("column default", ctx.rows(
        "SELECT TRIM(rf.RDB$FIELD_NAME) AS name, TRIM(rf.RDB$RELATION_NAME) AS parent, rf.RDB$DEFAULT_SOURCE AS src "
        "FROM RDB$RELATION_FIELDS rf JOIN RDB$RELATIONS r ON r.RDB$RELATION_NAME = rf.RDB$RELATION_NAME "
        "WHERE COALESCE(r.RDB$SYSTEM_FLAG, 0) = 0 AND rf.RDB$DEFAULT_SOURCE IS NOT NULL"), None, "tables")
    dom = ctx.rows("SELECT TRIM(RDB$FIELD_NAME) AS name, RDB$DEFAULT_SOURCE AS d, RDB$VALIDATION_SOURCE AS v "
                   "FROM RDB$FIELDS WHERE COALESCE(RDB$SYSTEM_FLAG, 0) = 0 AND RDB$FIELD_NAME NOT STARTING WITH 'RDB$'")
    add("domain default", [{"name": r["name"], "src": r["d"]} for r in dom], "domains")
    add("domain check", [{"name": r["name"], "src": r["v"]} for r in dom], "domains")
    add("index expression", ctx.rows(
        "SELECT TRIM(i.RDB$INDEX_NAME) AS name, i.RDB$EXPRESSION_SOURCE AS src FROM RDB$INDICES i "
        "JOIN RDB$RELATIONS r ON r.RDB$RELATION_NAME = i.RDB$RELATION_NAME "
        "WHERE COALESCE(r.RDB$SYSTEM_FLAG, 0) = 0 AND i.RDB$EXPRESSION_SOURCE IS NOT NULL"), "indices")
    for s in out:
        if not isinstance(s["text"], str):
            s["text"] = s["text"].decode("utf-8", "replace") if isinstance(s["text"], bytes) else str(s["text"])
    ctx._sources = out
    return out


def _label(s: dict) -> str:
    return s["kind"] + (f" of {s['parent']}" if s["parent"] else "")


@check
def reserved_in_source(ctx: Ctx) -> list[dict]:
    new = K.new_reserved(ctx.source, ctx.target)
    old_kw = K.keywords(ctx.source)
    items = []
    for s in _sources(ctx):
        found: dict[str, list[int]] = {}
        for w in K.words(s["text"]):
            if w.upper not in new:
                continue
            if w.upper in old_kw and w.after == "(":
                continue  # a built-in function of the source version (e.g. RDB$GET_CONTEXT(...)) stays valid
            found.setdefault(w.upper, []).append(w.line)
        if not found:
            continue
        where = "; ".join(f"{word} (line {', '.join(map(str, lines[:5]))}{'…' if len(lines) > 5 else ''})"
                          for word, lines in sorted(found.items()))
        first = K.line_of(s["text"], min(ln for lines in found.values() for ln in lines))
        quoted = ", ".join(f'"{w}"' for w in sorted(found))
        items.append(obj(s["kind"], s["name"], parent=s["parent"], link=s["link"],
                         detail=f"{where}: {first[:160]}",
                         fix=f"Write {quoted} in double quotes where it names an object or variable, or rename it."))
    if not items:
        return []
    return [finding(
        "reserved_in_source", CAT_RESERVED, WARNING,
        f"Code that uses words reserved in Firebird {ctx.target}",
        "These PSQL modules, views, defaults and constraints contain, outside strings and comments, words that "
        f"Firebird {ctx.target} reserves. They keep running after a backup and restore (the compiled form is "
        "restored), but they fail to compile when they are next altered or extracted and replayed. Some hits can "
        "be harmless (for example a word inside a dynamic SQL string built by concatenation is not checked); "
        "a trial restore with recompilation confirms each one.",
        "Quote the identifier with double quotes (in upper case) or rename the object or variable it refers to, "
        "then recompile the module on the old server to check it.",
        items)]


# Built-in functions (not reserved words) added by each version, from the release notes.
NEW_BUILTINS = {
    "4.0": {"BASE64_DECODE", "BASE64_ENCODE", "BLOB_APPEND", "COMPARE_DECFLOAT", "CRYPT_HASH", "CUME_DIST",
            "FIRST_DAY", "HEX_DECODE", "HEX_ENCODE", "LAST_DAY", "MAKE_DBKEY", "NORMALIZE_DECFLOAT", "NTILE",
            "PERCENT_RANK", "QUANTIZE", "RSA_DECRYPT", "RSA_ENCRYPT", "RSA_PRIVATE", "RSA_PUBLIC", "RSA_SIGN_HASH",
            "RSA_VERIFY_HASH", "TOTALORDER"},
    "5.0": {"UNICODE_CHAR", "UNICODE_VAL"},
    "6.0": {"ANY_VALUE", "BIN_AND_AGG", "BIN_OR_AGG", "BIN_XOR_AGG", "GENERATE_SERIES", "UNLIST"},
}


@check
def shadowed_functions(ctx: Ctx) -> list[dict]:
    """User functions and procedures named like a built-in function added by the target version."""
    new = set().union(*(names for v, names in NEW_BUILTINS.items() if ctx.crosses(v)))
    new &= K.new_keywords(ctx.source, ctx.target)
    if not new:
        return []
    rows = ctx.rows("SELECT 'function' AS kind, TRIM(RDB$FUNCTION_NAME) AS name FROM RDB$FUNCTIONS "
                    "WHERE COALESCE(RDB$SYSTEM_FLAG, 0) = 0 AND RDB$PACKAGE_NAME IS NULL UNION ALL "
                    "SELECT 'procedure', TRIM(RDB$PROCEDURE_NAME) FROM RDB$PROCEDURES "
                    "WHERE COALESCE(RDB$SYSTEM_FLAG, 0) = 0 AND RDB$PACKAGE_NAME IS NULL")
    hits = [(r["kind"].strip(), r["name"]) for r in rows if r["name"] in new]
    if not hits:
        return []
    return [finding(
        "shadowed_functions", CAT_RESERVED, INFO,
        f"Routines named like new Firebird {ctx.target} built-in functions",
        f"Firebird {ctx.target} adds built-in functions with these names. Your routine keeps precedence: an "
        "unquoted call still runs it, not the built-in one (checked on Firebird 4 and 5). Developers who expect "
        "the documented built-in behaviour get yours instead.",
        "Consider renaming the routine so the built-in function becomes usable, and document the clash.",
        [obj(k, n, link=(k + "s", n)) for k, n in hits])]


# ======================================================== removed / changed
# Built-in replacements for the functions of the ib_udf and fbudf libraries (by entry point). Firebird 4 also
# ships misc/upgrade/v4.0/udf_replace.sql, which turns the common ones into PSQL or udf_compat UDR functions.
UDF_REPLACEMENTS = {
    "ib_udf_abs": "ABS(x)", "ib_udf_acos": "ACOS(x)", "ib_udf_ascii_char": "ASCII_CHAR(n)",
    "ib_udf_ascii_val": "ASCII_VAL(s)", "ib_udf_asin": "ASIN(x)", "ib_udf_atan": "ATAN(x)",
    "ib_udf_atan2": "ATAN2(y, x)", "ib_udf_bin_and": "BIN_AND(a, b)", "ib_udf_bin_or": "BIN_OR(a, b)",
    "ib_udf_bin_xor": "BIN_XOR(a, b)", "ib_udf_ceiling": "CEILING(x)", "ib_udf_cos": "COS(x)",
    "ib_udf_cosh": "COSH(x)", "ib_udf_cot": "COT(x)", "ib_udf_div": "TRUNC(a / b) (or the udf_compat UDR)",
    "ib_udf_floor": "FLOOR(x)", "ib_udf_frac": "x - TRUNC(x) (or the udf_compat UDR)", "ib_udf_ln": "LN(x)",
    "ib_udf_log": "LOG(base, x)", "ib_udf_log10": "LOG10(x)", "ib_udf_lower": "LOWER(s)",
    "ib_udf_lpad": "LPAD(s, n, c)", "ib_udf_ltrim": "TRIM(LEADING FROM s)", "ib_udf_mod": "MOD(a, b)",
    "ib_udf_pi": "PI()", "ib_udf_rand": "RAND()", "ib_udf_rpad": "RPAD(s, n, c)",
    "ib_udf_rtrim": "TRIM(TRAILING FROM s)", "ib_udf_sign": "SIGN(x)", "ib_udf_sin": "SIN(x)",
    "ib_udf_sinh": "SINH(x)", "ib_udf_sqrt": "SQRT(x)", "ib_udf_srand": "RAND()",
    "ib_udf_strlen": "CHAR_LENGTH(s) (udf_replace.sql uses OCTET_LENGTH)",
    "ib_udf_substr": "SUBSTRING(s FROM m FOR n - m + 1)", "ib_udf_substrlen": "SUBSTRING(s FROM m FOR n)",
    "ib_udf_tan": "TAN(x)", "ib_udf_tanh": "TANH(x)",
    "addday": "DATEADD(n DAY TO ts)", "addday2": "DATEADD(n DAY TO ts)", "addhour": "DATEADD(n HOUR TO ts)",
    "addmillisecond": "DATEADD(n MILLISECOND TO ts)", "addminute": "DATEADD(n MINUTE TO ts)",
    "addmonth": "DATEADD(n MONTH TO ts)", "addsecond": "DATEADD(n SECOND TO ts)", "addweek": "DATEADD(n WEEK TO ts)",
    "addyear": "DATEADD(n YEAR TO ts)", "dnullif": "NULLIF(a, b)", "inullif": "NULLIF(a, b)",
    "snullif": "NULLIF(a, b)", "i64nullif": "NULLIF(a, b)", "idnvl": "COALESCE(a, b)", "invl": "COALESCE(a, b)",
    "snvl": "COALESCE(a, b)", "dnvl": "COALESCE(a, b)", "i64nvl": "COALESCE(a, b)",
    "fbround": "ROUND(x)", "i64round": "ROUND(x)", "fbtruncate": "TRUNC(x)", "i64truncate": "TRUNC(x)",
    "power": "POWER(b, e)", "right": "RIGHT(s, n)", "string2blob": "CAST(s AS BLOB SUB_TYPE TEXT)",
    "getexacttimestamp": "LOCALTIMESTAMP", "getexacttimestamputc": "CURRENT_TIMESTAMP AT TIME ZONE 'UTC' (Firebird 4+)",
    "isleapyear": "an expression on EXTRACT(YEAR FROM d) (or the udf_compat UDR)",
    "dow": "EXTRACT(WEEKDAY FROM d) (returns a number; or the udf_compat UDR)",
    "sdow": "EXTRACT(WEEKDAY FROM d) (returns a number; or the udf_compat UDR)",
}


@check
def legacy_udfs(ctx: Ctx) -> list[dict]:
    if float(ctx.target) < 4:
        return []
    rows = ctx.rows("SELECT TRIM(RDB$FUNCTION_NAME) AS name, TRIM(RDB$MODULE_NAME) AS module, "
                    "TRIM(RDB$ENTRYPOINT) AS entrypoint FROM RDB$FUNCTIONS "
                    "WHERE COALESCE(RDB$SYSTEM_FLAG, 0) = 0 AND RDB$LEGACY_FLAG = 1 ORDER BY 1")
    if not rows:
        return []
    deps = ctx.rows("SELECT TRIM(d.RDB$DEPENDED_ON_NAME) AS fn, COUNT(DISTINCT d.RDB$DEPENDENT_NAME) AS n "
                    "FROM RDB$DEPENDENCIES d JOIN RDB$FUNCTIONS f ON f.RDB$FUNCTION_NAME = d.RDB$DEPENDED_ON_NAME "
                    "AND f.RDB$LEGACY_FLAG = 1 AND f.RDB$PACKAGE_NAME IS NULL WHERE d.RDB$DEPENDED_ON_TYPE = 15 "
                    "GROUP BY 1")
    used = {r["fn"]: r["n"] for r in deps}
    items = []
    for r in rows:
        rep = UDF_REPLACEMENTS.get((r["entrypoint"] or "").lower())
        detail = f"{r['module']}!{r['entrypoint']}"
        if used.get(r["name"]):
            detail += f"; used by {used[r['name']]} object(s)"
        items.append(obj("function", r["name"], link=("functions", r["name"]), detail=detail,
                         fix=f"Replace calls with {rep}" if rep else
                         "Rewrite as a PSQL function (CREATE OR ALTER FUNCTION ... AS BEGIN ... END) or a UDR"))
    first = ctx.source == "3.0"
    return [finding(
        "legacy_udfs", CAT_FEATURES, BLOCKER if first else WARNING,
        "Legacy UDFs (DECLARE EXTERNAL FUNCTION)",
        "Firebird 4 deprecated legacy UDFs: the default UdfAccess = None in firebird.conf blocks them, so every call "
        "fails until the library is allowed again (checked: Firebird 4, 5 and the 6 snapshot all default to None). "
        "Neither ib_udf nor fbudf is shipped from Firebird 4 on (both were removed from the source tree); most of "
        "their functions have built-in equivalents." + ("" if first else " The source server already runs Firebird 4 or later, so its own "
                         "firebird.conf allows them: copy that setting and the libraries to the new server."),
        "Prefer replacing each call with the built-in function shown, then drop the declaration. Firebird 4+ "
        "includes misc/upgrade/v4.0/udf_replace.sql, which redeclares the common ib_udf/fbudf functions as PSQL "
        "functions or as UDRs from the udf_compat library (run it on the restored database). As a stopgap, set "
        "UdfAccess = Restrict UDF and copy the libraries to the new server.",
        items, ["https://github.com/FirebirdSQL/firebird/blob/v4.0.5/src/misc/upgrade/v4.0/udf_replace.sql"])]


@check
def blob_filters(ctx: Ctx) -> list[dict]:
    rows = ctx.rows("SELECT TRIM(RDB$FUNCTION_NAME) AS name, TRIM(RDB$MODULE_NAME) AS module, "
                    "TRIM(RDB$ENTRYPOINT) AS entrypoint FROM RDB$FILTERS WHERE COALESCE(RDB$SYSTEM_FLAG, 0) = 0")
    if not rows:
        return []
    return [finding(
        "blob_filters", CAT_FEATURES, WARNING, "BLOB filters (external libraries)",
        "BLOB filters are loaded from native libraries on the server, like legacy UDFs.",
        "Build or install the filter libraries for the new server's platform and version, and allow them "
        "in firebird.conf (UdfAccess); test reading the filtered BLOBs after the restore.",
        [obj("BLOB filter", r["name"], detail=f"{r['module']}!{r['entrypoint']}") for r in rows])]


@check
def external_tables(ctx: Ctx) -> list[dict]:
    rows = ctx.rows("SELECT TRIM(RDB$RELATION_NAME) AS name, TRIM(RDB$EXTERNAL_FILE) AS f FROM RDB$RELATIONS "
                    "WHERE RDB$EXTERNAL_FILE IS NOT NULL AND COALESCE(RDB$SYSTEM_FLAG, 0) = 0")
    if not rows:
        return []
    return [finding(
        "external_tables", CAT_CONFIG, WARNING, "External tables",
        "External tables read flat files on the server. Access is controlled by ExternalFileAccess in "
        "firebird.conf, whose default is None (checked on Firebird 4, 5 and 6). A fresh server installation "
        "therefore refuses them until it is configured.",
        "Copy the ExternalFileAccess setting from the old server (for example Restrict /path/to/dir), copy the "
        "files, and check the paths below on the new host. gbak backs up the table definitions, not the files.",
        [obj("table", r["name"], link=("tables", r["name"]), detail=r["f"]) for r in rows])]


@check
def dialect(ctx: Ctx) -> list[dict]:
    if ctx.facts.get("dialect") != 1:
        return []
    return [finding(
        "dialect1", CAT_TYPES, WARNING, "SQL dialect 1 database",
        "This database uses SQL dialect 1, the InterBase 5 compatibility mode (DATE holds a timestamp, double "
        "quotes delimit strings, large exact numerics become DOUBLE PRECISION). Newer servers still open dialect 1 "
        "databases, but new features and data types target dialect 3, and drivers increasingly assume it.",
        "Upgrade the server version first and keep the dialect. Plan a separate move to dialect 3 later "
        "(extract the metadata, adjust types and quoting, recreate the database in dialect 3 and copy the data).",
        [])]


@check
def authentication(ctx: Ctx) -> list[dict]:
    if not ctx.crosses("4.0"):
        return []
    out = []
    users = ctx.try_rows("SELECT TRIM(SEC$USER_NAME) AS name, TRIM(SEC$PLUGIN) AS plugin FROM SEC$USERS")
    legacy = [u for u in users or [] if (u["plugin"] or "").lower().startswith("legacy")]
    if legacy:
        out.append(finding(
            "legacy_auth_users", CAT_SECURITY, WARNING, "Users managed by Legacy_UserManager",
            "These users have passwords stored with the legacy (pre-Firebird 3) hash. Firebird 4 and later "
            "default to AuthServer = Srp256 and UserManager = Srp (checked on the test servers), so these users "
            "cannot log in on a new server with default settings.",
            "Recreate the users with the Srp plugin (CREATE USER name PASSWORD '...' USING PLUGIN Srp) on the new "
            "server and give them new passwords, or, only as a transition, add Legacy_Auth to AuthServer and "
            "Legacy_UserManager to UserManager in firebird.conf.",
            [obj("user", u["name"], link=("users", u["name"]), detail=u["plugin"]) for u in legacy]))
    out.append(finding(
        "srp256", CAT_SECURITY, INFO, "Authentication defaults and the security database",
        "Users live in the server's security database (security3.fdb, security4.fdb, security5.fdb, "
        "security6.fdb), not in your database, so a backup and restore does not carry them. Firebird 4 and later "
        "accept only Srp256 by default; client libraries older than Firebird 3.0.4 do not support it."
        + (f" This server has {len(users)} user(s) in its security database." if users else ""),
        "Before the switch, recreate the users and their passwords on the new server (or migrate the security "
        "database with gbak as described in the release notes), and upgrade fbclient on the application hosts.",
        []))
    return out


@check
def time_zones(ctx: Ctx) -> list[dict]:
    if not ctx.crosses("4.0"):
        return []
    items = []
    for s in _sources(ctx):
        hits = sorted({w.upper for w in K.words(s["text"]) if w.upper in ("CURRENT_TIMESTAMP", "CURRENT_TIME")})
        if hits:
            items.append(obj(s["kind"], s["name"], parent=s["parent"], link=s["link"], detail=", ".join(hits),
                             fix="Use " + " / ".join("LOCALTIMESTAMP" if h == "CURRENT_TIMESTAMP" else "LOCALTIME"
                                                     for h in hits)))
    out = [finding(
        "time_zone_types", CAT_TYPES, INFO, "New data types reach the clients",
        "From Firebird 4, CURRENT_TIMESTAMP and CURRENT_TIME return TIMESTAMP WITH TIME ZONE and TIME WITH TIME "
        "ZONE (checked on the test servers), and the new INT128 and DECFLOAT types can appear in results. "
        "Drivers that do not know these types fail with 'Data type unknown' or similar errors.",
        "Upgrade the drivers and fbclient, or set DataTypeCompatibility = 3.0 in firebird.conf or databases.conf "
        "(or SET BIND OF TIMESTAMP WITH TIME ZONE TO LEGACY per connection) so the server sends Firebird 3 types.",
        [], [RELEASE_NOTES["4.0"]])]
    if items:
        out.append(finding(
            "current_timestamp", CAT_TYPES, WARNING, "CURRENT_TIMESTAMP / CURRENT_TIME in stored code",
            "These objects use CURRENT_TIMESTAMP or CURRENT_TIME, which return values WITH TIME ZONE from Firebird 4. "
            "Assignments to TIMESTAMP columns still work (the value is converted to local time), but views and "
            "computed columns change their result type when recompiled, and comparisons may convert values.",
            "Where a local time is meant, use LOCALTIMESTAMP / LOCALTIME (available since Firebird 3.0.4, so the "
            "change can be made before the upgrade).",
            items))
    return out


@check
def read_consistency(ctx: Ctx) -> list[dict]:
    if not ctx.crosses("4.0"):
        return []
    return [finding(
        "read_consistency", CAT_TYPES, INFO, "READ COMMITTED uses read consistency",
        "Firebird 4 and later default to ReadConsistency = 1 (checked on the test servers): READ COMMITTED "
        "transactions run as READ COMMITTED READ CONSISTENCY and the RECORD_VERSION / NO_RECORD_VERSION options "
        "are ignored. A statement sees one consistent snapshot, and an update conflict may restart the statement.",
        "Test the application's concurrent workloads. ReadConsistency = 0 in firebird.conf restores the old "
        "behaviour while you test.",
        [], [RELEASE_NOTES["4.0"]])]


@check
def sequences_start(ctx: Ctx) -> list[dict]:
    if not ctx.crosses("4.0") or not ctx.facts["counts"].get("sequences"):
        return []
    return [finding(
        "sequence_start", CAT_TYPES, INFO, "CREATE SEQUENCE ... START WITH counts differently",
        "In Firebird 3, the first NEXT VALUE FOR of a sequence created with START WITH 10 returns 11; from "
        "Firebird 4 it returns 10 (checked on the test servers). Restored sequences keep their current values, "
        "but deployment scripts that create sequences produce values one step lower than before.",
        "Review scripts that create sequences with START WITH and expect a particular first value.",
        [])]


@check
def system_tables(ctx: Ctx) -> list[dict]:
    rows = ctx.rows(
        "SELECT DISTINCT TRIM(d.RDB$DEPENDENT_NAME) AS name, d.RDB$DEPENDENT_TYPE AS dtype, "
        "TRIM(d.RDB$DEPENDED_ON_NAME) AS rel FROM RDB$DEPENDENCIES d "
        "WHERE d.RDB$DEPENDED_ON_TYPE = 0 AND (d.RDB$DEPENDED_ON_NAME STARTING WITH 'RDB$' "
        "OR d.RDB$DEPENDED_ON_NAME STARTING WITH 'MON$' OR d.RDB$DEPENDED_ON_NAME STARTING WITH 'SEC$') "
        "AND d.RDB$DEPENDENT_NAME NOT STARTING WITH 'RDB$' AND d.RDB$DEPENDENT_NAME NOT STARTING WITH 'MON$' "
        "AND d.RDB$DEPENDENT_NAME NOT STARTING WITH 'SEC$' ORDER BY 1, 3")
    if not rows:
        return []
    kinds = {1: ("view", "views"), 2: ("trigger", "triggers"), 5: ("procedure", "procedures"),
             15: ("function", "functions"), 18: ("package", "packages"), 19: ("package", "packages")}
    by: dict[tuple, list[str]] = {}
    for r in rows:
        if r["dtype"] in (3, 4):  # computed column / validation: the name is a field, attach to nothing
            continue
        key = (r["name"], r["dtype"])
        by.setdefault(key, []).append(r["rel"])
    items = []
    for (name, dtype), rels in by.items():
        kind, otype = kinds.get(dtype, ("object", None))
        items.append(obj(kind, name, link=(otype, name) if otype else None, detail=", ".join(sorted(set(rels)))))
    if not items:
        return []
    fb6 = ctx.crosses("6.0")
    return [finding(
        "system_tables", CAT_SCHEMAS if fb6 else CAT_FEATURES, WARNING if fb6 else INFO,
        "Code that reads system tables",
        "These objects query RDB$, MON$ or SEC$ tables. Comparing the system tables of Firebird 3.0, 4.0, 5.0 and "
        "the 6.0 snapshot found no removed tables or columns, but each version adds columns (so SELECT * returns "
        "more of them) and new rows (new system packages, functions and keywords)."
        + (" From Firebird 4, object names in the system tables are VARCHAR(63) CHARACTER SET UTF8 instead of "
           "VARCHAR(31) UNICODE_FSS: code with fixed 31-character buffers or variables must be widened."
           if ctx.crosses("4.0") else "")
        + (" In Firebird 6 every object belongs to a schema: system tables carry RDB$SCHEMA_NAME, an object name "
           "is unique only within its schema, and queries that join system tables by name alone can return "
           "duplicates once a second schema exists." if fb6 else ""),
        "List the columns explicitly instead of SELECT *; in Firebird 6, also match RDB$SCHEMA_NAME when "
        "joining system tables by object name. Re-test these objects on the trial restore.",
        items)]


@check
def unicode_fss(ctx: Ctx) -> list[dict]:
    rows = ctx.rows(
        "SELECT TRIM(rf.RDB$RELATION_NAME) AS parent, TRIM(rf.RDB$FIELD_NAME) AS name FROM RDB$RELATION_FIELDS rf "
        "JOIN RDB$FIELDS f ON f.RDB$FIELD_NAME = rf.RDB$FIELD_SOURCE "
        "JOIN RDB$RELATIONS r ON r.RDB$RELATION_NAME = rf.RDB$RELATION_NAME "
        "WHERE COALESCE(r.RDB$SYSTEM_FLAG, 0) = 0 AND f.RDB$CHARACTER_SET_ID = 3 ORDER BY 1, 2")
    if not rows:
        return []
    return [finding(
        "unicode_fss", CAT_TYPES, INFO, "UNICODE_FSS columns",
        "UNICODE_FSS is the legacy Unicode character set. Firebird 3 stores metadata names in it (VARCHAR(31)); "
        "Firebird 4 moved the system tables to UTF8 with names up to 63 characters (checked on the test "
        "servers). User columns keep UNICODE_FSS, which accepts byte sequences that are not valid UTF-8, so "
        "such data can make a restore report malformed strings.",
        "Plan a move to UTF8 for these columns. If the restore reports malformed strings, gbak's -fix_fss_data "
        "option (with the character set the data really uses) repairs them.",
        [obj("column", r["name"], parent=r["parent"], link=("tables", r["parent"]), detail=f"of {r['parent']}")
         for r in rows])]


@check
def database_triggers(ctx: Ctx) -> list[dict]:
    rows = ctx.rows("SELECT TRIM(RDB$TRIGGER_NAME) AS name, RDB$TRIGGER_TYPE AS ttype FROM RDB$TRIGGERS "
                    "WHERE COALESCE(RDB$SYSTEM_FLAG, 0) = 0 AND RDB$RELATION_NAME IS NULL "
                    "AND COALESCE(RDB$TRIGGER_INACTIVE, 0) = 0")
    if not rows:
        return []
    from ..metadata.catalog import decode_trigger_type

    return [finding(
        "database_triggers", CAT_FEATURES, INFO, "Database and DDL triggers",
        "Active database-level triggers run on the new server too. An ON CONNECT trigger that fails there (for "
        "example because it calls a UDF that is not allowed, or uses a reserved word) blocks every login except "
        "with isql -nodbtriggers.",
        "Test a connection as a normal user right after the restore. Keep an isql -nodbtriggers session ready "
        "as SYSDBA to deactivate a failing trigger.",
        [obj("trigger", r["name"], link=("triggers", r["name"]), detail=decode_trigger_type(r["ttype"])[1])
         for r in rows])]


@check
def sql_security(ctx: Ctx) -> list[dict]:
    if not ctx.crosses("4.0"):
        return []
    c = ctx.facts["counts"]
    if not (c.get("procedures") or c.get("functions") or c.get("triggers") or c.get("packages")):
        return []
    return [finding(
        "sql_security", CAT_SECURITY, INFO, "SQL SECURITY for routines and triggers",
        "Firebird 4 adds SQL SECURITY DEFINER / INVOKER to tables, procedures, functions, packages and triggers. "
        "Restored objects get no SQL SECURITY clause and keep the privilege rules they had: privileges granted "
        "to a procedure, trigger or function are still honoured.",
        "No change is needed. Check grants on the trial restore; consider SQL SECURITY DEFINER where you now "
        "grant many privileges to routines.",
        [], [RELEASE_NOTES["4.0"]])]


@check
def invalid_modules(ctx: Ctx) -> list[dict]:
    rows = ctx.rows(
        "SELECT 'procedure' AS kind, TRIM(RDB$PROCEDURE_NAME) AS name, TRIM(RDB$PACKAGE_NAME) AS parent "
        "FROM RDB$PROCEDURES WHERE COALESCE(RDB$SYSTEM_FLAG, 0) = 0 AND RDB$VALID_BLR = 0 "
        "UNION ALL SELECT 'function', TRIM(RDB$FUNCTION_NAME), TRIM(RDB$PACKAGE_NAME) FROM RDB$FUNCTIONS "
        "WHERE COALESCE(RDB$SYSTEM_FLAG, 0) = 0 AND RDB$VALID_BLR = 0 "
        "UNION ALL SELECT 'trigger', TRIM(RDB$TRIGGER_NAME), TRIM(RDB$RELATION_NAME) FROM RDB$TRIGGERS "
        "WHERE COALESCE(RDB$SYSTEM_FLAG, 0) = 0 AND RDB$VALID_BLR = 0")
    if not rows:
        return []
    links = {"procedure": "procedures", "function": "functions", "trigger": "triggers"}
    for r in rows:
        r["kind"] = r["kind"].strip()
    return [finding(
        "invalid_modules", CAT_FEATURES, WARNING, "Invalid PSQL modules",
        "These modules are marked invalid (RDB$VALID_BLR = 0), usually after a table or domain they use was "
        "changed. They are carried over as they are and fail on the new server as they do now.",
        "Recompile them on the old server first (open each one and save it, or CREATE OR ALTER it) so problems "
        "are not mixed up with upgrade problems.",
        [obj(r["kind"], r["name"], parent=r["parent"] if r["kind"] != "trigger" else None,
             link=("packages", r["parent"]) if r["parent"] and r["kind"] != "trigger" else (links[r["kind"]], r["name"]),
             detail=f"on {r['parent']}" if r["kind"] == "trigger" and r["parent"] else None) for r in rows])]


# ============================================================== storage / ODS
@check
def ods_change(ctx: Ctx) -> list[dict]:
    src, tgt = ctx.facts["ods"], "{}.{}".format(*K.ODS[ctx.target])
    direct = ctx.source == "4.0" and ctx.target == "5.0"
    return [finding(
        "ods", CAT_STORAGE, INFO, f"On-disk structure changes from ODS {src} to ODS {tgt}",
        f"Firebird {ctx.target} writes ODS {tgt} (ODS per version, checked on the test servers: Firebird 3 = 12.0, "
        "4 = 13.0, 5 = 13.1, 6 = 14.0). A major ODS change means the new server cannot open the old file: the "
        "database must be moved with a gbak backup made by the old server and a restore made by the new one. "
        "Never use nbackup or a file copy across major versions: they copy pages in the old format."
        + (" Firebird 5 can open a Firebird 4 (ODS 13.0) database directly and upgrades it to ODS 13.1 on the "
           "first read-write attachment; that is one-way, so a backup and restore is still the safer path and "
           "keeps the old file usable for a rollback." if direct else ""),
        "Follow the migration plan below: gbak -b on the old server, gbak -c on the new server. A backup made by "
        "the newer server cannot be restored on the older one.",
        [], [RELEASE_NOTES.get(ctx.target, "")])]


@check
def page_size(ctx: Ctx) -> list[dict]:
    ps = ctx.facts.get("page_size") or 0
    if ctx.crosses("6.0") and ps < 8192:
        return [finding(
            "page_size_fb6", CAT_STORAGE, WARNING, f"Page size {ps} is below the Firebird 6 minimum",
            "Firebird 6 supports page sizes from 8192 to 32768 bytes (MIN_PAGE_SIZE in its ods.h; the 6 snapshot "
            "creates an 8192-byte database when 4096 is requested). The restored database will use larger pages, "
            "which changes the memory used by the page cache (DefaultDbCachePages counts pages).",
            "Restore with gbak -c -page_size 8192 (or 16384), and size the page cache in pages accordingly.",
            [])]
    if ps and ps < 8192:
        return [finding(
            "page_size_small", CAT_STORAGE, INFO, f"Small page size ({ps} bytes)",
            "The default page size has been 8192 since Firebird 3; larger pages allow longer index keys and fewer "
            "index levels for big tables.",
            "Consider restoring with gbak -c -page_size 8192 or 16384 while you are migrating anyway.", [])]
    return []


@check
def files_and_shadows(ctx: Ctx) -> list[dict]:
    rows = ctx.rows("SELECT TRIM(RDB$FILE_NAME) AS f, RDB$SHADOW_NUMBER AS shadow FROM RDB$FILES")
    if not rows:
        return []
    shadows = [r for r in rows if r["shadow"]]
    secondary = [r for r in rows if not r["shadow"]]
    out = []
    if secondary:
        out.append(finding(
            "secondary_files", CAT_STORAGE, INFO, "Multi-file database",
            "The database has secondary files. gbak restores into the file names you give it; without them the "
            "new database is a single file.",
            "Decide whether to keep the multi-file layout (pass several target files with their sizes to gbak -c) "
            "or to use one file, which is simpler on today's file systems.",
            [obj("file", r["f"]) for r in secondary]))
    if shadows:
        out.append(finding(
            "shadows", CAT_STORAGE, INFO, "Shadows",
            "The database has shadow files. A restore creates them again at the stored paths unless gbak -c "
            "-no_shadow is used.",
            "Check that the shadow paths exist on the new server, or restore with -no_shadow and recreate them.",
            [obj("shadow file", r["f"], detail=f"shadow {r['shadow']}") for r in shadows]))
    return out


@check
def encryption(ctx: Ctx) -> list[dict]:
    if not ctx.facts.get("encrypted"):
        return []
    return [finding(
        "encrypted", CAT_SECURITY, WARNING, "Encrypted database",
        "The database is encrypted with a crypt plugin. The new server needs the same plugin built for its "
        "version, and the key holder must be available to gbak too.",
        "Install and configure the crypt plugin and key holder on the new server before the restore; from "
        "Firebird 4, gbak can also encrypt the backup file itself (-crypt, -keyholder).",
        [])]


# ===================================================================== FB6
@check
def fb6_schemas(ctx: Ctx) -> list[dict]:
    if not ctx.crosses("6.0"):
        return []
    clash = [o for o in _named_objects(ctx) if o["name"] in ("PUBLIC", "SYSTEM", "PLG$PROFILER")]
    out = [finding(
        "fb6_schemas", CAT_SCHEMAS, INFO, "Objects move into the PUBLIC schema",
        "Firebird 6 adds SQL schemas. A restored database puts every user object in the PUBLIC schema and the "
        "system tables in SYSTEM. Unqualified names resolve through the search path (PUBLIC, SYSTEM by default), "
        "so existing SQL keeps working, but tools and queries that read system tables must now take "
        "RDB$SCHEMA_NAME into account. Firebird 6 is still a snapshot: re-check before the final release.",
        "Nothing to change for the move itself. Use SET SEARCH_PATH when you introduce more schemas, and qualify "
        "names (PUBLIC.ORDERS) in new code that must not depend on the search path.",
        [], [RELEASE_NOTES["6.0"]])]
    if clash:
        out.append(finding(
            "fb6_schema_names", CAT_SCHEMAS, WARNING, "Objects named like a Firebird 6 schema",
            "These objects are named PUBLIC, SYSTEM or PLG$PROFILER, which are schema names in Firebird 6. A "
            "qualified reference such as SYSTEM.X or PUBLIC.X is read as a schema, not as your object.",
            "Rename them (or recreate them under another name) before moving to Firebird 6.",
            [obj(o["kind"], o["name"], parent=o["parent"], link=o["link"]) for o in clash]))
    return out
