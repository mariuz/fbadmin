"""Finer permissions for fbadmin users (server mode): per server, database and object.

The model (see docs/PERMISSIONS.md):

* A **grant** gives a *subject* (one fbadmin user, or a group of users) a *level* on a
  *server registration* (or ``*`` = every server), optionally narrowed to one *database*
  and to an *object name pattern* (``CUSTOMER``, ``SALES_*``; case-insensitive globs).
* Levels are ordered: ``none`` < ``read`` < ``write`` (DML) < ``ddl`` < ``admin`` (admin
  tools: backup, restore, shutdown, sweep, Firebird users, trace, maintenance...).
* For a request the **most specific** matching grant wins (object pattern before database
  before server, an exact server before ``*``); between grants that are equally specific
  (e.g. one for the user and one for their group) the higher level wins.
* fbadmin **administrators** are never restricted. A user **without any grant** keeps the
  behaviour of earlier versions (everything their server registrations allow) unless the
  administrator switches the default to "no access" - so upgrading changes nothing.
* **API tokens** cap the level (read-only tokens: ``read``) and can be limited to some
  server registrations; that applies to administrators' tokens too.

Enforcement is central: :class:`AccessScopedStore` hides servers and databases the user may
not see (so every endpoint answers 404 for them), :class:`AccessScopedManager` opens
read-only Firebird transactions where the user may only read, and
:func:`check_request` (called by the security middleware) maps each request to the level
it needs, classifying SQL statements (SELECT = read, DML = write, DDL = ddl).
"""

from __future__ import annotations

import fnmatch
import json
import re
import threading
import time
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import parse_qs

from ..connections import ConnectionManager, FbConnection, connect
from ..query import apply_tpb
from ..sqlsplit import split_sql, strip_comments
from .crypto import decrypt, is_token
from .readonly import category, is_read_statement
from .servers import UserScopedManager, UserScopedStore
from .sessions import Principal, current_principal

NONE, READ, WRITE, DDL, ADMIN = 0, 1, 2, 3, 4
LEVELS = ["none", "read", "write", "ddl", "admin"]
LEVEL_LABELS = {
    "none": "no access",
    "read": "read (queries)",
    "write": "write (queries and DML)",
    "ddl": "DDL (change metadata)",
    "admin": "admin tools (backup, restore, shutdown, users)",
}

SCHEMA = """
CREATE TABLE IF NOT EXISTS access_grants (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    subject_type TEXT NOT NULL,
    subject TEXT NOT NULL,
    server_id TEXT NOT NULL,
    db_id TEXT,
    object_pattern TEXT,
    level TEXT NOT NULL,
    note TEXT,
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS access_groups (
    name TEXT PRIMARY KEY COLLATE NOCASE,
    description TEXT
);
CREATE TABLE IF NOT EXISTS access_group_members (
    group_name TEXT NOT NULL COLLATE NOCASE,
    user_id INTEGER NOT NULL,
    PRIMARY KEY (group_name, user_id)
);
"""


def level_value(name: str) -> int:
    try:
        return LEVELS.index(name)
    except ValueError:
        raise ValueError(f"Unknown level {name!r}; use one of {', '.join(LEVELS)}") from None


@dataclass(frozen=True)
class Grant:
    id: int
    subject_type: str
    subject: str
    server_id: str
    db_id: str | None
    object_pattern: str | None
    level: int

    def matches(self, sid: str, dbid: str | None, obj: str | None) -> bool:
        if self.server_id not in ("*", sid):
            return False
        if self.db_id is not None and self.db_id != dbid:
            return False
        if self.object_pattern is not None:
            return obj is not None and _obj_match(self.object_pattern, obj)
        return True

    @property
    def specificity(self) -> tuple[int, int, int]:
        return (self.object_pattern is not None, self.db_id is not None, self.server_id != "*")


def _obj_match(pattern: str, name: str) -> bool:
    p = pattern.upper()
    n = name.upper()
    return fnmatch.fnmatchcase(n, p) or ("." in n and fnmatch.fnmatchcase(n.rsplit(".", 1)[1], p))


