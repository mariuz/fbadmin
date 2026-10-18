"""Server registries with encrypted passwords, per-user lists and shared servers.

* :class:`EncryptedServerStore` - ``servers.json`` whose saved passwords are
  AES-GCM tokens (``fbenc:v1:...``) once a master password is set. Plaintext
  passwords from older files are encrypted the first time the vault is unlocked.
* :class:`UserScopedStore` (server mode) - each fbadmin user has their own
  ``users/<id>/servers.json``; ``shared_servers.json`` holds servers that admins
  share with everybody. Passwords of shared servers are never shared: each user
  keeps their own (in memory, or saved encrypted in their own file).
* :class:`UserScopedManager` / :class:`UserScopedJobs` - one connection manager
  and one job list per fbadmin user, so query sessions, metadata connections and
  background jobs are never visible to other users.
"""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from typing import Any

from ..connections import ConnectionManager
from ..jobs import JobManager
from ..store import DatabaseEntry, ServerEntry, ServerStore, _configured_password
from .crypto import decrypt, encrypt, is_token
from .errors import AccessDenied, LoginRequired, MasterPasswordRequired
from .sessions import Principal, current_principal


def _require_admin(p: Principal, what: str = "Shared servers are managed by fbadmin administrators") -> None:
    if not p.is_admin:
        raise AccessDenied(what)


class EncryptedServerStore(ServerStore):
    def __init__(self, path: Path, encrypted: bool = False):
        self.key: bytes | None = None
        self.encrypted = encrypted
        # passwords of *shared* servers saved by this user: server id -> token (or plaintext if not encrypted)
        self.shared_passwords: dict[str, str] = {}
        self._shared_session: dict[str, str] = {}
        super().__init__(path)

    # --- persistence ---------------------------------------------------
    def _load(self) -> None:
        if self.path.exists():
            raw = json.loads(self.path.read_text("utf-8") or "{}")
            for item in raw.get("servers", []):
                srv = ServerEntry(**item)
                self._servers[srv.id] = srv
            self.shared_passwords = dict(raw.get("shared_passwords") or {})

    def _protect(self, value: str, aad: str) -> str:
        if not self.encrypted or is_token(value):
            return value
        if self.key is None:
            return value  # legacy plaintext loaded while locked: encrypted on unlock
        return encrypt(self.key, value, aad)

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        out = []
        for srv in self._servers.values():
            if srv.save_password and srv.password:
                srv.password = self._protect(srv.password, srv.id)
            data = srv.model_dump()
            if not srv.save_password:
                data["password"] = None
            out.append(data)
        doc: dict[str, Any] = {"servers": out}
        for sid, pw in list(self.shared_passwords.items()):
            self.shared_passwords[sid] = self._protect(pw, "shared:" + sid)
        if self.shared_passwords:
            doc["shared_passwords"] = self.shared_passwords
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(doc, indent=2), "utf-8")
        os.chmod(tmp, 0o600)
        tmp.replace(self.path)

    # --- vault ----------------------------------------------------------
    @property
    def locked(self) -> bool:
        return self.encrypted and self.key is None

    def _check_can_save(self, password: str | None, save: bool) -> None:
        if password and save and self.locked:
            raise MasterPasswordRequired(None)

    def plaintext_count(self) -> int:
        n = sum(1 for s in self._servers.values() if s.save_password and s.password and not is_token(s.password))
        return n + sum(1 for v in self.shared_passwords.values() if not is_token(v))

    def unlock(self, key: bytes) -> None:
        """Use ``key`` from now on; encrypts any plaintext password left from older files."""
        with self._lock:
            self.key = key
            if self.encrypted and self.plaintext_count():
                self._save()

    def lock(self) -> None:
        with self._lock:
            self.key = None

    def enable_encryption(self, key: bytes) -> int:
        """Turn on encryption and migrate existing plaintext passwords. Returns how many were migrated."""
        with self._lock:
            n = self.plaintext_count()
            self.encrypted = True
            self.key = key
            self._save()
            return n

    def disable_encryption(self) -> None:
        """Store passwords in plaintext again (needs the key)."""
        with self._lock:
            if self.locked:
                raise MasterPasswordRequired(None)
            for srv in self._servers.values():
                if is_token(srv.password):
                    srv.password = decrypt(self.key, srv.password, srv.id)  # type: ignore[arg-type]
            for sid, v in list(self.shared_passwords.items()):
                if is_token(v):
                    self.shared_passwords[sid] = decrypt(self.key, v, "shared:" + sid)  # type: ignore[arg-type]
            self.encrypted = False
            self.key = None
            self._save()

    def forget_encrypted(self) -> int:
        """Drop every encrypted password (forgotten master password). Returns how many were dropped."""
        with self._lock:
            n = 0
            for srv in self._servers.values():
                if is_token(srv.password):
                    srv.password = None
                    n += 1
            for sid in [k for k, v in self.shared_passwords.items() if is_token(v)]:
                self.shared_passwords.pop(sid)
                n += 1
            self.encrypted = False
            self.key = None
            self._save()
            return n

    def _reveal(self, value: str | None, aad: str) -> str | None:
        if not is_token(value):
            return value
        if self.key is None:
            return None
        try:
            return decrypt(self.key, value, aad)  # type: ignore[arg-type]
        except ValueError:
            return None

    # --- overrides -----------------------------------------------------
    def add(self, srv: ServerEntry) -> ServerEntry:
        self._check_can_save(srv.password, srv.save_password)
        return super().add(srv)

    def update(self, server_id: str, changes: dict) -> ServerEntry:
        with self._lock:
            srv = self.get(server_id)
            save = changes.get("save_password", srv.save_password)
            self._check_can_save(changes.get("password"), bool(save))
            return super().update(server_id, changes)

    def password_for(self, server_id: str) -> str | None:
        with self._lock:
            srv = self.get(server_id)
            return (self._reveal(srv.password, srv.id) or self._session_passwords.get(server_id)
                    or _configured_password(server_id))

    def needs_unlock(self, server_id: str) -> bool:
        with self._lock:
            srv = self._servers.get(server_id)
            if srv is None:
                return self.locked and is_token(self.shared_passwords.get(server_id)) \
                    and server_id not in self._shared_session
            return self.locked and is_token(srv.password) and server_id not in self._session_passwords

    # --- this user's passwords for shared servers ----------------------
    def shared_password_for(self, server_id: str) -> str | None:
        with self._lock:
            return self._shared_session.get(server_id) or self._reveal(
                self.shared_passwords.get(server_id), "shared:" + server_id)

    def set_shared_password(self, server_id: str, password: str | None, save: bool) -> None:
        with self._lock:
            if not password:
                if not save:
                    self.shared_passwords.pop(server_id, None)
                    self._save()
                return
            if save:
                self._check_can_save(password, True)
                self.shared_passwords[server_id] = password
                self._save()
            else:
                self.shared_passwords.pop(server_id, None)
                self._shared_session[server_id] = password
                self._save()

    def remember_shared_password(self, server_id: str, password: str) -> None:
        with self._lock:
            self._shared_session[server_id] = password


