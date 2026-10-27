"""Replication setup wizard (roadmap 2.2): primary and replica end to end, then a check that changes arrive.

What fbadmin can do over the wire: publication (SQL), the copy of the primary (nbackup through the Services
API), the replica mode (Services API) and the check. What it can't: ``replication.conf`` and the journal
directories live on the servers. fbadmin generates the blocks, writes them when the file is reachable from
fbadmin (``write_conf``), and checks that the primary's block is active (its replication sequence moves once a
change is journaled) before it copies anything.

Found with Firebird 4 and 5:

* The replica is a physical copy, ``nbackup -B 0`` restored with ``nbackup -SEQ``: it keeps the primary's GUID,
  record formats and replication sequence, so the replica applies exactly the segments written after the copy.
  A gbak copy doesn't work: the replica applies changes by physical record format, and gbak may lay a table
  out differently ("Record format with length 144 is not found for table ..." stops the replica), and it
  starts over at the oldest archived segment.
* asynchronous: publication first, then a journaled change and the sequence check, then the copy: no change
  is lost or applied twice, even with applications writing.
* synchronous: the copy first (publication still off, so the primary doesn't try to reach a replica that
  doesn't exist yet), then publication. Writes of other applications in between don't reach the replica, so
  fbadmin warns when other attachments exist.
* the replica server reads its replication.conf block at startup: it must be restarted (asynchronous).
* the copy must reach the replica server: a directory both servers see ("shared"), or the administrator
  moves the file and the setup continues with the restore ("manual").
"""

from __future__ import annotations

import os
import re
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator

from ..maintenance import ops
from . import replication

CHECK_TABLE = "FBADMIN_REPLICATION_CHECK"
CHECK_DDL = (f"CREATE TABLE {CHECK_TABLE} (TOKEN VARCHAR(32) NOT NULL PRIMARY KEY, "
             "SENT_AT TIMESTAMP DEFAULT CURRENT_TIMESTAMP NOT NULL)")


class SetupIn(BaseModel):
    replica_server_id: str
    replica_path: str
    replica_name: str | None = None          # name of the replica in fbadmin
    mode: Literal["async", "sync"] = "async"
    transfer: Literal["shared", "manual"] = "shared"  # how the copy reaches the replica server
    tables: list[str] | None = None          # None: all tables (INCLUDE ALL, also tables created later)
    # asynchronous: directories as the primary server sees them, and the archive as the replica sees it
    journal_directory: str | None = None
    archive_directory: str | None = None
    source_directory: str | None = None
    archive_timeout: int | None = Field(default=10, ge=1, le=3600)
    # where the primary writes the copy, and (shared) the same directory as the replica server sees it
    copy_directory: str | None = None
    copy_directory_replica: str | None = None
    # synchronous: the replica as the primary server reaches it, and the user it applies changes with
    sync_address: str | None = None          # host or host/port
    sync_user: str | None = None
    sync_password: str | None = None

    @field_validator("replica_path", "journal_directory", "archive_directory", "source_directory",
                     "copy_directory", "copy_directory_replica", "sync_address", "sync_user")
    @classmethod
    def _one_line(cls, v: str | None) -> str | None:
        if v is None:
            return None
        v = v.strip()
        if any(c in v for c in "\r\n\0{}\"") or len(v) > 1000:
            raise ValueError("Paths and names are single lines without braces or quotes")
        return v or None


# ------------------------------------------------------------------ paths
def _windows(path: str) -> bool:
    return "\\" in path or bool(re.match(r"^[A-Za-z]:", path))


def _dir(path: str) -> str:
    sep = "\\" if _windows(path) else "/"
    i = path.rfind(sep)
    return path[:i] if i > 0 else (path[:i + 1] if i == 0 else ".")


def _join(directory: str, name: str) -> str:
    sep = "\\" if _windows(directory) else "/"
    return directory.rstrip("/\\") + sep + name


def _same_path(a: str, b: str) -> bool:
    if _windows(a) or _windows(b):
        return a.replace("/", "\\").rstrip("\\").lower() == b.replace("/", "\\").rstrip("\\").lower()
    return a.rstrip("/") == b.rstrip("/")


def _stem(path: str) -> str:
    name = re.split(r"[\\/]", path)[-1]
    return name.rsplit(".", 1)[0] or name


