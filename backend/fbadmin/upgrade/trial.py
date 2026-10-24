"""Trial restore: back up the source database with its own server, restore the backup on the target
server into a scratch database, recompile every PSQL module and view there, and report what failed.

This is the strongest check the assistant has: the target server's own parser compiles the code, so
reserved words, removed features and changed rules show up as real errors instead of guesses.
"""

from __future__ import annotations

import re
import threading
import time
import uuid
from collections.abc import Callable
from contextlib import AbstractContextManager, contextmanager
from pathlib import Path
from typing import Any

from .. import jobs
from ..maintenance import ops
from ..maintenance.verify import check_scratch
from ..metadata import Catalog
from ..metadata import types as mtypes
from ..sqlsplit import split_sql
from . import checks as C
from . import keywords as K

SCRATCH_PREFIX = "fbadmin_upgrade_"
CONFIG_KEYS = ("UdfAccess", "ExternalFileAccess", "AuthServer", "UserManager", "WireCrypt", "DataTypeCompatibility",
               "ReadConsistency", "DefaultDbCachePages", "ServerMode", "ParallelWorkers", "MaxParallelWorkers",
               "DefaultTimeZone", "SecurityDatabase")
_quote_lock = threading.Lock()


def scratch_path(directory_of: str, override: str | None = None) -> str:
    """The scratch database path on the target server: ``override`` or next to ``directory_of``."""
    if override and override.strip():
        return override.strip()
    m = re.match(r"^(.*[\\/])[^\\/]+$", directory_of or "")
    if not m:
        raise ValueError("Cannot tell a directory on the target server for the trial database: "
                         "enter a path for it (for example /var/lib/firebird/data/upgrade_test.fdb)")
    return f"{m.group(1)}{SCRATCH_PREFIX}{uuid.uuid4().hex[:8]}.fdb"


@contextmanager
def _quote_all_versions():
    """Make the DDL generator quote every word reserved in any Firebird version (3 to 6) while
    recompiling, so a name like TRUNCATE in the generated header does not fail on Firebird 6. Extra
    quoting is always valid, so other threads generating DDL meanwhile are not harmed."""
    with _quote_lock:
        old = mtypes.RESERVED
        mtypes.RESERVED = old | K.ALL_RESERVED
        try:
            yield
        finally:
            mtypes.RESERVED = old


def _modules(cat: Catalog) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    out += [("views", r["name"]) for r in cat.list_objects("views")]
    out += [("procedures", r["name"]) for r in cat.list_objects("procedures")]
    out += [("functions", r["name"]) for r in cat.rows(
        "SELECT TRIM(RDB$FUNCTION_NAME) AS name FROM RDB$FUNCTIONS WHERE COALESCE(RDB$SYSTEM_FLAG, 0) = 0 "
        "AND RDB$PACKAGE_NAME IS NULL AND COALESCE(RDB$LEGACY_FLAG, 0) = 0 AND RDB$ENGINE_NAME IS NULL ORDER BY 1")]
    out += [("packages", r["name"]) for r in cat.list_objects("packages")]
    out += [("triggers", r["name"]) for r in cat.rows(
        "SELECT TRIM(t.RDB$TRIGGER_NAME) AS name FROM RDB$TRIGGERS t WHERE COALESCE(t.RDB$SYSTEM_FLAG, 0) = 0 "
        "AND t.RDB$ENGINE_NAME IS NULL AND NOT EXISTS (SELECT 1 FROM RDB$CHECK_CONSTRAINTS cc "
        "WHERE cc.RDB$TRIGGER_NAME = t.RDB$TRIGGER_NAME) ORDER BY 1")]
    return out


def _first_lines(msg: str, n: int = 4) -> str:
    lines = [ln.strip() for ln in str(msg).splitlines() if ln.strip()]
    return " / ".join(lines[:n])


def recompile(conn, log: ops.Log) -> list[dict[str, Any]]:
    """Recreate each view and PSQL module from its stored source; return the failures."""
    cat = Catalog(conn, schema="")
    failures: list[dict[str, Any]] = []
    mods = _modules(cat)
    conn.commit()
    log(f"Recompiling {len(mods)} view(s) and PSQL module(s) on the target server")
    with _quote_all_versions():
        for otype, name in mods:
            jobs.check()
            try:
                ddl = cat.get_object(otype, name)["ddl"]
                conn.commit()
            except Exception as e:  # noqa: BLE001
                conn.rollback()
                failures.append({"otype": otype, "name": name, "error": f"cannot read its definition: {e}"})
                continue
            stmts = [s.sql for s in split_sql(ddl) if not s.sql.lstrip().upper().startswith("COMMENT ON")]
            try:
                cur = conn.cursor()
                for sql in stmts:
                    cur.execute(sql)
                conn.commit()
            except Exception as e:  # noqa: BLE001
                try:
                    conn.rollback()
                except Exception:  # noqa: BLE001
                    pass
                failures.append({"otype": otype, "name": name, "error": _first_lines(str(e))})
                log(f"  {otype[:-1]} {name}: {_first_lines(str(e), 2)}")
    log(f"Recompile done: {len(mods) - len(failures)} ok, {len(failures)} failed")
    return failures


