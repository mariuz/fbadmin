"""``fbadmin extract`` (DDL) and ``fbadmin diff`` (schema migration script, optional data sync)."""

from __future__ import annotations

import argparse
import re
import sys
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from .common import DIFFERENCES, FAILED, USAGE, CliError, Out, Target, add_db_arg, resolve

TYPES = ("collations", "domains", "udfs", "sequences", "exceptions", "roles", "tables", "procedures", "functions",
         "packages", "views", "indices", "triggers")


def _close(conn) -> None:
    for fn in (conn.commit, conn.close):
        try:
            fn()
        except Exception:  # noqa: BLE001
            pass


def _write(text: str, output: str | None) -> None:
    if output and output != "-":
        p = Path(output).expanduser()
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, "utf-8")
    else:
        sys.stdout.write(text)
        sys.stdout.flush()


# ===================================================================== extract
def _parse_types(value: str | None) -> set[str] | None:
    if not value:
        return None
    types = {x.strip().lower() for x in value.split(",") if x.strip()}
    aliases = {"generators": "sequences", "indexes": "indices", "external_functions": "udfs"}
    types = {aliases.get(x, x) for x in types}
    bad = types - set(TYPES)
    if bad:
        raise CliError(f"Unknown object type(s): {', '.join(sorted(bad))}. Known: {', '.join(TYPES)}", USAGE)
    return types


def filter_snapshot(snap, types: set[str]) -> None:
    """Keep only objects of ``types`` (and the grants and comments that belong to them)."""
    from ..schema.diff import _comment_object

    for t in TYPES:
        if t not in types:
            setattr(snap, t, {})
    if "views" not in types:
        snap.view_order = []
    kept = set()
    for t in TYPES:
        kept |= set(getattr(snap, t))
    snap.grants = [g for g in snap.grants if g["object"] in kept or g.get("otype") == 13 and "roles" in types]
    snap.comments = {k: v for k, v in snap.comments.items() if _comment_object(k) in kept}


@contextmanager
def _schema_ctx(conn, schema: str | None):
    """Firebird 6: run inside one schema (catalog scope and search path)."""
    if not schema:
        yield
        return
    from ..metadata.types import quote_ident
    from ..schema_ctx import current_schema

    cur = conn.cursor()
    try:
        cur.execute("SELECT RDB$GET_CONTEXT('SYSTEM', 'SEARCH_PATH') FROM RDB$DATABASE")
        original = cur.fetchone()[0] or "PUBLIC, SYSTEM"
    except Exception as exc:  # noqa: BLE001
        raise CliError("--schema needs Firebird 6 or later", USAGE) from exc
    token = current_schema.set(schema)
    cur.execute(f"SET SEARCH_PATH TO {quote_ident(schema)}, SYSTEM")
    try:
        yield
    finally:
        current_schema.reset(token)
        cur.execute(f"SET SEARCH_PATH TO {original}")


def _snapshots(conn, schema: str | None) -> Iterator[tuple[str | None, Any]]:
    """(schema, Snapshot) for the requested schema, or every user schema on Firebird 6, or the database."""
    from ..schema.extract import user_schemas
    from ..schema.model import Snapshot

    schemas = [schema] if schema else user_schemas(conn)
    if not schemas or schemas == ["PUBLIC"]:  # Firebird 3-5, or Firebird 6 with only the PUBLIC schema
        yield None, Snapshot(conn)
        return
    for n, s in enumerate(schemas):
        with _schema_ctx(conn, s):
            yield s, Snapshot(conn, include_global=n == 0)


def _safe_name(name: str) -> str:
    return re.sub(r'[\\/:*?"<>|\x00-\x1f]+', "_", name).strip(" .") or "_"


def split_objects(conn, directory: Path, schema: str | None, types: set[str] | None) -> list[str]:
    """One file per object: DIR/[schema/]type/NAME.sql (the DDL the schema diff compares)."""
    written = []
    directory.mkdir(parents=True, exist_ok=True)
    for s, snap in _snapshots(conn, schema):
        if types:
            filter_snapshot(snap, types)
        base = directory / _safe_name(s) if s else directory
        for (kind, name), ddl in sorted(snap.objects().items()):
            p = base / kind / f"{_safe_name(name)}.sql"
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(ddl, "utf-8")
            written.append(str(p))
    return written


