"""Servers and databases registered in an fbadmin data directory, for cron jobs on the fbadmin host.

Sources, in order:

* ``FBADMIN_SERVERS_FILE`` (preconfigured servers; passwords come from their ``password_env`` variable)
* ``<data dir>/servers.json`` (desktop mode). Saved passwords may be encrypted with the master password:
  give it with ``FBADMIN_MASTER_PASSWORD`` or ``--master-password-env NAME``.
* ``<data dir>/shared_servers.json`` (server mode: servers shared by the administrators). Their passwords are
  never stored in that file, so give one with ``--password-env``. Per-user registrations of server mode are
  encrypted with each user's login and are not available to the command line.
"""

from __future__ import annotations

import argparse
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .common import USAGE, CliError, Target


@dataclass
class Registration:
    srv: Any  # ServerEntry
    source: str  # "servers-file" | "servers.json" | "shared_servers.json"
    password_env: str | None = None


def data_dir(args: argparse.Namespace) -> Path:
    if getattr(args, "data_dir", None):
        return Path(args.data_dir).expanduser()
    from ..config import _default_data_dir

    return _default_data_dir()


def _server_mode(ddir: Path) -> bool:
    from ..security.settings import load

    try:
        return load(ddir).server_mode
    except Exception:  # noqa: BLE001 - unreadable auth.json: treat as desktop mode
        return False


def _load_json_servers(path: Path, source: str) -> list[Registration]:
    from ..store import ServerEntry

    if not path.is_file():
        return []
    try:
        raw = json.loads(path.read_text("utf-8") or "{}")
    except (OSError, ValueError) as exc:
        raise CliError(f"Cannot read {path}: {exc}", USAGE) from exc
    return [Registration(ServerEntry(**item), source) for item in raw.get("servers", [])]


def _load_servers_file() -> list[Registration]:
    path = os.environ.get("FBADMIN_SERVERS_FILE")
    if not path:
        return []
    from .. import preconfig
    from ..store import DatabaseEntry, ServerEntry

    try:
        specs = preconfig.load_file(Path(path))
    except Exception as exc:  # noqa: BLE001
        raise CliError(f"Cannot read FBADMIN_SERVERS_FILE {path}: {exc}", USAGE) from exc
    out = []
    for spec in specs:
        if not spec.get("name"):
            continue
        sid = preconfig.server_id(spec["name"])
        fields = {k: spec[k] for k in preconfig._FIELDS if k in spec}
        srv = ServerEntry(id=sid, name=spec["name"], save_password=False, password=None, **fields)
        for d in spec.get("databases") or []:
            p = d["path"] if isinstance(d, dict) else str(d)
            name = (d.get("name") if isinstance(d, dict) else None) or Path(p).stem or p
            srv.databases.append(DatabaseEntry(name=name, path=p))
        out.append(Registration(srv, "servers-file", spec.get("password_env")))
    return out


def load(args: argparse.Namespace) -> list[Registration]:
    ddir = data_dir(args)
    regs = _load_servers_file()
    if _server_mode(ddir):
        saved = _load_json_servers(ddir / "shared_servers.json", "shared_servers.json")
    else:
        saved = _load_json_servers(ddir / "servers.json", "servers.json")
    # the web app copies the servers file into its registry: the file itself wins
    ids = {r.srv.id for r in regs}
    return regs + [r for r in saved if r.srv.id not in ids]


def find(regs: list[Registration], ref: str) -> Registration:
    low = ref.lower()
    for match in (lambda r: r.srv.id == ref, lambda r: r.srv.name == ref, lambda r: r.srv.name.lower() == low):
        hits = [r for r in regs if match(r)]
        if len(hits) == 1:
            return hits[0]
        if len(hits) > 1:
            raise CliError(f"Several registered servers are called {ref!r}; use the id", USAGE)
    names = ", ".join(sorted(r.srv.name for r in regs)) or "none"
    raise CliError(f"No registered server {ref!r} (registered: {names}). See 'fbadmin servers'.", USAGE)