def defaults(primary_path: str, body: SetupIn) -> SetupIn:
    """Fill the directories the form left empty (next to the primary database)."""
    base = _join(_dir(primary_path), "replication")
    b = body.model_copy()
    if b.mode == "async":
        b.journal_directory = b.journal_directory or _join(base, "journal")
        b.archive_directory = b.archive_directory or _join(base, "archive")
        b.source_directory = b.source_directory or b.archive_directory
    b.copy_directory = b.copy_directory or base
    b.copy_directory_replica = b.copy_directory_replica or b.copy_directory
    return b


# ------------------------------------------------------------------ facts
def other_attachments(conn) -> list[dict[str, Any]]:
    """Attachments of other applications (not fbadmin's own: same client process id)."""
    cur = conn.cursor()
    cur.execute("SELECT MON$ATTACHMENT_ID, TRIM(MON$USER), TRIM(MON$REMOTE_PROCESS), MON$REMOTE_PID, "
                "TRIM(MON$REMOTE_ADDRESS) FROM MON$ATTACHMENTS WHERE MON$ATTACHMENT_ID <> CURRENT_CONNECTION "
                "AND COALESCE(MON$SYSTEM_FLAG, 0) = 0")
    out = []
    for aid, user, proc, pid, addr in cur.fetchall():
        if pid == os.getpid():
            continue
        out.append({"id": aid, "user": user, "process": (proc or "").replace("\\", "/").split("/")[-1] or None,
                    "address": addr})
    return out


def _missing(exc: Exception) -> bool:
    return bool(re.search(r"No such file|cannot find|not found|does not exist|I/O error.*open|CreateFile",
                          str(exc), re.I))


def replica_exists(connect_replica) -> bool | str:
    """True when the replica file exists, False when it doesn't, an error text when it can't be told."""
    try:
        conn = connect_replica()
    except Exception as e:  # noqa: BLE001 - the driver's error for a missing file
        return False if _missing(e) else str(e).strip().splitlines()[0][:300]
    try:
        conn.close()
    except Exception:  # noqa: BLE001
        pass
    return True


# ------------------------------------------------------------------ plan
def _check(out: list, level: str, code: str, message: str) -> None:
    out.append({"level": level, "code": code, "message": message})


def blocks(primary_path: str, b: SetupIn, guid: str | None, replica_host: str, replica_port: int,
           replica_user: str, replica_password: str | None) -> dict[str, Any]:
    """The replication.conf blocks of both servers (and what they must contain)."""
    if b.mode == "async":
        prim = replication.generate_conf({
            "role": "primary", "database": primary_path, "journal_directory": b.journal_directory,
            "journal_archive_directory": b.archive_directory, "journal_archive_timeout": b.archive_timeout})
        rep = replication.generate_conf({
            "role": "replica", "database": b.replica_path, "journal_source_directory": b.source_directory,
            "source_guid": guid, "apply_idle_timeout": 2})
        return {"primary": prim["block"], "replica": rep["block"]}
    address = b.sync_address or (replica_host if replica_port == 3050 else f"{replica_host}/{replica_port}")
    user = b.sync_user or replica_user
    shown = b.sync_password or replica_password or "<password>"
    prim = replication.generate_conf({"role": "primary_sync", "database": primary_path,
                                      "sync_replica": f"{user}:{shown}@{address}:{b.replica_path}"})
    return {"primary": prim["block"], "replica": None}


