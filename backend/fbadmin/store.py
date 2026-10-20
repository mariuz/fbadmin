"""Persistent registry of servers and databases (pgAdmin's "Servers" tree).

Stored as JSON in ``<data_dir>/servers.json`` with 0600 permissions.
Passwords are only written when the user ticks "save password".
"""

from __future__ import annotations

import json
import os
import threading
import uuid
from pathlib import Path

from pydantic import BaseModel, Field


def _configured_password(server_id: str) -> str | None:
    """Password of a server from FBADMIN_SERVERS_FILE (read from its environment variable)."""
    from .preconfig import password_for

    return password_for(server_id)


def _new_id() -> str:
    return uuid.uuid4().hex[:12]


class DatabaseEntry(BaseModel):
    id: str = Field(default_factory=_new_id)
    name: str
    path: str  # file path on the server or alias from databases.conf


class ServerEntry(BaseModel):
    id: str = Field(default_factory=_new_id)
    name: str
    host: str = "localhost"
    port: int = 3050
    user: str = "SYSDBA"
    password: str | None = None
    save_password: bool = True
    role: str | None = None
    charset: str = "UTF8"
    wire_crypt: bool = True
    # --- security options (milestone 0.4)
    wire_compress: bool = False
    auth_plugin: str | None = None  # Srp256 | Srp | Legacy_Auth (None = driver default)
    read_only: bool = False  # block everything but SELECT on this server
    shared: bool = False  # server mode: visible to every fbadmin user, managed by admins
    group: str = "Servers"
    # local database files opened by the embedded engine in this process (see embedded.py)
    embedded: bool = False
    databases: list[DatabaseEntry] = Field(default_factory=list)

    def public(self) -> dict:
        data = self.model_dump(exclude={"password"})
        data["has_password"] = bool(self.password)
        return data


class ServerStore:
    def __init__(self, path: Path):
        self.path = path
        self._lock = threading.RLock()
        self._servers: dict[str, ServerEntry] = {}
        # Passwords typed at connect time but not saved to disk.
        self._session_passwords: dict[str, str] = {}
        self._load()

    def _load(self) -> None:
        if self.path.exists():
            raw = json.loads(self.path.read_text("utf-8") or "{}")
            for item in raw.get("servers", []):
                srv = ServerEntry(**item)
                self._servers[srv.id] = srv

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        out = []
        for srv in self._servers.values():
            data = srv.model_dump()
            if not srv.save_password:
                data["password"] = None
            out.append(data)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"servers": out}, indent=2), "utf-8")
        os.chmod(tmp, 0o600)
        tmp.replace(self.path)

    # --- servers -------------------------------------------------------
    def list(self) -> list[ServerEntry]:
        with self._lock:
            return list(self._servers.values())

    def get(self, server_id: str) -> ServerEntry:
        with self._lock:
            try:
                return self._servers[server_id]
            except KeyError:
                raise KeyError(f"Unknown server {server_id!r}") from None

    def add(self, srv: ServerEntry) -> ServerEntry:
        with self._lock:
            self._servers[srv.id] = srv
            if srv.password and not srv.save_password:
                self._session_passwords[srv.id] = srv.password
            self._save()
            return srv

    def update(self, server_id: str, changes: dict) -> ServerEntry:
        with self._lock:
            srv = self.get(server_id)
            data = srv.model_dump()
            for k, v in changes.items():
                if k == "password" and v in (None, ""):
                    continue  # keep existing password unless a new one is given
                if k in data and k not in ("id", "databases"):
                    data[k] = v
            new = ServerEntry(**data)
            self._servers[server_id] = new
            self._save()
            return new

    def remove(self, server_id: str) -> None:
        with self._lock:
            self._servers.pop(server_id, None)
            self._session_passwords.pop(server_id, None)
            self._save()

    def password_for(self, server_id: str) -> str | None:
        with self._lock:
            srv = self.get(server_id)
            if srv.embedded:  # the embedded engine does not authenticate
                from .embedded import EMBEDDED_PASSWORD

                return EMBEDDED_PASSWORD
            return srv.password or self._session_passwords.get(server_id) or _configured_password(server_id)

    def remember_password(self, server_id: str, password: str) -> None:
        with self._lock:
            self._session_passwords[server_id] = password

    # --- databases -----------------------------------------------------
    def add_database(self, server_id: str, db: DatabaseEntry) -> DatabaseEntry:
        with self._lock:
            srv = self.get(server_id)
            srv.databases.append(db)
            self._save()
            return db

    def get_database(self, server_id: str, db_id: str) -> DatabaseEntry:
        srv = self.get(server_id)
        for db in srv.databases:
            if db.id == db_id:
                return db
        raise KeyError(f"Unknown database {db_id!r}")

    def update_database(self, server_id: str, db_id: str, changes: dict) -> DatabaseEntry:
        with self._lock:
            db = self.get_database(server_id, db_id)
            for k in ("name", "path"):
                if changes.get(k):
                    setattr(db, k, changes[k])
            self._save()
            return db

    def remove_database(self, server_id: str, db_id: str) -> None:
        with self._lock:
            srv = self.get(server_id)
            srv.databases = [d for d in srv.databases if d.id != db_id]
            self._save()
