"""Versioned migrations (roadmap 2.2): numbered SQL scripts applied in order, a record of what was applied in
each database, and a dry run on a copy first.

Scripts are named ``V<version>__<description>.sql`` (Flyway's convention: ``V1__create_customers.sql``,
``V1.2__add_email.sql``, ``V2026_10_30_1__orders.sql``) and come from a git repository (a branch, tag or
commit; fetched by fbadmin), a directory, or the request itself (the command-line tool sends the files of
a local directory). Each database records the applied scripts in ``FBADMIN_MIGRATIONS``: version, script,
SHA-256 checksum, who and when, how long, and whether it failed.

Rules (``status``):

* a script whose checksum differs from the applied one is *changed*: an applied script must not be edited
  (write a new one); ``repair`` accepts the new checksum when the change is harmless (comments);
* a failed script blocks the next run until it is fixed by hand and ``repair`` removes the failed record:
  Firebird commits DDL statement by statement, so a half-applied script can't be rolled back;
* a pending script with a lower version than an applied one is *late* (merged out of order): refused
  unless out-of-order runs are allowed;
* applied versions without a script are *missing* (a warning);
* ``baseline`` marks an existing database as being at a version: older scripts are not run on it.

The dry run restores a copy of the database on the same server (metadata only by default, or with the data),
runs the pending scripts on the copy, reports each script and the schema changes they make, and drops the
copy. ``apply`` wants a passed dry run of the same scripts unless told to skip it.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, field_validator

from .config import settings

TABLE = "FBADMIN_MIGRATIONS"
LOCK_TABLE = "FBADMIN_MIGRATIONS_LOCK"
SCRATCH_PREFIX = "fbadmin_migrate_"
DRY_RUN_VALID = 24 * 3600
NAME = re.compile(r"^V(\d+(?:[._]\d+)*)__(.+)\.sql$", re.IGNORECASE)
_local = threading.Lock()
_running: set[str] = set()

DDL = [
    f"CREATE TABLE {TABLE} (MIGRATION_VERSION VARCHAR(50) NOT NULL PRIMARY KEY, DESCR VARCHAR(200), "
    "SCRIPT_NAME VARCHAR(255), CHECKSUM CHAR(64), KIND VARCHAR(10) NOT NULL, SUCCESS SMALLINT NOT NULL, "
    "INSTALLED_BY VARCHAR(63), INSTALLED_AT TIMESTAMP DEFAULT CURRENT_TIMESTAMP NOT NULL, EXECUTION_MS INTEGER, "
    "SOURCE_REF VARCHAR(120), ERROR_TEXT VARCHAR(2000))",
    f"CREATE TABLE {LOCK_TABLE} (ID SMALLINT NOT NULL PRIMARY KEY, LOCKED_AT TIMESTAMP, LOCKED_BY VARCHAR(100))",
]


# ------------------------------------------------------------------ scripts
class Script(BaseModel):
    version: str
    key: tuple[int, ...]
    description: str
    name: str
    sql: str
    checksum: str


def checksum(text: str) -> str:
    """SHA-256 of the script with line endings and a BOM normalised (a checkout on Windows is the same script)."""
    norm = text.lstrip("﻿").replace("\r\n", "\n").replace("\r", "\n")
    return hashlib.sha256(norm.encode("utf-8")).hexdigest()


def parse(files: list[tuple[str, str]]) -> tuple[list[Script], list[str]]:
    """Scripts from (file name, text) pairs, ordered by version; plus warnings for files that aren't."""
    out: dict[tuple[int, ...], Script] = {}
    warnings: list[str] = []
    for name, text in files:
        base = name.replace("\\", "/").rsplit("/", 1)[-1]
        if not base.lower().endswith(".sql"):
            continue
        m = NAME.match(base)
        if not m:
            warnings.append(f"{name} is ignored: migration scripts are named V<version>__<description>.sql")
            continue
        parts = re.split(r"[._]", m.group(1))
        key = tuple(int(p) for p in parts)
        while len(key) > 1 and key[-1] == 0:  # 1.0 is version 1
            key = key[:-1]
        version = ".".join(str(p) for p in key)
        if key in out:
            raise ValueError(f"Two scripts have version {version}: {out[key].name} and {name}")
        out[key] = Script(version=version, key=key, description=m.group(2).replace("_", " ").strip(), name=name,
                          sql=text.lstrip("﻿"), checksum=checksum(text))
    return [out[k] for k in sorted(out)], warnings


