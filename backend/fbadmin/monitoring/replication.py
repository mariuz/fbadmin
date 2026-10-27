"""Firebird 4+ replication helpers: publication, replica mode and ``replication.conf`` generation."""

from __future__ import annotations

from typing import Any

from firebirdsql.consts import isc_action_svc_properties, isc_spb_dbname
from firebirdsql.utils import int_to_bytes

from ..metadata import Catalog
from ..metadata.types import quote_ident

ISC_SPB_PRP_REPLICA_MODE = 46  # isc_spb_prp_replica_mode (Firebird 4)
REPLICA_MODES = {"none": 0, "read_only": 1, "read_write": 2}
REPLICA_NAMES = {0: "none", 1: "read_only", 2: "read_write"}


def status(conn) -> dict[str, Any]:
    cat = Catalog(conn)
    major = cat.engine_major
    supported = bool(cat.one(
        "SELECT COUNT(*) AS n FROM RDB$RELATIONS WHERE RDB$RELATION_NAME = 'RDB$PUBLICATIONS'")["n"])
    out: dict[str, Any] = {"engine_version": cat.engine_version, "engine_major": major, "supported": supported}
    if not supported:
        return out
    db = cat.one("SELECT MON$REPLICA_MODE AS replica_mode, TRIM(MON$DATABASE_NAME) AS database_name, "
                 "MON$GUID AS guid FROM MON$DATABASE") or {}
    ctx = cat.one("SELECT RDB$GET_CONTEXT('SYSTEM', 'REPLICATION_SEQUENCE') AS seq FROM RDB$DATABASE") or {}
    pubs = cat.rows(
        "SELECT TRIM(RDB$PUBLICATION_NAME) AS name, TRIM(RDB$OWNER_NAME) AS owner, RDB$SYSTEM_FLAG AS system_flag, "
        "RDB$ACTIVE_FLAG AS active, RDB$AUTO_ENABLE AS auto_enable FROM RDB$PUBLICATIONS")
    published = {r["t"] for r in cat.rows(
        "SELECT TRIM(RDB$TABLE_NAME) AS t FROM RDB$PUBLICATION_TABLES")}
    tables = cat.rows(
        "SELECT TRIM(r.RDB$RELATION_NAME) AS name, "
        "(SELECT COUNT(*) FROM RDB$RELATION_CONSTRAINTS c WHERE c.RDB$RELATION_NAME = r.RDB$RELATION_NAME "
        "AND c.RDB$CONSTRAINT_TYPE IN ('PRIMARY KEY', 'UNIQUE')) AS keys "
        "FROM RDB$RELATIONS r WHERE COALESCE(r.RDB$SYSTEM_FLAG, 0) = 0 AND r.RDB$VIEW_BLR IS NULL "
        "AND COALESCE(r.RDB$RELATION_TYPE, 0) IN (0, 3) "
        "AND r.RDB$RELATION_NAME NOT STARTING WITH 'PLG$' "  # profiler / plugin tables
        "ORDER BY r.RDB$RELATION_NAME")
    for t in tables:
        t["published"] = t["name"] in published
        t["has_key"] = bool(t.pop("keys"))
    seq = ctx.get("seq")
    mode = db.get("replica_mode")
    default = next((p for p in pubs if p["name"] == "RDB$DEFAULT"), pubs[0] if pubs else None)
    warnings = []
    if default and default["active"] and not published and not default["auto_enable"]:
        warnings.append("Publication is enabled but no table is included: nothing will be replicated.")
    no_key = [t["name"] for t in tables if t["published"] and not t["has_key"]]
    if no_key:
        warnings.append("Published tables without a primary/unique key are replicated using RDB$DB_KEY and "
                        "updates/deletes may fail on the replica: " + ", ".join(no_key))
    if mode and default and default["active"]:
        warnings.append("This database is a replica and also publishes changes (cascading replication).")
    out.update({
        "database_name": db.get("database_name"),
        "guid": db.get("guid"),
        "replica_mode": REPLICA_NAMES.get(mode, mode),
        "replication_sequence": int(seq) if seq not in (None, "") else None,
        "publications": pubs,
        "publication_enabled": bool(default and default["active"]),
        "auto_enable": bool(default and default["auto_enable"]),
        "tables": tables,
        "warnings": warnings,
    })
    return out


def set_publication(conn, enabled: bool) -> str:
    sql = f"ALTER DATABASE {'ENABLE' if enabled else 'DISABLE'} PUBLICATION"
    conn.cursor().execute(sql)
    return sql


def change_tables(conn, action: str, tables: list[str] | None = None, all_tables: bool = False) -> str:
    if action not in ("include", "exclude"):
        raise ValueError("action must be 'include' or 'exclude'")
    verb = "INCLUDE" if action == "include" else "EXCLUDE"
    prep = "TO" if action == "include" else "FROM"
    if all_tables:
        sql = f"ALTER DATABASE {verb} ALL {prep} PUBLICATION"
    else:
        if not tables:
            raise ValueError("No tables given")
        sql = f"ALTER DATABASE {verb} TABLE {', '.join(quote_ident(t) for t in tables)} {prep} PUBLICATION"
    conn.cursor().execute(sql)
    return sql