@dataclass
class Access:
    """What one principal may do (computed from the grants, cached per grants version)."""

    unrestricted: bool = True
    cap: int = ADMIN
    servers: frozenset[str] | None = None
    grants: list[Grant] = field(default_factory=list)

    @property
    def free(self) -> bool:
        """No check needed at all (desktop mode, admins and users without grants, full tokens)."""
        return self.unrestricted and self.cap == ADMIN and self.servers is None

    def _base(self, sid: str, dbid: str | None, obj: str | None) -> int:
        if self.unrestricted:
            return ADMIN
        best: tuple[int, int, int] | None = None
        lvl = NONE
        for g in self.grants:
            if not g.matches(sid, dbid, obj):
                continue
            spec = g.specificity
            if best is None or spec > best:
                best, lvl = spec, g.level
            elif spec == best:
                lvl = max(lvl, g.level)
        return lvl

    def level(self, sid: str, dbid: str | None = None, obj: str | None = None) -> int:
        if self.servers is not None and sid not in self.servers:
            return NONE
        return min(self.cap, self._base(sid, dbid, obj))

    def object_rules(self, sid: str, dbid: str) -> list[Grant]:
        if self.unrestricted:
            return []
        return [g for g in self.grants if g.object_pattern is not None and g.server_id in ("*", sid)
                and g.db_id in (None, dbid)]

    def db_visible(self, sid: str, dbid: str) -> bool:
        if self.servers is not None and sid not in self.servers:
            return False
        if self.level(sid, dbid) > NONE:
            return True
        return self.cap > NONE and any(g.level > NONE for g in self.object_rules(sid, dbid))

    def server_visible(self, sid: str, db_ids: list[str]) -> bool:
        if self.servers is not None and sid not in self.servers:
            return False
        return self.level(sid) > NONE or any(self.db_visible(sid, d) for d in db_ids)

    def hidden_objects(self, sid: str, dbid: str) -> bool:
        """True when some objects of the database are invisible to this principal, so tools that
        work on the whole database (DDL extract, compare, ERD...) must be refused."""
        rules = self.object_rules(sid, dbid)
        if not rules:
            return False
        return self.level(sid, dbid) == NONE or any(g.level == NONE for g in rules)

    def server_max(self, sid: str, db_ids: list[str]) -> int:
        lv = [self.level(sid)] + [self.level(sid, d) for d in db_ids]
        lv += [min(self.cap, g.level) for d in db_ids for g in self.object_rules(sid, d)]
        return max(lv)


FREE = Access()


