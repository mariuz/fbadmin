"""Remote mode of the command-line tool: run commands through a running fbadmin server.

``fbadmin --url https://fbadmin.example.com --token-env FBADMIN_TOKEN backup @Production/app -o app.fbk``

The commands use the server's REST API with an API token (``Authorization: Bearer``), so they work
with the server's registered servers, saved passwords and permissions, from a machine that cannot
reach Firebird itself. Databases are named as registered on that server: ``@SERVER/DATABASE`` (or
``--server``/``--database``); ``@SERVER:PATH`` names a new database file for ``restore``. Long
operations run as jobs on the server: their log is shown while they run, and Ctrl+C cancels them.
See docs/CLI.md.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .common import CONNECT, DIFFERENCES, FAILED, USAGE, CliError, Out, clean_error

CHUNK = 1 << 16
_PROGRESS = re.compile(r"(MB received|^Sent \d+%|^Restoring|^Backing up|^Test restore|Cancelling|Stopped the server|"
                       r"^Dropped|^Restore (completed|stopped)|^Backup completed|^Recompil)")


# ------------------------------------------------------------------ settings
def url_from(args: argparse.Namespace) -> str | None:
    return getattr(args, "url", None) or os.environ.get("FBADMIN_URL") or None


def token_from(args: argparse.Namespace) -> str | None:
    if getattr(args, "token", None):
        return args.token
    env = getattr(args, "token_env", None)
    if env:
        val = os.environ.get(env)
        if not val:
            raise CliError(f"The environment variable {env} (--token-env) is not set", USAGE)
        return val
    return os.environ.get("FBADMIN_TOKEN") or None


def add_remote_options(p: argparse.ArgumentParser, *, top: bool) -> None:
    """``--url``/``--token``... on the top-level parser and on every subcommand (either position works)."""
    g = p.add_argument_group("remote mode (run through a fbadmin server)")
    d = {} if top else {"default": argparse.SUPPRESS}  # a subcommand must not reset a value given before it
    g.add_argument("--url", metavar="URL", help="fbadmin server to run the command on (default $FBADMIN_URL)", **d)
    g.add_argument("--token-env", metavar="NAME", help="environment variable with the API token "
                                                       "(default $FBADMIN_TOKEN)", **d)
    g.add_argument("--token", help="API token (visible in the process list: prefer --token-env)", **d)
    g.add_argument("--ca-cert", metavar="FILE", help="CA certificate (PEM) for the server's HTTPS certificate", **d)


# ------------------------------------------------------------------ HTTP client
class Client:
    def __init__(self, url: str, token: str | None, ca_cert: str | None = None, timeout: float = 120):
        if not re.match(r"^https?://", url):
            raise CliError(f"--url must start with http:// or https:// (got {url!r})", USAGE)
        self.base = url.rstrip("/")
        self.token = token
        self.timeout = timeout
        self.ctx = ssl.create_default_context(cafile=ca_cert) if ca_cert else None

    def _req(self, method: str, path: str, *, body: Any = None, data: Any = None, length: int | None = None,
             params: dict[str, Any] | None = None, content_type: str | None = None):
        url = self.base + path
        if params:
            q = {k: ("true" if v is True else "false" if v is False else v) for k, v in params.items() if v is not None}
            url += ("&" if "?" in url else "?") + urllib.parse.urlencode(q)
        headers = {"Accept": "application/json", "User-Agent": "fbadmin-cli"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        if body is not None:
            data = json.dumps(body).encode()
            content_type = "application/json"
        if content_type:
            headers["Content-Type"] = content_type
        if length is not None:
            headers["Content-Length"] = str(length)
        req = urllib.request.Request(url, data=data, method=method, headers=headers)
        try:
            return urllib.request.urlopen(req, timeout=self.timeout, context=self.ctx)  # noqa: S310 - http(s) only
        except urllib.error.HTTPError as exc:
            raise self._error(exc) from None
        except urllib.error.URLError as exc:
            raise CliError(f"Cannot reach fbadmin at {self.base}: {exc.reason}", CONNECT) from None
        except OSError as exc:  # timeouts, connection resets
            raise CliError(f"Cannot reach fbadmin at {self.base}: {exc}", CONNECT) from None

    def _error(self, exc: urllib.error.HTTPError) -> CliError:
        try:
            data = json.loads(exc.read() or b"{}")
        except ValueError:
            data = {}
        detail = data.get("detail")
        msg = detail if isinstance(detail, str) else json.dumps(detail) if detail else exc.reason
        code = data.get("code")
        if exc.code == 401 and code == "password_required":
            return CliError(f"The fbadmin server has no saved password for this Firebird server: save it in the "
                            f"web app ({msg})", CONNECT)
        if exc.code == 401:
            hint = " (give an API token with --token-env NAME or $FBADMIN_TOKEN)" if not self.token else ""
            return CliError(f"Not signed in to {self.base}: {msg}{hint}", CONNECT)
        if exc.code == 403:
            return CliError(f"Not allowed by {self.base}: {msg}", CONNECT)
        if exc.code == 404:
            return CliError(f"Not found on {self.base}: {msg}", USAGE)
        if exc.code == 413:
            return CliError(f"{msg}: the upload is larger than the server accepts (FBADMIN_MAX_REQUEST_SIZE on the "
                            "server); restore from a file on the Firebird server with --server-file instead", FAILED)
        return CliError(msg, FAILED)

    def json(self, method: str, path: str, body: Any = None, **kw) -> Any:
        with self._req(method, path, body=body, **kw) as r:
            raw = r.read()
        try:
            return json.loads(raw) if raw else None
        except ValueError:
            raise CliError(f"Unexpected answer from {self.base}{path.split('?')[0]}: is --url a fbadmin server, "
                           "and as recent as this command-line tool?", CONNECT) from None

    def stream(self, method: str, path: str, body: Any = None, **kw) -> Iterator[bytes]:
        with self._req(method, path, body=body, **kw) as r:
            while chunk := r.read(CHUNK):
                yield chunk

    def text(self, method: str, path: str, **kw) -> str:
        with self._req(method, path, **kw) as r:
            return r.read().decode("utf-8")

    # --- jobs
    def wait_job(self, job: dict, out: Out) -> dict:
        """Follow a job's log until it ends. Ctrl+C cancels it on the server (twice: stop waiting)."""
        jid = job["id"]
        shown = 0
        cancelled_at = None
        while True:
            try:
                j = self.json("GET", f"/api/jobs/{jid}")
                for line in (j.get("log") or [])[shown:]:
                    (out.log if _PROGRESS.search(line) else out.detail)(line)
                shown = len(j.get("log") or [])
                if j["status"] != "running":
                    return j
                time.sleep(0.5)
            except KeyboardInterrupt:
                if cancelled_at is not None or not job.get("cancellable"):
                    raise
                out.log("Cancelling the job on the server (Ctrl+C again to stop waiting)...")
                try:
                    self.json("POST", f"/api/jobs/{jid}/cancel")
                except CliError as exc:
                    out.log(f"Could not cancel: {exc}")
                cancelled_at = time.time()