def version_key(v: str) -> tuple[int, ...]:
    if not re.fullmatch(r"\d+(?:[._]\d+)*", v or ""):
        raise ValueError(f"A version is numbers separated by dots, like 1, 1.2 or 2026.10.30 (not {v!r})")
    key = tuple(int(p) for p in re.split(r"[._]", v))
    while len(key) > 1 and key[-1] == 0:
        key = key[:-1]
    return key


def read_dir(path: Path) -> list[tuple[str, str]]:
    if not path.is_dir():
        raise ValueError(f"{path} is not a directory")
    return [(str(p.relative_to(path)).replace("\\", "/"), p.read_text("utf-8"))
            for p in sorted(path.rglob("*.sql")) if p.is_file() and ".git" not in p.parts]


# ------------------------------------------------------------------ the record in the database
def _has(conn, table: str) -> bool:
    if not re.fullmatch(r"[A-Z_]+", table):
        raise ValueError(table)
    cur = conn.cursor()
    # a literal: a parameter compared with RDB$RELATION_NAME finds nothing on Firebird 6 snapshots
    cur.execute(f"SELECT COUNT(*) FROM RDB$RELATIONS WHERE RDB$RELATION_NAME = '{table}'")
    n = cur.fetchone()[0]
    conn.commit()
    return bool(n)


def ensure_tables(conn) -> None:
    for table, ddl in zip((TABLE, LOCK_TABLE), DDL, strict=True):
        if _has(conn, table):
            continue
        try:
            conn.cursor().execute(ddl)
            conn.commit()
        except Exception as e:  # noqa: BLE001 - another run created it meanwhile
            conn.rollback()
            if "exist" not in str(e).lower() and "unique" not in str(e).lower():
                raise
    cur = conn.cursor()
    cur.execute(f"SELECT COUNT(*) FROM {LOCK_TABLE}")
    if not cur.fetchone()[0]:
        try:
            cur.execute(f"INSERT INTO {LOCK_TABLE} (ID) VALUES (1)")
            conn.commit()
        except Exception:  # noqa: BLE001
            conn.rollback()
    conn.commit()


def applied(conn) -> list[dict[str, Any]]:
    if not _has(conn, TABLE):
        return []
    cur = conn.cursor()
    cur.execute(f"SELECT TRIM(MIGRATION_VERSION), DESCR, SCRIPT_NAME, CHECKSUM, TRIM(KIND), SUCCESS, INSTALLED_BY, "
                f"INSTALLED_AT, EXECUTION_MS, SOURCE_REF, ERROR_TEXT FROM {TABLE}")
    rows = []
    for v, d, s, c, k, ok, by, at, ms, ref, err in cur.fetchall():
        rows.append({"version": v, "key": version_key(v), "description": d, "script": s,
                     "checksum": (c or "").strip() or None, "kind": k, "success": bool(ok), "installed_by": by,
                     "installed_at": at.timestamp() if at else None, "execution_ms": ms, "source_ref": ref,
                     "error": err})
    conn.commit()
    return sorted(rows, key=lambda r: r["key"])


def _record(conn, *, version: str, description: str | None, script: str | None, checksum_: str | None, kind: str,
            success: bool, user: str | None, ms: int | None, ref: str | None, error: str | None) -> None:
    cur = conn.cursor()
    cur.execute(f"DELETE FROM {TABLE} WHERE MIGRATION_VERSION = ?", (version,))
    cur.execute(f"INSERT INTO {TABLE} (MIGRATION_VERSION, DESCR, SCRIPT_NAME, CHECKSUM, KIND, SUCCESS, INSTALLED_BY, "
                "EXECUTION_MS, SOURCE_REF, ERROR_TEXT) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (version, (description or "")[:200], (script or "")[:255] or None, checksum_, kind, int(success),
                 (user or "")[:63] or None, ms, (ref or "")[:120] or None, (error or "")[:2000] or None))
    conn.commit()