class AccessControl:
    """Grants, groups and the default policy, stored in ``fbadmin.db``."""

    def __init__(self, sec):
        self.sec = sec
        self._lock = threading.RLock()
        self._ready = False
        self._version = 0
        self._cache: tuple[int, list[Grant], dict[int, set[str]], str] | None = None

    # ------------------------------------------------------------ storage
    def _db(self):
        db = self.sec.db
        if not self._ready:
            with self._lock:
                if not self._ready:
                    db.conn().executescript(SCHEMA)
                    self._ready = True
        return db

    def _changed(self) -> None:
        with self._lock:
            self._version += 1
            self._cache = None
        # connections opened with the old rights (read-only or not) must not be reused
        try:
            mgr = self.sec.state.manager
            if hasattr(mgr, "close_restricted"):
                mgr.close_restricted(self)
        except Exception:  # noqa: BLE001
            pass

    def _load(self) -> tuple[list[Grant], dict[int, set[str]], str]:
        with self._lock:
            if self._cache is not None and self._cache[0] == self._version:
                return self._cache[1], self._cache[2], self._cache[3]
            if not self.sec.db.exists:
                grants, members, default = [], {}, "full"
            else:
                db = self._db()
                grants = [Grant(r["id"], r["subject_type"], r["subject"], r["server_id"], r["db_id"],
                                r["object_pattern"], level_value(r["level"]))
                          for r in db.execute("SELECT * FROM access_grants ORDER BY id").fetchall()]
                members: dict[int, set[str]] = {}
                for r in db.execute("SELECT group_name, user_id FROM access_group_members").fetchall():
                    members.setdefault(int(r["user_id"]), set()).add(r["group_name"].lower())
                default = db.get_meta("access_default") or "full"
            self._cache = (self._version, grants, members, default)
            return grants, members, default

    @property
    def default(self) -> str:
        return self._load()[2]

    def set_default(self, value: str) -> None:
        if value not in ("full", "none"):
            raise ValueError("The default must be 'full' or 'none'")
        self._db().set_meta("access_default", value)
        self._changed()

    # ------------------------------------------------------------ evaluation
    def for_user(self, user_id: int, role: str) -> Access:
        grants, members, default = self._load()
        if role == "admin":
            return Access()
        groups = members.get(user_id, set())
        mine = [g for g in grants if (g.subject_type == "user" and g.subject == str(user_id))
                or (g.subject_type == "group" and g.subject.lower() in groups)]
        if not mine and default == "full":
            return Access()
        return Access(unrestricted=False, grants=mine)

    def for_principal(self, p: Principal | None) -> Access:
        if p is None or not self.sec.server_mode:
            return FREE
        acc = self.for_user(p.user_id, p.role)
        if p.token_id is not None:
            acc = Access(unrestricted=acc.unrestricted, grants=acc.grants,
                         cap=READ if p.token_read_only else ADMIN,
                         servers=frozenset(p.token_servers) if p.token_servers else None)
        return acc

    def current(self) -> Access:
        return self.for_principal(current_principal.get())

    # ------------------------------------------------------------ grants
    def list_grants(self) -> list[dict[str, Any]]:
        if not self.sec.db.exists:
            return []
        rows = self._db().execute("SELECT * FROM access_grants ORDER BY server_id, db_id, object_pattern, id")
        return [dict(r) for r in rows.fetchall()]

    def _check_subject(self, subject_type: str, subject: str) -> str:
        if subject_type == "user":
            user = self.sec.db.get_user(int(subject)) if str(subject).isdigit() else self.sec.db.find_user(subject)
            if user is None:
                raise ValueError(f"Unknown fbadmin user {subject!r}")
            return str(user["id"])
        if subject_type == "group":
            if self._db().execute("SELECT 1 FROM access_groups WHERE name = ?", (subject,)).fetchone() is None:
                raise ValueError(f"Unknown group {subject!r}")
            return subject
        raise ValueError("subject_type must be 'user' or 'group'")

    def _validate(self, data: dict[str, Any]) -> tuple:
        subject = self._check_subject(data["subject_type"], str(data["subject"]))
        level_value(data["level"])
        server_id = (data.get("server_id") or "*").strip()
        db_id = data.get("db_id") or None
        pattern = (data.get("object_pattern") or "").strip() or None
        if db_id and server_id == "*":
            raise ValueError("Choose a server for a database grant")
        return (data["subject_type"], subject, server_id, db_id, pattern, data["level"], data.get("note"))

    def _grant(self, grant_id: int) -> dict[str, Any]:
        row = self._db().execute("SELECT * FROM access_grants WHERE id = ?", (grant_id,)).fetchone()
        if row is None:
            raise KeyError(f"Unknown grant {grant_id}")
        return dict(row)

    def add_grant(self, data: dict[str, Any]) -> dict[str, Any]:
        cur = self._db().execute(
            "INSERT INTO access_grants (subject_type, subject, server_id, db_id, object_pattern, level, note, "
            "created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)", (*self._validate(data), time.time()))
        self._changed()
        return self._grant(cur.lastrowid)

    def update_grant(self, grant_id: int, data: dict[str, Any]) -> dict[str, Any]:
        merged = {**self._grant(grant_id), **data}
        self._db().execute(
            "UPDATE access_grants SET subject_type = ?, subject = ?, server_id = ?, db_id = ?, object_pattern = ?, "
            "level = ?, note = ? WHERE id = ?", (*self._validate(merged), grant_id))
        self._changed()
        return self._grant(grant_id)

    def delete_grant(self, grant_id: int) -> None:
        self._db().execute("DELETE FROM access_grants WHERE id = ?", (grant_id,))
        self._changed()

    def forget_user(self, user_id: int) -> None:
        if not self.sec.db.exists:
            return
        db = self._db()
        db.execute("DELETE FROM access_grants WHERE subject_type = 'user' AND subject = ?", (str(user_id),))
        db.execute("DELETE FROM access_group_members WHERE user_id = ?", (user_id,))
        self._changed()

    # ------------------------------------------------------------ groups
    def list_groups(self) -> list[dict[str, Any]]:
        if not self.sec.db.exists:
            return []
        db = self._db()
        out = []
        for g in db.execute("SELECT name, description FROM access_groups ORDER BY name").fetchall():
            ids = [r[0] for r in db.execute("SELECT user_id FROM access_group_members WHERE group_name = ? "
                                            "ORDER BY user_id", (g["name"],)).fetchall()]
            out.append({"name": g["name"], "description": g["description"], "members": ids})
        return out

    def save_group(self, name: str, description: str | None, members: list[int]) -> dict[str, Any]:
        name = (name or "").strip()
        if not name or len(name) > 64:
            raise ValueError("A group needs a name (up to 64 characters)")
        db = self._db()
        for uid in members:
            if self.sec.db.get_user(int(uid)) is None:
                raise ValueError(f"Unknown fbadmin user id {uid}")
        db.execute("INSERT INTO access_groups (name, description) VALUES (?, ?) "
                   "ON CONFLICT(name) DO UPDATE SET description = excluded.description", (name, description))
        db.execute("DELETE FROM access_group_members WHERE group_name = ?", (name,))
        for uid in sorted(set(int(u) for u in members)):
            db.execute("INSERT INTO access_group_members (group_name, user_id) VALUES (?, ?)", (name, uid))
        self._changed()
        return next(g for g in self.list_groups() if g["name"].lower() == name.lower())

    def delete_group(self, name: str) -> None:
        db = self._db()
        db.execute("DELETE FROM access_group_members WHERE group_name = ?", (name,))
        db.execute("DELETE FROM access_grants WHERE subject_type = 'group' AND subject = ?", (name,))
        db.execute("DELETE FROM access_groups WHERE name = ?", (name,))
        self._changed()

    # ------------------------------------------------------------ effective rights (UI)
    def effective(self, acc: Access, servers: list) -> dict[str, Any]:
        """Effective levels per server and database, for the "My access" view and the admin preview."""
        out = []
        for srv in servers:
            dbs = [d.id for d in srv.databases]
            if not acc.server_visible(srv.id, dbs):
                continue
            entry = {"server_id": srv.id, "server_name": srv.name, "shared": srv.shared, "read_only": srv.read_only,
                     "level": LEVELS[acc.level(srv.id)], "databases": []}
            for d in srv.databases:
                if not acc.db_visible(srv.id, d.id):
                    continue
                lvl = acc.level(srv.id, d.id)
                if srv.read_only:
                    lvl = min(lvl, READ)
                entry["databases"].append({
                    "db_id": d.id, "name": d.name, "level": LEVELS[lvl],
                    "objects": [{"pattern": g.object_pattern, "level": LEVELS[min(acc.cap, g.level)]}
                                for g in acc.object_rules(srv.id, d.id)],
                })
            out.append(entry)
        return {"restricted": not acc.unrestricted, "cap": LEVELS[acc.cap],
                "token_servers": sorted(acc.servers) if acc.servers is not None else None, "servers": out}