def finished(j: dict, what: str) -> None:
    if j["status"] == "cancelled":
        raise CliError(f"{what} was cancelled", 130)
    if j["status"] != "finished":
        raise CliError(f"{what} failed: {clean_error(Exception(j.get('error') or 'see the log'))}", FAILED)


def client(args: argparse.Namespace) -> Client:
    return Client(url_from(args) or "", token_from(args), getattr(args, "ca_cert", None))


# ------------------------------------------------------------------ targets
@dataclass
class RemoteTarget:
    server_id: str
    server: str
    db_id: str | None = None
    db: str | None = None
    path: str | None = None
    new_path: bool = False  # @SERVER:PATH (a database file that is not registered)
    read_only: bool = False

    @property
    def label(self) -> str:
        if self.db:
            return f"{self.server} / {self.db}"
        return f"{self.server}:{self.path}" if self.path else self.server

    @property
    def base(self) -> str:
        return f"/api/servers/{self.server_id}/databases/{self.db_id}"


def _find(items: list[dict], ref: str, what: str, names: str) -> dict:
    low = ref.lower()
    for match in (lambda x: x["id"] == ref, lambda x: x["name"] == ref, lambda x: x["name"].lower() == low,
                  lambda x: x.get("path") == ref):
        hits = [x for x in items if match(x)]
        if len(hits) == 1:
            return hits[0]
        if len(hits) > 1:
            raise CliError(f"Several {what}s are called {ref!r}; use the id", USAGE)
    raise CliError(f"No {what} {ref!r} on the fbadmin server (registered: {names or 'none'}). "
                   "See 'fbadmin servers'.", USAGE)