def cmd_extract(args: argparse.Namespace, out: Out) -> int:
    from ..schema.extract import build_sections, extract_database, make_header, render

    t = resolve(args, args.db)
    types = _parse_types(args.types)
    conn = t.connect()
    try:
        if args.split:
            files = split_objects(conn, Path(args.split).expanduser(), args.schema, types)
            out.result({"database": t.label, "directory": args.split, "files": len(files)},
                       f"Wrote {len(files)} file(s) to {args.split}")
            return 0
        opts = {"set_term": not args.no_set_term, "grants": not args.no_grants, "comments": not args.no_comments,
                "sequence_values": not args.no_sequence_values}
        if not types and not args.schema:
            sql = extract_database(conn, header=not args.no_header, path=t.path, **opts)
        else:
            parts = []
            for n, (_s, snap) in enumerate(_snapshots(conn, args.schema)):
                if types:
                    filter_snapshot(snap, types)
                head = None
                if n == 0 and not args.no_header:
                    ps = snap.cat.one("SELECT MON$PAGE_SIZE AS ps FROM MON$DATABASE") or {}
                    head = make_header(snap, t.path, ps.get("ps"))
                if _s and not args.schema:  # several Firebird 6 schemas: one block each, as extract_database
                    from ..metadata.types import quote_ident
                    from ..schema.extract import user_schemas

                    if n == 0:
                        head = (head or "") + "".join(f"CREATE SCHEMA {quote_ident(x)};\n"
                                                      for x in user_schemas(conn) if x != "PUBLIC")
                    head = (head or "") + f"/* ===== Schema {_s} ===== */\nSET SEARCH_PATH TO {quote_ident(_s)}, SYSTEM;\n"
                secs = build_sections(snap, grants=opts["grants"], comments=opts["comments"],
                                      sequence_values=opts["sequence_values"])
                parts.append(render(secs, set_term=opts["set_term"], header=head))
            sql = "\n".join(parts)
    finally:
        _close(conn)
    if args.json:
        res: dict[str, Any] = {"database": t.label, "bytes": len(sql.encode())}
        if args.output and args.output != "-":
            _write(sql, args.output)
            res["file"] = args.output
        else:
            res["sql"] = sql
        out.result(res)
        return 0
    _write(sql, args.output)
    if args.output and args.output != "-":
        out.log(f"DDL of {t.label} written to {args.output} ({len(sql.encode())} bytes)")
    return 0


# ===================================================================== diff
def _is_script(spec: str) -> bool:
    if spec.startswith("@"):
        return False
    if spec.startswith("file:"):
        return True
    p = Path(spec).expanduser()
    return p.suffix.lower() in (".sql", ".ddl") and p.is_file()


def _script_path(spec: str) -> Path:
    p = Path(spec[5:] if spec.startswith("file:") else spec).expanduser()
    if not p.is_file():
        raise CliError(f"No such DDL file: {p}", USAGE)
    return p


@contextmanager
def _side_snapshot(spec: str, own: Target | None, other: Target | None, args: argparse.Namespace,
                   charset: str | None, out: Out):
    """Snapshot of a database, or of a DDL script applied to a temporary database on the other side's server."""
    from ..query import run_script
    from ..schema.model import Snapshot

    real = other
    if own is not None:
        conn = own.connect()
        try:
            yield Snapshot(conn), own.label
        finally:
            _close(conn)
        return
    path = _script_path(spec)
    if real is None:
        raise CliError("At least one side of the diff must be a database (the DDL script is applied to a "
                       "temporary database on its server)", USAGE)
    temp_dir = args.temp_dir
    if not temp_dir:
        m = re.match(r"^(.*)[/\\][^/\\]+$", real.path or "")
        if not m:
            raise CliError("Cannot choose a directory for the temporary database (the database is an alias): "
                           "use --temp-dir DIR (a directory on the Firebird server)", USAGE)
        temp_dir = m.group(1)
    srv = real.srv.model_copy(update={"charset": charset}) if charset else real.srv
    tmp_path = f"{temp_dir.rstrip('/')}/fbadmin_diff_{uuid.uuid4().hex[:10]}.fdb"
    tmp = Target(srv=srv, path=tmp_path, password=real.password, label=str(path))
    out.log(f"Applying {path} to a temporary database {tmp_path}")
    conn = tmp.connect(create=True)
    try:
        res = run_script(conn, path.read_text("utf-8"), autocommit=True, max_rows=1)
        if res["error"]:
            e = res["error"]
            raise CliError(f"The DDL script {path} failed at line {e['line']}: {e['message']}", FAILED)
        yield Snapshot(conn), path.name
    finally:
        try:
            conn.commit()
        except Exception:  # noqa: BLE001
            pass
        try:
            conn.drop_database()
        except Exception:  # noqa: BLE001
            pass