# ======================================================================= store / manager
class AccessScopedStore(UserScopedStore):
    """The per-user registry, filtered by the current principal's rights."""

    def _acc(self) -> Access:
        from ..api.deps import get_state

        return get_state().security.access.current()

    def _view(self, srv, acc: Access):
        if acc.free:
            return srv
        dbs = [d for d in srv.databases if acc.db_visible(srv.id, d.id)]
        ro = srv.read_only or acc.server_max(srv.id, [d.id for d in srv.databases]) < WRITE
        return srv.model_copy(update={"databases": dbs, "read_only": ro})

    def list(self):
        acc = self._acc()
        items = super().list()
        if acc.free:
            return items
        return [self._view(s, acc) for s in items if acc.server_visible(s.id, [d.id for d in s.databases])]

    def get(self, server_id: str):
        srv = super().get(server_id)
        acc = self._acc()
        if acc.free:
            return srv
        if not acc.server_visible(srv.id, [d.id for d in srv.databases]):
            raise KeyError(f"Unknown server {server_id!r}")
        return self._view(srv, acc)

    def get_database(self, server_id: str, db_id: str):
        db = super().get_database(server_id, db_id)
        acc = self._acc()
        if not acc.free and not acc.db_visible(server_id, db_id):
            raise KeyError(f"Unknown database {db_id!r}")
        return db

    def password_for(self, server_id: str) -> str | None:
        pw = super().password_for(server_id)
        p = current_principal.get()
        if pw or p is None or p.token_key is None:
            return pw
        # API token request while the user is signed out: the token carries a wrapped copy of the
        # user's vault key, so saved (encrypted) passwords work without unlocking the vault.
        own = self._own(server_id)
        if own is not None:
            value, aad = own._servers[server_id].password, server_id
        else:
            value, aad = self.user_store().shared_passwords.get(server_id), "shared:" + server_id
        if is_token(value):
            try:
                return decrypt(p.token_key, value, aad)
            except ValueError:
                return None
        return None

    def needs_unlock(self, server_id: str) -> bool:
        p = current_principal.get()
        if p is not None and p.token_id is not None:
            return False
        return super().needs_unlock(server_id)


class AccessConnectionManager(ConnectionManager):
    """Opens read-only Firebird transactions where the principal may only read."""

    def _read_only(self, server_id: str, db_id: str) -> bool:
        from ..api.deps import get_state

        acc = get_state().security.access.current()
        return not acc.free and acc.level(server_id, db_id) < WRITE

    def _open(self, server_id: str, db_id: str) -> FbConnection:
        srv = self.store.get(server_id)
        db = self.store.get_database(server_id, db_id)
        if not srv.read_only and self._read_only(server_id, db_id):
            srv = srv.model_copy(update={"read_only": True})
        return FbConnection(connect(srv, db.path, self._password(server_id)))

    def _open_meta(self, key: tuple[str, str]) -> FbConnection:
        if self._read_only(*key):
            return self._open(*key)  # the Python driver applies the read-only transaction
        return super()._open_meta(key)

    def open_session(self, server_id: str, db_id: str):
        sid, sess = super().open_session(server_id, db_id)
        if not sess.force_read_only and self._read_only(server_id, db_id):
            sess.force_read_only = sess.tx.read_only = True
            apply_tpb(sess.conn, sess.tx.tpb())
        return sid, sess


class AccessScopedManager(UserScopedManager):
    """One connection manager per user, and a separate one per API token (scripts never share
    query sessions or read-only state with the browser sessions of the same user)."""

    def __init__(self, store: UserScopedStore):
        super().__init__(store)
        self._factory = lambda: AccessConnectionManager(store)  # type: ignore[arg-type]
        self._owners: dict[Any, int] = {}

    def __getattr__(self, name: str):
        if name.startswith("_"):
            raise AttributeError(name)
        p = current_principal.get()
        if p is None:
            from .errors import LoginRequired

            raise LoginRequired()
        key: Any = p.user_id
        if p.token_id is not None:
            key = f"token:{p.token_id}"
            self._owners[key] = p.user_id
        return getattr(self.for_user(key), name)

    def close_user(self, user_id: int) -> None:
        super().close_user(user_id)
        for key in [k for k, uid in list(self._owners.items()) if uid == user_id]:
            self.close_token_key(key)

    def close_token(self, token_id: int) -> None:
        self.close_token_key(f"token:{token_id}")

    def close_token_key(self, key: Any) -> None:
        self._owners.pop(key, None)
        m = self.pop_user(key)  # type: ignore[arg-type]
        if m:
            m.close_all()

    def close_restricted(self, ac: AccessControl) -> None:
        """Grants changed: drop the connections of every non-administrator (they are reopened with
        the new rights on the next request)."""
        with self._ilock:
            keys = list(self._items)
        for key in keys:
            uid = self._owners.get(key, key)
            try:
                user = ac.sec.db.get_user(int(uid))
            except (TypeError, ValueError):
                user = None
            if user is None or user["role"] != "admin" or isinstance(key, str):
                m = self.pop_user(key)  # type: ignore[arg-type]
                if m:
                    m.close_all()