# ------------------------------------------------------------------ status
def status(scripts: list[Script], rows: list[dict[str, Any]], *, out_of_order: bool = False,
           target: str | None = None) -> dict[str, Any]:
    """Every script and record with its state, the pending scripts in order, and what blocks a run."""
    by_v = {r["key"]: r for r in rows}
    baseline = max((r["key"] for r in rows if r["kind"] == "BASELINE"), default=None)
    done = [r["key"] for r in rows if r["kind"] == "SQL" and r["success"]]
    top = max(done + ([baseline] if baseline else []), default=None)
    tkey = version_key(target) if target else None
    items: list[dict[str, Any]] = []
    pending: list[Script] = []
    errors: list[str] = []
    warnings: list[str] = []
    for s in scripts:
        r = by_v.get(s.key)
        item: dict[str, Any] = {"version": s.version, "description": s.description, "script": s.name,
                                "checksum": s.checksum}
        if r and r["kind"] == "SQL":
            item.update(installed_at=r["installed_at"], installed_by=r["installed_by"],
                        execution_ms=r["execution_ms"], source_ref=r["source_ref"])
            if not r["success"]:
                item["state"] = "failed"
                item["error"] = r["error"]
                errors.append(f"{s.name} failed on {_when(r)}: fix the database by hand, then repair (it removes "
                              "the failed record) and run again")
            elif r["checksum"] and r["checksum"] != s.checksum:
                item["state"] = "changed"
                errors.append(f"{s.name} was changed after it was applied: undo the edit and write a new script "
                              "(or repair, to accept the new checksum)")
            else:
                item["state"] = "applied"
        elif r and r["kind"] == "BASELINE" or (baseline and s.key <= baseline):
            item["state"] = "baseline"
        elif tkey and s.key > tkey:
            item["state"] = "above_target"
        elif top and s.key < top:
            item["state"] = "late"
            if out_of_order:
                pending.append(s)
            else:
                errors.append(f"{s.name} has a lower version than one already applied ({'.'.join(map(str, top))}): "
                              "allow out-of-order scripts, or give it a higher version")
        else:
            item["state"] = "pending"
            pending.append(s)
        items.append(item)
    names = {s.key for s in scripts}
    for r in rows:
        if r["key"] not in names and r["kind"] == "SQL":
            items.append({"version": r["version"], "description": r["description"], "script": r["script"],
                          "checksum": r["checksum"], "state": "missing" if r["success"] else "failed",
                          "installed_at": r["installed_at"], "installed_by": r["installed_by"],
                          "execution_ms": r["execution_ms"], "source_ref": r["source_ref"], "error": r["error"]})
            if r["success"]:
                warnings.append(f"Version {r['version']} ({r['script']}) was applied but its script is gone")
            else:
                errors.append(f"Version {r['version']} ({r['script']}) failed and its script is gone: repair")
    items.sort(key=lambda i: version_key(i["version"]))
    if baseline and baseline not in names:  # the baseline's own line, when no script has its version
        items.insert(0, {"version": ".".join(map(str, baseline)), "description": next(
            r["description"] for r in rows if r["key"] == baseline), "script": None, "checksum": None,
            "state": "baseline_record"})
        items.sort(key=lambda i: version_key(i["version"]))
    return {"items": items, "pending": [p.version for p in pending], "errors": errors, "warnings": warnings,
            "current": ".".join(map(str, top)) if top else None, "fingerprint": fingerprint(pending, rows),
            "_pending": pending}


def _when(r: dict) -> str:
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(r["installed_at"])) if r.get("installed_at") else "?"


def fingerprint(pending: list[Script], rows: list[dict[str, Any]]) -> str:
    """The pending scripts and the database's record: a dry run is valid for an apply with the same one."""
    h = hashlib.sha256()
    for s in pending:
        h.update(f"{s.version}\0{s.checksum}\0".encode())
    h.update(b"|")
    for r in rows:
        h.update(f"{r['version']}\0{r['checksum']}\0{r['success']}\0".encode())
    return h.hexdigest()[:24]


def public(st: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in st.items() if not k.startswith("_")}


# ------------------------------------------------------------------ running scripts
@contextmanager
def lock(connect, key: str, who: str):
    """One run per database: in this process, and across processes (a row lock held by a NO WAIT transaction)."""
    from .query import TxOptions, apply_tpb

    with _local:
        if key in _running:
            raise RuntimeError("A migration run on this database is in progress")
        _running.add(key)
    conn = None
    try:
        conn = connect()
        ensure_tables(conn)
        apply_tpb(conn, TxOptions(wait=False).tpb())
        try:
            conn.cursor().execute(f"UPDATE {LOCK_TABLE} SET LOCKED_AT = CURRENT_TIMESTAMP, LOCKED_BY = ? WHERE ID = 1",
                                  (who[:100],))
        except Exception as e:  # noqa: BLE001 - update conflict: another fbadmin or CLI holds it
            raise RuntimeError(f"A migration run on this database is in progress elsewhere ({e})") from None
        yield
    finally:
        if conn is not None:
            try:
                conn.rollback()
                conn.close()
            except Exception:  # noqa: BLE001
                pass
        with _local:
            _running.discard(key)