def resolve(c: Client, args: argparse.Namespace, spec: str | None, *, need_db: bool = True,
            allow_path: bool = False) -> RemoteTarget:
    server_ref = db_ref = None
    path = None
    if spec and spec.startswith("@"):
        rest = spec[1:]
        if allow_path and re.match(r"^[^/:]+:", rest):  # @SERVER:PATH (a new database file)
            server_ref, path = rest.split(":", 1)
        else:
            server_ref, _, db_ref = rest.partition("/")
            db_ref = db_ref or None
    elif spec:
        raise CliError(f"In remote mode, name a database registered on the fbadmin server: @SERVER/DATABASE "
                       f"(not {spec!r}). See 'fbadmin servers'.", USAGE)
    else:
        server_ref, db_ref = getattr(args, "server", None), getattr(args, "database", None)
    if not server_ref:
        raise CliError("Name the database as @SERVER/DATABASE (or --server/--database) of the fbadmin server", USAGE)
    servers = c.json("GET", "/api/servers")
    srv = _find(servers, server_ref, "server", ", ".join(sorted(s["name"] for s in servers)))
    t = RemoteTarget(server_id=srv["id"], server=srv["name"], read_only=bool(srv.get("read_only")))
    if path:
        t.path, t.new_path = path, True
        known = next((d for d in srv["databases"] if d["path"] == path), None)
        if known:  # the path is registered: use the registration
            t.db_id, t.db, t.new_path = known["id"], known["name"], False
        return t
    dbs = srv.get("databases") or []
    if db_ref is None and need_db:
        if len(dbs) != 1:
            raise CliError(f"Server {srv['name']!r} has {len(dbs)} registered databases: choose one with "
                           "@SERVER/DATABASE or --database", USAGE)
        db_ref = dbs[0]["id"]
    if db_ref is not None:
        d = _find(dbs, db_ref, f"database of {srv['name']}", ", ".join(x["name"] for x in dbs))
        t.db_id, t.db, t.path = d["id"], d["name"], d["path"]
    return t


def _not_remote(args: argparse.Namespace, *names: str) -> None:
    """Options that only make sense when fbadmin connects to Firebird itself."""
    for n in names:
        v = getattr(args, n.replace("-", "_"), None)
        if v not in (None, False, [], ""):
            raise CliError(f"--{n} is not available in remote mode", USAGE)


LOCAL_CONNECTION = ("host", "port", "user", "password", "password-env", "role", "charset", "auth-plugin",
                    "no-wire-crypt", "wire-compress", "data-dir", "master-password-env")