def _config(cat: Catalog) -> dict[str, Any] | None:
    try:
        rows = cat.rows("SELECT TRIM(RDB$CONFIG_NAME) AS name, RDB$CONFIG_VALUE AS val, "
                        "RDB$CONFIG_DEFAULT AS dflt, RDB$CONFIG_IS_SET AS is_set FROM RDB$CONFIG", scoped=False)
    except Exception:  # noqa: BLE001 - Firebird 3 or no privilege
        cat.conn.rollback()
        return None
    return {r["name"]: {"value": r["val"], "default": r["dflt"], "set": bool(r["is_set"])}
            for r in rows if r["name"] in CONFIG_KEYS}


def run_trial(*, source_service: Callable[[], AbstractContextManager], source_path: str,
              target_service: Callable[[], AbstractContextManager], target_connect: Callable[[str], Any],
              scratch: str, registered: list[str], work_dir: Path, metadata_only: bool, keep: bool,
              source_facts: dict[str, Any], log: ops.Log) -> dict[str, Any]:
    """Back up on the source server, restore on the target server, check and recompile there."""
    check_scratch(scratch, registered)
    ours = Path(scratch.replace("\\", "/")).name.lower().startswith(SCRATCH_PREFIX)
    work_dir.mkdir(parents=True, exist_ok=True)
    fbk = work_dir / f"upgrade_{uuid.uuid4().hex[:8]}.fbk"
    t0 = time.time()
    restore_log: list[str] = []
    result: dict[str, Any] = {"scratch": scratch, "kept": False, "metadata_only": metadata_only}
    try:
        with source_service() as svc:
            smajor = ops.engine_major(svc)
            log(f"Backing up {source_path} with the source server (Firebird {smajor})"
                + (" - metadata only" if metadata_only else ""))
            size = ops.backup_to_local_file(svc, source_path, fbk, ops.BackupOptions(metadata_only=metadata_only),
                                            ops.option_major(svc), log)
        result["backup_bytes"] = size
        with target_service() as svc:
            tmajor = ops.engine_major(svc)
            result["target_engine"] = svc.getServerVersion()
            opts = ops.RestoreOptions(replace=ours)
            if tmajor >= 6 and (source_facts.get("page_size") or 0) < 8192:
                opts.page_size = 8192
            log(f"Restoring into {scratch} with the target server (Firebird {tmajor})"
                + (f", page size {opts.page_size}" if opts.page_size else ""))

            def rlog(line: str) -> None:
                restore_log.append(line)
                if re.search(r"error|warning|failed|cannot", line, re.I):
                    log(line)

            ops.restore_from_local_file(svc, fbk, scratch, opts, ops.option_major(svc), rlog)
        result["restore_seconds"] = round(time.time() - t0, 1)
        log(f"Restore finished in {result['restore_seconds']} s")
    except ops.RestoreCancelled as exc:
        if ours or exc.created:  # gbak may still hold the file for a moment: retry the drop
            ops.drop_partial(lambda: target_connect(scratch), scratch, log)
        raise
    except Exception:
        if ours:
            _drop(target_connect, scratch, log)
        raise
    finally:
        fbk.unlink(missing_ok=True)

    try:
        conn = target_connect(scratch)
    except Exception:
        if ours:
            _drop(target_connect, scratch, log)
        raise
    try:
        cat = Catalog(conn, schema="")
        facts = C.database_facts(cat)
        conn.commit()
        result["restored"] = {k: facts[k] for k in ("engine_version", "ods", "page_size", "dialect", "counts")}
        result["config"] = _config(cat)
        conn.commit()
        result["invalid_after_restore"] = cat.rows(
            "SELECT 'procedure' AS kind, TRIM(RDB$PROCEDURE_NAME) AS name FROM RDB$PROCEDURES "
            "WHERE COALESCE(RDB$SYSTEM_FLAG, 0) = 0 AND RDB$VALID_BLR = 0 "
            "UNION ALL SELECT 'function', TRIM(RDB$FUNCTION_NAME) FROM RDB$FUNCTIONS "
            "WHERE COALESCE(RDB$SYSTEM_FLAG, 0) = 0 AND RDB$VALID_BLR = 0 "
            "UNION ALL SELECT 'trigger', TRIM(RDB$TRIGGER_NAME) FROM RDB$TRIGGERS "
            "WHERE COALESCE(RDB$SYSTEM_FLAG, 0) = 0 AND RDB$VALID_BLR = 0", scoped=False)
        for r in result["invalid_after_restore"]:
            r["kind"] = r["kind"].strip()
        conn.commit()
        result["recompile_failures"] = recompile(conn, log)
        result["restore_messages"] = [ln for ln in restore_log if re.search(r"error|warning", ln, re.I)][:50]
    finally:
        if keep and not jobs.cancelling():
            conn.close()
            result["kept"] = True
            log(f"Kept the trial database {scratch} on the target server")
        else:
            try:
                conn.drop_database()
                log("Trial database dropped")
            except Exception as e:  # noqa: BLE001
                log(f"Could not drop the trial database: {e}")
    result["seconds"] = round(time.time() - t0, 1)
    result["findings"] = trial_findings(result, source_facts)
    return result


