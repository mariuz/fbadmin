"""``fbadmin migrate``: versioned migrations from a directory of ``V<version>__<description>.sql`` scripts.

    fbadmin migrate status  @Production/app --dir db/migrations
    fbadmin migrate dry-run @Production/app --dir db/migrations          # on a copy, which is dropped again
    fbadmin migrate apply   @Production/app --dir db/migrations          # dry run first, then the database
    fbadmin migrate baseline @Production/app --version 3                 # an existing database is at version 3
    fbadmin migrate repair  @Production/app --dir db/migrations          # after fixing a failed script by hand

Locally fbadmin connects to Firebird itself; with --url it asks an fbadmin server, which records who applied
what in its audit log. Exit codes: 0 ok, 1 pending scripts (status --check), 4 a script or the dry run failed.
"""

from __future__ import annotations

import argparse
import getpass
import subprocess
from pathlib import Path
from typing import Any

from .common import DIFFERENCES, FAILED, USAGE, CliError, Out, add_db_arg, resolve

ACTIONS = ("status", "dry-run", "apply", "baseline", "repair")


def _user() -> str:
    try:
        return getpass.getuser()
    except Exception:  # noqa: BLE001
        return "fbadmin-cli"


def _dir_files(args: argparse.Namespace) -> tuple[list[tuple[str, str]], str]:
    from .. import migrations as M

    if not args.dir:
        raise CliError("Give the directory of the migration scripts with --dir", USAGE)
    d = Path(args.dir).expanduser()
    if not d.is_dir():
        raise CliError(f"{d} is not a directory", USAGE)
    files = M.read_dir(d)
    ref = args.ref
    if not ref:  # the commit of the checkout, when the directory is in a git repository
        try:
            p = subprocess.run(["git", "-C", str(d), "rev-parse", "--short=10", "HEAD"], capture_output=True,
                               text=True, timeout=10)
            ref = f"{d.name}@{p.stdout.strip()}" if p.returncode == 0 and p.stdout.strip() else str(d)
        except (OSError, subprocess.TimeoutExpired):
            ref = str(d)
    return files, ref[:120]


def _print_status(st: dict[str, Any], out: Out) -> None:
    if out.json:
        return
    for i in st["items"]:
        when = ""
        if i.get("installed_at"):
            import time

            when = time.strftime("%Y-%m-%d %H:%M", time.localtime(i["installed_at"]))
        print(f"{i['version']:>12}  {i['state']:<12}  {i.get('script') or '(baseline)':<50}  {when}", flush=True)
    for w in st["warnings"]:
        out.log(f"warning: {w}")
    for e in st["errors"]:
        out.log(f"error: {e}")


def _report_dry_run(rep: dict[str, Any], out: Out) -> None:
    for s in rep.get("scripts", []):
        if s["ok"]:
            out.log(f"  ok      {s['script']} ({s['statements']} statement(s), {s['ms']} ms)")
        else:
            out.log(f"  FAILED  {s['script']} line {s['error']['line']}: {s['error']['message']}")
    sm = rep.get("summary") or {}
    if sm:
        out.log(f"Schema changes: {sm.get('created', 0)} created, {sm.get('altered', 0)} altered, "
                f"{sm.get('dropped', 0)} dropped")
    for c in (rep.get("changes") or [])[:50]:
        out.detail(f"  {c['change']:<8} {c['type']} {c['name']}")


def cmd_migrate(args: argparse.Namespace, out: Out) -> int:
    from .. import migrations as M

    t = resolve(args, args.db)
    key = f"cli/{t.label}"
    if args.action == "baseline":
        if not args.version:
            raise CliError("Give the version the database is at with --version", USAGE)
        conn = t.connect()
        try:
            M.baseline(conn, args.version, args.description or "Baseline", user=_user())
        finally:
            conn.close()
        out.result({"database": t.label, "baseline": args.version}, f"{t.label} is at version {args.version}")
        return 0
    files, ref = _dir_files(args)
    scripts, warnings = M.parse(files)
    for w in warnings:
        out.log(f"warning: {w}")
    conn = t.connect()
    try:
        rows = M.applied(conn)
        if args.action == "repair":
            res = M.repair(conn, scripts)
            out.result({"database": t.label, **res}, f"Repaired {t.label}: removed failed records "
                       f"{res['removed_failed'] or 'none'}, accepted checksums {res['accepted_checksums'] or 'none'}")
            return 0
    finally:
        conn.close()
    st = M.status(scripts, rows, out_of_order=args.out_of_order, target=args.target)
    view = {"database": t.label, "source_ref": ref, **M.public(st)}
    if args.action == "status":
        _print_status(st, out)
        out.result(view, f"{t.label}: version {st['current'] or 'none'}, {len(st['pending'])} pending")
        if st["errors"]:
            return FAILED
        return DIFFERENCES if args.check and st["pending"] else 0
    if st["errors"]:
        _print_status(st, out)
        raise CliError(" ".join(st["errors"]), FAILED)
    pending = st["_pending"]
    res: dict[str, Any] = {"database": t.label, "source_ref": ref, "pending": st["pending"]}
    if not pending:
        out.result({**res, "applied": []}, f"{t.label} is up to date (version {st['current'] or 'none'})")
        return 0
    if args.action == "dry-run" or not args.no_dry_run:
        out.log(f"Dry run of {len(pending)} script(s) on a {'metadata-only ' if args.copy == 'metadata' else ''}"
                f"copy of {t.label}")
        rep = M.dry_run(pending=pending, db_path=t.path, scratch=M.scratch_path(t.path, "cli", args.scratch),
                        copy=args.copy, svc_factory=t.service, connect=lambda p: t.connect(path=p),
                        registered=[t.path], user=_user(), ref=ref, log=out.detail)
        _report_dry_run(rep, out)
        res["dry_run"] = rep
        if not rep["ok"]:
            out.result({**res, "ok": False}, None)
            raise CliError("The dry run failed: nothing was applied", FAILED)
        if args.action == "dry-run":
            out.result(res, f"Dry run passed: {len(pending)} script(s) would be applied to {t.label}")
            return 0
    rep = M.apply(lambda: t.connect(), scripts, key=key, user=_user(), ref=ref, log=out.log,
                  out_of_order=args.out_of_order, target=args.target)
    res.update(applied=[r["script"] for r in rep["applied"]], current=rep["current"], seconds=rep["seconds"])
    if rep["failed"]:
        f = rep["failed"]
        res["failed"] = f
        out.result({**res, "ok": False}, None)
        raise CliError(f"{f['script']} failed at line {f['error']['line']}: {f['error']['message']}", FAILED)
    out.result(res, f"Applied {len(rep['applied'])} script(s) to {t.label}: now at version {rep['current']}")
    return 0