# ------------------------------------------------------------------ commands
def cmd_servers(args: argparse.Namespace, out: Out, c: Client) -> int:
    servers = c.json("GET", "/api/servers")
    rows = [{"id": s["id"], "name": s["name"], "host": s.get("host"), "port": s.get("port"), "user": s.get("user"),
             "embedded": bool(s.get("embedded")), "read_only": bool(s.get("read_only")),
             "password": "saved" if s.get("password_known") else "none",
             "databases": [{"id": d["id"], "name": d["name"], "path": d["path"]} for d in s.get("databases", [])]}
            for s in servers]
    if not args.json:
        if not rows:
            out.log(f"No servers registered on {c.base} (for this user)")
        for s in rows:
            where = "embedded" if s["embedded"] else f"{s['host']}/{s['port']}"
            print(f"{s['name']}  [{s['id']}]  {where}  user {s['user']}  password: {s['password']}")
            for d in s["databases"]:
                print(f"    {d['name']}  [{d['id']}]  {d['path']}")
    out.result({"url": c.base, "servers": rows})
    return 0


def cmd_check(args: argparse.Namespace, out: Out, c: Client) -> int:
    has_db = bool(args.db or getattr(args, "database", None))
    try:
        t = resolve(c, args, args.db, need_db=has_db and not args.server_only)
        res: dict[str, Any] = {"target": t.label, "url": c.base}
        if t.db_id and not args.server_only:
            info = (c.json("GET", t.base + "/info") or {}).get("info") or {}
            ver = info.get("engine_version")
            ods = f"{info.get('ods_major')}.{info.get('ods_minor')}"
            res.update(engine_version=ver, ods=ods, page_size=info.get("page_size"), dialect=info.get("sql_dialect"),
                       read_only=bool(info.get("read_only")), charset=info.get("charset"))
            text = (f"OK {t.label}: Firebird {ver}, ODS {ods}, page size {info.get('page_size')}, dialect "
                    f"{info.get('sql_dialect')}, charset {info.get('charset') or 'NONE'}"
                    + (", read-only" if info.get("read_only") else ""))
        else:
            info = c.json("GET", f"/api/servers/{t.server_id}/info")
            res.update(server_version=info.get("server_version"))
            text = f"OK {t.label}: {info.get('server_version')}"
    except CliError as exc:
        if exc.code == USAGE:
            raise
        raise CliError(str(exc), DIFFERENCES) from exc
    out.result(res, text)
    return 0


def cmd_sql(args: argparse.Namespace, out: Out, c: Client) -> int:
    from .misc import _format_rows

    _not_remote(args, "create", "page-size", "single-transaction")
    scripts = _scripts(args)
    t = resolve(c, args, args.db)
    started = time.time()
    results: list[dict[str, Any]] = []
    error = None
    for name, text in scripts:
        out.detail(f"Running {name}")
        res = c.json("POST", t.base + "/execute", {"sql": text})
        for item in res["results"]:
            item["script"] = name
            results.append(item)
            if item["kind"] == "select" and not args.json:
                if args.max_rows is not None and len(item.get("rows") or []) > args.max_rows:
                    item["rows"], item["truncated"] = item["rows"][:args.max_rows], True
                print(_format_rows(item), flush=True)
            elif item["kind"] != "error":
                out.detail(f"line {item['line']}: {item['message']}")
        if res.get("error"):
            e = res["error"]
            error = {"script": name, "line": e["line"], "message": e["message"], "sql": (e.get("sql") or "")[:2000]}
            break
    data = {"database": t.label, "statements": len([r for r in results if r["kind"] != "error"]),
            "seconds": round(time.time() - started, 2),
            "results": [{k: r.get(k) for k in ("script", "line", "kind", "message", "columns", "rows", "row_count")
                         if r.get(k) is not None} for r in results]}
    if error:
        data["error_at"] = error
        raise CliError(f"{error['script']}, line {error['line']}: {error['message']}", FAILED, data)
    out.result(data, f"{data['statements']} statement(s) executed in {data['seconds']} s", to_stderr=True)
    return 0


def _scripts(args: argparse.Namespace) -> list[tuple[str, str]]:
    if args.command and args.file:
        raise CliError("Use either -c SQL or -f FILE", USAGE)
    if args.command:
        return [("-c", args.command)]
    if args.file:
        out = []
        for f in args.file:
            if f == "-":
                out.append(("stdin", sys.stdin.read()))
                continue
            p = Path(f).expanduser()
            if not p.is_file():
                raise CliError(f"No such script file: {p}", USAGE)
            out.append((str(p), p.read_text("utf-8-sig")))
        return out
    if sys.stdin.isatty():
        raise CliError("Give a script with -f FILE, -c SQL or on stdin", USAGE)
    return [("stdin", sys.stdin.read())]


