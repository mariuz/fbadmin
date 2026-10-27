"""Local SQLite database (``<data_dir>/fbadmin.db``): fbadmin users and the audit log.

Created lazily, so desktop mode without auditing never writes it.
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT NOT NULL UNIQUE COLLATE NOCASE,
    email TEXT,
    password_hash TEXT,
    role TEXT NOT NULL DEFAULT 'user',
    active INTEGER NOT NULL DEFAULT 1,
    auth_source TEXT NOT NULL DEFAULT 'internal',
    vault TEXT,
    created_at REAL NOT NULL,
    last_login REAL
);
CREATE TABLE IF NOT EXISTS audit (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    username TEXT,
    client_ip TEXT,
    server_id TEXT,
    server_name TEXT,
    server_host TEXT,
    db_id TEXT,
    db_name TEXT,
    db_path TEXT,
    category TEXT,
    statement TEXT,
    success INTEGER NOT NULL,
    error TEXT,
    row_count INTEGER,
    elapsed_ms REAL
);
CREATE INDEX IF NOT EXISTS audit_ts ON audit(ts);
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
"""

USER_COLUMNS = ("id", "username", "email", "role", "active", "auth_source", "created_at", "last_login")




class Result:
    """The fetched rows of a statement, with the parts of a cursor that callers use."""

    def __init__(self, rows: list, lastrowid: int | None, rowcount: int, description):
        self._rows, self._i = rows, 0
        self.lastrowid, self.rowcount, self.description = lastrowid, rowcount, description

    def fetchone(self):
        if self._i >= len(self._rows):
            return None
        self._i += 1
        return self._rows[self._i - 1]

    def fetchall(self) -> list:
        rest, self._i = self._rows[self._i:], len(self._rows)
        return rest

    def fetchmany(self, size: int = 1) -> list:
        part = self._rows[self._i:self._i + size]
        self._i += len(part)
        return part

    def __iter__(self):
        return iter(self.fetchall())

def _wal(c: sqlite3.Connection, wait: float = 15.0) -> None:
    """Switch to WAL. Several processes opening a new database together (fbadmin workers starting at once)
    can get "database is locked" here at once: changing the journal mode doesn't always go through the busy
    timeout, so retry for a while (another process has usually switched it already)."""
    deadline = time.monotonic() + wait
    delay = 0.02
    while True:
        try:
            if str(c.execute("PRAGMA journal_mode").fetchone()[0]).lower() == "wal":
                return
            c.execute("PRAGMA journal_mode=WAL")
            return
        except sqlite3.OperationalError as exc:
            if "locked" not in str(exc).lower() and "busy" not in str(exc).lower() or time.monotonic() > deadline:
                raise
            time.sleep(delay)
            delay = min(delay * 2, 0.5)