# ======================================================================= request checks
@dataclass
class Refusal:
    status: int
    detail: str
    code: str


@dataclass
class Need:
    """What a request needs: ``level`` on (server, database, object); ``whole_db`` = the tool
    reads the whole database, so hidden objects make it unavailable."""

    server_id: str
    db_id: str | None
    level: int
    obj: str | None = None
    whole_db: bool = False
    sql: str | None = None
    filter: str | None = None  # response filter: "objects" | "completion"


_DB_PATH = re.compile(r"^/api/servers/([^/]+)/databases/([^/]+)(/.*)?$")
_SRV_PATH = re.compile(r"^/api/servers/([^/]+)(/.*)?$")
_QSESS = re.compile(r"^/api/query/sessions/([^/]+)(?:/([^/]+))?$")
REGISTRY = -1  # marker: changes fbadmin's own server registry
VISIBLE = -2  # marker: only needs the server to be visible

# (methods, pattern on the path after /databases/{db_id}, level, object group / query or body key, flags)
_DB_RULES: list[tuple[str, re.Pattern[str], int, Any, str]] = [
    ("*", re.compile(r"^/(connect|disconnect)$"), READ, None, ""),
    ("W", re.compile(r"^/execute$"), READ, None, "sql"),
    ("GET", re.compile(r"^/objects/[^/]+$"), READ, None, "filter:objects"),
    ("GET", re.compile(r"^/objects/[^/]+/([^/]+)(?:/[^/]+)?$"), READ, 1, ""),
    ("DELETE", re.compile(r"^/objects/[^/]+/([^/]+)$"), DDL, 1, ""),
    ("GET", re.compile(r"^/tables/([^/]+)/(?:data|count)$"), READ, 1, ""),
    ("POST", re.compile(r"^/tables/([^/]+)/data$"), WRITE, 1, ""),
    ("GET", re.compile(r"^/completion$"), READ, None, "filter:completion"),
    ("GET", re.compile(r"^/designer/relations/([^/]+)$"), READ, 1, ""),
    ("GET", re.compile(r"^/designer/(?:comments/)?[^/]+/([^/]+)$"), READ, 1, ""),
    ("GET", re.compile(r"^/designer/lookups$"), READ, None, "whole"),
    ("GET", re.compile(r"^/schema/(?:extract|report|erd|search)$"), READ, None, "whole"),
    ("GET", re.compile(r"^/schema/export$"), READ, "q:table", "whole-if-missing"),
    ("POST", re.compile(r"^/schema/export$"), READ, "b:table", "whole-if-missing"),
    ("POST", re.compile(r"^/schema/(?:import|generate)$"), WRITE, "b:table", "whole-if-missing"),
    ("GET", re.compile(r"^/privileges"), READ, None, "whole"),
    ("POST", re.compile(r"^/(?:ddl|explain|statistics|search|completion|preview|params)$"), READ, None, ""),
    ("*", re.compile(r"^/event-monitor/post$"), WRITE, None, ""),
    ("*", re.compile(r"^/event-monitor(/|$)"), READ, None, ""),
    ("GET", re.compile(r"^/debug/routine$"), READ, "q:name", ""),
    ("GET", re.compile(r"^/debug/"), READ, None, ""),
    ("POST", re.compile(r"^/debug/parse$"), READ, None, ""),
    ("*", re.compile(r"^/debug/"), WRITE, None, ""),
    ("POST", re.compile(r"^/datacompare/[^/]+/apply$"), WRITE, None, "whole"),
    ("POST", re.compile(r"^/upgrade/check$"), READ, None, "whole"),
    ("POST", re.compile(r"^/monitoring/advisor$"), READ, None, ""),
    ("GET", re.compile(r"^/(?:maintenance|security)(/|$)"), ADMIN, None, ""),
    ("GET", re.compile(r".*"), READ, None, ""),
    ("*", re.compile(r".*"), ADMIN, None, ""),
]

# Endpoints outside /api/servers/... that need no database rights (or check them themselves).
_GLOBAL_ALLOWED = re.compile(
    r"^/api/(query/params|query/isolations|schema/uploads(/.*)?|upgrade/render|replication/conf|schedules(/.*)?"
    r"|jobs(/.*)?|datacompare/runs/[^/]+|access/me(/.*)?|auth/(logout|password|vault/.*|2fa(/.*)?))$")
_TOKEN_FORBIDDEN = re.compile(r"^/api/(auth/(?!status$)|access/me/|admin/tokens(/|$))")