def plan(*, primary: dict[str, Any], b: SetupIn, same_server: bool, replica_major: int | None,
         replica_exists_: bool | str, others: list[dict[str, Any]], conf: dict[str, Any]) -> dict[str, Any]:
    """Checks and steps. ``primary``: replication.status() of the primary plus ``path``/``name``."""
    checks: list[dict[str, str]] = []
    major = primary.get("engine_major") or 0
    if not primary.get("supported"):
        _check(checks, "error", "version", f"Replication needs Firebird 4 or newer; the primary runs "
               f"{primary.get('engine_version')}.")
    if replica_major is not None and replica_major < 4:
        _check(checks, "error", "replica_version", f"The replica server runs Firebird {replica_major}: "
               "replication needs Firebird 4 or newer.")
    elif replica_major is not None and major and replica_major < major:
        _check(checks, "error", "replica_older", f"The replica server (Firebird {replica_major}) is older than the "
               f"primary (Firebird {major}): it can't open the copy.")
    elif replica_major is not None and major and replica_major > major:
        _check(checks, "error", "versions", f"The replica server runs Firebird {replica_major}, the primary "
               f"Firebird {major}: the replica is a physical copy, so both need the same version.")
    if primary.get("supported") and primary.get("replica_mode") not in (None, "none"):
        _check(checks, "error", "is_replica", "This database is a replica itself; set up replication on its "
               "primary (or switch its replica mode to none first).")
    if same_server and _same_path(primary["path"], b.replica_path):
        _check(checks, "error", "same_file", "The replica must be another file than the primary.")
    if replica_exists_ is True:
        _check(checks, "error", "replica_exists", f"{b.replica_path} exists on the replica server: choose a new "
               "file (fbadmin never overwrites a database).")
    elif isinstance(replica_exists_, str):
        _check(checks, "error", "replica_unreachable", f"Can't check the replica file: {replica_exists_}")
    tables = primary.get("tables") or []
    names = {t["name"] for t in tables}
    if b.tables is not None:
        unknown = [t for t in b.tables if t not in names]
        if unknown:
            _check(checks, "error", "unknown_tables", "Unknown tables: " + ", ".join(unknown))
        if not b.tables:
            _check(checks, "error", "no_tables", "Choose the tables to replicate (or all tables).")
    chosen = [t for t in tables if b.tables is None or t["name"] in b.tables]
    no_key = [t["name"] for t in chosen if not t["has_key"] and t["name"] != CHECK_TABLE]
    if no_key:
        _check(checks, "warning", "no_key", "Tables without a primary or unique key are replicated by RDB$DB_KEY: "
               "updates and deletes may not find their rows on the replica: " + ", ".join(no_key[:20])
               + (" …" if len(no_key) > 20 else ""))
    if b.mode == "sync" and primary.get("publication_enabled"):
        _check(checks, "error", "sync_publication", "Publication is enabled: for synchronous replication the copy "
               "is made first, with publication off. Disable publication first (Replication tab).")
    if others:
        who = ", ".join(f"{a['user']} ({a['process'] or a['address'] or '?'})" for a in others[:5])
        if b.mode == "sync":
            _check(checks, "warning", "writers", f"{len(others)} other attachment(s): {who}. Changes they make "
                   "between the copy and the start of publication don't reach the replica: stop them for the "
                   "setup.")
        elif not ((primary.get("replication_sequence") or 0) > 0):
            _check(checks, "warning", "reopen", f"{len(others)} other attachment(s) keep the database open: the "
                   "primary's replication.conf block is read when the database is opened, so close them after "
                   "adding the block.")
    if b.transfer == "shared" and not same_server:
        _check(checks, "info", "copy_dir", f"The primary server writes the copy (nbackup) to {b.copy_directory}; "
               f"the replica server reads it from {b.copy_directory_replica}: both must be the same storage.")
    elif b.transfer == "shared":
        _check(checks, "info", "copy_dir", f"The copy (nbackup) is written to {b.copy_directory} and restored "
               "from there; it isn't removed afterwards.")
    else:
        _check(checks, "info", "copy_manual", f"The primary server writes the copy (nbackup) to {b.copy_directory}; "
               "the setup then waits for you to copy the file to the replica server, and continues with the "
               "restore.")
    if b.mode == "async":
        _check(checks, "info", "directories", f"Create {b.journal_directory} and {b.archive_directory} on the "
               "primary server (writable by the Firebird server's user) and make the archive readable on the "
               f"replica server as {b.source_directory}. Firebird doesn't create them: a missing directory "
               "turns replication off.")
    else:
        _check(checks, "warning", "sync_password", "The primary's replication.conf holds the replica's user and "
               "password in clear text; that user needs the REPLICATE_INTO_DATABASE privilege (or SYSDBA).")
    copy = ["copy", "transfer", "restore"] if b.transfer == "manual" else ["copy", "restore"]
    if b.mode == "async":
        steps = ["conf_primary", "publication", "journal_check", *copy, "replica_mode", "register",
                 "conf_replica", "restart_replica", "verify"]
    else:
        steps = ["conf_primary", *copy, "replica_mode", "register", "publication", "verify"]
    return {"ok": not any(c["level"] == "error" for c in checks), "checks": checks, "steps": steps,
            "settings": b.model_dump(exclude={"sync_password"}), "conf": conf,
            "primary": {k: primary.get(k) for k in ("engine_version", "guid", "replication_sequence",
                                                     "publication_enabled", "path")}}