def find_database(srv: Any, ref: str | None) -> Any:
    if ref is None:
        if len(srv.databases) == 1:
            return srv.databases[0]
        raise CliError(f"Server {srv.name!r} has {len(srv.databases)} registered databases: choose one with "
                       "--database (or @SERVER/DATABASE)", USAGE)
    low = ref.lower()
    for match in (lambda d: d.id == ref, lambda d: d.name == ref, lambda d: d.name.lower() == low,
                  lambda d: d.path == ref):
        hits = [d for d in srv.databases if match(d)]
        if len(hits) == 1:
            return hits[0]
    names = ", ".join(d.name for d in srv.databases) or "none"
    raise CliError(f"Server {srv.name!r} has no registered database {ref!r} (registered: {names})", USAGE)


def _master_key(args: argparse.Namespace) -> bytes:
    from ..security.crypto import WrongPassword, vault_unlock

    var = getattr(args, "master_password_env", None) or "FBADMIN_MASTER_PASSWORD"
    master = os.environ.get(var)
    if not master:
        raise CliError("The saved password is encrypted with the fbadmin master password. Set "
                       f"{var} (or --master-password-env NAME), or give the Firebird password with --password-env.",
                       USAGE)
    f = data_dir(args) / "master_key.json"
    if not f.is_file():
        raise CliError(f"The saved password is encrypted but {f} is missing", USAGE)
    try:
        return vault_unlock(json.loads(f.read_text("utf-8")), master)
    except WrongPassword:
        raise CliError(f"Wrong fbadmin master password (from {var})", USAGE) from None


def saved_password(args: argparse.Namespace, reg: Registration) -> str | None:
    from ..security.crypto import decrypt, is_token

    srv = reg.srv
    if srv.embedded:
        from ..embedded import EMBEDDED_PASSWORD

        return EMBEDDED_PASSWORD
    if reg.password_env:
        return os.environ.get(reg.password_env)
    if is_token(srv.password):
        try:
            return decrypt(_master_key(args), srv.password, srv.id)
        except ValueError as exc:
            raise CliError(f"Cannot decrypt the saved password of {srv.name!r}: {exc}", USAGE) from exc
    return srv.password or None


def target(args: argparse.Namespace, server_ref: str, db_ref: str | None, *, need_db: bool,
           password: str | None, user: str | None) -> Target:
    reg = find(load(args), server_ref)
    pw = password or saved_password(args, reg) or os.environ.get("ISC_PASSWORD")
    srv = reg.srv.model_copy(update={"password": None})  # the password travels in Target.password only
    if user:
        srv = srv.model_copy(update={"user": user})
    for opt, field in (("role", "role"), ("charset", "charset"), ("auth_plugin", "auth_plugin")):
        if getattr(args, opt, None):
            srv = srv.model_copy(update={field: getattr(args, opt)})
    db = find_database(srv, db_ref) if (need_db or db_ref) else None
    if not pw:
        hint = (f"set the environment variable {reg.password_env}" if reg.password_env else
                "the registration has no saved password; use --password-env NAME or ISC_PASSWORD")
        raise CliError(f"No password for server {srv.name!r}: {hint}", USAGE)
    where = "embedded" if srv.embedded else f"{srv.host}/{srv.port}"
    label = f"{srv.name} / {db.name} ({where}:{db.path})" if db else f"{srv.name} ({where})"
    return Target(srv=srv, path=db.path if db else None, password=pw, label=label,
                  registered_paths=[d.path for d in srv.databases])


def describe(args: argparse.Namespace) -> list[dict[str, Any]]:
    """The registrations without passwords (``fbadmin servers``)."""
    from ..security.crypto import is_token

    out = []
    for r in load(args):
        s = r.srv
        pw = ("env " + r.password_env if r.password_env else "encrypted" if is_token(s.password)
              else "saved" if s.password else "none")
        out.append({"id": s.id, "name": s.name, "source": r.source, "host": s.host, "port": s.port, "user": s.user,
                    "embedded": s.embedded, "read_only": s.read_only, "password": pw,
                    "databases": [{"id": d.id, "name": d.name, "path": d.path} for d in s.databases]})
    return out