def _method_ok(rule_method: str, method: str) -> bool:
    if rule_method == "*":
        return True
    if rule_method == "W":
        return method not in ("GET", "HEAD", "OPTIONS")
    return method == rule_method or (rule_method == "GET" and method == "HEAD")


def _jbody(body: bytes) -> dict[str, Any]:
    try:
        data = json.loads(body or b"{}")
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def needs_body(path: str) -> bool:
    return path.startswith(("/api/servers/", "/api/query/", "/api/datacompare/", "/api/schema/", "/api/ai/"))


def request_needs(st, method: str, path: str, query: str, body: bytes) -> list[Need] | Refusal | None:
    """Map a request to what it needs. None = nothing to check (not database-specific)."""
    m = _DB_PATH.match(path)
    if m and m.group(2) != "create":
        sid, dbid, rest = m.group(1), m.group(2), m.group(3) or ""  # the ASGI path is already decoded
        if rest == "" or rest == "/":
            return [Need(sid, None, REGISTRY)] if method not in ("GET", "HEAD") else [Need(sid, dbid, READ)]
        for rule_method, pat, level, objref, flags in _DB_RULES:
            mm = pat.match(rest)
            if not mm or not _method_ok(rule_method, method):
                continue
            need = Need(sid, dbid, level, whole_db=flags == "whole")
            if isinstance(objref, int):
                need.obj = mm.group(objref)
            elif isinstance(objref, str):
                src, key = objref.split(":")
                val = (parse_qs(query).get(key) or [None])[0] if src == "q" else _jbody(body).get(key)
                if val:
                    need.obj = str(val)
                elif flags == "whole-if-missing":
                    need.whole_db = True
            if flags == "sql":
                need.sql = str(_jbody(body).get("sql") or "")
            if flags.startswith("filter:"):
                need.filter = flags.split(":", 1)[1]
            return [need]
    m = _SRV_PATH.match(path)
    if m:
        sid, rest = m.group(1), m.group(2) or ""
        if method in ("GET", "HEAD") and rest in ("", "/info", "/maintenance/capabilities"):
            return [Need(sid, None, VISIBLE)]
        if method == "POST" and rest in ("/connect", "/disconnect"):
            return [Need(sid, None, VISIBLE)]
        if method == "PUT" and rest == "":
            keys = set(_jbody(body))
            return [Need(sid, None, VISIBLE if keys <= {"password", "save_password"} else REGISTRY)]
        if (method == "DELETE" and rest == "") or (method == "POST" and rest == "/databases") or \
                (method in ("PUT", "DELETE") and re.match(r"^/databases/[^/]+$", rest)):
            return [Need(sid, None, REGISTRY)]
        if method == "POST" and rest == "/backup":
            db = _jbody(body).get("database_id")
            return [Need(sid, str(db) if db else None, ADMIN)]
        return [Need(sid, None, ADMIN)]
    m = _QSESS.match(path)
    if m:
        try:
            sess = st.manager.session(m.group(1))
        except KeyError:
            return None  # the endpoint answers 404
        need = Need(sess.server_id, sess.db_id, READ)
        if m.group(2) == "execute" and method == "POST":
            need.sql = str(_jbody(body).get("sql") or "")
        return [need]
    b = _jbody(body) if method not in ("GET", "HEAD") else {}
    if path == "/api/query/sessions" and method == "POST":
        return [Need(str(b.get("server_id")), str(b.get("database_id")), READ)]
    if path in ("/api/datacompare/compare", "/api/datacompare/tables", "/api/schema/diff", "/api/schema/copy") \
            and method == "POST":
        out = []
        for side in ("source", "target"):
            ep = b.get(side) or {}
            if not isinstance(ep, dict) or not ep.get("server_id"):
                continue
            dbid = ep.get("db_id")
            if not dbid:  # schema diff against a script: a scratch database is created on the server
                out.append(Need(str(ep["server_id"]), None, ADMIN))
                continue
            lvl = DDL if (path == "/api/schema/copy" and side == "target") else READ
            out.append(Need(str(ep["server_id"]), str(dbid), lvl, whole_db=True))
        return out
    if path == "/api/ai/ask" and method == "POST" and b.get("server_id") and b.get("database_id"):
        return [Need(str(b["server_id"]), str(b["database_id"]), READ)]
    return None


def check_request(st, acc: Access, p: Principal, method: str, path: str, query: str,
                  body: bytes) -> tuple[Refusal | None, list[Need]]:
    """Refusal for this request (or None) and the needs (for response filters)."""
    unsafe = method not in ("GET", "HEAD", "OPTIONS")
    if p.token_id is not None and _TOKEN_FORBIDDEN.match(path):
        return Refusal(403, "API tokens cannot manage logins, two-factor authentication or tokens",
                       "token_forbidden"), []
    needs = request_needs(st, method, path, query, body)
    if isinstance(needs, Refusal):
        return needs, []
    if needs is None:
        if not unsafe or _GLOBAL_ALLOWED.match(path):
            return None, []
        if acc.unrestricted and acc.cap >= WRITE:
            return None, []  # e.g. admin endpoints: they check the role themselves
        if acc.cap < WRITE:
            return Refusal(403, "This API token is read-only", "token_read_only"), []
        return Refusal(403, "Your fbadmin permissions do not allow this - ask an administrator",
                       "permission_denied"), []
    for need in needs:
        r = _check_need(st, acc, need)
        if r:
            return r, needs
    return None, needs


