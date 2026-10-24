"""Maintenance and server tools (milestone 0.5).

Backup/restore streamed through the browser, nbackup, gfix operations, limbo
transactions, shutdown/online, database properties, gstat charts, encryption
and the RDB$CONFIG viewer. Long operations run as background jobs.
"""

from __future__ import annotations

import threading
import time
import uuid
from collections.abc import Iterator
from pathlib import Path

import firebirdsql
from fastapi import APIRouter, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel, Field

from ..config import settings
from ..connections import connect
from ..maintenance import archive, gstat, ops
from ..metadata.types import quote_ident
from ..store import DatabaseEntry
from .deps import get_state

router = APIRouter(prefix="/api/servers/{server_id}", tags=["maintenance"])
DB = "/databases/{db_id}/maintenance"


def _db(server_id: str, db_id: str) -> DatabaseEntry:
    return get_state().store.get_database(server_id, db_id)


def _major(server_id: str) -> int:
    """The version whose service options may be used (see ops.option_major)."""
    with get_state().manager.service(server_id) as svc:
        return ops.option_major(svc)


def _job(kind: str, description: str, server_id: str, fn) -> dict:
    return get_state().jobs.start(kind, description, server_id, fn).summary()


def _register(server_id: str, name: str | None, path: str, log) -> None:
    if name:
        get_state().store.add_database(server_id, DatabaseEntry(name=name, path=path))
        log(f"Registered database as {name!r}")


@router.get("/maintenance/capabilities")
def capabilities(server_id: str):
    with get_state().manager.service(server_id) as svc:
        version = svc.getServerVersion()
        major = ops.engine_major(svc)
        usable = ops.option_major(svc)  # lower with an older client library (native driver)
    return {"server_version": version, "engine_major": major, "option_major": usable,
            "features": ops.capabilities(usable)}


# ===================================================================== backup
class BackupIn(BaseModel):
    target: str = "archive"  # archive (fbadmin host) | server (path on the Firebird server)
    backup_file: str | None = None
    options: ops.BackupOptions = Field(default_factory=ops.BackupOptions)


class _Download:
    def __init__(self, server_id: str, filename: str, ctx, chunks: Iterator[bytes]):
        self.server_id = server_id
        self.filename = filename
        self.ctx = ctx
        self.chunks = chunks
        self.created = time.time()
        self.claimed = False

    def close(self) -> None:
        try:
            self.ctx.__exit__(None, None, None)
        except Exception:  # noqa: BLE001
            pass


_downloads: dict[str, _Download] = {}
_dl_lock = threading.Lock()
DOWNLOAD_TTL = 120


def _expire_downloads() -> None:
    now = time.time()
    with _dl_lock:
        for tok in [t for t, d in _downloads.items() if not d.claimed and now - d.created > DOWNLOAD_TTL]:
            _downloads.pop(tok).close()


@router.post(DB + "/backup/stream")
def prepare_backup_download(server_id: str, db_id: str, body: ops.BackupOptions):
    """Start a backup to stdout and return a one-time download token.

    Option errors are reported here (as JSON); the browser then fetches
    ``/maintenance/downloads/{token}`` which streams the .fbk without buffering it.
    """
    _expire_downloads()
    db = _db(server_id, db_id)
    ctx = get_state().manager.service(server_id)
    svc = ctx.__enter__()
    try:
        major = ops.option_major(svc)
        chunks = ops.backup_stream(svc, db.path, body, major)
    except BaseException:
        ctx.__exit__(None, None, None)
        raise
    stamp = time.strftime("%Y%m%d-%H%M%S")
    safe = "".join(c if c.isalnum() or c in "-_." else "_" for c in db.name)
    token = uuid.uuid4().hex
    with _dl_lock:
        _downloads[token] = _Download(server_id, f"{safe}_{stamp}.fbk", ctx, chunks)
    return {"token": token, "filename": _downloads[token].filename,
            "url": f"/api/servers/{server_id}/maintenance/downloads/{token}"}


@router.get("/maintenance/downloads/{token}")
def download_backup(server_id: str, token: str):
    with _dl_lock:
        d = _downloads.get(token)
        if d is None or d.claimed or d.server_id != server_id:
            raise LookupError("Unknown or expired download")
        d.claimed = True
        _downloads.pop(token)

    def gen():
        try:
            yield from d.chunks
        finally:
            d.close()

    return StreamingResponse(gen(), media_type="application/octet-stream",
                             headers={"Content-Disposition": f'attachment; filename="{d.filename}"'})


