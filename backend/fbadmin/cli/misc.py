"""``fbadmin sql`` (run a script), ``fbadmin check`` (health check) and ``fbadmin servers`` (registrations)."""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path
from typing import Any

from .common import DIFFERENCES, FAILED, USAGE, CliError, Out, add_db_arg, clean_error, resolve


# ======================================================================== sql
def _format_rows(item: dict[str, Any]) -> str:
    cols = [c["name"] for c in item["columns"]]
    lines = ["\t".join(cols)]
    for r in item["rows"]:
        lines.append("\t".join("" if v is None else str(v) for v in r))
    if item.get("truncated"):
        lines.append(f"... (more than {item['row_count']} rows; raise --max-rows)")
    return "\n".join(lines)


def cmd_sql(args: argparse.Namespace, out: Out) -> int:
    from ..query import run_script

    if args.command and args.file:
        raise CliError("Use either -c SQL or -f FILE", USAGE)
    if args.command:
        scripts = [("-c", args.command)]
    elif args.file:
        scripts = []
        for f in args.file:
            if f == "-":
                scripts.append(("stdin", sys.stdin.read()))
                continue
            p = Path(f).expanduser()
            if not p.is_file():
                raise CliError(f"No such script file: {p}", USAGE)
            scripts.append((str(p), p.read_text("utf-8-sig")))
    else:
        if sys.stdin.isatty():
            raise CliError("Give a script with -f FILE, -c SQL or on stdin", USAGE)
        scripts = [("stdin", sys.stdin.read())]
    t = resolve(args, args.db)
    if args.create:
        out.log(f"Creating {t.label}")
    conn = t.connect(create=args.create, page_size=args.page_size or 16384)
    started = time.time()
    results: list[dict[str, Any]] = []
    error = None
    try:
        for name, text in scripts:
            out.detail(f"Running {name}")
            res = run_script(conn, text, autocommit=not args.single_transaction, max_rows=args.max_rows)
            for item in res["results"]:
                item["script"] = name
                results.append(item)
                if item["kind"] == "select" and not args.json:
                    print(_format_rows(item), flush=True)
                elif item["kind"] != "error":
                    out.detail(f"line {item['line']}: {item['message']}")
            if res["error"]:
                e = res["error"]
                error = {"script": name, "line": e["line"], "message": clean_error(Exception(e["message"])),
                         "sql": e["sql"][:2000]}
                break
        if args.single_transaction:
            if error:
                conn.rollback()
                out.log("Rolled back: no change was committed")
            else:
                conn.commit()
    finally:
        try:
            conn.close()
        except Exception:  # noqa: BLE001
            pass
    data = {"database": t.label, "statements": len([r for r in results if r["kind"] != "error"]),
            "seconds": round(time.time() - started, 2),
            "results": [{k: r.get(k) for k in ("script", "line", "kind", "message", "columns", "rows", "row_count")
                         if r.get(k) is not None} for r in results]}
    if error:
        data["error_at"] = error
        raise CliError(f"{error['script']}, line {error['line']}: {error['message']}", FAILED, data)
    out.result(data, f"{data['statements']} statement(s) executed in {data['seconds']} s", to_stderr=True)
    return 0


# ====================================================================== check
def cmd_check(args: argparse.Namespace, out: Out) -> int:
    from ..maintenance import ops

    has_db = bool(args.db or args.database)
    try:
        t = resolve(args, args.db, need_db=has_db and not args.server_only)
        res: dict[str, Any] = {"target": t.label}
        if t.path and not args.server_only:
            conn = t.connect()
            try:
                cur = conn.cursor()
                cur.execute("SELECT RDB$GET_CONTEXT('SYSTEM', 'ENGINE_VERSION'), MON$ODS_MAJOR, MON$ODS_MINOR, "
                            "MON$PAGE_SIZE, MON$SQL_DIALECT, MON$READ_ONLY, "
                            "(SELECT TRIM(RDB$CHARACTER_SET_NAME) FROM RDB$DATABASE) FROM MON$DATABASE")
                ver, ods_major, ods_minor, page_size, dialect, read_only, charset = cur.fetchone()
                conn.commit()
            finally:
                conn.close()
            res.update(engine_version=ver, ods=f"{ods_major}.{ods_minor}", page_size=page_size, dialect=dialect,
                       read_only=bool(read_only), charset=charset)
            text = (f"OK {t.label}: Firebird {ver}, ODS {ods_major}.{ods_minor}, page size {page_size}, "
                    f"dialect {dialect}, charset {charset or 'NONE'}" + (", read-only" if read_only else ""))
        else:
            with t.service() as svc:
                version = svc.getServerVersion()
                major = ops.engine_major(svc)
            res.update(server_version=version, engine_major=major)
            text = f"OK {t.label}: {version}"
    except CliError as exc:
        if exc.code == USAGE:
            raise
        raise CliError(str(exc), DIFFERENCES) from exc
    except Exception as exc:  # noqa: BLE001
        raise CliError(clean_error(exc), DIFFERENCES) from exc
    out.result(res, text)
    return 0


# ==================================================================== servers
def cmd_servers(args: argparse.Namespace, out: Out) -> int:
    from . import registry

    regs = registry.describe(args)
    if not args.json:
        if not regs:
            out.log(f"No registered servers in {registry.data_dir(args)}")
        for s in regs:
            where = "embedded" if s["embedded"] else f"{s['host']}/{s['port']}"
            print(f"{s['name']}  [{s['id']}]  {where}  user {s['user']}  password: {s['password']}  ({s['source']})")
            for d in s["databases"]:
                print(f"    {d['name']}  [{d['id']}]  {d['path']}")
    out.result({"data_dir": str(registry.data_dir(args)), "servers": regs})
    return 0


# ==================================================================== parsers
def add_parsers(sub, common) -> None:
    p = sub.add_parser("sql", parents=common, help="run an SQL script (stops at the first error)",
                       description="Run SQL scripts with the Query Tool's script runner (SET TERM, COMMIT, isql "
                                   "commands are understood). Stops at the first error with exit code 4.")
    add_db_arg(p)
    p.add_argument("-f", "--file", action="append", metavar="FILE", help="script file (repeatable; - for stdin)")
    p.add_argument("-c", "--command", metavar="SQL", help="SQL text to run")
    p.add_argument("--create", action="store_true", help="create the database first")
    p.add_argument("--page-size", type=int, help="page size for --create (default 16384)")
    p.add_argument("--single-transaction", action="store_true",
                   help="run everything in one transaction, rolled back on error (default: commit each statement)")
    p.add_argument("--max-rows", type=int, default=1000, help="rows printed per SELECT (default %(default)s)")
    p.set_defaults(func=cmd_sql)

    p = sub.add_parser("check", parents=common, help="health check: connect and print version and ODS",
                       description="Connect to a database (or only the server) and print the Firebird version, "
                                   "ODS and page size. Exit code 0 when healthy, 1 otherwise.")
    add_db_arg(p)
    p.add_argument("--server-only", action="store_true", help="only check the server (service manager)")
    p.set_defaults(func=cmd_check)

    p = sub.add_parser("servers", parents=common, help="list the servers and databases registered in fbadmin",
                       description="List the registrations a cron job can use with --server/--database or "
                                   "@SERVER/DATABASE (passwords are never shown).")
    p.set_defaults(func=cmd_servers)