# ------------------------------------------------------------------ run
def ensure_check_table(conn) -> bool:
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) FROM RDB$RELATIONS WHERE RDB$RELATION_NAME = ?", (CHECK_TABLE,))
    if cur.fetchone()[0]:
        return False
    cur.execute(CHECK_DDL)
    conn.commit()
    return True


def publish(conn, tables: list[str] | None, log) -> None:
    st = replication.status(conn)
    conn.commit()
    if not st["publication_enabled"]:
        log(replication.set_publication(conn, True))
        conn.commit()
    if tables is None:
        log(replication.change_tables(conn, "include", all_tables=True))
    else:
        missing = [t for t in [*tables, CHECK_TABLE] if t not in {x["name"] for x in st["tables"] if x["published"]}]
        if missing:
            log(replication.change_tables(conn, "include", missing))
    conn.commit()


def send_token(conn) -> dict[str, Any]:
    """Insert a check row on the primary (old rows are removed: deletes replicate too)."""
    token = uuid.uuid4().hex
    cur = conn.cursor()
    cur.execute(f"DELETE FROM {CHECK_TABLE} WHERE SENT_AT < DATEADD(-1 DAY TO CURRENT_TIMESTAMP)")
    cur.execute(f"INSERT INTO {CHECK_TABLE} (TOKEN) VALUES (?)", (token,))
    conn.commit()
    cur.execute("SELECT RDB$GET_CONTEXT('SYSTEM', 'REPLICATION_SEQUENCE') FROM RDB$DATABASE")
    seq = cur.fetchone()[0]
    conn.commit()
    return {"token": token, "sent_at": time.time(), "primary_sequence": int(seq) if seq not in (None, "") else None}


def token_arrived(conn, token: str) -> dict[str, Any]:
    cur = conn.cursor()
    cur.execute("SELECT MON$REPLICA_MODE FROM MON$DATABASE")
    mode = replication.REPLICA_NAMES.get(cur.fetchone()[0])
    cur.execute("SELECT COUNT(*) FROM RDB$RELATIONS WHERE RDB$RELATION_NAME = ?", (CHECK_TABLE,))
    if not cur.fetchone()[0]:
        conn.commit()
        return {"arrived": False, "replica_mode": mode, "table": False}
    cur.execute(f"SELECT COUNT(*) FROM {CHECK_TABLE} WHERE TOKEN = ?", (token,))
    n = cur.fetchone()[0]
    conn.commit()
    return {"arrived": bool(n), "replica_mode": mode, "table": True}


def journal_check(conn, log) -> int:
    """Asynchronous: a journaled change must move the replication sequence above 0, else the primary's
    replication.conf block isn't active (missing, a directory missing, or the database not reopened)."""
    t = send_token(conn)
    seq = t["primary_sequence"] or 0
    if seq <= 0:
        raise ValueError(
            "The primary writes no journal: its replication.conf block is not active. Check that the block is "
            "in replication.conf of the primary server, that the journal and archive directories exist and are "
            "writable by the Firebird server, and that the database was reopened (all attachments closed) after "
            "the block was added. Publication is now enabled; nothing was copied.")
    log(f"Journal active: replication sequence {seq}")
    return seq


def copy_name(primary_path: str) -> str:
    return f"{_stem(primary_path)}-replica-{datetime.now().strftime('%Y%m%d%H%M%S')}.nbk"


def physical_copy(primary_svc, primary_path: str, b: SetupIn, log) -> str:
    """nbackup level 0 of the primary into the copy directory; returns the file (as the primary sees it)."""
    src = _join(b.copy_directory or _dir(primary_path), copy_name(primary_path))
    log(f"nbackup level 0 of {primary_path} to {src}")
    ops.nbackup(primary_svc, primary_path, 0, src, log=log)
    return src


def replica_copy_path(copy_file: str, b: SetupIn) -> str:
    """The copy as the replica server sees it (shared directory)."""
    name = re.split(r"[\\/]", copy_file)[-1]
    return _join(b.copy_directory_replica or b.copy_directory or _dir(copy_file), name)