@router.post(DB + "/backup", status_code=202)
def backup_job(server_id: str, db_id: str, body: BackupIn):
    """Backup as a background job: into the fbadmin archive (streamed) or to a file on the server."""
    st = get_state()
    db = _db(server_id, db_id)
    if body.target == "server" and not body.backup_file:
        raise ValueError("backup_file (a path on the Firebird server) is required")

    def run(log):
        with st.manager.service(server_id) as svc:
            major = ops.option_major(svc)
            if body.target == "server":
                log(f"Backing up {db.path} to {body.backup_file} (on the server)")
                ops.backup_server_file(svc, db.path, body.backup_file, body.options, major, log)
            else:
                dest = archive.new_path("manual", db.name)
                log(f"Backing up {db.path} into the fbadmin archive")
                ops.backup_to_local_file(svc, db.path, dest, body.options, major, log)
        log("Backup completed")

    return _job("backup", f"Backup {db.name}", server_id, run)


# ==================================================================== restore
class RestoreIn(BaseModel):
    source: str = "server"  # server (path on the Firebird server) | archive (fbadmin archive path)
    backup_file: str
    database_path: str
    register_as: str | None = None
    options: ops.RestoreOptions = Field(default_factory=ops.RestoreOptions)


def _restore_job(server_id: str, description: str, local: Path | None, server_file: str | None,
                 database_path: str, o: ops.RestoreOptions, register_as: str | None, cleanup: bool) -> dict:
    st = get_state()

    def run(log):
        try:
            with st.manager.service(server_id) as svc:
                major = ops.option_major(svc)
                if local is not None:
                    log(f"Restoring {local.name} ({local.stat().st_size} bytes) to {database_path} via stdin")
                    ops.restore_from_local_file(svc, local, database_path, o, major, log)
                else:
                    log(f"Restoring {server_file} to {database_path}")
                    ops.restore_server_file(svc, server_file, database_path, o, major, log)
            log("Restore completed")
            _register(server_id, register_as, database_path, log)
        finally:
            if cleanup and local is not None:
                local.unlink(missing_ok=True)

    return _job("restore", description, server_id, run)


@router.post("/maintenance/restore", status_code=202)
def restore_job(server_id: str, body: RestoreIn):
    if body.source == "archive":
        local = archive.resolve(body.backup_file)
        return _restore_job(server_id, f"Restore {local.name}", local, None, body.database_path, body.options,
                            body.register_as, cleanup=False)
    return _restore_job(server_id, f"Restore {body.backup_file}", None, body.backup_file, body.database_path,
                        body.options, body.register_as, cleanup=False)


@router.post("/maintenance/restore/upload", status_code=202)
async def restore_upload(server_id: str, request: Request, database_path: str, filename: str = "upload.fbk",
                         register_as: str | None = None, keep: bool = False, replace: bool = False,
                         page_size: int | None = None, buffers: int | None = None,
                         deactivate_indexes: bool = False, no_validity: bool = False, one_at_a_time: bool = False,
                         use_all_space: bool = False, metadata_only: bool = False, read_only: bool = False,
                         parallel_workers: int | None = None, skip_data: str | None = None,
                         include_data: str | None = None, crypt_plugin: str | None = None,
                         keyholder: str | None = None, key_name: str | None = None,
                         fix_fss_data: str | None = None, fix_fss_metadata: str | None = None):
    """Upload a .fbk (raw request body, streamed to disk in chunks) and restore it via stdin.

    With ``keep=true`` the upload is kept in the fbadmin archive (folder ``uploads``).
    """
    o = ops.RestoreOptions(replace=replace, page_size=page_size, buffers=buffers,
                           deactivate_indexes=deactivate_indexes, no_validity=no_validity,
                           one_at_a_time=one_at_a_time, use_all_space=use_all_space, metadata_only=metadata_only,
                           read_only=read_only, parallel_workers=parallel_workers, skip_data=skip_data,
                           include_data=include_data, crypt_plugin=crypt_plugin, keyholder=keyholder,
                           key_name=key_name, fix_fss_data=fix_fss_data, fix_fss_metadata=fix_fss_metadata)
    get_state().store.get(server_id)
    if keep:
        dest = archive.new_path("uploads", Path(filename).stem)
    else:
        dest = settings.data_dir / "uploads" / f"{uuid.uuid4().hex}.fbk"
    dest.parent.mkdir(parents=True, exist_ok=True)
    size = 0
    f = open(dest, "wb")  # noqa: SIM115
    try:
        async for chunk in request.stream():
            if chunk:
                await run_in_threadpool(f.write, chunk)
                size += len(chunk)
    except BaseException:
        f.close()
        dest.unlink(missing_ok=True)
        raise
    f.close()
    if size == 0:
        dest.unlink(missing_ok=True)
        raise ValueError("The uploaded file is empty")
    return _restore_job(server_id, f"Restore upload {filename}", dest, None, database_path, o, register_as,
                        cleanup=not keep)


