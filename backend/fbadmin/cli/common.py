"""Shared pieces of the command-line tool: exit codes, output, database targets and connections."""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any

# ------------------------------------------------------------------ exit codes
OK = 0
DIFFERENCES = 1   # diff --exit-code found differences; check: unhealthy
USAGE = 2         # bad arguments, missing file (argparse uses 2 as well)
CONNECT = 3       # cannot connect or log in
FAILED = 4        # the operation itself failed (gbak error, script error, ...)
VERIFY = 5        # backup written but its test restore failed


class CliError(Exception):
    def __init__(self, message: str, code: int = FAILED, data: dict[str, Any] | None = None):
        super().__init__(message)
        self.code = code
        self.data = data or {}  # partial result, included in the --json error document


def clean_error(exc: BaseException) -> str:
    """Firebird errors come as ``0:message``; drop the status prefix and byte-string noise."""
    msg = str(exc).strip() or exc.__class__.__name__
    msg = re.sub(r"^-?\d+:", "", msg).strip()
    msg = re.sub(r"\bb'([^']*)'", r"\1", msg)
    msg = re.sub(r"(?m)^\s*@\d+\s*$\n?", "", msg)  # gbak's message-number lines
    return msg.strip()


# ---------------------------------------------------------------------- output
class Out:
    """Progress and messages go to stderr; results go to stdout (one JSON document with --json)."""

    def __init__(self, args: argparse.Namespace):
        self.json = bool(getattr(args, "json", False))
        self.quiet = bool(getattr(args, "quiet", False))
        self.verbose = bool(getattr(args, "verbose", False))

    def log(self, msg: str) -> None:
        if not self.quiet:
            print(msg, file=sys.stderr, flush=True)

    def detail(self, msg: str) -> None:
        """Lines shown only with --verbose (gbak output, every statement...)."""
        if self.verbose and not self.quiet:
            print(msg, file=sys.stderr, flush=True)

    def result(self, data: dict[str, Any], text: str | None = None, *, to_stderr: bool = False) -> None:
        if self.json:
            print(json.dumps({"ok": True, **data}, indent=2, default=str), flush=True)
        elif text and not self.quiet:
            print(text, file=sys.stderr if to_stderr else sys.stdout, flush=True)

    def error(self, msg: str, code: int, data: dict[str, Any] | None = None) -> None:
        if self.json:
            print(json.dumps({**(data or {}), "ok": False, "error": msg, "exit_code": code}, indent=2, default=str),
                  flush=True)
        print(f"fbadmin: error: {msg}", file=sys.stderr, flush=True)


# --------------------------------------------------------------------- targets
@dataclass
class Target:
    """A database (or just a server) with everything needed to connect. Never printed with its password."""

    srv: Any  # store.ServerEntry
    path: str | None
    password: str = field(repr=False, default="")
    label: str = ""
    registered_paths: list[str] = field(default_factory=list)  # other databases of the registration

    @property
    def name(self) -> str:
        p = (self.path or "database").replace("\\", "/").rstrip("/").split("/")[-1]
        return re.sub(r"\.(fdb|gdb|ib)$", "", p, flags=re.I) or "database"

    def connect(self, *, create: bool = False, page_size: int = 16384, path: str | None = None):
        from ..connections import connect

        db = path or self.path
        if not db:
            raise CliError("No database given", USAGE)
        try:
            return connect(self.srv, db, self.password, create=create, page_size=page_size)
        except Exception as exc:  # noqa: BLE001
            raise CliError(f"Cannot connect to {self.label if not path else path}: {clean_error(exc)}",
                           CONNECT) from exc

    @contextmanager
    def service(self) -> Iterator[Any]:
        if self.srv.embedded:
            from ..embedded import services as embedded_services

            with embedded_services() as svc:
                yield svc
            return
        from firebirdsql import services

        from ..connections import wire_options

        try:
            svc = services.connect(host=self.srv.host, port=self.srv.port, user=self.srv.user,
                                   password=self.password, wire_crypt=self.srv.wire_crypt, timeout=60,
                                   **wire_options(self.srv))
        except Exception as exc:  # noqa: BLE001
            raise CliError(f"Cannot connect to the service manager of {self.srv.host}/{self.srv.port}: "
                           f"{clean_error(exc)}", CONNECT) from exc
        try:
            yield svc
        finally:
            try:
                svc.close()
            except Exception:  # noqa: BLE001
                pass


_WIN_DRIVE = re.compile(r"^[A-Za-z]:[\\/]")


def parse_dsn(dsn: str) -> tuple[str | None, int | None, str]:
    """``host[/port]:path``, ``inet://host[:port]/path`` or a plain path/alias -> (host, port, path)."""
    m = re.match(r"^inet[46]?://([^/:]+|\[[^\]]+\])(?::(\d+))?/(.+)$", dsn)
    if m:
        return m.group(1).strip("[]"), int(m.group(2)) if m.group(2) else None, m.group(3)
    if _WIN_DRIVE.match(dsn):  # C:\data\x.fdb (local path, no host)
        return None, None, dsn
    m = re.match(r"^([^:/\\]+)(?:/(\d+))?:(.+)$", dsn)
    if m:
        return m.group(1), int(m.group(2)) if m.group(2) else None, m.group(3)
    return None, None, dsn


