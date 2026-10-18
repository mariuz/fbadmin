"""High-level maintenance operations on top of :mod:`svc` (gbak, nbackup, gfix, properties, shutdown)."""

from __future__ import annotations

import re
from collections.abc import Callable, Iterator
from pathlib import Path

from pydantic import BaseModel

from . import gstat
from . import svc as S

Log = Callable[[str], None]


def _nolog(_line: str) -> None:
    pass


# ------------------------------------------------------------------ versions
def engine_major(svc) -> int:
    m = re.search(r"-[VTB](\d+)\.", svc.getServerVersion())  # V = release, T = test (snapshot), B = beta
    return int(m.group(1)) if m else 3


def capabilities(major: int) -> dict[str, bool]:
    return {
        "skip_data": major >= 3,
        "include_data": major >= 4,
        "zip": major >= 4,
        "crypt_backup": major >= 4,
        "parallel_workers": major >= 5,
        "nfix": major >= 4,
        "rdb_config": major >= 4,
        "crypt_state": major >= 4,
        "online_validation": major >= 3,
        "replica_mode": major >= 4,
    }


def require(major: int, minimum: int, what: str) -> None:
    if major < minimum:
        raise ValueError(f"{what} requires Firebird {minimum} or later (server is Firebird {major})")


# ------------------------------------------------------------ backup / restore
class BackupOptions(BaseModel):
    metadata_only: bool = False
    garbage_collect: bool = True
    transportable: bool = True
    ignore_checksums: bool = False
    ignore_limbo: bool = False
    convert_external: bool = False
    expand: bool = False
    old_descriptions: bool = False
    zip: bool = False                      # FB4+
    parallel_workers: int | None = None     # FB5+
    skip_data: str | None = None            # FB3+ (SIMILAR TO pattern)
    include_data: str | None = None         # FB4+
    crypt_plugin: str | None = None         # FB4+
    keyholder: str | None = None            # FB4+
    key_name: str | None = None             # FB4+


class RestoreOptions(BaseModel):
    replace: bool = False
    page_size: int | None = None
    buffers: int | None = None
    deactivate_indexes: bool = False
    no_shadow: bool = False
    no_validity: bool = False
    one_at_a_time: bool = False
    use_all_space: bool = False
    metadata_only: bool = False
    read_only: bool = False
    parallel_workers: int | None = None     # FB5+
    skip_data: str | None = None
    include_data: str | None = None         # FB4+
    crypt_plugin: str | None = None         # FB4+
    keyholder: str | None = None
    key_name: str | None = None
    fix_fss_data: str | None = None         # charset name
    fix_fss_metadata: str | None = None


def _crypt(spb: S.Spb, o, major: int) -> None:
    if o.crypt_plugin or o.keyholder or o.key_name:
        require(major, 4, "Backup encryption (crypt plugin / key)")
    if o.keyholder:
        spb.str(S.BKP_KEYHOLDER, o.keyholder)
    if o.key_name:
        spb.str(S.BKP_KEYNAME, o.key_name)
    if o.crypt_plugin:
        spb.str(S.BKP_CRYPT, o.crypt_plugin)


def _data_filters(spb: S.Spb, o, major: int) -> None:
    if o.skip_data:
        spb.str(S.BKP_SKIP_DATA, o.skip_data)
    if o.include_data:
        require(major, 4, "Include data filter")
        spb.str(S.BKP_INCLUDE_DATA, o.include_data)
    if o.parallel_workers and o.parallel_workers > 1:
        require(major, 5, "Parallel workers")
        spb.int(S.BKP_PARALLEL_WORKERS, o.parallel_workers)