def run_scripts(conn, pending: list[Script], *, user: str | None, ref: str | None, log, record: bool = True,
                stop_on_error: bool = True) -> list[dict[str, Any]]:
    """Run the pending scripts in order (each statement committed: Firebird DDL needs it) and record them."""
    from .query import run_script

    results = []
    for s in pending:
        log(f"Running {s.name}")
        t0 = time.time()
        res = run_script(conn, s.sql, autocommit=True, max_rows=10)
        ms = round((time.time() - t0) * 1000)
        statements = sum(1 for i in res["results"] if i["kind"] != "ignored")
        item = {"version": s.version, "script": s.name, "ok": res["error"] is None, "ms": ms, "statements": statements}
        if res["error"]:
            e = res["error"]
            item["error"] = {"message": e["message"], "line": e["line"], "sql": e["sql"][:2000]}
            log(f"FAILED {s.name}, line {e['line']}: {e['message']}")
        else:
            log(f"Applied {s.name} ({statements} statement(s), {ms} ms)")
        if record:
            _record(conn, version=s.version, description=s.description, script=s.name, checksum_=s.checksum,
                    kind="SQL", success=item["ok"], user=user, ms=ms, ref=ref,
                    error=None if item["ok"] else f"line {item['error']['line']}: {item['error']['message']}")
        results.append(item)
        if not item["ok"] and stop_on_error:
            break
    return results


def apply(connect, scripts: list[Script], *, key: str, user: str | None, ref: str | None, log,
          out_of_order: bool = False, target: str | None = None) -> dict[str, Any]:
    """Apply the pending scripts (status rules first); returns the report."""
    t0 = time.time()
    with lock(connect, key, user or "fbadmin"):
        conn = connect()
        try:
            ensure_tables(conn)
            st = status(scripts, applied(conn), out_of_order=out_of_order, target=target)
            if st["errors"]:
                raise ValueError(" ".join(st["errors"]))
            pending = st["_pending"]
            if not pending:
                log("Nothing to apply: the database is up to date")
            results = run_scripts(conn, pending, user=user, ref=ref, log=log)
            after = applied(conn)
        finally:
            conn.close()
    ok = all(r["ok"] for r in results)
    return {"ok": ok, "applied": [r for r in results if r["ok"]], "failed": next((r for r in results if not r["ok"]), None),
            "results": results, "seconds": round(time.time() - t0, 1), "source_ref": ref,
            "current": public(status(scripts, after, out_of_order=out_of_order))["current"]}


def baseline(conn, version: str, description: str, *, user: str | None) -> None:
    version_key(version)
    ensure_tables(conn)
    if any(r["kind"] == "SQL" for r in applied(conn)):
        raise ValueError("Scripts were applied to this database already: a baseline is for databases without a "
                         "migration history")
    _record(conn, version=".".join(map(str, version_key(version))), description=description or "Baseline",
            script=None, checksum_=None, kind="BASELINE", success=True, user=user, ms=None, ref=None, error=None)


def repair(conn, scripts: list[Script]) -> dict[str, Any]:
    """Remove failed records, and accept the current checksum of changed scripts."""
    removed, realigned = [], []
    cur = conn.cursor()
    by_v = {s.key: s for s in scripts}
    for r in applied(conn):
        if not r["success"]:
            cur.execute(f"DELETE FROM {TABLE} WHERE MIGRATION_VERSION = ?", (r["version"],))
            removed.append(r["version"])
        elif r["kind"] == "SQL" and r["key"] in by_v and r["checksum"] != by_v[r["key"]].checksum:
            cur.execute(f"UPDATE {TABLE} SET CHECKSUM = ? WHERE MIGRATION_VERSION = ?",
                        (by_v[r["key"]].checksum, r["version"]))
            realigned.append(r["version"])
    conn.commit()
    return {"removed_failed": removed, "accepted_checksums": realigned}


# ------------------------------------------------------------------ dry run
def scratch_path(db_path: str, key: str, override: str | None = None) -> str:
    if override:
        return override
    m = re.match(r"^(.*[\\/])[^\\/]+$", db_path)
    if not m:
        raise ValueError(f"The database is registered by alias ({db_path!r}): give the dry run's scratch path")
    safe = re.sub(r"[^A-Za-z0-9_]", "_", key)[:40]
    return f"{m.group(1)}{SCRATCH_PREFIX}{safe}.fdb"