def _data_compare(src_t: Target, dst_t: Target, tables: list[str] | None, out: Out) -> tuple[dict, bytes]:
    from ..datacompare import engine

    src, dst = src_t.connect(), dst_t.connect()
    try:
        engine.snapshot_readonly(src)
        engine.snapshot_readonly(dst)
        st = engine.user_tables(engine.open_catalog(src))
        tt = engine.user_tables(engine.open_catalog(dst))
        src.commit()
        dst.commit()
        if tables:
            names = []
            for n in tables:
                name = n if n in st else n.upper() if n.upper() in st else None
                if name is None:
                    raise CliError(f"Table {n!r} is not in the source database", USAGE)
                names.append(name)
        else:
            names = sorted(set(st) & set(tt))
        run = engine.Run(source=("cli", "source"), target=("cli", "target"), source_name=src_t.label,
                         target_name=dst_t.label)
        try:
            res = engine.compare_tables(src, dst, out.detail, run, tables=[{"name": n} for n in names])
            script = b"".join(run.script_chunks()) if run.statement_count() else b""
        finally:
            run.cleanup()
    finally:
        _close(src)
        _close(dst)
    summary = {"statements": res["statements"], "totals": res["totals"], "skipped": res["skipped"],
               "tables": [{k: s[k] for k in ("name", "status", "error", "only_source", "only_target", "different",
                                              "identical")} for s in res["tables"]]}
    return summary, script


def cmd_diff(args: argparse.Namespace, out: Out) -> int:
    from ..metadata import Catalog
    from ..schema.diff import compare

    if args.no_schema and args.data is None:
        raise CliError("--no-schema needs --data (there would be nothing to compare)", USAGE)
    src_spec, dst_spec = args.source, args.target
    src_t = None if _is_script(src_spec) else resolve(args, src_spec)
    dst_t = None if _is_script(dst_spec) else resolve(args, dst_spec, prefix="target_")
    if src_t is None and dst_t is None:
        raise CliError("Both sides are DDL files: at least one side must be a database", USAGE)
    res: dict[str, Any] = {"source": src_t.label if src_t else src_spec, "target": dst_t.label if dst_t else dst_spec}
    differences = 0
    script = ""
    if not args.no_schema:
        real = src_t or dst_t
        charset = None
        if src_t is None or dst_t is None:  # the script side gets the default character set of the database side
            conn = real.connect()
            try:
                charset = Catalog(conn).default_charset
            finally:
                _close(conn)
        with _side_snapshot(src_spec, src_t, dst_t, args, charset, out) as (src, src_name), \
                _side_snapshot(dst_spec, dst_t, src_t, args, charset, out) as (tgt, tgt_name):
            header = f"/* Migration generated by fbadmin: makes {tgt_name} match {src_name} */\n"
            diff = compare(src, tgt, set_term=not args.no_set_term, header=header)
        differences += len(diff["items"])
        res["schema"] = {"differences": len(diff["items"]), "summary": diff["summary"], "warnings": diff["warnings"],
                         "items": [{k: i[k] for k in ("type", "name", "status")} for i in diff["items"]]}
        script = diff["script"] if diff["items"] or args.data is None else ""
        s = diff["summary"]
        out.log(f"Schema: {len(diff['items'])} difference(s) ({s['create']} to create, {s['alter']} to alter, "
                f"{s['drop']} to drop)")
        for i in diff["items"]:
            out.detail(f"  {i['status']:6} {i['type']} {i['name']}")
        for w in diff["warnings"]:
            out.log(f"Warning: {w}")
    if args.data is not None:
        if src_t is None or dst_t is None:
            raise CliError("--data compares rows of two databases, not a DDL file", USAGE)
        summary, data_script = _data_compare(src_t, dst_t, args.data or None, out)
        differences += summary["statements"]
        res["data"] = summary
        tot = summary["totals"]
        out.log(f"Data: {tot.get('only_source', 0)} row(s) to insert, {tot.get('different', 0)} to update, "
                f"{tot.get('only_target', 0)} to delete")
        for sk in summary["skipped"]:
            out.log(f"Data: {sk['name']} skipped - {sk['reason']}")
        errors = [x for x in summary["tables"] if x["status"] == "error"]
        for x in errors:
            out.log(f"Data: {x['name']} failed - {x['error']}")
        if data_script:
            script = (script + "\n" if script else "") + data_script.decode("utf-8")
        if errors:
            raise CliError(f"The data comparison failed for {len(errors)} table(s)", FAILED, res)
    res["differences"] = differences
    if not script:
        script = "/* No differences */\n"
    if args.json:
        if args.output and args.output != "-":
            _write(script, args.output)
            res["file"] = args.output
        else:
            res["script"] = script
        out.result(res)
    else:
        _write(script, args.output)
        if args.output and args.output != "-":
            out.log(f"Script written to {args.output}")
    if differences == 0:
        out.log("No differences")
    return DIFFERENCES if (args.exit_code and differences) else 0