def cmd_extract(args: argparse.Namespace, out: Out, c: Client) -> int:
    from .schema import _write

    _not_remote(args, "split", "types", "schema")
    t = resolve(c, args, args.db)
    sql = c.json("GET", t.base + "/schema/extract", params={
        "set_term": not args.no_set_term, "grants": not args.no_grants, "comments": not args.no_comments,
        "sequence_values": not args.no_sequence_values, "header": not args.no_header})["sql"]
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


def cmd_diff(args: argparse.Namespace, out: Out, c: Client) -> int:
    from .schema import _is_script, _script_path, _write

    _not_remote(args, "target-user", "target-password", "target-password-env")
    if args.no_schema and args.data is None:
        raise CliError("--no-schema needs --data (there would be nothing to compare)", USAGE)
    src_t = None if _is_script(args.source) else resolve(c, args, args.source)
    dst_t = None if _is_script(args.target) else resolve(c, args, args.target)
    if src_t is None and dst_t is None:
        raise CliError("Both sides are DDL files: at least one side must be a database", USAGE)
    real = src_t or dst_t

    def side(t: RemoteTarget | None, spec: str) -> dict:
        if t is not None:
            return {"server_id": t.server_id, "db_id": t.db_id}
        p = _script_path(spec)
        return {"server_id": real.server_id, "sql": p.read_text("utf-8-sig"), "temp_dir": args.temp_dir}

    res: dict[str, Any] = {"source": src_t.label if src_t else args.source,
                           "target": dst_t.label if dst_t else args.target}
    differences = 0
    script = ""
    if not args.no_schema:
        diff = c.json("POST", "/api/schema/diff", {"source": side(src_t, args.source),
                                                   "target": side(dst_t, args.target),
                                                   "set_term": not args.no_set_term})
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
        summary, data_script = _data_compare(c, src_t, dst_t, args.data or None, out)
        differences += summary["statements"]
        res["data"] = summary
        tot = summary["totals"]
        out.log(f"Data: {tot.get('only_source', 0)} row(s) to insert, {tot.get('different', 0)} to update, "
                f"{tot.get('only_target', 0)} to delete")
        errors = [x for x in summary["tables"] if x["status"] == "error"]
        for x in errors:
            out.log(f"Data: {x['name']} failed - {x['error']}")
        if data_script:
            script = (script + "\n" if script else "") + data_script
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


def _data_compare(c: Client, src: RemoteTarget, dst: RemoteTarget, tables: list[str] | None,
                  out: Out) -> tuple[dict, str]:
    pair = {"source": {"server_id": src.server_id, "db_id": src.db_id},
            "target": {"server_id": dst.server_id, "db_id": dst.db_id}}
    listing = c.json("POST", "/api/datacompare/tables", pair)
    comparable = {t["name"]: t for t in listing["tables"] if t["comparable"]}
    if tables:
        names = []
        for n in tables:
            hit = next((x for x in listing["tables"] if x["name"].upper() == n.upper()), None)
            if hit is None:
                raise CliError(f"No table {n!r} in either database", USAGE)
            if not hit["comparable"]:
                raise CliError(f"Table {hit['name']} cannot be compared: {hit['reason']}", USAGE)
            names.append(hit["name"])
    else:
        names = sorted(comparable)
    job = c.json("POST", "/api/datacompare/compare", {**pair, "tables": [{"name": n} for n in names],
                                                     "max_differences": 0})
    finished(c.wait_job(job, out), "The data comparison")
    res = c.json("GET", f"/api/datacompare/jobs/{job['id']}")
    script = c.text("GET", f"/api/datacompare/jobs/{job['id']}/script") if res["statements"] else ""
    summary = {"tables": [{k: t[k] for k in ("name", "status", "error", "only_source", "only_target", "different",
                                             "identical")} for t in res["tables"]],
               "skipped": res["skipped"], "totals": res["totals"], "statements": res["statements"],
               "job_id": job["id"]}
    return summary, script