# ==================================================================== archive
@router.get("/maintenance/archive")
def archive_list(server_id: str):
    return archive.list_backups()


@router.get("/maintenance/archive/download")
def archive_download(server_id: str, path: str):
    p = archive.resolve(path)
    return FileResponse(p, media_type="application/octet-stream", filename=p.name)


@router.delete("/maintenance/archive", status_code=204)
def archive_delete(server_id: str, path: str):
    archive.delete(path)


# ==================================================================== nbackup
class NbackupIn(BaseModel):
    level: int = 0
    file: str
    direct: bool | None = None
    no_triggers: bool = False
    guid: str | None = None


class NrestoreIn(BaseModel):
    files: list[str]
    database_path: str
    inplace: bool = False
    register_as: str | None = None


@router.post(DB + "/nbackup", status_code=202)
def nbackup(server_id: str, db_id: str, body: NbackupIn):
    db = _db(server_id, db_id)

    def run(log):
        log(f"nbackup level {body.level} of {db.path} to {body.file}")
        with get_state().manager.service(server_id) as svc:
            ops.nbackup(svc, db.path, body.level, body.file, direct=body.direct, no_triggers=body.no_triggers,
                        guid=body.guid, log=log)
        log("nbackup completed")

    return _job("nbackup", f"nbackup L{body.level} {db.name}", server_id, run)


@router.post("/maintenance/nrestore", status_code=202)
def nrestore(server_id: str, body: NrestoreIn):
    def run(log):
        log(f"Restoring chain {', '.join(body.files)} to {body.database_path}")
        with get_state().manager.service(server_id) as svc:
            ops.nrestore(svc, body.files, body.database_path, inplace=body.inplace, log=log)
        log("nbackup restore completed")
        _register(server_id, body.register_as, body.database_path, log)

    return _job("nrestore", f"nbackup restore {body.database_path}", server_id, run)


def _sql(server_id: str, db_id: str, *statements: str) -> None:
    """Run statements on a fresh attachment (not the shared metadata connection)."""
    st = get_state()
    srv = st.store.get(server_id)
    conn = connect(srv, _db(server_id, db_id).path, st.manager._password(server_id))
    try:
        cur = conn.cursor()
        for s in statements:
            cur.execute(s)
            conn.commit()
    finally:
        conn.close()


@router.post(DB + "/nbackup/lock")
def nbackup_lock(server_id: str, db_id: str):
    """ALTER DATABASE BEGIN BACKUP (nbackup -L): the file can now be copied safely."""
    _sql(server_id, db_id, "ALTER DATABASE BEGIN BACKUP")
    return database_state(server_id, db_id)


@router.post(DB + "/nbackup/unlock")
def nbackup_unlock(server_id: str, db_id: str):
    """ALTER DATABASE END BACKUP (nbackup -N): merge the delta file back."""
    _sql(server_id, db_id, "ALTER DATABASE END BACKUP")
    return database_state(server_id, db_id)


class PathIn(BaseModel):
    database_path: str | None = None


@router.post(DB + "/nbackup/fixup")
def nbackup_fixup(server_id: str, db_id: str, body: PathIn):
    """nbackup -F: mark a copied, locked database file as normal (Firebird 4+ via Services API)."""
    path = body.database_path or _db(server_id, db_id).path
    get_state().manager.disconnect(server_id, db_id)
    lines: list[str] = []
    with get_state().manager.service(server_id) as svc:
        ops.nfix(svc, path, ops.option_major(svc), lines.append)
    return {"ok": True, "output": lines}


