"""Restore drills (roadmap 2.2): restore the newest backup the way it would be restored after a loss, and check it.

A drill takes the newest backup of a backup schedule (fbadmin's archive) or the newest backup in cloud storage
(downloaded first: that copy is the one left when the fbadmin host is gone), restores it into a scratch database
- on another server if wanted - and checks it: the restore works (an encrypted backup needs its key), the
database opens, an online validation finds no errors, every table can be read, and how its row counts compare
with the live database. It measures the backup's age (the data lost if the database were lost now) and the time
to download and restore (how long a recovery takes), fails when they exceed the limits, and keeps the report.
"""

from __future__ import annotations

import json
import re
import time
import uuid
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator

from ..config import settings
from . import archive, ops, verify

SCRATCH_PREFIX = "fbadmin_drill_"
MAX_TABLES = 1000


class DrillOptions(BaseModel):
    source: Literal["schedule", "storage"] = "schedule"
    backup_schedule_id: str | None = None   # source "schedule": its newest archived backup
    target_id: str | None = None            # source "storage": the newest backup under the prefix
    prefix: str = ""
    server_id: str | None = None            # where to restore (default: the schedule's server)
    path: str | None = None                 # scratch database there (default: next to the database)
    validate_db: bool = True                # online validation of the restored database
    count_rows: bool = True
    compare: bool = True                    # row counts and tables against the live database
    max_age_hours: float | None = Field(default=26, gt=0)   # fail when the newest backup is older
    max_minutes: float | None = Field(default=None, gt=0)   # fail when download + restore take longer
    keep: bool = False                      # keep the restored database (registered in fbadmin)

    @field_validator("path", "prefix")
    @classmethod
    def _line(cls, v: str | None) -> str | None:
        if v is not None and any(c in v for c in "\r\n\0"):
            raise ValueError("A single line")
        return v.strip() if v else v


def scratch_path(db_path: str, schedule_id: str, override: str | None) -> str:
    if override:
        return override
    m = re.match(r"^(.*[\\/])[^\\/]+$", db_path)
    if not m:
        raise ValueError(f"The database is registered by alias ({db_path!r}): set the drill's restore path")
    return f"{m.group(1)}{SCRATCH_PREFIX}{schedule_id}.fdb"


# ------------------------------------------------------------------ the newest backup
def newest_archived(schedule_id: str) -> dict[str, Any]:
    items = archive.list_backups(schedule_id)
    if not items:
        raise LookupError("The backup schedule has no backups in the archive yet")
    b = items[0]
    return {"kind": "archive", "name": b["name"], "path": b["path"], "size": b["size"], "modified": b["modified"],
            "local": archive.root() / b["path"]}


def newest_stored(target_id: str, prefix: str, log) -> dict[str, Any]:
    from ..storage import get_store, object_prefix, open_client
    from ..storage.base import sort_newest

    t = get_store().get(target_id)
    client = open_client(t)
    folder = object_prefix(t, prefix)
    objs = [o for o in client.list(folder + "/" if folder else "") if archive.is_backup_name(o["key"])]
    if not objs:
        raise LookupError(f"No backups under {folder or '/'} in storage target {t.name!r}")
    o = sort_newest(objs)[0]
    work = settings.data_dir / "drills" / "downloads"
    name = o["key"].rsplit("/", 1)[-1]
    dest = work / f"{uuid.uuid4().hex[:8]}-{name}"  # two drills may download the same object
    log(f"Downloading {o['key']} ({o['size']} bytes) from {t.name!r}")
    t0 = time.time()
    client.download_file(o["key"], dest, log)
    return {"kind": "storage", "target": t.name, "name": name, "key": o["key"], "size": o["size"],
            "modified": o["modified"], "local": dest, "download_seconds": round(time.time() - t0, 1)}


# ------------------------------------------------------------------ checks
def _user_tables(cur) -> list[str]:
    cur.execute("SELECT TRIM(RDB$RELATION_NAME) FROM RDB$RELATIONS WHERE COALESCE(RDB$SYSTEM_FLAG, 0) = 0 "
                "AND RDB$VIEW_BLR IS NULL AND COALESCE(RDB$RELATION_TYPE, 0) IN (0, 3) "
                "AND RDB$RELATION_NAME NOT STARTING WITH 'PLG$' ORDER BY 1")
    return [r[0] for r in cur.fetchall()]