# --- backup
def cmd_backup(args: argparse.Namespace, out: Out, c: Client) -> int:
    from .backup import _new_file, _safe, _zstd, backup_options, retention, write_stream

    _not_remote(args, "verify", "verify-path")
    opts = backup_options(args).model_dump()
    targets = [x for x in (args.output, args.dir, args.server_file, getattr(args, "archive", False) or None) if x]
    if len(targets) != 1:
        raise CliError("Choose one destination: -o FILE (or - for stdout), --dir DIRECTORY, --server-file PATH or "
                       "--archive (the fbadmin server's backup archive)", USAGE)
    t = resolve(c, args, args.db)
    started = time.time()
    res: dict[str, Any] = {"database": t.label, "url": c.base}
    mbase = f"/api/servers/{t.server_id}/databases/{t.db_id}/maintenance"
    if args.server_file or args.archive:
        if args.compress not in (None, "none") or args.checksum or args.keep is not None:
            raise CliError("--compress, --checksum and --keep work on local files", USAGE)
        body = {"target": "server", "backup_file": args.server_file} if args.server_file else {"target": "archive"}
        where = f"{args.server_file} (on the Firebird server)" if args.server_file else "the fbadmin server's archive"
        out.log(f"Backing up {t.label} to {where}")
        job = c.json("POST", mbase + "/backup", {**body, "options": opts})
        finished(c.wait_job(job, out), "The backup")
        res.update({"server_file": args.server_file} if args.server_file else {"archive": True})
        res["seconds"] = round(time.time() - started, 1)
        out.result(res, f"Backup of {t.label} written to {where} in {res['seconds']} s")
        return 0
    to_stdout = args.output == "-"
    kind = args.compress
    if kind is None:
        o = (args.output or "").lower()
        kind = "gzip" if o.endswith(".gz") else "zstd" if o.endswith(".zst") else "none"
    if kind == "zstd":
        _zstd()
    if args.keep is not None and not args.dir:
        raise CliError("--keep needs --dir (old backups are deleted from that directory)", USAGE)
    if to_stdout and (args.checksum or args.json):
        raise CliError("--checksum and --json cannot be used with -o - (stdout)", USAGE)
    if to_stdout:
        dest = None
    elif args.dir:
        d = Path(args.dir).expanduser()
        d.mkdir(parents=True, exist_ok=True)
        dest = _new_file(d, _safe(args.name or _stem(t.path or t.db or "backup")), kind)
    else:
        dest = Path(args.output).expanduser()
        dest.parent.mkdir(parents=True, exist_ok=True)
    out.log(f"Backing up {t.label} to {dest or 'stdout'}" + (f" ({kind})" if kind != "none" else ""))
    prep = c.json("POST", mbase + "/backup/stream", opts)  # option errors are reported here
    res["compress"] = kind
    res.update(write_stream(c.stream("GET", prep["url"]), dest, kind, out))
    if dest:
        res["file"] = str(dest)
        if args.checksum:
            side = Path(str(dest) + ".sha256")
            side.write_text(f"{res['sha256']}  {dest.name}\n", "utf-8")
            res["checksum_file"] = str(side)
    res["seconds"] = round(time.time() - started, 1)
    if args.keep is not None and args.keep > 0:
        res["removed"] = retention(Path(args.dir).expanduser(), _safe(args.name or _stem(t.path or "")), args.keep)
        for r in res["removed"]:
            out.log(f"Retention: deleted {r}")
    out.result(res, f"Backup of {t.label} written to {dest or 'stdout'}, {res['file_bytes']} bytes in "
                    f"{res['seconds']} s", to_stderr=to_stdout)
    return 0