@router.get(DB + "/nbackup/history")
def nbackup_history(server_id: str, db_id: str):
    with get_state().manager.meta(server_id, db_id) as conn:
        cur = conn.cursor()
        cur.execute("SELECT RDB$BACKUP_ID, RDB$TIMESTAMP, RDB$BACKUP_LEVEL, RDB$GUID, RDB$SCN, "
                    "TRIM(RDB$FILE_NAME) FROM RDB$BACKUP_HISTORY ORDER BY RDB$BACKUP_ID")
        keys = ("id", "timestamp", "level", "guid", "scn", "file")
        return [dict(zip(keys, [v.isoformat() if hasattr(v, "isoformat") else v for v in r], strict=True))
                for r in cur.fetchall()]


# ======================================================================= gfix
class ValidateIn(BaseModel):
    mode: str = "online"  # online | full | check | mend
    tables_include: str | None = None
    tables_exclude: str | None = None
    indices_include: str | None = None
    indices_exclude: str | None = None
    lock_timeout: int | None = None
    ignore_checksums: bool = False


@router.post(DB + "/validate", status_code=202)
def validate(server_id: str, db_id: str, body: ValidateIn):
    db = _db(server_id, db_id)
    if body.mode not in ("online", "full", "check", "mend"):
        raise ValueError("mode must be online, full, check or mend")
    if body.mode != "online":
        get_state().manager.disconnect(server_id, db_id)

    def run(log):
        with get_state().manager.service(server_id) as svc:
            if body.mode == "online":
                log(f"Online validation of {db.path}")
                out = ops.validate_online(svc, db.path, tables_include=body.tables_include,
                                          tables_exclude=body.tables_exclude, indices_include=body.indices_include,
                                          indices_exclude=body.indices_exclude, lock_timeout=body.lock_timeout,
                                          log=log)
            else:
                log(f"Validation ({body.mode}) of {db.path}")
                out = ops.repair(svc, db.path, body.mode, ignore_checksums=body.ignore_checksums, log=log)
        if not out:
            log("No errors reported")
        log("Validation completed")

    return _job("validate", f"Validate ({body.mode}) {db.name}", server_id, run)


class RepairIn(BaseModel):
    parallel_workers: int | None = None


@router.post(DB + "/sweep", status_code=202)
def sweep(server_id: str, db_id: str, body: RepairIn | None = None):
    db = _db(server_id, db_id)
    workers = body.parallel_workers if body else None

    def run(log):
        log(f"Sweeping {db.path}")
        with get_state().manager.service(server_id) as svc:
            ops.repair(svc, db.path, "sweep", parallel_workers=workers, major=ops.option_major(svc), log=log)
        log("Sweep completed")

    return _job("sweep", f"Sweep {db.name}", server_id, run)


@router.post(DB + "/icu", status_code=202)
def fix_icu(server_id: str, db_id: str):
    """gfix -icu: rebuild indices that depend on ICU collations after an ICU upgrade."""
    db = _db(server_id, db_id)
    get_state().manager.disconnect(server_id, db_id)

    def run(log):
        with get_state().manager.service(server_id) as svc:
            ops.repair(svc, db.path, "icu", log=log)
        log("ICU fix completed")

    return _job("icu", f"Fix ICU {db.name}", server_id, run)


@router.get(DB + "/limbo")
def limbo_list(server_id: str, db_id: str):
    with get_state().manager.service(server_id) as svc:
        return ops.limbo_list(svc, _db(server_id, db_id).path)


@router.post(DB + "/limbo/{transaction_id}/{action}")
def limbo_resolve(server_id: str, db_id: str, transaction_id: int, action: str):
    with get_state().manager.service(server_id) as svc:
        out = ops.limbo_resolve(svc, _db(server_id, db_id).path, transaction_id, action)
    with get_state().manager.service(server_id) as svc:
        return {"output": out, "limbo": ops.limbo_list(svc, _db(server_id, db_id).path)}


# ============================================================ shutdown/online
class ShutdownIn(BaseModel):
    mode: str = "full"        # multi | single | full
    method: str = "force"     # force | attachments | transactions
    timeout: int = 0


class OnlineIn(BaseModel):
    mode: str = "normal"      # normal | multi | single


@router.post(DB + "/shutdown")
def shutdown(server_id: str, db_id: str, body: ShutdownIn):
    db = _db(server_id, db_id)
    get_state().manager.disconnect(server_id, db_id)
    with get_state().manager.service(server_id) as svc:
        ops.shutdown(svc, db.path, body.mode, body.method, body.timeout)
        return ops.header(svc, db.path)