# ===================================================================== parsers
def add_parsers(sub, common) -> None:
    p = sub.add_parser("extract", parents=common, help="extract the DDL of a database",
                       description="Write the full DDL of a database (like isql -x), in dependency order.")
    add_db_arg(p)
    p.add_argument("-o", "--output", metavar="FILE", help="write to FILE (default stdout)")
    p.add_argument("--split", metavar="DIR", help="write one file per object: DIR/[SCHEMA/]TYPE/NAME.sql")
    p.add_argument("--types", metavar="LIST", help="only these object types, comma separated: " + ", ".join(TYPES))
    p.add_argument("--schema", help="only this schema (Firebird 6)")
    p.add_argument("--no-grants", action="store_true", help="leave out GRANT statements")
    p.add_argument("--no-comments", action="store_true", help="leave out COMMENT ON statements")
    p.add_argument("--no-sequence-values", action="store_true", help="leave out ALTER SEQUENCE ... RESTART WITH")
    p.add_argument("--no-header", action="store_true", help="leave out the header comment and SET SQL DIALECT")
    p.add_argument("--no-set-term", action="store_true", help="no SET TERM around PSQL (for tools that split by ^)")
    p.set_defaults(func=cmd_extract)

    p = sub.add_parser("diff", parents=common, help="schema diff: migration script that makes TARGET match SOURCE",
                       description="Compare two schemas and print the migration script that makes TARGET match "
                                   "SOURCE. Either side may be a DDL file (.sql), applied to a temporary database "
                                   "on the other side's server.")
    p.add_argument("source", metavar="SOURCE", help="desired schema: a database or a .sql DDL file")
    p.add_argument("target", metavar="TARGET", help="database to update (or a .sql DDL file)")
    p.add_argument("-o", "--output", metavar="FILE", help="write the script to FILE (default stdout)")
    p.add_argument("--exit-code", action="store_true", help="exit with 1 when there are differences (CI drift check)")
    p.add_argument("--no-set-term", action="store_true", help="no SET TERM around PSQL")
    p.add_argument("--temp-dir", metavar="DIR",
                   help="directory on the server for the temporary database of a DDL file side")
    p.add_argument("--data", nargs="*", metavar="TABLE",
                   help="also compare the rows of these tables (all common tables if none given) and append the "
                        "data sync script")
    p.add_argument("--no-schema", action="store_true", help="with --data: compare only data")
    t = p.add_argument_group("target credentials (default: the same as the source)")
    t.add_argument("--target-user", help="user for the target")
    t.add_argument("--target-password", help="password for the target")
    t.add_argument("--target-password-env", metavar="NAME", help="environment variable with the target password")
    p.set_defaults(func=cmd_diff)