# ---------------------------------------------------------------- server mode
class UserScopedStore:
    """Same interface as ServerStore, scoped to the fbadmin user of the current request."""

    PASSWORD_KEYS = ("password", "save_password")

    def __init__(self, data_dir: Path):
        self.data_dir = data_dir
        self.shared = EncryptedServerStore(data_dir / "shared_servers.json")
        self._stores: dict[int, EncryptedServerStore] = {}
        self._lock = threading.RLock()

    # --- helpers -------------------------------------------------------
    def principal(self) -> Principal:
        p = current_principal.get()
        if p is None:
            raise LoginRequired()
        return p

    def user_path(self, user_id: int) -> Path:
        return self.data_dir / "users" / str(int(user_id)) / "servers.json"

    def user_store(self, user_id: int | None = None) -> EncryptedServerStore:
        uid = self.principal().user_id if user_id is None else user_id
        with self._lock:
            st = self._stores.get(uid)
            if st is None:
                st = EncryptedServerStore(self.user_path(uid), encrypted=True)
                self._stores[uid] = st
            return st

    def drop_user(self, user_id: int) -> None:
        with self._lock:
            self._stores.pop(user_id, None)

    def _own(self, server_id: str) -> EncryptedServerStore | None:
        st = self.user_store()
        return st if server_id in st._servers else None

    def _shared_entry(self, server_id: str) -> ServerEntry | None:
        return self.shared._servers.get(server_id)

    # --- servers -------------------------------------------------------
    def list(self) -> list[ServerEntry]:
        return self.user_store().list() + self.shared.list()

    def get(self, server_id: str) -> ServerEntry:
        own = self._own(server_id)
        if own:
            return own.get(server_id)
        return self.shared.get(server_id)

    def add(self, srv: ServerEntry) -> ServerEntry:
        p = self.principal()
        if not srv.shared:
            return self.user_store().add(srv)
        _require_admin(p)
        pw, save = srv.password, srv.save_password
        entry = srv.model_copy(update={"password": None})
        if pw:
            self.user_store().set_shared_password(entry.id, pw, save)
        return self.shared.add(entry)

    def update(self, server_id: str, changes: dict) -> ServerEntry:
        p = self.principal()
        own = self._own(server_id)
        if own:
            if changes.get("shared"):
                _require_admin(p)
                srv = own.get(server_id)
                pw = changes.get("password") or own.password_for(server_id)
                save = changes.get("save_password", srv.save_password)
                data = srv.model_dump()
                data.update({k: v for k, v in changes.items() if k in data and k not in ("id", "databases")})
                data.update(shared=True, password=None)
                own.remove(server_id)
                new = self.shared.add(ServerEntry(**data))
                if pw:
                    own.set_shared_password(server_id, pw, bool(save))
                return new
            return own.update(server_id, changes)
        srv = self.shared.get(server_id)
        st = self.user_store()
        if any(k in changes for k in self.PASSWORD_KEYS):
            save = changes.get("save_password", server_id in st.shared_passwords)
            if changes.get("password"):
                st.set_shared_password(server_id, changes["password"], bool(save))
            elif "save_password" in changes and not save:
                st.set_shared_password(server_id, None, False)
        other = {k: v for k, v in changes.items() if k not in self.PASSWORD_KEYS and getattr(srv, k, v) != v}
        if not other:
            return srv
        _require_admin(p)
        if other.get("shared") is False:
            data = srv.model_dump()
            data.update({k: v for k, v in other.items() if k in data and k not in ("id", "databases")})
            data.update(shared=False, password=None)
            self.shared.remove(server_id)
            return st.add(ServerEntry(**data))
        return self.shared.update(server_id, other)

    def remove(self, server_id: str) -> None:
        own = self._own(server_id)
        if own:
            own.remove(server_id)
            return
        if self._shared_entry(server_id) is None:
            raise KeyError(f"Unknown server {server_id!r}")
        _require_admin(self.principal())
        self.shared.remove(server_id)

    def password_for(self, server_id: str) -> str | None:
        own = self._own(server_id)
        if own:
            return own.password_for(server_id)
        self.shared.get(server_id)  # KeyError if unknown
        return self.user_store().shared_password_for(server_id) or _configured_password(server_id)

    def needs_unlock(self, server_id: str) -> bool:
        return self.user_store().needs_unlock(server_id)

    def remember_password(self, server_id: str, password: str) -> None:
        own = self._own(server_id)
        if own:
            own.remember_password(server_id, password)
        else:
            self.shared.get(server_id)
            self.user_store().remember_shared_password(server_id, password)

    # --- databases -----------------------------------------------------
    def _db_target(self, server_id: str) -> ServerStore:
        own = self._own(server_id)
        if own:
            return own
        self.shared.get(server_id)
        _require_admin(self.principal())
        return self.shared

    def add_database(self, server_id: str, db: DatabaseEntry) -> DatabaseEntry:
        return self._db_target(server_id).add_database(server_id, db)

    def get_database(self, server_id: str, db_id: str) -> DatabaseEntry:
        own = self._own(server_id)
        return (own or self.shared).get_database(server_id, db_id)

    def update_database(self, server_id: str, db_id: str, changes: dict) -> DatabaseEntry:
        return self._db_target(server_id).update_database(server_id, db_id, changes)

    def remove_database(self, server_id: str, db_id: str) -> None:
        self._db_target(server_id).remove_database(server_id, db_id)