def _drop(target_connect: Callable[[str], Any], scratch: str, log: ops.Log) -> None:
    try:
        target_connect(scratch).drop_database()
        log(f"Dropped the incomplete trial database {scratch}")
    except Exception:  # noqa: BLE001 - nothing was created, or it cannot be opened
        pass


_TREE = {"views": "view", "procedures": "procedure", "functions": "function", "packages": "package",
         "triggers": "trigger"}


def trial_findings(res: dict[str, Any], source_facts: dict[str, Any]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    fails = res.get("recompile_failures") or []
    if fails:
        out.append(C.finding(
            "trial_recompile", C.CAT_RESERVED, C.BLOCKER, "Objects that do not compile on the target server",
            "The trial restore worked, but recreating these objects from their source failed on the target "
            "server. They still run there from the restored compiled form, but any change to them, and any "
            "script that recreates them, fails until the source is fixed.",
            "Fix the source on the old server (quote or rename reserved words, replace removed functions) and run "
            "the trial again.",
            [C.obj(_TREE.get(f["otype"], f["otype"]), f["name"], link=(f["otype"], f["name"]), detail=f["error"])
             for f in fails]))
    restored = (res.get("restored") or {}).get("counts") or {}
    before = source_facts.get("counts") or {}
    missing = {k: (v, restored.get(k, 0)) for k, v in before.items() if restored.get(k, 0) < v and k != "users"}
    if missing:
        out.append(C.finding(
            "trial_counts", C.CAT_STORAGE, C.BLOCKER, "The restored database has fewer objects",
            "Some objects did not survive the backup and restore: "
            + ", ".join(f"{k} {a} → {b}" for k, (a, b) in missing.items()) + ".",
            "Read the restore log for the objects gbak could not create and fix them in the source.", []))
    invalid = res.get("invalid_after_restore") or []
    if invalid:
        out.append(C.finding(
            "trial_invalid", C.CAT_FEATURES, C.WARNING, "Modules marked invalid after the restore",
            "The target server marked these modules invalid while restoring them.",
            "Recompile them on the target server and fix the errors it reports.",
            [C.obj(r["kind"], r["name"]) for r in invalid]))
    msgs = res.get("restore_messages") or []
    if msgs:
        out.append(C.finding(
            "trial_messages", C.CAT_STORAGE, C.WARNING, "Warnings and errors in the restore log",
            "gbak reported these lines while restoring on the target server.",
            "Review each message; most refer to an object that must be fixed in the source.",
            [C.obj("message", m[:300]) for m in msgs]))
    r = res.get("restored") or {}
    out.append(C.finding(
        "trial_ok", C.CAT_STORAGE, C.INFO, "Trial restore on the target server",
        f"Restored on {res.get('target_engine', 'the target server')} as ODS {r.get('ods')} with page size "
        f"{r.get('page_size')} in {res.get('restore_seconds')} s"
        + (" (metadata only)" if res.get("metadata_only") else "")
        + f"; {sum((r.get('counts') or {}).values())} objects; recompiled with "
        f"{len(fails)} failure(s).",
        "Keep the trial database (option) to test the applications against it before the real migration.", []))
    return out