def dry_run(*, pending: list[Script], db_path: str, scratch: str, copy: Literal["metadata", "full"], svc_factory,
            connect, registered: list[str], user: str | None, ref: str | None, log) -> dict[str, Any]:
    """Restore a copy of the database next to it, run the pending scripts there, report and drop the copy."""
    from .maintenance import drift, ops, verify
    from .schemahistory import collect_snapshots

    verify.check_scratch(scratch, registered)
    ours = Path(scratch.replace("\\", "/")).name.lower().startswith(SCRATCH_PREFIX)
    t0 = time.time()
    work = settings.data_dir / "migrations" / "work"
    fbk = work / f"dry_{uuid.uuid4().hex[:8]}.fbk"
    report: dict[str, Any] = {"copy": copy, "scratch": scratch, "scripts": [], "started_at": t0}
    try:
        with svc_factory() as svc:
            major = ops.option_major(svc)
            log(f"Copying the database ({'metadata only' if copy == 'metadata' else 'with its data'}) to {scratch}")
            ops.backup_to_local_file(svc, db_path, fbk, ops.BackupOptions(metadata_only=copy == "metadata"), major,
                                     lambda _m: None)
            ops.restore_from_local_file(svc, fbk, scratch, ops.RestoreOptions(replace=ours), major, lambda _m: None)
    finally:
        fbk.unlink(missing_ok=True)
    report["copy_seconds"] = round(time.time() - t0, 1)
    conn = connect(scratch)
    try:
        before = {s or "": snap for s, snap in collect_snapshots(conn)}
        conn.commit()
        report["scripts"] = run_scripts(conn, pending, user=user, ref=ref, log=log, record=False)
        after = {s or "": snap for s, snap in collect_snapshots(conn)}
        conn.commit()
        cmp = drift.compare(after, before, drift.DriftOptions(ignore_names=[f"{TABLE}*"]),
                            header="/* Schema changes made by the pending migrations (dry run) */\n")
        report["changes"] = [{"schema": i["schema"], "type": i["type"], "name": i["name"],
                              "change": {"missing": "created", "extra": "dropped", "changed": "altered"}[i["status"]],
                              "before": i["database_ddl"], "after": i["baseline_ddl"]} for i in cmp["items"]]
        report["summary"] = {c: sum(1 for x in report["changes"] if x["change"] == c)
                             for c in ("created", "altered", "dropped")}
    finally:
        try:
            conn.drop_database()
            log(f"Dropped the copy {scratch}")
        except Exception as e:  # noqa: BLE001
            log(f"Could not drop the copy {scratch}: {e}")
            conn.close()
    report["ok"] = all(s["ok"] for s in report["scripts"])
    report["seconds"] = round(time.time() - t0, 1)
    report["finished_at"] = time.time()
    log("Dry run passed" if report["ok"] else "Dry run FAILED")
    return report


# ------------------------------------------------------------------ sources (git, directory)
class Source(BaseModel):
    kind: Literal["git", "directory"] = "git"
    url: str | None = None                 # git: remote URL or a local repository path
    ref: str = "main"                      # git: branch, tag or commit
    path: str = ""                         # git: folder in the repository; directory: the directory
    token: str | None = None               # git over HTTPS: env:NAME
    token_user: str = "x-access-token"
    out_of_order: bool = False
    copy_mode: Literal["metadata", "full"] = "metadata"   # what the dry run copies
    scratch: str | None = None             # the dry run's copy (default: next to the database)

    @field_validator("url", "path", "scratch")
    @classmethod
    def _line(cls, v: str | None) -> str | None:
        if v is not None and any(c in v for c in "\0\r\n"):
            raise ValueError("A single line")
        return v.strip() if v else v


def repo_dir(key: str) -> Path:
    return settings.data_dir / "migrations" / "repos" / re.sub(r"[^A-Za-z0-9_-]", "_", key)