def restore_copy(replica_svc, copy_file: str, replica_path: str, log) -> None:
    log(f"nbackup restore (keeping the replication sequence) of {copy_file} to {replica_path}")
    ops.nrestore(replica_svc, [copy_file], replica_path, sequence=True, log=log)


def make_replica(connect_replica, replica_svc_factory, replica_path: str, log) -> None:
    """The copy becomes a read-only replica; its own publication is turned off first (a replica that publishes
    is cascading replication)."""
    conn = connect_replica()
    try:
        cur = conn.cursor()
        cur.execute("SELECT RDB$ACTIVE_FLAG FROM RDB$PUBLICATIONS WHERE RDB$PUBLICATION_NAME = 'RDB$DEFAULT'")
        row = cur.fetchone()
        conn.commit()
        if row and row[0]:
            cur.execute("ALTER DATABASE DISABLE PUBLICATION")
            conn.commit()
            log("Publication of the copy turned off")
    finally:
        conn.close()
    with replica_svc_factory() as rsvc:
        replication.set_replica_mode(rsvc, replica_path, "read_only")
    log(f"{replica_path} is a read-only replica")


# ------------------------------------------------------------------ replication.conf files
_DB_LINE = re.compile(r"^\s*database\s*=\s*(.+?)\s*(\{)?\s*(#.*)?$", re.I)


def merge_conf(text: str, database: str, block: str) -> tuple[str, bool]:
    """Put ``block`` (a ``database = ... { }`` block) into replication.conf text: it replaces the block of
    the same database, or is appended. Returns (text, replaced)."""
    lines = text.splitlines(keepends=True)
    i = 0
    while i < len(lines):
        m = _DB_LINE.match(lines[i])
        if m and _same_path(m.group(1).strip().strip('"'), database):
            j = i if m.group(2) else i + 1
            while j < len(lines) and "{" not in lines[j]:
                j += 1
            depth, k = 0, j
            while k < len(lines):
                depth += lines[k].count("{") - lines[k].count("}")
                if depth <= 0 and "}" in lines[k]:
                    break
                k += 1
            if k >= len(lines):
                raise ValueError(f"The block of {database} in replication.conf has no closing brace")
            new = block if block.endswith("\n") else block + "\n"
            return "".join(lines[:i]) + new + "".join(lines[k + 1:]), True
        i += 1
    sep = "" if not text or text.endswith("\n\n") else ("\n" if text.endswith("\n") else "\n\n")
    return text + sep + (block if block.endswith("\n") else block + "\n"), False


def allowed_conf_files() -> list[str]:
    raw = os.environ.get("FBADMIN_REPLICATION_CONF", "")
    return [p.strip() for p in raw.split(os.pathsep if os.pathsep in raw else ",") if p.strip()]


def write_conf(path: str, database: str, block: str, *, server_mode: bool) -> dict[str, Any]:
    """Write a block into a replication.conf that fbadmin can reach (local install, mounted volume). The file
    is rewritten in place (keeps its owner and works with a bind-mounted file); a copy is kept next to it.
    In server mode only the files listed in FBADMIN_REPLICATION_CONF can be written."""
    p = Path(path).expanduser()
    if p.name.lower() != "replication.conf":
        raise ValueError("Only a file named replication.conf can be written")
    if server_mode:
        allowed = [Path(a).expanduser().resolve() for a in allowed_conf_files()]
        if p.resolve() not in allowed:
            raise PermissionError("In server mode fbadmin writes only the replication.conf files listed in "
                                  "FBADMIN_REPLICATION_CONF")
    if not p.is_file():
        raise ValueError(f"{p} doesn't exist (fbadmin only changes an existing replication.conf)")
    if not _DB_LINE.match(block.splitlines()[0] if block else ""):
        raise ValueError("The block must start with database = <path>")
    old = p.read_text(encoding="utf-8", errors="replace")
    new, replaced = merge_conf(old, database, block)
    backup = p.with_name(f"replication.conf.{datetime.now().strftime('%Y%m%d%H%M%S')}.bak")
    backup.write_text(old, encoding="utf-8")
    with open(p, "r+", encoding="utf-8") as f:  # in place: same inode
        f.seek(0)
        f.write(new)
        f.truncate()
    return {"path": str(p), "replaced": replaced, "backup": str(backup)}