class _PerUser:
    """Routes attribute access to the object of the current fbadmin user."""

    def __init__(self, factory):
        self._factory = factory
        self._items: dict[int, Any] = {}
        self._ilock = threading.Lock()

    def for_user(self, user_id: int):
        with self._ilock:
            obj = self._items.get(user_id)
            if obj is None:
                obj = self._items[user_id] = self._factory()
            return obj

    def all(self) -> list[Any]:
        with self._ilock:
            return list(self._items.values())

    def pop_user(self, user_id: int):
        with self._ilock:
            return self._items.pop(user_id, None)

    def __getattr__(self, name: str):
        if name.startswith("_"):
            raise AttributeError(name)
        p = current_principal.get()
        if p is None:
            raise LoginRequired()
        return getattr(self.for_user(p.user_id), name)


class UserScopedManager(_PerUser):
    def __init__(self, store: UserScopedStore):
        super().__init__(lambda: ConnectionManager(store))  # type: ignore[arg-type]
        self.store = store
        self.on_reap = None  # called by the idle reaper (expires login sessions)

    def reap_idle(self, max_idle: int) -> int:
        if self.on_reap:
            self.on_reap()
        return sum(m.reap_idle(max_idle) for m in self.all())

    def close_all(self) -> None:
        for m in self.all():
            m.close_all()

    def close_user(self, user_id: int) -> None:
        m = self.pop_user(user_id)
        if m:
            m.close_all()

    def disconnect_everywhere(self, server_id: str) -> None:
        for m in self.all():
            m.disconnect(server_id)


class UserScopedJobs(_PerUser):
    def __init__(self):
        super().__init__(JobManager)