def set_replica_mode(svc, database: str, mode: str) -> None:
    """Switch replica mode with the services API (same as ``gfix -replica <mode>``)."""
    if mode not in REPLICA_MODES:
        raise ValueError(f"Unknown replica mode {mode!r}; use none, read_only or read_write")
    s = svc.str_to_bytes(database)
    spb = bytes([isc_action_svc_properties, isc_spb_dbname]) + int_to_bytes(len(s), 2) + s
    spb += bytes([ISC_SPB_PRP_REPLICA_MODE, REPLICA_MODES[mode]])
    svc._op_service_start(spb)
    svc._op_response()
    while True:
        svc._op_service_info(bytes([0x02]), bytes([0x3E]))
        (_h, _oid, buf) = svc._op_response()
        if buf[:4] == bytes([0x3E, 0x00, 0x00, 0x01]):
            break


# ------------------------------------------------------------------ replication.conf
def _q(v: str) -> str:
    """Quote values that Firebird's config parser would misread: spaces, and braces (a GUID such as
    {3452A1DD-...} unquoted is an "illegal line", which turns off replication for every database)."""
    v = str(v)
    return f'"{v}"' if any(ch.isspace() or ch in "{}#" for ch in v) else v


def generate_conf(p: dict[str, Any]) -> dict[str, Any]:
    """Build a ``replication.conf`` ``database = ...`` block from form values.

    role: ``primary`` (asynchronous, journal files), ``primary_sync`` (synchronous, to a replica
    connection string) or ``replica`` (applies journal files from journal_source_directory).
    """
    role = p.get("role", "primary")
    db = (p.get("database") or "").strip()
    if not db:
        raise ValueError("Database path (or alias) is required")
    lines: list[str] = []
    notes: list[str] = []
    errors: list[str] = []

    def opt(key: str, val: Any, comment: str | None = None) -> None:
        if val is None or val == "":
            return
        if isinstance(val, bool):
            val = "true" if val else "false"
        if comment:
            lines.append(f"    # {comment}")
        lines.append(f"    {key} = {_q(val)}")

    if role == "primary":
        jd = p.get("journal_directory")
        ad = p.get("journal_archive_directory")
        if not jd:
            errors.append("journal_directory is required for asynchronous replication")
        if not ad:
            errors.append("journal_archive_directory is required: the replica reads archived segments from it")
        opt("journal_directory", jd, "Where the primary writes journal segments")
        opt("journal_file_prefix", p.get("journal_file_prefix"))
        opt("journal_segment_size", p.get("journal_segment_size"), "Max segment size before it is archived")
        opt("journal_segment_count", p.get("journal_segment_count"), "Max number of full segments kept")
        opt("journal_group_flush_delay", p.get("journal_group_flush_delay"))
        opt("journal_archive_directory", ad, "Archived segments: share this with the replica (copy or NFS)")
        opt("journal_archive_command", p.get("journal_archive_command"),
            "Optional: command to archive a segment ($(filename), $(pathname), $(archivepathname))")
        opt("journal_archive_timeout", p.get("journal_archive_timeout"),
            "Seconds after which a non-full segment is archived anyway")
        notes.append("On the primary database run: ALTER DATABASE ENABLE PUBLICATION; and "
                     "ALTER DATABASE INCLUDE ALL TO PUBLICATION; (or include selected tables).")
    elif role == "primary_sync":
        target = p.get("sync_replica")
        if not target:
            errors.append("sync_replica connection string is required for synchronous replication")
        opt("sync_replica", target, "user:password@host:/path/replica.fdb (one line per replica)")
        notes.append("Synchronous replication: each commit waits until the replica has applied it.")
        notes.append("On the primary database run: ALTER DATABASE ENABLE PUBLICATION; and "
                     "ALTER DATABASE INCLUDE ALL TO PUBLICATION;")
    elif role == "replica":
        src = p.get("journal_source_directory")
        if not src:
            errors.append("journal_source_directory is required on the replica")
        opt("journal_source_directory", src, "Where archived segments of the primary arrive")
        opt("source_guid", p.get("source_guid"), "Optional: accept only segments of this primary (its GUID)")
        opt("apply_idle_timeout", p.get("apply_idle_timeout"))
        opt("apply_error_timeout", p.get("apply_error_timeout"))
        notes.append("Create the replica from a physical copy of the primary (nbackup -B 0, restored with "
                     "nbackup -R -SEQ; a gbak copy doesn't work), then set it to replica mode: gfix -replica "
                     "read_only <db> - or let Set up a replica… in fbadmin do all of it.")
        notes.append("The replica server reads this block at startup: restart it after adding the block.")
    else:
        raise ValueError("role must be primary, primary_sync or replica")
    opt("verbose_logging", p.get("verbose_logging"))
    opt("log_errors", p.get("log_errors"))
    opt("report_errors", p.get("report_errors"))
    opt("disable_on_error", p.get("disable_on_error"))
    opt("include_filter", p.get("include_filter"), "Regex of tables to replicate (in addition to publication)")
    opt("exclude_filter", p.get("exclude_filter"))
    opt("cascade_replication", p.get("cascade_replication"))
    text = f"database = {_q(db)}\n{{\n" + "\n".join(lines) + "\n}\n"
    header = ("# replication.conf (in the Firebird root directory) - generated by fbadmin\n"
              "# Reload: changes are read when the database is next opened (first attachment).\n")
    return {"text": header + text, "block": text, "errors": errors, "notes": notes}