def read_source(src: Source, key: str, log=lambda _m: None) -> tuple[list[tuple[str, str]], str | None]:
    """(files, source ref) of a configured source."""
    if src.kind == "directory":
        root = Path(src.path).expanduser()
        _check_directory(root)
        return read_dir(root), str(root)
    from .schemahistory import _resolve_token, check_remote, check_subdir, check_token
    from .schemahistory.gitrepo import GitError, Repo, check_ref

    url = check_remote(src.url)
    if not url:
        raise ValueError("The git repository URL (or path) is required")
    sub = check_subdir(src.path)
    ref = check_ref(src.ref or "main")
    repo = Repo(repo_dir(key), token=_resolve_token(check_token(src.token)), token_user=src.token_user)
    if not repo.is_repo():
        repo.path.mkdir(parents=True, exist_ok=True)
        repo.run("init", "-q")
    repo.set_remote(url)
    log(f"Fetching {url}")
    repo.run("fetch", "--quiet", "--prune", "--tags", "origin", auth=True, timeout=300)
    sha = None
    for cand in (f"refs/remotes/origin/{ref}", f"refs/tags/{ref}", ref):
        p = repo.run("rev-parse", "--verify", "-q", f"{cand}^{{commit}}", check=False)
        if p.returncode == 0 and p.stdout.strip():
            sha = p.stdout.strip()
            break
    if not sha:
        raise ValueError(f"No branch, tag or commit {ref!r} in {url}")
    names = [n for n in repo.out("ls-tree", "-r", "--name-only", sha, "--", sub or ".").splitlines()
             if n.lower().endswith(".sql")]
    files = []
    for n in names:
        try:
            text = repo.out("show", f"{sha}:{n}")
        except GitError as e:
            raise ValueError(str(e)) from None
        rel = n[len(sub) + 1:] if sub and n.startswith(sub + "/") else n
        files.append((rel, text))
    return files, f"{ref}@{sha[:10]}"


def _check_directory(root: Path) -> None:
    allowed = os.environ.get("FBADMIN_MIGRATIONS_ROOT")
    if _server_mode():
        if not allowed:
            raise ValueError("In server mode migration directories must be inside FBADMIN_MIGRATIONS_ROOT (not set): "
                             "use a git repository")
        real, base = Path(os.path.realpath(root)), Path(os.path.realpath(allowed))
        if real != base and base not in real.parents:
            raise ValueError(f"In server mode the directory must be inside {allowed} (FBADMIN_MIGRATIONS_ROOT)")
    if not root.is_dir():
        raise ValueError(f"{root} is not a directory on the fbadmin host")


def _server_mode() -> bool:
    try:
        from .api.deps import get_state

        return bool(get_state().security.server_mode)
    except Exception:  # noqa: BLE001
        return False


# ------------------------------------------------------------------ per-database settings and the last dry run
class Store:
    def __init__(self, data_dir: Path | None = None):
        self.file = Path(data_dir or settings.data_dir) / "migrations" / "config.json"
        self._lock = threading.Lock()

    def _load(self) -> dict[str, Any]:
        return json.loads(self.file.read_text()) if self.file.exists() else {"sources": {}, "dry_runs": {}}

    def _save(self, d: dict[str, Any]) -> None:
        self.file.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.file.with_suffix(".tmp")
        tmp.write_text(json.dumps(d, indent=1, default=str))
        tmp.replace(self.file)

    def get(self, key: str) -> Source | None:
        s = self._load()["sources"].get(key)
        return Source(**s) if s else None

    def put(self, key: str, src: Source | None) -> None:
        with self._lock:
            d = self._load()
            if src is None:
                d["sources"].pop(key, None)
                d["dry_runs"].pop(key, None)
            else:
                d["sources"][key] = src.model_dump()
            self._save(d)

    def save_dry_run(self, key: str, report: dict[str, Any]) -> None:
        with self._lock:
            d = self._load()
            d["dry_runs"][key] = report
            self._save(d)

    def dry_run(self, key: str) -> dict[str, Any] | None:
        return self._load()["dry_runs"].get(key)


def store() -> Store:
    return Store()


def dry_run_valid(report: dict[str, Any] | None, fp: str) -> str | None:
    """Why the last dry run doesn't cover an apply with fingerprint ``fp`` (None: it does)."""
    if not report:
        return "Run a dry run first"
    if not report.get("ok"):
        return "The last dry run failed: fix the scripts and run the dry run again"
    if report.get("fingerprint") != fp:
        return "The scripts or the database changed since the last dry run: run it again"
    if time.time() - (report.get("finished_at") or 0) > DRY_RUN_VALID:
        return "The last dry run is older than 24 hours: run it again"
    return None


__all__ = ["Script", "Source", "Store", "apply", "applied", "baseline", "checksum", "dry_run", "dry_run_valid",
           "ensure_tables", "parse", "public", "read_dir", "read_source", "repair", "scratch_path", "status",
           "store"]