def backup_spb(db_path: str, target: str, o: BackupOptions, major: int, verbose: bool) -> S.Spb:
    spb = S.Spb(S.ACT_BACKUP).str(S.SPB_DBNAME, db_path).str(S.BKP_FILE, target)
    opts = 0
    for flag, bit in ((o.metadata_only, S.BKP_OPT_METADATA_ONLY), (not o.garbage_collect, S.BKP_OPT_NO_GARBAGE_COLLECT),
                      (not o.transportable, S.BKP_OPT_NON_TRANSPORTABLE), (o.ignore_checksums, S.BKP_OPT_IGNORE_CHECKSUMS),
                      (o.ignore_limbo, S.BKP_OPT_IGNORE_LIMBO), (o.convert_external, S.BKP_OPT_CONVERT),
                      (o.expand, S.BKP_OPT_EXPAND), (o.old_descriptions, S.BKP_OPT_OLD_DESCRIPTIONS)):
        if flag:
            opts |= bit
    if o.zip:
        require(major, 4, "Compressed (zip) backup")
        opts |= S.BKP_OPT_ZIP
    spb.int(S.SPB_OPTIONS, opts)
    _data_filters(spb, o, major)
    _crypt(spb, o, major)
    if verbose:
        spb.flag(S.SPB_VERBOSE)
    return spb


def restore_spb(source: str, db_path: str, o: RestoreOptions, major: int, verbose: bool) -> S.Spb:
    spb = S.Spb(S.ACT_RESTORE).str(S.BKP_FILE, source).str(S.SPB_DBNAME, db_path)
    opts = S.RES_OPT_REPLACE if o.replace else S.RES_OPT_CREATE
    for flag, bit in ((o.deactivate_indexes, S.RES_OPT_DEACTIVATE_IDX), (o.no_shadow, S.RES_OPT_NO_SHADOW),
                      (o.no_validity, S.RES_OPT_NO_VALIDITY), (o.one_at_a_time, S.RES_OPT_ONE_AT_A_TIME),
                      (o.use_all_space, S.RES_OPT_USE_ALL_SPACE), (o.metadata_only, S.RES_OPT_METADATA_ONLY)):
        if flag:
            opts |= bit
    spb.int(S.SPB_OPTIONS, opts)
    if o.page_size:
        spb.int(S.RES_PAGE_SIZE, o.page_size)
    if o.buffers:
        spb.int(S.RES_BUFFERS, o.buffers)
    if o.read_only:
        spb.byte(S.RES_ACCESS_MODE, S.PRP_AM_READONLY)
    if o.fix_fss_data:
        spb.str(S.RES_FIX_FSS_DATA, o.fix_fss_data)
    if o.fix_fss_metadata:
        spb.str(S.RES_FIX_FSS_METADATA, o.fix_fss_metadata)
    _data_filters(spb, o, major)
    _crypt(spb, o, major)
    if verbose:
        spb.flag(S.SPB_VERBOSE)
    return spb


def backup_stream(svc, db_path: str, o: BackupOptions, major: int) -> Iterator[bytes]:
    """Start a backup to stdout; returns an iterator of chunks.

    The first chunk is fetched eagerly so option errors surface before streaming starts.
    """
    S.start(svc, backup_spb(db_path, "stdout", o, major, verbose=False))
    it = S.iter_to_eof(svc)
    first = next(it, b"")

    def gen() -> Iterator[bytes]:
        if first:
            yield first
        yield from it

    return gen()