@router.post(DB + "/online")
def online(server_id: str, db_id: str, body: OnlineIn):
    db = _db(server_id, db_id)
    with get_state().manager.service(server_id) as svc:
        ops.bring_online(svc, db.path, body.mode)
        return ops.header(svc, db.path)


# ================================================================ properties
def _mon_database(conn) -> dict:
    cur = conn.cursor()
    cur.execute("SELECT * FROM MON$DATABASE")
    row = cur.fetchone()
    names = [d[0].strip() for d in cur.description]
    data = {n.replace("MON$", "").lower(): v for n, v in zip(names, row, strict=True)}
    cur.execute("SELECT RDB$LINGER, TRIM(RDB$CHARACTER_SET_NAME) FROM RDB$DATABASE")
    linger, charset = cur.fetchone()
    data["linger"] = linger or 0
    data["default_charset"] = charset
    for k, v in list(data.items()):
        if hasattr(v, "isoformat"):
            data[k] = v.isoformat()
        elif isinstance(v, (bytes, bytearray)):
            data[k] = v.hex()
    return data


BACKUP_STATES = {0: "normal", 1: "locked", 2: "merge"}
CRYPT_STATES = {0: "not encrypted", 1: "encrypted", 2: "decrypting", 3: "encrypting"}


def database_state(server_id: str, db_id: str) -> dict:
    """Header page (via Services, works in shutdown) plus MON$DATABASE / RDB$DATABASE when attachable."""
    db = _db(server_id, db_id)
    with get_state().manager.service(server_id) as svc:
        major = ops.engine_major(svc)  # first: on FB6 a failed header read drops the service link
        hdr = ops.header(svc, db.path)
    out: dict = {"header": hdr, "engine_major": major, "database": None, "error": None}
    if hdr["shutdown_mode"] in ("full", "single"):
        out["error"] = f"Database is in {hdr['shutdown_mode']} shutdown; showing header page only"
        return out
    try:
        with get_state().manager.meta(server_id, db_id) as conn:
            d = _mon_database(conn)
        d["backup_state_name"] = BACKUP_STATES.get(d.get("backup_state"), str(d.get("backup_state")))
        if "crypt_state" in d:
            d["crypt_state_name"] = CRYPT_STATES.get(d["crypt_state"], str(d["crypt_state"]))
        pages = d.get("pages") or 0
        d["crypt_progress"] = round(100 * (d.get("crypt_page") or 0) / pages, 1) if pages else None
        out["database"] = d
    except firebirdsql.Error as exc:
        out["error"] = str(exc).strip()
    return out


@router.get(DB + "/properties")
def get_properties(server_id: str, db_id: str):
    return database_state(server_id, db_id)


class PropertiesIn(BaseModel):
    page_buffers: int | None = None
    sweep_interval: int | None = None
    forced_writes: bool | None = None
    reserve_space: bool | None = None
    read_only: bool | None = None
    dialect: int | None = None
    linger: int | None = None             # seconds, 0 = drop linger
    default_charset: str | None = None


@router.put(DB + "/properties")
def set_properties(server_id: str, db_id: str, body: PropertiesIn):
    st = get_state()
    db = _db(server_id, db_id)
    changed: list[str] = []
    sql = []
    if body.linger is not None:
        sql.append("ALTER DATABASE DROP LINGER" if body.linger <= 0 else f"ALTER DATABASE SET LINGER TO {int(body.linger)}")
        changed.append(f"Linger set to {max(0, body.linger)} s")
    if body.default_charset:
        sql.append(f"ALTER DATABASE SET DEFAULT CHARACTER SET {quote_ident(body.default_charset.upper())}")
        changed.append(f"Default character set {body.default_charset.upper()}")
    st.manager.disconnect(server_id, db_id)
    if sql:
        if body.read_only is False:
            # Leaving read-only mode must happen before DDL can run.
            with st.manager.service(server_id) as svc:
                changed += ops.set_properties(svc, db.path, read_only=False)
            body.read_only = None
        _sql(server_id, db_id, *sql)
    with st.manager.service(server_id) as svc:
        changed += ops.set_properties(svc, db.path, page_buffers=body.page_buffers,
                                      sweep_interval=body.sweep_interval, forced_writes=body.forced_writes,
                                      reserve_space=body.reserve_space, dialect=body.dialect,
                                      read_only=body.read_only)
    return {"changed": changed, "state": database_state(server_id, db_id)}