def _deny(acc: Access, need: Need, what: str) -> Refusal:
    if acc.cap < need.level and acc.cap == READ:
        return Refusal(403, f"This API token is read-only ({what})", "token_read_only")
    return Refusal(403, f"Not allowed by your fbadmin permissions: {what} needs the "
                        f"'{LEVELS[need.level]}' level", "permission_denied")


def _check_need(st, acc: Access, need: Need) -> Refusal | None:
    sid, dbid = need.server_id, need.db_id
    try:
        srv = UserScopedStore.get(st.store, sid)  # unfiltered: tell "hidden" from "unknown"
    except (KeyError, AttributeError):
        try:
            srv = st.store.get(sid)
        except KeyError:
            return None  # unknown server: the endpoint answers 404
    db_ids = [d.id for d in srv.databases]
    if not acc.server_visible(sid, db_ids):
        return Refusal(404, f"Unknown server {sid!r}", "not_found")
    if need.level == VISIBLE:
        return None
    if need.level == REGISTRY:
        if acc.unrestricted and acc.cap >= WRITE and acc.servers is None:
            return None
        if acc.cap < WRITE:
            return Refusal(403, "This API token is read-only (server registrations)", "token_read_only")
        return Refusal(403, "Your access is limited by fbadmin permissions: only administrators change "
                            "these server registrations", "permission_denied")
    if dbid is None:
        if acc.level(sid) < need.level:
            return _deny(acc, need, "this server action")
        return None
    if dbid not in db_ids:
        return None  # unknown database: the endpoint answers 404
    if not acc.db_visible(sid, dbid):
        return Refusal(404, f"Unknown database {dbid!r}", "not_found")
    hidden = acc.hidden_objects(sid, dbid)
    if need.whole_db and hidden:
        return Refusal(403, "Not allowed by your fbadmin permissions: this tool reads the whole database, "
                            "and some of its objects are hidden from you", "permission_denied")
    if need.sql is not None:
        return _check_sql(acc, need, hidden)
    lvl = acc.level(sid, dbid, need.obj) if need.obj else acc.level(sid, dbid)
    if lvl < need.level:
        return _deny(acc, need, f"'{need.obj}'" if need.obj else "this database action")
    return None


# ----------------------------------------------------------------------- SQL statements
_IDENT = r'(?:"(?:[^"]|"")+"|[A-Za-z][\w$]*)'
_QNAME = rf"({_IDENT}(?:\s*\.\s*{_IDENT})?)"
_OBJ_TYPES = (r"(?:GLOBAL\s+TEMPORARY\s+)?TABLE|VIEW|PROCEDURE|FUNCTION|TRIGGER|PACKAGE(?:\s+BODY)?|SEQUENCE|"
              r"GENERATOR|(?:UNIQUE\s+)?(?:ASC(?:ENDING)?\s+|DESC(?:ENDING)?\s+)?INDEX|EXCEPTION|DOMAIN|ROLE|"
              r"MAPPING|USER|COLLATION|CHARACTER\s+SET|FILTER|EXTERNAL\s+FUNCTION|SCHEMA")
_TARGETS = [
    re.compile(rf"^(?:INSERT|MERGE)\s+INTO\s+{_QNAME}", re.I),
    re.compile(rf"^UPDATE\s+OR\s+INSERT\s+INTO\s+{_QNAME}", re.I),
    re.compile(rf"^UPDATE\s+{_QNAME}", re.I),
    re.compile(rf"^DELETE\s+FROM\s+{_QNAME}", re.I),
    re.compile(rf"^EXECUTE\s+PROCEDURE\s+{_QNAME}", re.I),
    re.compile(rf"^(?:CREATE\s+OR\s+ALTER|RECREATE|CREATE|ALTER|DROP)\s+(?:{_OBJ_TYPES})\s+(?:IF\s+(?:NOT\s+)?EXISTS\s+)?"
               rf"{_QNAME}", re.I),
    re.compile(rf"^COMMENT\s+ON\s+(?:{_OBJ_TYPES}|COLUMN|PARAMETER)\s+{_QNAME}", re.I),
    re.compile(rf"^SET\s+(?:GENERATOR|STATISTICS\s+INDEX)\s+{_QNAME}", re.I),
]
_SECOND_TARGETS = [  # tables an index or trigger belongs to, the object of a GRANT/REVOKE
    re.compile(rf"\bINDEX\s+{_IDENT}(?:\s*\.\s*{_IDENT})?\s+ON\s+{_QNAME}", re.I),
    re.compile(rf"^(?:CREATE\s+OR\s+ALTER|RECREATE|CREATE|ALTER)\s+TRIGGER\s+{_IDENT}\s+(?:\w+\s+){{0,6}}?FOR\s+{_QNAME}",
               re.I),
    re.compile(rf"^(?:GRANT|REVOKE)\b.*?\bON\s+(?:TABLE\s+|VIEW\s+|PROCEDURE\s+|FUNCTION\s+|PACKAGE\s+|"
               rf"SEQUENCE\s+|GENERATOR\s+|EXCEPTION\s+|DOMAIN\s+)?{_QNAME}", re.I | re.S),
]
_NOISE = re.compile(r"(\"(?:[^\"]|\"\")*\")|[qQ]'([(\[{<]|.).*?(?:[)\]}>]|\2)'|'(?:[^']|'')*'|--[^\n]*|/\*.*?\*/", re.S)