def _counts(conn, tables: list[str], log=None) -> dict[str, int | str]:
    from ..metadata.types import quote_ident

    out: dict[str, int | str] = {}
    cur = conn.cursor()
    for t in tables[:MAX_TABLES]:
        try:
            cur.execute(f"SELECT COUNT(*) FROM {quote_ident(t)}")
            out[t] = int(cur.fetchone()[0])
        except Exception as e:  # noqa: BLE001 - a table that can't be read is a finding
            out[t] = "error: " + (str(e).strip().splitlines() or [""])[0][:200]
            if log:
                log(f"Can't read {t}: {out[t]}")
    conn.commit()
    return out


def facts(conn) -> dict[str, Any]:
    cur = conn.cursor()
    cur.execute("SELECT "
                "(SELECT COUNT(*) FROM RDB$RELATIONS WHERE COALESCE(RDB$SYSTEM_FLAG, 0) = 0 AND RDB$VIEW_BLR IS NULL), "
                "(SELECT COUNT(*) FROM RDB$RELATIONS WHERE COALESCE(RDB$SYSTEM_FLAG, 0) = 0 AND RDB$VIEW_BLR IS NOT NULL), "
                "(SELECT COUNT(*) FROM RDB$PROCEDURES WHERE COALESCE(RDB$SYSTEM_FLAG, 0) = 0), "
                "(SELECT COUNT(*) FROM RDB$TRIGGERS WHERE COALESCE(RDB$SYSTEM_FLAG, 0) = 0), "
                "(SELECT COUNT(*) FROM RDB$GENERATORS WHERE COALESCE(RDB$SYSTEM_FLAG, 0) = 0) "
                "FROM RDB$DATABASE")
    t, v, p, tr, g = cur.fetchone()
    conn.commit()
    return {"tables": t, "views": v, "procedures": p, "triggers": tr, "sequences": g}


def compare(restored: dict[str, int | str], live: dict[str, int | str]) -> dict[str, Any]:
    rows = []
    for name in sorted(set(restored) | set(live)):
        b, lv = restored.get(name), live.get(name)
        status = ("missing" if b is None else "extra" if lv is None else
                  "error" if isinstance(b, str) else "same" if b == lv else "differs")
        rows.append({"table": name, "backup_rows": b, "live_rows": lv, "status": status})
    return {"tables": rows, "missing": [r["table"] for r in rows if r["status"] == "missing"],
            "extra": [r["table"] for r in rows if r["status"] == "extra"],
            "differs": sum(1 for r in rows if r["status"] == "differs")}


