"""Full database DDL extract, ordered by dependencies (like ``isql -x``).

Order: collations, domains, legacy UDFs, sequences, exceptions, roles, tables (columns plus primary and
unique keys; computed columns that use other objects start as placeholders), foreign keys, PSQL stubs
(procedures and functions with empty bodies, package headers), views in dependency order, the real
PSQL bodies (ALTER PROCEDURE/FUNCTION, package bodies), deferred computed columns, check constraints,
indices, triggers, grants and comments. Creating stubs first and the bodies later is the isql technique
that breaks dependency cycles between PSQL modules and views.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .. import __version__
from ..metadata import Catalog
from ..metadata.types import quote_ident
from ..schema_ctx import current_schema
from .model import Snapshot


@dataclass
class Section:
    title: str
    statements: list[tuple[str, bool]] = field(default_factory=list)  # (sql without terminator, is_psql)

    def add(self, sql: str | None, psql: bool = False) -> None:
        if sql:
            self.statements.append((sql.strip().rstrip(";").rstrip(), psql))


def build_sections(snap: Snapshot, *, grants: bool = True, comments: bool = True,
                   sequence_values: bool = True) -> list[Section]:
    q = quote_ident
    secs: list[Section] = []

    def sec(title: str) -> Section:
        s = Section(title)
        secs.append(s)
        return s

    s = sec("Collations")
    for o in snap.collations.values():
        s.add(o["ddl"])
    s = sec("Domains")
    for o in snap.domains.values():
        s.add(o["ddl"])
    s = sec("External functions (UDF)")
    for o in snap.udfs.values():
        s.add(o["ddl"])
    s = sec("Sequences")
    for o in snap.sequences.values():
        s.add(o["ddl"])
        if sequence_values:
            s.add(o["value_ddl"])
    s = sec("Exceptions")
    for o in snap.exceptions.values():
        s.add(o["ddl"])
    s = sec("Roles")
    for o in snap.roles.values():
        s.add(o["ddl"])
    s = sec("Tables")
    for t in snap.tables.values():
        s.add(t["create_placeholder"])
    s = sec("Foreign keys")
    for n, t in snap.tables.items():
        for c in t["fks"]:
            s.add(f"ALTER TABLE {q(n)} ADD {c['ddl']}")
    s = sec("Stored functions and procedures (headers with empty bodies)")
    for f in snap.functions.values():
        s.add(f["stub"], True)
    for p in snap.procedures.values():
        s.add(p["stub"], True)
    s = sec("Package headers")
    for p in snap.packages.values():
        s.add(p["header"], True)
    s = sec("Views")
    for n in snap.view_order:
        s.add(snap.views[n]["ddl"])
    s = sec("Stored function and procedure bodies")
    for f in snap.functions.values():
        if not f["external"]:
            s.add(f["alter"], True)
    for p in snap.procedures.values():
        if not p["external"]:
            s.add(p["alter"], True)
    s = sec("Package bodies")
    for p in snap.packages.values():
        s.add(p["body"], True)
    s = sec("Computed columns using other objects")
    for t in snap.tables.values():
        for d in t["deferred"]:
            s.add(d)
    s = sec("Check constraints")
    for n, t in snap.tables.items():
        for c in t["checks"]:
            s.add(f"ALTER TABLE {q(n)} ADD {c['ddl']}")
    s = sec("Indices")
    for i in snap.indices.values():
        for stmt in i["ddl"]:
            s.add(stmt)
    s = sec("Triggers")
    for t in sorted(snap.triggers.items(), key=lambda kv: (kv[1]["relation"] or "", kv[0])):
        s.add(t[1]["ddl"], True)
    if grants:
        s = sec("Grants")
        for g in snap.grants:
            s.add(g["ddl"])
    if comments:
        s = sec("Comments")
        for _k, stmt in sorted(snap.comments.items()):
            s.add(stmt)
    return [x for x in secs if x.statements]


def render(sections: list[Section], set_term: bool = True, header: str | None = None) -> str:
    out: list[str] = []
    if header:
        out.append(header.rstrip() + "\n")
    in_term = False
    for sec in sections:
        use_term = set_term and any(psql for _, psql in sec.statements)
        if use_term and not in_term:
            out.append("SET TERM ^ ;\n")
        elif in_term and not use_term:
            out.append("SET TERM ; ^\n")
        in_term = use_term
        out.append(f"/* {sec.title} */")
        term = "^" if use_term else ";"
        for sql, _psql in sec.statements:
            out.append(f"{sql}{term}\n" if "\n" in sql else f"{sql}{term}")
        if not out[-1].endswith("\n"):
            out.append("")
    if in_term:
        out.append("SET TERM ; ^")
    return "\n".join(out).rstrip() + "\n"


def make_header(snap: Snapshot, path: str | None = None, page_size: int | None = None) -> str:
    # comments go before SET SQL DIALECT so they never end up in front of a SET TERM command
    lines = [f"/* DDL extracted by fbadmin {__version__}, Firebird {snap.cat.engine_version} */"]
    create = f"CREATE DATABASE '{path or 'database.fdb'}'"
    if page_size:
        create += f" PAGE_SIZE {page_size}"
    create += f" DEFAULT CHARACTER SET {snap.charset}"
    lines.append(f"/* {create}; */")
    lines.append("SET SQL DIALECT 3;")
    return "\n".join(lines) + "\n"


def user_schemas(conn) -> list[str]:
    """Firebird 6: the schemas holding user objects, PUBLIC first; [] on older servers."""
    cat = Catalog(conn, schema="")
    if not cat.schemas_supported:
        return []
    names = [r["name"] for r in cat.list_schemas()
             if not r["system_flag"] and r["name"] != "SYSTEM" and not r["name"].startswith("PLG$")]
    return sorted(names, key=lambda n: (n != "PUBLIC", n))


def extract_database(conn, *, set_term: bool = True, header: bool = True, grants: bool = True,
                     comments: bool = True, sequence_values: bool = True, path: str | None = None) -> str:
    schemas = user_schemas(conn)
    if schemas and schemas != ["PUBLIC"]:
        return _extract_schemas(conn, schemas, set_term=set_term, header=header, grants=grants,
                                comments=comments, sequence_values=sequence_values, path=path)
    snap = Snapshot(conn)
    head = None
    if header:
        ps = snap.cat.one("SELECT MON$PAGE_SIZE AS ps FROM MON$DATABASE") or {}
        head = make_header(snap, path, ps.get("ps"))
    sections = build_sections(snap, grants=grants, comments=comments, sequence_values=sequence_values)
    return render(sections, set_term=set_term, header=head)


def _extract_schemas(conn, schemas: list[str], *, set_term: bool, header: bool, grants: bool, comments: bool,
                     sequence_values: bool, path: str | None) -> str:
    """Firebird 6 database with several schemas: one block per schema, each run under its search path
    (so the unqualified names in the DDL and in PSQL bodies resolve as they did in the source)."""
    out: list[str] = []
    cur = conn.cursor()
    cur.execute("SELECT RDB$GET_CONTEXT('SYSTEM', 'SEARCH_PATH') FROM RDB$DATABASE")
    original = cur.fetchone()[0] or "PUBLIC, SYSTEM"
    for n, schema in enumerate(schemas):
        token = current_schema.set(schema)
        try:
            # the server resolves names (e.g. GEN_ID of a sequence) through the search path, too
            cur.execute(f"SET SEARCH_PATH TO {quote_ident(schema)}, SYSTEM")
            snap = Snapshot(conn, include_global=n == 0)
        finally:
            current_schema.reset(token)
            cur.execute(f"SET SEARCH_PATH TO {original}")
        if n == 0 and header:
            ps = snap.cat.one("SELECT MON$PAGE_SIZE AS ps FROM MON$DATABASE") or {}
            out.append(make_header(snap, path, ps.get("ps")).rstrip())
            out.append("".join(f"CREATE SCHEMA {quote_ident(s)};\n" for s in schemas if s != "PUBLIC").rstrip())
        elif n == 0:
            out.append("".join(f"CREATE SCHEMA {quote_ident(s)};\n" for s in schemas if s != "PUBLIC").rstrip())
        sections = build_sections(snap, grants=grants, comments=comments, sequence_values=sequence_values)
        out.append(f"/* ===== Schema {schema} ===== */\nSET SEARCH_PATH TO {quote_ident(schema)}, SYSTEM;")
        out.append(render(sections, set_term=set_term).rstrip())
    return "\n\n".join(x for x in out if x) + "\n"
