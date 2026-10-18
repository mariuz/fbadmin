"""Preconfigured servers: ``FBADMIN_SERVERS_FILE``.

For deployments (Docker, Kubernetes, a Firebird operator) the Firebird servers can be declared
in a JSON or YAML file instead of being registered by hand:

.. code-block:: yaml

    servers:
      - name: Production
        host: firebird.db.svc.cluster.local   # e.g. the Service an operator created
        port: 3050
        user: SYSDBA
        password_env: FIREBIRD_PASSWORD       # read from the environment (a Kubernetes Secret)
        read_only: false
        databases:
          - {name: employee, path: employee}  # alias or file path on the server

At startup each entry is registered (or updated) with a stable id ``cfg-<name>``. Passwords are
never written to disk: they are read from the named environment variable whenever fbadmin
connects. In server mode the servers are shared (visible to every user, managed by the file).
Databases and settings registered by hand are left alone; servers removed from the file are
removed from the registry on the next start.
"""

from __future__ import annotations

import json
import logging
import os
import re
from pathlib import Path
from typing import Any

from .store import DatabaseEntry, ServerEntry

log = logging.getLogger("fbadmin.preconfig")

PREFIX = "cfg-"
_password_env: dict[str, str] = {}  # server id -> environment variable holding its password
_FIELDS = ("host", "port", "user", "role", "charset", "wire_crypt", "wire_compress", "auth_plugin",
           "read_only", "group")


def server_id(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-") or "server"
    return PREFIX + slug


def password_for(sid: str) -> str | None:
    var = _password_env.get(sid)
    return os.environ.get(var) if var else None


def load_file(path: Path) -> list[dict[str, Any]]:
    text = path.read_text("utf-8")
    if path.suffix.lower() in (".yaml", ".yml"):
        import yaml  # PyYAML

        data = yaml.safe_load(text) or {}
    else:
        data = json.loads(text or "{}")
    servers = data.get("servers", data) if isinstance(data, dict) else data
    if not isinstance(servers, list):
        raise ValueError(f"{path}: expected a list of servers under 'servers'")
    return servers


def apply(store: Any, specs: list[dict[str, Any]], *, shared: bool = False) -> list[str]:
    """Register/update the configured servers in ``store`` (a ServerStore); returns their ids."""
    ids = []
    for spec in specs:
        if not spec.get("name"):
            raise ValueError("every preconfigured server needs a name")
        sid = server_id(spec["name"])
        ids.append(sid)
        if spec.get("password_env"):
            _password_env[sid] = spec["password_env"]
        fields = {k: spec[k] for k in _FIELDS if k in spec}
        dbs = spec.get("databases") or []
        existing = store._servers.get(sid)
        if existing is None:
            srv = ServerEntry(id=sid, name=spec["name"], save_password=False, shared=shared, password=None, **fields)
        else:
            srv = existing.model_copy(update={"name": spec["name"], "shared": shared, "password": None,
                                              "save_password": False, **fields})
        known = {d.path: d for d in srv.databases}
        for d in dbs:
            path = d["path"] if isinstance(d, dict) else str(d)
            name = (d.get("name") if isinstance(d, dict) else None) or Path(path).stem or path
            if path in known:
                known[path].name = name
            else:
                entry = DatabaseEntry(id=f"{sid}-{re.sub(r'[^a-z0-9]+', '-', name.lower()).strip('-')}", name=name, path=path)
                srv.databases.append(entry)
                known[path] = entry
        with store._lock:
            store._servers[sid] = srv
            store._save()
    # servers that were removed from the file
    with store._lock:
        stale = [k for k in store._servers if k.startswith(PREFIX) and k not in ids]
        for k in stale:
            store._servers.pop(k, None)
            _password_env.pop(k, None)
        if stale:
            store._save()
    return ids


def apply_from_env(state: Any) -> list[str]:
    path = os.environ.get("FBADMIN_SERVERS_FILE")
    if not path:
        return []
    specs = load_file(Path(path))
    target = getattr(state.store, "shared", None)  # server mode: the shared registry
    ids = apply(target if target is not None else state.store, specs, shared=target is not None)
    log.info("preconfigured %d server(s) from %s", len(ids), path)
    return ids