# ================================================================ statistics
class StatisticsIn(BaseModel):
    tables: list[str] | None = None
    system: bool = False
    record_versions: bool = True


@router.post(DB + "/gstat", status_code=202)
def gstat_job(server_id: str, db_id: str, body: StatisticsIn | None = None):
    """Run gstat (data + index pages + record versions) as a job; parse it with GET /gstat/{job_id}."""
    db = _db(server_id, db_id)
    b = body or StatisticsIn()

    def run(log):
        with get_state().manager.service(server_id) as svc:
            ops.statistics_text(svc, db.path, record_versions=b.record_versions, system=b.system, tables=b.tables,
                                log=log)

    return _job("gstat", f"Statistics {db.name}", server_id, run)


@router.get("/maintenance/gstat/{job_id}")
def gstat_result(server_id: str, job_id: str, index_depth: int | None = None):
    job = get_state().jobs.get(job_id)
    if job.status == "running":
        return {"status": "running", "lines": len(job.log)}
    if job.status == "failed":
        return {"status": "failed", "error": job.error}
    return {"status": "finished", **_analyse("\n".join(job.log), index_depth)}


class GstatText(BaseModel):
    text: str
    index_depth: int | None = None


@router.post("/maintenance/gstat/parse")
def gstat_parse(server_id: str, body: GstatText):
    """Parse gstat output produced elsewhere (e.g. pasted from the command line)."""
    return _analyse(body.text, body.index_depth)


def _analyse(text: str, index_depth: int | None) -> dict:
    stats = gstat.parse(text)
    th = {"index_depth": index_depth} if index_depth else None
    return {**stats, "attention": gstat.attention(stats, th), "text": text}


# ================================================================ encryption
class EncryptIn(BaseModel):
    plugin: str
    key_name: str | None = None


@router.post(DB + "/encrypt")
def encrypt(server_id: str, db_id: str, body: EncryptIn):
    if not body.plugin.strip():
        raise ValueError("Crypt plugin name is required")
    sql = f"ALTER DATABASE ENCRYPT WITH {quote_ident(body.plugin.strip())}"
    if body.key_name:
        sql += f" KEY {quote_ident(body.key_name.strip())}"
    _sql(server_id, db_id, sql)
    return crypt_state(server_id, db_id)


@router.post(DB + "/decrypt")
def decrypt(server_id: str, db_id: str):
    _sql(server_id, db_id, "ALTER DATABASE DECRYPT")
    return crypt_state(server_id, db_id)


@router.get(DB + "/crypt-state")
def crypt_state(server_id: str, db_id: str):
    with get_state().manager.meta(server_id, db_id) as conn:
        d = _mon_database(conn)
    state = d.get("crypt_state")
    return {"crypt_state": state, "crypt_state_name": CRYPT_STATES.get(state) if state is not None else None,
            "crypt_page": d.get("crypt_page"), "pages": d.get("pages"),
            "progress": round(100 * (d.get("crypt_page") or 0) / d["pages"], 1) if d.get("pages") else None,
            "supported_state": "crypt_state" in d}


# =================================================================== config
@router.get(DB + "/config")
def config(server_id: str, db_id: str):
    """firebird.conf / databases.conf values effective for this database (RDB$CONFIG, Firebird 4+)."""
    with get_state().manager.meta(server_id, db_id) as conn:
        cur = conn.cursor()
        cur.execute("SELECT 1 FROM RDB$RELATIONS WHERE RDB$RELATION_NAME = 'RDB$CONFIG'")
        if not cur.fetchone():
            return {"available": False, "rows": [],
                    "message": "RDB$CONFIG is not available: the configuration viewer needs Firebird 4 or later."}
        cur.execute("SELECT RDB$CONFIG_ID, TRIM(RDB$CONFIG_NAME), RDB$CONFIG_VALUE, RDB$CONFIG_DEFAULT, "
                    "RDB$CONFIG_IS_SET, RDB$CONFIG_SOURCE FROM RDB$CONFIG ORDER BY RDB$CONFIG_ID")
        rows = [{"id": r[0], "name": r[1], "value": r[2], "default": r[3], "is_set": bool(r[4]),
                 "source": r[5]} for r in cur.fetchall()]
    return {"available": True, "rows": rows, "message": None}