# ------------------------------------------------------------------ remote mode (--url)
def cmd_remote(args: argparse.Namespace, out: Out, c) -> int:
    from .remote import finished, resolve as rresolve

    t = rresolve(c, args, args.db)
    base = t.base + "/migrations"
    if args.action == "baseline":
        if not args.version:
            raise CliError("Give the version the database is at with --version", USAGE)
        c.json("POST", base + "/baseline", {"version": args.version, "description": args.description or "Baseline"})
        out.result({"database": t.label, "baseline": args.version}, f"{t.label} is at version {args.version}")
        return 0
    body: dict[str, Any] = {"out_of_order": args.out_of_order or None, "target": args.target}
    if args.dir:
        files, ref = _dir_files(args)
        body.update(scripts=[{"name": n, "sql": s} for n, s in files], source_ref=ref)
    if args.action == "repair":
        res = c.json("POST", base + "/repair", body)
        out.result({"database": t.label, **res}, f"Repaired {t.label}")
        return 0
    st = c.json("POST", base + "/status", body)
    if args.action == "status":
        _print_status(st, out)
        out.result({"database": t.label, **st}, f"{t.label}: version {st['current'] or 'none'}, "
                   f"{len(st['pending'])} pending")
        return FAILED if st["errors"] else DIFFERENCES if args.check and st["pending"] else 0
    if st["errors"]:
        _print_status(st, out)
        raise CliError(" ".join(st["errors"]), FAILED)
    if not st["pending"]:
        out.result({"database": t.label, "applied": []}, f"{t.label} is up to date (version {st['current'] or 'none'})")
        return 0
    res: dict[str, Any] = {"database": t.label, "pending": st["pending"]}
    if args.action == "dry-run" or not args.no_dry_run:
        job = c.json("POST", base + "/dry-run", {**body, "copy_mode": args.copy, "scratch": args.scratch})
        finished(c.wait_job(job, out), "The dry run")
        rep = c.json("GET", base + f"/runs/{job['id']}")
        _report_dry_run(rep, out)
        res["dry_run"] = rep
        if not rep["ok"]:
            raise CliError("The dry run failed: nothing was applied", FAILED)
        if args.action == "dry-run":
            out.result(res, f"Dry run passed: {len(st['pending'])} script(s) would be applied to {t.label}")
            return 0
    job = c.json("POST", base + "/apply", {**body, "skip_dry_run": bool(args.no_dry_run)})
    j = c.wait_job(job, out)
    rep = c.json("GET", base + f"/runs/{job['id']}")
    if j["status"] != "finished":
        res["failed"] = rep.get("failed")
        out.result({**res, "ok": False}, None)
    finished(j, "Applying the migrations")
    res.update(applied=[r["script"] for r in rep.get("applied", [])], current=rep.get("current"))
    out.result(res, f"Applied {len(res['applied'])} script(s) to {t.label}: now at version {res['current']}")
    return 0


def add_parsers(sub, common) -> None:
    p = sub.add_parser("migrate", parents=common, help="versioned migrations: status, dry-run, apply, baseline, repair",
                       description="Apply numbered migration scripts (V<version>__<description>.sql) in order. The "
                                   "database records what was applied in FBADMIN_MIGRATIONS. apply runs a dry run on "
                                   "a copy of the database first (--no-dry-run skips it).")
    p.add_argument("action", choices=ACTIONS)
    add_db_arg(p)
    p.add_argument("--dir", metavar="DIRECTORY", help="the migration scripts (in remote mode: else the fbadmin "
                                                     "server's configured source)")
    p.add_argument("--ref", metavar="LABEL", help="recorded as the source of the scripts (default: the git commit "
                                                  "of the directory)")
    p.add_argument("--target", metavar="VERSION", help="apply up to this version")
    p.add_argument("--out-of-order", action="store_true", help="also apply pending scripts older than applied ones")
    p.add_argument("--copy", choices=("metadata", "full"), default="metadata",
                   help="what the dry run copies: the metadata (fast, default) or the data too")
    p.add_argument("--scratch", metavar="PATH", help="the dry run's copy on the server (default: next to the database)")
    p.add_argument("--no-dry-run", action="store_true", help="apply without a dry run first")
    p.add_argument("--check", action="store_true", help="status: exit code 1 when scripts are pending")
    p.add_argument("--version", metavar="VERSION", help="baseline: the version the database is at")
    p.add_argument("--description", help="baseline: its description")
    p.set_defaults(func=cmd_migrate)