def run(*, opts: DrillOptions, schedule_id: str, source: dict[str, Any], svc_factory, connect, live_counts,
        registered: list[str], log) -> dict[str, Any]:
    """Restore ``source['local']`` into the scratch database through ``svc_factory()``, check and drop it.
    ``live_counts(tables)`` gives the live database's row counts (or None to skip the comparison)."""
    from . import backupcrypt

    target = opts.path
    verify.check_scratch(target, registered)
    ours = Path(target.replace("\\", "/")).name.lower().startswith(SCRATCH_PREFIX)
    report: dict[str, Any] = {"checks": [], "restore": {"path": target}}
    checks = report["checks"]

    def check(code: str, ok: bool | None, message: str) -> None:
        checks.append({"code": code, "ok": ok, "message": message})
        log(("OK: " if ok else "WARNING: " if ok is None else "FAILED: ") + message)

    src: Path = source["local"]
    enc = backupcrypt.info(src) if backupcrypt.is_encrypted(src) else None
    report["source"] = {k: v for k, v in source.items() if k != "local"}
    report["source"].update({"encrypted": enc is not None, "key_id": enc["key_id"] if enc else None})
    age_h = (time.time() - source["modified"]) / 3600
    report["source"]["age_hours"] = round(age_h, 2)
    if opts.max_age_hours:
        check("age", age_h <= opts.max_age_hours, f"The newest backup is {age_h:.1f} h old (limit "
              f"{opts.max_age_hours:g} h)")
    if enc:
        try:
            backupcrypt.keyring().get(enc["key_id"])
            check("key", True, f"fbadmin has the key {enc['key_id']} of this encrypted backup")
        except backupcrypt.BackupKeyError as e:
            check("key", False, str(e))
            return report
    t0 = time.time()
    with svc_factory() as svc:
        major = ops.option_major(svc)
        log(f"Restoring {source['name']} into {target}")
        try:
            ops.restore_from_local_file(svc, src, target, ops.RestoreOptions(replace=ours), major, log)
        except Exception as e:
            check("restore", False, "The restore failed: " + (str(e).strip().splitlines() or ["?"])[-1][:500])
            if ours:
                ops.drop_partial(lambda: connect(target), target, log, wait=10)
            return report
        report["restore"]["seconds"] = round(time.time() - t0, 1)
        check("restore", True, f"Restored in {report['restore']['seconds']} s")
        total = report["restore"]["seconds"] + (source.get("download_seconds") or 0)
        report["recovery_seconds"] = round(total, 1)
        if opts.max_minutes:
            check("duration", total <= opts.max_minutes * 60, f"Download and restore took {total / 60:.1f} min "
                  f"(limit {opts.max_minutes:g} min)")
        try:
            if opts.validate_db:
                lines = ops.validate_online(svc, target, log=lambda _l: None)
                errors = verify.validation_errors(lines)
                report["validation_errors"] = errors
                check("validation", errors == 0, "Online validation: " + (f"{errors} error(s)" if errors else "no errors"))
        except Exception as e:  # noqa: BLE001
            check("validation", False, f"Online validation failed: {e}")
    conn = connect(target)
    try:
        report["restored"] = facts(conn)
        check("open", True, "The restored database opens: {tables} tables, {views} views, {procedures} procedures, "
              "{triggers} triggers".format(**report["restored"]))
        if opts.count_rows or opts.compare:
            cur = conn.cursor()
            tables = _user_tables(cur)
            conn.commit()
            counts = _counts(conn, tables, log)
            bad = [t for t, v in counts.items() if isinstance(v, str)]
            report["rows"] = sum(v for v in counts.values() if isinstance(v, int))
            check("read", not bad, f"Every table can be read ({len(counts)} tables, {report['rows']} rows)" if not bad
                  else f"{len(bad)} table(s) can't be read: {', '.join(bad[:10])}")
            if len(tables) > MAX_TABLES:
                check("read_limit", None, f"Only the first {MAX_TABLES} of {len(tables)} tables were counted")
            live = live_counts(list(counts)) if opts.compare else None
            if live is not None:
                cmp = compare(counts, live)
                report["compare"] = cmp
                if cmp["missing"] or cmp["extra"]:
                    check("tables", None, "Tables differ from the live database: "
                          + "; ".join(p for p in (
                              f"only in the backup: {', '.join(cmp['missing'][:10])}" if cmp["missing"] else "",
                              f"only in the live database: {', '.join(cmp['extra'][:10])}" if cmp["extra"] else "")
                              if p))
                else:
                    check("tables", True, f"Same tables as the live database; {cmp['differs']} with other row "
                          "counts (changes since the backup)")
            else:
                report["compare"] = None
                report["tables"] = [{"table": t, "backup_rows": v} for t, v in counts.items()]
    finally:
        if opts.keep:
            conn.close()
            report["kept"] = True
            log(f"Kept the restored database {target}")
        else:
            try:
                conn.drop_database()
                log("Restored database dropped")
            except Exception as e:  # noqa: BLE001
                log(f"Could not drop the restored database: {e}")
                conn.close()
    return report


# ------------------------------------------------------------------ reports
def _dir() -> Path:
    return settings.data_dir / "drills"


def save_report(schedule_id: str, report: dict[str, Any]) -> None:
    d = _dir()
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{re.sub(r'[^A-Za-z0-9_-]', '_', schedule_id)}.json").write_text(json.dumps(report, indent=1, default=str))


def load_report(schedule_id: str) -> dict[str, Any] | None:
    p = _dir() / f"{re.sub(r'[^A-Za-z0-9_-]', '_', schedule_id)}.json"
    return json.loads(p.read_text()) if p.exists() else None


def delete_report(schedule_id: str) -> None:
    (_dir() / f"{re.sub(r'[^A-Za-z0-9_-]', '_', schedule_id)}.json").unlink(missing_ok=True)