class SecurityDB:
    def __init__(self, path: Path):
        self.path = path
        self._conn: sqlite3.Connection | None = None
        self._lock = threading.RLock()

    @property
    def exists(self) -> bool:
        return self.path.exists()

    def conn(self) -> sqlite3.Connection:
        with self._lock:
            if self._conn is None:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                new = not self.path.exists()
                c = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None)
                c.row_factory = sqlite3.Row
                _wal(c)
                c.executescript(SCHEMA)
                if "token" not in {r[1] for r in c.execute("PRAGMA table_info(audit)")}:
                    try:
                        c.execute("ALTER TABLE audit ADD COLUMN token TEXT")  # API token name (2.0)
                    except sqlite3.OperationalError as exc:  # another process added it meanwhile
                        if "duplicate column" not in str(exc).lower():
                            raise
                if new:
                    os.chmod(self.path, 0o600)
                self._conn = c
            return self._conn

    def execute(self, sql: str, params: tuple | list = ()) -> Result:
        """Run a statement and fetch its rows while holding the lock: the connection is shared by the request
        threads, and a cursor read after the lock is released can be reset by another thread's statement
        (fetchone() then returns None)."""
        with self._lock:
            cur = self.conn().execute(sql, params)
            return Result(cur.fetchall(), cur.lastrowid, cur.rowcount, cur.description)

    def close(self) -> None:
        with self._lock:
            if self._conn is not None:
                self._conn.close()
                self._conn = None

    # ------------------------------------------------------------- users
    def user_count(self) -> int:
        return self.execute("SELECT COUNT(*) FROM users").fetchone()[0]

    def list_users(self) -> list[dict[str, Any]]:
        rows = self.execute(f"SELECT {', '.join(USER_COLUMNS)}, vault IS NOT NULL AS has_vault "
                            "FROM users ORDER BY username").fetchall()
        return [_user(r) for r in rows]

    def get_user(self, user_id: int) -> dict[str, Any] | None:
        r = self.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
        return _user(r, full=True) if r else None

    def find_user(self, username: str) -> dict[str, Any] | None:
        r = self.execute("SELECT * FROM users WHERE username = ?", (username,)).fetchone()
        return _user(r, full=True) if r else None

    def create_user(self, username: str, *, email: str | None, password_hash: str | None, role: str,
                    auth_source: str, active: bool = True, vault: dict | None = None) -> dict[str, Any]:
        username = username.strip()
        if not username:
            raise ValueError("Username is required")
        if role not in ("admin", "user"):
            raise ValueError("Role must be 'admin' or 'user'")
        if self.find_user(username):
            raise ValueError(f"User {username!r} already exists")
        row = (username, email, password_hash, role, int(active), auth_source,
               json.dumps(vault) if vault else None, time.time())
        try:
            cur = self.execute(
                "INSERT INTO users (username, email, password_hash, role, active, auth_source, vault, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)", row)
        except sqlite3.IntegrityError:  # created by another request or process just now
            raise ValueError(f"User {username!r} already exists") from None
        return self.get_user(cur.lastrowid)  # type: ignore[return-value]

    def create_first_user(self, username: str, *, email: str | None, password_hash: str | None, role: str,
                          auth_source: str, vault: dict | None = None) -> dict[str, Any] | None:
        """Create a user only while there are none, atomically - also across processes sharing the data
        directory (``BEGIN IMMEDIATE`` takes SQLite's write lock). None: another request or process
        created the first user meanwhile (e.g. several workers bootstrapping the admin at startup)."""
        username = username.strip()
        if not username:
            raise ValueError("Username is required")
        with self._lock:
            c = self.conn()
            c.execute("BEGIN IMMEDIATE")
            try:
                if c.execute("SELECT COUNT(*) FROM users").fetchone()[0]:
                    c.execute("ROLLBACK")
                    return None
                cur = c.execute(
                    "INSERT INTO users (username, email, password_hash, role, active, auth_source, vault, created_at) "
                    "VALUES (?, ?, ?, ?, 1, ?, ?, ?)",
                    (username, email, password_hash, role, auth_source, json.dumps(vault) if vault else None,
                     time.time()))
                c.execute("COMMIT")
            except BaseException:
                c.execute("ROLLBACK")
                raise
            return self.get_user(cur.lastrowid)

    def update_user(self, user_id: int, **changes: Any) -> dict[str, Any]:
        allowed = {"email", "password_hash", "role", "active", "vault", "last_login", "auth_source"}
        sets, params = [], []
        for k, v in changes.items():
            if k not in allowed:
                continue
            if k == "role" and v not in ("admin", "user"):
                raise ValueError("Role must be 'admin' or 'user'")
            if k == "vault":
                v = json.dumps(v) if v else None
            if k == "active":
                v = int(bool(v))
            sets.append(f"{k} = ?")
            params.append(v)
        if sets:
            self.execute(f"UPDATE users SET {', '.join(sets)} WHERE id = ?", (*params, user_id))
        user = self.get_user(user_id)
        if user is None:
            raise KeyError(f"Unknown user {user_id}")
        return user

    def delete_user(self, user_id: int) -> None:
        self.execute("DELETE FROM users WHERE id = ?", (user_id,))

    def admin_count(self, exclude: int | None = None) -> int:
        return self.execute("SELECT COUNT(*) FROM users WHERE role = 'admin' AND active = 1 AND id <> ?",
                            (exclude or -1,)).fetchone()[0]

    # ------------------------------------------------------------- audit
    def add_audit(self, entry: dict[str, Any]) -> None:
        cols = ("ts", "username", "client_ip", "server_id", "server_name", "server_host", "db_id", "db_name",
                "db_path", "category", "statement", "success", "error", "row_count", "elapsed_ms", "token")
        self.execute(f"INSERT INTO audit ({', '.join(cols)}) VALUES ({', '.join('?' for _ in cols)})",
                     tuple(entry.get(c) for c in cols))

    def query_audit(self, *, limit: int = 100, offset: int = 0, username: str | None = None,
                    server: str | None = None, text: str | None = None, success: bool | None = None,
                    since: float | None = None, until: float | None = None,
                    category: str | None = None) -> dict[str, Any]:
        where, params = [], []
        if category:
            where.append("category = ?")
            params.append(category)
        if username:
            where.append("username = ?")
            params.append(username)
        if server:
            where.append("(server_id = ? OR server_name = ?)")
            params += [server, server]
        if text:
            where.append("(statement LIKE ? OR error LIKE ? OR db_name LIKE ? OR db_path LIKE ?)")
            params += [f"%{text}%"] * 4
        if success is not None:
            where.append("success = ?")
            params.append(int(success))
        if since:
            where.append("ts >= ?")
            params.append(since)
        if until:
            where.append("ts <= ?")
            params.append(until)
        w = (" WHERE " + " AND ".join(where)) if where else ""
        total = self.execute(f"SELECT COUNT(*) FROM audit{w}", params).fetchone()[0]
        rows = self.execute(f"SELECT * FROM audit{w} ORDER BY id DESC LIMIT ? OFFSET ?",
                            (*params, limit, offset)).fetchall()
        return {"total": total, "rows": [{**dict(r), "success": bool(r["success"])} for r in rows]}

    # ------------------------------------------------------------- meta
    def get_meta(self, key: str) -> str | None:
        r = self.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return r[0] if r else None

    def set_meta(self, key: str, value: str) -> None:
        self.execute("INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)", (key, value))


def _user(r: sqlite3.Row, full: bool = False) -> dict[str, Any]:
    d = dict(r)
    d["active"] = bool(d["active"])
    if full:
        d["vault"] = json.loads(d["vault"]) if d.get("vault") else None
    else:
        d["has_vault"] = bool(d.get("has_vault"))
    return d