def backup_to_local_file(svc, db_path: str, dest: Path, o: BackupOptions, major: int, log: Log = _nolog) -> int:
    """Stream a backup through the Services API into a local file (on the fbadmin host)."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".part")
    total = 0
    next_report = 8 << 20
    try:
        with open(tmp, "wb") as f:
            for chunk in backup_stream(svc, db_path, o, major):
                f.write(chunk)
                total += len(chunk)
                if total >= next_report:
                    log(f"{total / 1048576:.1f} MB received")
                    next_report += 8 << 20
        tmp.replace(dest)
    finally:
        tmp.unlink(missing_ok=True)
    log(f"Backup written to {dest} ({total} bytes)")
    return total


def restore_from_local_file(svc, src: Path, db_path: str, o: RestoreOptions, major: int, log: Log = _nolog) -> None:
    """Restore a local .fbk file by streaming it to the server's gbak via stdin."""
    size = src.stat().st_size
    sent = 0
    marks = [size * p // 10 for p in range(1, 11)]
    with open(src, "rb") as f:
        def read(n: int) -> bytes:
            nonlocal sent
            data = f.read(n)
            sent += len(data)
            while marks and sent >= marks[0] and size:
                marks.pop(0)
                log(f"Sent {sent * 100 // size}% ({sent} of {size} bytes)")
            return data

        S.start(svc, restore_spb("stdin", db_path, o, major, verbose=True))
        S.feed_stdin(svc, read, log)


def backup_server_file(svc, db_path: str, backup_file: str, o: BackupOptions, major: int, log: Log) -> None:
    S.run(svc, backup_spb(db_path, backup_file, o, major, verbose=True), log)


def restore_server_file(svc, backup_file: str, db_path: str, o: RestoreOptions, major: int, log: Log) -> None:
    S.run(svc, restore_spb(backup_file, db_path, o, major, verbose=True), log)


# -------------------------------------------------------------------- nbackup
def nbackup(svc, db_path: str, level: int, file: str, *, direct: bool | None = None,
            no_triggers: bool = False, guid: str | None = None, log: Log = _nolog) -> None:
    spb = S.Spb(S.ACT_NBAK).str(S.SPB_DBNAME, db_path).str(S.NBK_FILE, file)
    if guid:
        spb.str(S.NBK_GUID, guid)
    else:
        spb.int(S.NBK_LEVEL, level)
    if direct is not None:
        spb.str(S.NBK_DIRECT, "ON" if direct else "OFF")
    if no_triggers:
        spb.int(S.SPB_OPTIONS, S.NBK_OPT_NO_TRIGGERS)
    S.run(svc, spb, log)


def nrestore(svc, files: list[str], db_path: str, *, inplace: bool = False, log: Log = _nolog) -> None:
    if not files:
        raise ValueError("At least one nbackup file is required")
    spb = S.Spb(S.ACT_NREST)
    for f in files:
        spb.str(S.NBK_FILE, f)
    spb.str(S.SPB_DBNAME, db_path)
    if inplace:
        spb.int(S.SPB_OPTIONS, S.NBK_OPT_INPLACE)
    S.run(svc, spb, log)


def nfix(svc, db_path: str, major: int, log: Log = _nolog) -> None:
    require(major, 4, "nbackup fixup through the Services API")
    S.run(svc, S.Spb(S.ACT_NFIX).str(S.SPB_DBNAME, db_path), log)


# --------------------------------------------------------------------- repair
REPAIR_MODES = {
    "validate": S.RPR_OPT_VALIDATE_DB,
    "full": S.RPR_OPT_VALIDATE_DB | S.RPR_OPT_FULL,
    "check": S.RPR_OPT_VALIDATE_DB | S.RPR_OPT_CHECK_DB,  # read-only validation (gfix -v -n)
    "mend": S.RPR_OPT_MEND_DB | S.RPR_OPT_FULL | S.RPR_OPT_IGNORE_CHECKSUM,
    "sweep": S.RPR_OPT_SWEEP_DB,
    "icu": S.RPR_OPT_ICU,
    "kill_shadows": S.RPR_OPT_KILL_SHADOWS,
}


def repair(svc, db_path: str, mode: str, *, ignore_checksums: bool = False, parallel_workers: int | None = None,
           major: int = 3, log: Log = _nolog) -> list[str]:
    if mode not in REPAIR_MODES:
        raise ValueError(f"Unknown repair mode {mode!r}")
    opts = REPAIR_MODES[mode] | (S.RPR_OPT_IGNORE_CHECKSUM if ignore_checksums else 0)
    spb = S.Spb(S.ACT_REPAIR).str(S.SPB_DBNAME, db_path).int(S.SPB_OPTIONS, opts)
    if parallel_workers and parallel_workers > 1:
        require(major, 5, "Parallel workers")
        spb.int(S.RPR_PAR_WORKERS, parallel_workers)
    return S.run(svc, spb, log)


def validate_online(svc, db_path: str, *, tables_include: str | None = None, tables_exclude: str | None = None,
                    indices_include: str | None = None, indices_exclude: str | None = None,
                    lock_timeout: int | None = None, log: Log = _nolog) -> list[str]:
    spb = S.Spb(S.ACT_VALIDATE).str(S.SPB_DBNAME, db_path)
    for tag, val in ((S.VAL_TAB_INCL, tables_include), (S.VAL_TAB_EXCL, tables_exclude),
                     (S.VAL_IDX_INCL, indices_include), (S.VAL_IDX_EXCL, indices_exclude)):
        if val:
            spb.str(tag, val)
    if lock_timeout is not None:
        spb.int(S.VAL_LOCK_TIMEOUT, lock_timeout)
    return S.run(svc, spb, log)


def limbo_list(svc, db_path: str) -> list[dict]:
    S.start(svc, S.Spb(S.ACT_REPAIR).str(S.SPB_DBNAME, db_path).int(S.SPB_OPTIONS, S.RPR_OPT_LIST_LIMBO))
    out = S.parse_limbo(S.query(svc, bytes([S.INFO_SVC_LIMBO_TRANS])))
    S.drain(svc)
    return out


def limbo_resolve(svc, db_path: str, transaction_id: int, action: str, log: Log = _nolog) -> list[str]:
    tags = {"commit": (S.RPR_COMMIT_TRANS, S.RPR_COMMIT_TRANS_64),
            "rollback": (S.RPR_ROLLBACK_TRANS, S.RPR_ROLLBACK_TRANS_64),
            "recover": (S.RPR_RECOVER_TWO_PHASE, S.RPR_RECOVER_TWO_PHASE_64)}
    if action not in tags:
        raise ValueError(f"Unknown limbo action {action!r}")
    spb = S.Spb(S.ACT_REPAIR).str(S.SPB_DBNAME, db_path)
    t32, t64 = tags[action]
    if transaction_id > 0xFFFFFFFF:
        spb.int64(t64, transaction_id)
    else:
        spb.int(t32, transaction_id)
    return S.run(svc, spb, log)


# ----------------------------------------------------------------- properties
def set_properties(svc, db_path: str, *, page_buffers: int | None = None, sweep_interval: int | None = None,
                   forced_writes: bool | None = None, read_only: bool | None = None, dialect: int | None = None,
                   reserve_space: bool | None = None, log: Log = _nolog) -> list[str]:
    """Apply each property with its own service call (as gfix does); returns what was changed."""
    done: list[str] = []

    def apply(spb_fn, label: str) -> None:
        spb = S.Spb(S.ACT_PROPERTIES).str(S.SPB_DBNAME, db_path)
        spb_fn(spb)
        S.run(svc, spb, log)
        done.append(label)
        log(label)

    if page_buffers is not None:
        apply(lambda b: b.int(S.PRP_PAGE_BUFFERS, page_buffers), f"Page buffers set to {page_buffers}")
    if sweep_interval is not None:
        apply(lambda b: b.int(S.PRP_SWEEP_INTERVAL, sweep_interval), f"Sweep interval set to {sweep_interval}")
    if forced_writes is not None:
        apply(lambda b: b.byte(S.PRP_WRITE_MODE, S.PRP_WM_SYNC if forced_writes else S.PRP_WM_ASYNC),
              f"Forced writes {'on' if forced_writes else 'off'}")
    if reserve_space is not None:
        apply(lambda b: b.byte(S.PRP_RESERVE_SPACE, S.PRP_RES if reserve_space else S.PRP_RES_USE_FULL),
              f"Reserve space {'on' if reserve_space else 'off (use full pages)'}")
    if dialect is not None:
        if dialect not in (1, 3):
            raise ValueError("SQL dialect must be 1 or 3")
        apply(lambda b: b.int(S.PRP_SET_SQL_DIALECT, dialect), f"SQL dialect set to {dialect}")
    if read_only is not None:
        apply(lambda b: b.byte(S.PRP_ACCESS_MODE, S.PRP_AM_READONLY if read_only else S.PRP_AM_READWRITE),
              f"Access mode {'read-only' if read_only else 'read-write'}")
    return done


SHUTDOWN_METHODS = {"force": S.PRP_FORCE_SHUTDOWN, "attachments": S.PRP_ATTACHMENTS_SHUTDOWN,
                    "transactions": S.PRP_TRANSACTIONS_SHUTDOWN}


def shutdown(svc, db_path: str, mode: str = "full", method: str = "force", timeout: int = 0,
             log: Log = _nolog) -> None:
    """New-style (FB 2.5+) shutdown: mode multi/single/full, method force/attachments/transactions."""
    if mode not in ("multi", "single", "full"):
        raise ValueError("Shutdown mode must be multi, single or full")
    if method not in SHUTDOWN_METHODS:
        raise ValueError("Shutdown method must be force, attachments or transactions")
    spb = (S.Spb(S.ACT_PROPERTIES).str(S.SPB_DBNAME, db_path)
           .byte(S.PRP_SHUTDOWN_MODE, S.SHUT_MODES[mode]).int(SHUTDOWN_METHODS[method], max(0, int(timeout))))
    S.run(svc, spb, log)


def bring_online(svc, db_path: str, mode: str = "normal", log: Log = _nolog) -> None:
    if mode not in ("normal", "multi", "single"):
        raise ValueError("Online mode must be normal, multi or single")
    spb = S.Spb(S.ACT_PROPERTIES).str(S.SPB_DBNAME, db_path).byte(S.PRP_ONLINE_MODE, S.SHUT_MODES[mode])
    S.run(svc, spb, log)


# ------------------------------------------------------------------ statistics
def statistics_text(svc, db_path: str, *, header_only: bool = False, data: bool = True, indices: bool = True,
                    record_versions: bool = True, system: bool = False, tables: list[str] | None = None,
                    log: Log | None = None) -> str:
    if header_only:
        opts = S.STS_HDR_PAGES
    else:
        opts = ((S.STS_DATA_PAGES if data else 0) | (S.STS_IDX_PAGES if indices else 0)
                | (S.STS_RECORD_VERSIONS if record_versions else 0) | (S.STS_SYS_RELATIONS if system else 0))
        if tables:
            opts |= S.STS_TABLE
    spb = S.Spb(S.ACT_DB_STATS).str(S.SPB_DBNAME, db_path).int(S.SPB_OPTIONS, opts)
    if tables and not header_only:
        spb.str(S.SPB_COMMAND_LINE, " ".join(tables))
    return "\n".join(line.rstrip() for line in S.run(svc, spb, log))


def header(svc, db_path: str) -> dict:
    """Parsed header page (works for databases in shutdown, too)."""
    try:
        text = statistics_text(svc, db_path, header_only=True)
    except Exception as e:
        # Firebird 6 gstat refuses to read the header of a database in full
        # shutdown; the error itself tells us the state.
        if "shutdown" not in str(e).lower():
            raise
        return {"attributes": ["full shutdown"], "shutdown_mode": "full", "forced_writes": None,
                "read_only": None, "reserve_space": None, "backup_state": None,
                "encrypted": None, "partial": True}
    hdr = gstat.parse_header(text.splitlines())
    attrs = hdr["attributes"]
    hdr["shutdown_mode"] = gstat.shutdown_mode(attrs)
    hdr["forced_writes"] = "force write" in attrs
    hdr["read_only"] = "read only" in attrs
    hdr["reserve_space"] = "no reserve" not in attrs
    hdr["backup_state"] = ("merge" if any("merge" in a for a in attrs)
                           else "locked" if any("backup lock" in a for a in attrs) else "normal")
    hdr["encrypted"] = any("encrypted" in a for a in attrs)
    return hdr