def _stem(path: str) -> str:
    p = path.replace("\\", "/").rstrip("/").split("/")[-1]
    return re.sub(r"\.(fdb|gdb|ib)$", "", p, flags=re.I) or "database"


# --- restore
def cmd_restore(args: argparse.Namespace, out: Out, c: Client) -> int:
    from .backup import open_backup, restore_options

    pos = list(args.args)
    if args.server_file:
        if len(pos) != 1:
            raise CliError("With --server-file give only the database to restore into", USAGE)
        backup, spec = None, pos[0]
    else:
        if len(pos) != 2:
            raise CliError("Give the backup file (or - for stdin) and the database: BACKUP @SERVER/DATABASE "
                           "(or @SERVER:PATH for a new database file)", USAGE)
        backup, spec = pos
    t = resolve(c, args, spec, allow_path=True)
    if not t.path:
        raise CliError("Name the database to restore into: @SERVER/DATABASE or @SERVER:PATH", USAGE)
    o = restore_options(args)
    if t.db_id and not o.replace:
        raise CliError(f"{t.label} is a registered database: add --replace to overwrite it", USAGE)
    if t.db_id:  # the fbadmin server's own connections to the database would block the restore
        c.json("POST", f"{t.base}/disconnect")
    started = time.time()
    res: dict[str, Any] = {"database": t.label, "path": t.path, "replace": o.replace, "url": c.base}
    register = getattr(args, "register_as", None)
    if args.server_file:
        out.log(f"Restoring {args.server_file} (on the Firebird server) into {t.label}")
        job = c.json("POST", f"/api/servers/{t.server_id}/maintenance/restore",
                     {"source": "server", "backup_file": args.server_file, "database_path": t.path,
                      "register_as": register, "options": o.model_dump()})
        res["server_file"] = args.server_file
    else:
        src = None if backup == "-" else Path(backup).expanduser()
        if src is not None and not src.is_file():
            raise CliError(f"No such backup file: {src}", USAGE)
        reader, raw, size, kind = open_backup(src)
        params = {k: v for k, v in o.model_dump().items() if v not in (None, False)}
        params.update(database_path=t.path, filename=src.name if src else "stdin.fbk", register_as=register)
        out.log(f"Uploading {src or 'stdin'} and restoring it into {t.label}")
        try:
            if kind == "none" and src is not None:
                job = c.json("POST", f"/api/servers/{t.server_id}/maintenance/restore/upload", params=params,
                             data=raw, length=size, content_type="application/octet-stream")
            else:  # decompressed (or stdin) on the fly: chunked upload
                job = c.json("POST", f"/api/servers/{t.server_id}/maintenance/restore/upload", params=params,
                             data=_chunks(reader), content_type="application/octet-stream")
        finally:
            if src is not None:
                raw.close()
        res["file"] = str(src) if src else "-"
    finished(c.wait_job(job, out), "The restore")
    res["seconds"] = round(time.time() - started, 1)
    out.result(res, f"Restored {res.get('file') or res.get('server_file')} into {t.label} in {res['seconds']} s")
    return 0


def _chunks(reader) -> Iterable[bytes]:
    while chunk := reader.read(CHUNK):
        yield chunk


def cmd_migrate(args: argparse.Namespace, out: Out, c: Client) -> int:
    from .migrate import cmd_remote

    return cmd_remote(args, out, c)


COMMANDS = {"servers": cmd_servers, "check": cmd_check, "sql": cmd_sql, "extract": cmd_extract, "diff": cmd_diff,
            "backup": cmd_backup, "restore": cmd_restore, "migrate": cmd_migrate}


def run(args: argparse.Namespace, out: Out) -> int:
    fn = COMMANDS.get(args.cmd)
    if fn is None:
        raise CliError(f"{args.cmd} is not available in remote mode", USAGE)
    _not_remote(args, *LOCAL_CONNECTION)
    return fn(args, out, client(args))