def password_from(args: argparse.Namespace, prefix: str = "") -> str | None:
    """--password, then --password-env NAME (both may have a ``target_`` prefix)."""
    pw = getattr(args, prefix + "password", None)
    if pw:
        return pw
    env = getattr(args, prefix + "password_env", None)
    if env:
        val = os.environ.get(env)
        if val is None:
            raise CliError(f"The environment variable {env} (--{prefix.replace('_', '-')}password-env) is not set",
                           USAGE)
        return val
    return None


def make_server(host: str, port: int, user: str, args: argparse.Namespace):
    from ..store import ServerEntry

    return ServerEntry(name=f"{host}/{port}", host=host, port=port, user=user, role=getattr(args, "role", None),
                       charset=getattr(args, "charset", None) or "UTF8",
                       wire_crypt=not getattr(args, "no_wire_crypt", False),
                       wire_compress=bool(getattr(args, "wire_compress", False)),
                       auth_plugin=getattr(args, "auth_plugin", None), save_password=False)


def resolve(args: argparse.Namespace, spec: str | None, *, prefix: str = "", need_db: bool = True) -> Target:
    """Turn a database argument (DSN, path, alias or ``@server/database``) plus the connection options into a
    Target. ``--server``/``--database`` (registrations in the data dir) are used when ``spec`` is empty."""
    from . import registry

    use_flags = not prefix and not spec  # --server/--database stand in for the DATABASE argument
    server_ref = getattr(args, "server", None) if use_flags else None
    db_ref = getattr(args, "database", None) if use_flags else None
    if spec and spec.startswith("@"):
        server_ref, _, db_ref = spec[1:].partition("/")
        db_ref = db_ref or None
        spec = None
    explicit_pw = (password_from(args, prefix) if prefix else None) or password_from(args)
    user = getattr(args, prefix + "user", None) or (getattr(args, "user", None) if prefix else None)
    if server_ref:
        return registry.target(args, server_ref, db_ref, need_db=need_db, password=explicit_pw, user=user)
    if db_ref:
        raise CliError("--database needs --server (a registered server)", USAGE)
    if not spec and need_db:
        raise CliError("Give a database (host/port:path, a path or an alias) or --server/--database", USAGE)
    host, port, path = parse_dsn(spec) if spec else (None, None, None)
    host = host or getattr(args, "host", None) or os.environ.get("ISC_HOST") or "localhost"
    port = port or getattr(args, "port", None) or 3050
    user = user or os.environ.get("ISC_USER") or "SYSDBA"
    pw = explicit_pw or os.environ.get("ISC_PASSWORD")
    if not pw:
        raise CliError("No password: use --password-env NAME, set ISC_PASSWORD, or --password", USAGE)
    srv = make_server(host, int(port), user, args)
    label = f"{host}/{port}:{path}" if path else f"{host}/{port}"
    return Target(srv=srv, path=path, password=pw, label=label)


DB_HELP = ("host[/port]:path, inet://host:port/path, a path or alias (with --host), "
           "or @SERVER/DATABASE from the fbadmin registry")


def add_db_arg(p: argparse.ArgumentParser) -> None:
    p.add_argument("db", nargs="?", metavar="DATABASE", help=DB_HELP)


def add_connection_options(p: argparse.ArgumentParser, *, database: bool = True) -> None:
    """Connection options shared by every subcommand."""
    g = p.add_argument_group("connection")
    g.add_argument("--host", help="Firebird server (default $ISC_HOST or localhost)")
    g.add_argument("--port", type=int, help="port (default 3050)")
    g.add_argument("-u", "--user", help="user (default $ISC_USER or SYSDBA)")
    g.add_argument("-p", "--password", help="password (visible to other users in the process list: prefer "
                                            "--password-env or $ISC_PASSWORD)")
    g.add_argument("--password-env", metavar="NAME", help="read the password from this environment variable")
    g.add_argument("--role", help="SQL role")
    g.add_argument("--charset", help="connection character set (default UTF8)")
    g.add_argument("--auth-plugin", choices=("Srp256", "Srp", "Legacy_Auth"), help="authentication plugin")
    g.add_argument("--no-wire-crypt", action="store_true", help="disable wire encryption")
    g.add_argument("--wire-compress", action="store_true", help="enable wire compression")
    r = p.add_argument_group("fbadmin registry (saved servers)")
    r.add_argument("--data-dir", help="fbadmin data directory (default $FBADMIN_DATA_DIR or ~/.fbadmin)")
    r.add_argument("--server", metavar="NAME", help="registered server (name or id); also FBADMIN_SERVERS_FILE")
    if database:
        r.add_argument("--database", metavar="NAME", help="registered database of --server (name, id or path)")
    r.add_argument("--master-password-env", metavar="NAME",
                   help="environment variable holding the fbadmin master password, to decrypt saved passwords "
                        "(default FBADMIN_MASTER_PASSWORD)")


def add_output_options(p: argparse.ArgumentParser) -> None:
    g = p.add_argument_group("output")
    g.add_argument("--json", action="store_true", help="print the result as one JSON document on stdout")
    g.add_argument("-q", "--quiet", action="store_true", help="no progress messages")
    g.add_argument("-v", "--verbose", action="store_true", help="more detail (gbak output, statements)")
    g.add_argument("--debug", action="store_true", help="show a traceback on errors")