def _strip_noise(sql: str) -> str:
    """Remove string literals and comments (quoted identifiers stay)."""
    return _NOISE.sub(lambda m: m.group(1) or " ", sql)


def _norm(name: str) -> str:
    parts = re.findall(_IDENT, name)
    return ".".join(p[1:-1].replace('""', '"') if p.startswith('"') else p.upper() for p in parts)


_EXEC_BLOCK = re.compile(r"^EXECUTE\s+BLOCK\b", re.I)


def statement_level(sql: str) -> int:
    if _EXEC_BLOCK.match(strip_comments(sql).strip()):
        return DDL  # any PSQL, including EXECUTE STATEMENT '<any DDL>'
    cat = category(sql)
    if cat in ("QUERY", "TX"):
        return READ
    if cat == "DDL":
        return DDL
    if cat == "DML":
        return WRITE
    return READ if is_read_statement(sql) else DDL


def statement_targets(sql: str) -> list[str]:
    h = strip_comments(sql).strip()
    out = []
    for rx in _TARGETS:
        m = rx.match(h)
        if m:
            out.append(_norm(m.group(1)))
            break
    for rx in _SECOND_TARGETS:
        m = rx.search(h)
        if m:
            out.append(_norm(m.group(1)))
    return out


def identifiers(sql: str) -> set[str]:
    text = _strip_noise(sql)
    out = set()
    for m in re.finditer(rf"{_IDENT}(?:\s*\.\s*{_IDENT})?", text):
        out.add(_norm(m.group(0)))
    return out


def _check_sql(acc: Access, need: Need, has_object_rules_hidden: bool) -> Refusal | None:
    sid, dbid = need.server_id, need.db_id
    assert dbid is not None
    rules = acc.object_rules(sid, dbid)
    for st in split_sql(need.sql or ""):
        sql = st.sql
        req = statement_level(sql)
        targets = statement_targets(sql) if req > READ else []
        first = sql.strip().splitlines()[0][:120] if sql.strip() else ""
        if rules:
            for name in identifiers(sql):
                if any(_obj_match(g.object_pattern or "", name) for g in rules) and acc.level(sid, dbid, name) < READ:
                    return Refusal(403, f"Not allowed by your fbadmin permissions: {name} is hidden from you "
                                        f"(refused: {first})", "permission_denied")
        if targets:
            for name in targets:
                if acc.level(sid, dbid, name) < req:
                    return _deny(acc, Need(sid, dbid, req), f"{LEVELS[req].upper()} on {name} (refused: {first})")
        elif acc.level(sid, dbid) < req:
            return _deny(acc, Need(sid, dbid, req), f"this statement (refused: {first})")
    return None


# ----------------------------------------------------------------------- response filters
def filter_response(acc: Access, need: Need, data: Any) -> Any:
    sid, dbid = need.server_id, need.db_id
    if not dbid or not acc.object_rules(sid, dbid):
        return data

    def ok(name: Any) -> bool:
        return isinstance(name, str) and acc.level(sid, dbid, name) >= READ

    if need.filter == "objects" and isinstance(data, list):
        return [o for o in data if not isinstance(o, dict) or ok(o.get("name"))]
    if need.filter == "completion" and isinstance(data, dict):
        out = dict(data)
        if isinstance(data.get("relations"), dict):
            out["relations"] = {k: v for k, v in data["relations"].items() if ok(k)}
        for key in ("procedures", "functions"):
            if isinstance(data.get(key), list):
                out[key] = [n for n in data[key] if ok(n)]
        if isinstance(data.get("packages"), dict):
            out["packages"] = {k: v for k, v in data["packages"].items() if ok(k)}
        return out
    return data


def require_level(server_id: str, db_id: str | None, level: int, what: str) -> None:
    """For endpoints that check rights themselves (schedules): raise AccessDenied below ``level``."""
    from ..api.deps import get_state
    from .errors import AccessDenied

    acc = get_state().security.access.current()
    if acc.free:
        return
    have = acc.level(server_id, db_id) if db_id else acc.level(server_id)
    if have < level:
        if acc.cap < level and acc.cap == READ:
            raise AccessDenied(f"This API token is read-only ({what})")
        raise AccessDenied(f"Not allowed by your fbadmin permissions: {what} needs the '{LEVELS[level]}' level")
