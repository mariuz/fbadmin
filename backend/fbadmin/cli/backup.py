"""``fbadmin backup`` and ``fbadmin restore``: gbak through the Services API, like the web UI's backups."""

from __future__ import annotations

import argparse
import gzip
import hashlib
import re
import sys
import time
import uuid
from pathlib import Path
from typing import Any, BinaryIO

from ..maintenance import ops, verify
from ..maintenance import svc as S
from .common import FAILED, USAGE, VERIFY, CliError, Out, Target, add_db_arg, clean_error, resolve

SUFFIX = {"none": ".fbk", "gzip": ".fbk.gz", "zstd": ".fbk.zst"}
GZIP_MAGIC = b"\x1f\x8b"
ZSTD_MAGIC = b"\x28\xb5\x2f\xfd"


def _zstd():
    try:
        from compression import zstd  # Python 3.14+
    except ImportError:
        raise CliError("zstd compression needs Python 3.14 or later (compression.zstd); use --compress gzip",
                       USAGE) from None
    return zstd


def _mb(n: int) -> str:
    for unit, div in (("GB", 1 << 30), ("MB", 1 << 20), ("KB", 1 << 10)):
        if n >= div:
            return f"{n / div:.1f} {unit}"
    return f"{n} bytes"


def _similar_list(names: list[str] | None) -> str | None:
    """Table names -> a gbak SIMILAR TO pattern (NAME1|NAME2)."""
    return "|".join(n.strip() for n in names) if names else None


def _pattern(pattern: str | None, tables: list[str] | None, what: str) -> str | None:
    if pattern and tables:
        raise CliError(f"Use either --{what}-data PATTERN or --{what}-table NAME, not both", USAGE)
    return pattern or _similar_list(tables)


# ===================================================================== backup
class _HashingWriter:
    """File-like object that counts and hashes what is written to the real file."""

    def __init__(self, f: BinaryIO):
        self.f = f
        self.sha = hashlib.sha256()
        self.size = 0

    def write(self, b) -> int:
        self.f.write(b)
        self.sha.update(b)
        self.size += len(b)
        return len(b)

    def flush(self) -> None:
        self.f.flush()


def _compressor(kind: str, sink):
    if kind == "gzip":
        return gzip.GzipFile(fileobj=sink, mode="wb", compresslevel=6, mtime=0, filename="")
    if kind == "zstd":
        return _zstd().ZstdFile(sink, mode="wb")
    return None


def backup_options(args: argparse.Namespace) -> ops.BackupOptions:
    return ops.BackupOptions(
        metadata_only=args.metadata_only, garbage_collect=not args.no_gc, transportable=not args.non_transportable,
        ignore_checksums=args.ignore_checksums, ignore_limbo=args.ignore_limbo, zip=args.zip,
        parallel_workers=args.parallel, skip_data=_pattern(args.skip_data, args.skip_table, "skip"),
        include_data=_pattern(args.include_data, args.include_table, "include"),
        crypt_plugin=args.crypt_plugin, keyholder=args.keyholder, key_name=args.key_name,
    )


def _new_file(directory: Path, prefix: str, kind: str) -> Path:
    stamp = time.strftime("%Y%m%d-%H%M%S")
    base = f"{prefix}_{stamp}"
    p = directory / (base + SUFFIX[kind])
    n = 1
    while p.exists():
        p = directory / f"{base}_{n}{SUFFIX[kind]}"
        n += 1
    return p


def _safe(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", name).strip("._") or "backup"


def retention(directory: Path, prefix: str, keep: int) -> list[str]:
    """Delete the oldest ``<prefix>_YYYYmmdd-HHMMSS*.fbk[.gz|.zst]`` files so that ``keep`` remain."""
    pat = re.compile(rf"^{re.escape(prefix)}_\d{{8}}-\d{{6}}(_\d+)?\.fbk(\.gz|\.zst)?$")
    files = sorted((p for p in directory.iterdir() if p.is_file() and pat.match(p.name)),
                   key=lambda p: (p.stat().st_mtime, p.name), reverse=True)
    removed = []
    for p in files[keep:]:
        p.unlink(missing_ok=True)
        Path(str(p) + ".sha256").unlink(missing_ok=True)
        removed.append(str(p))
    return removed


def stream_backup(svc, t: Target, opts: ops.BackupOptions, major: int, dest: Path | None, kind: str,
                  out: Out) -> dict[str, Any]:
    """Write the backup stream to ``dest`` (None = stdout), compressed with ``kind``."""
    raw = 0
    next_report = 64 << 20
    tmp = dest.with_name(dest.name + ".part") if dest else None
    f = open(tmp, "wb") if tmp else sys.stdout.buffer  # noqa: SIM115 - closed below
    sink = _HashingWriter(f)
    comp = _compressor(kind, sink)
    w = comp or sink
    try:
        for chunk in ops.backup_stream(svc, t.path, opts, major):
            w.write(chunk)
            raw += len(chunk)
            if raw >= next_report:
                out.log(f"{_mb(raw)} received")
                next_report += 64 << 20
        if comp:
            comp.close()
        sink.flush()
        if tmp:
            f.close()
            tmp.replace(dest)
    finally:
        if tmp:
            f.close()
            tmp.unlink(missing_ok=True)
    return {"bytes": raw, "file_bytes": sink.size, "sha256": sink.sha.hexdigest()}


def cmd_backup(args: argparse.Namespace, out: Out) -> int:
    t = resolve(args, args.db)
    opts = backup_options(args)
    targets = [x for x in (args.output, args.dir, args.server_file) if x]
    if len(targets) != 1:
        raise CliError("Choose one destination: -o FILE (or - for stdout), --dir DIRECTORY or --server-file PATH",
                       USAGE)
    to_stdout = args.output == "-"
    kind = args.compress
    if kind is None:
        o = (args.output or "").lower()
        kind = "gzip" if o.endswith(".gz") else "zstd" if o.endswith(".zst") else "none"
    if kind == "zstd":
        _zstd()
    if args.keep is not None and not args.dir:
        raise CliError("--keep needs --dir (old backups are deleted from that directory)", USAGE)
    if args.server_file and (kind != "none" or args.checksum):
        raise CliError("--compress and --checksum work on local files; on the server use --zip (Firebird 4+)", USAGE)
    if to_stdout and (args.checksum or args.verify or args.json):
        raise CliError("--checksum, --verify and --json cannot be used with -o - (stdout)", USAGE)
    started = time.time()
    res: dict[str, Any] = {"database": t.label, "compress": kind}
    with t.service() as svc:
        major = ops.engine_major(svc)
        res["engine_major"] = major
        if args.server_file:
            out.log(f"Backing up {t.label} to {args.server_file} (on the server)")
            ops.backup_server_file(svc, t.path, args.server_file, opts, major, out.detail)
            res["server_file"] = args.server_file
            dest = None
        else:
            if to_stdout:
                dest = None
            elif args.dir:
                d = Path(args.dir).expanduser()
                d.mkdir(parents=True, exist_ok=True)
                dest = _new_file(d, _safe(args.name or t.name), kind)
            else:
                dest = Path(args.output).expanduser()
                dest.parent.mkdir(parents=True, exist_ok=True)
            out.log(f"Backing up {t.label} to {dest or 'stdout'}" + (f" ({kind})" if kind != "none" else ""))
            res.update(stream_backup(svc, t, opts, major, dest, kind, out))
            if dest:
                res["file"] = str(dest)
                if args.checksum:
                    side = Path(str(dest) + ".sha256")
                    side.write_text(f"{res['sha256']}  {dest.name}\n", "utf-8")
                    res["checksum_file"] = str(side)
        res["seconds"] = round(time.time() - started, 1)
        if args.verify:
            try:
                res["verify"] = verify_backup(svc, t, major, out, local=dest, server_file=args.server_file,
                                              scratch=args.verify_path)
            except CliError:
                raise
            except Exception as exc:  # noqa: BLE001
                res["verify"] = {"ok": False, "error": clean_error(exc)}
                raise CliError(f"The backup was written but its test restore failed: {clean_error(exc)}",
                               VERIFY, res) from exc
    if args.keep is not None and args.keep > 0:
        res["removed"] = retention(Path(args.dir).expanduser(), _safe(args.name or t.name), args.keep)
        for r in res["removed"]:
            out.log(f"Retention: deleted {r}")
    where = res.get("file") or res.get("server_file") or "stdout"
    size = f", {_mb(res['file_bytes'])}" if "file_bytes" in res else ""
    text = f"Backup of {t.label} written to {where}{size} in {res['seconds']} s"
    if res.get("verify"):
        v = res["verify"]
        text += f"; test restore ok ({v['tables']} tables, {v['indices']} indices)"
    out.result(res, text, to_stderr=to_stdout)
    return 0


# ===================================================================== verify
def verify_backup(svc, t: Target, major: int, out: Out, *, local: Path | None, server_file: str | None,
                  scratch: str | None) -> dict[str, Any]:
    """Restore the backup into a scratch database next to the source, open it, count objects, drop it."""
    try:
        target = verify.scratch_path(t.path or "", "cli_" + uuid.uuid4().hex[:8], scratch)
    except ValueError:
        raise CliError(f"The database is given by alias ({t.path!r}), so fbadmin cannot tell its directory: "
                       "give a scratch database path on the server with --verify-path", USAGE) from None
    verify.check_scratch(target, [*t.registered_paths, t.path or ""])
    replace = Path(target.replace("\\", "/")).name.lower().startswith(verify.SCRATCH_PREFIX)
    t0 = time.time()
    out.log(f"Test restore into {target}")
    try:
        o = ops.RestoreOptions(replace=replace)
        if server_file:
            ops.restore_server_file(svc, server_file, target, o, major, out.detail)
        else:
            assert local is not None
            restore_local(svc, local, target, o, major, out)
        conn = t.connect(path=target)
        try:
            cur = conn.cursor()
            cur.execute("SELECT "
                        "(SELECT COUNT(*) FROM RDB$RELATIONS WHERE COALESCE(RDB$SYSTEM_FLAG, 0) = 0), "
                        "(SELECT COUNT(*) FROM RDB$INDICES WHERE COALESCE(RDB$SYSTEM_FLAG, 0) = 0), "
                        "(SELECT COUNT(*) FROM RDB$PROCEDURES WHERE COALESCE(RDB$SYSTEM_FLAG, 0) = 0) "
                        "FROM RDB$DATABASE")
            tables, indices, procedures = cur.fetchone()
            conn.commit()
        finally:
            conn.drop_database()
    except Exception:
        if replace:  # never drop a file fbadmin did not name itself
            try:
                t.connect(path=target).drop_database()
            except Exception:  # noqa: BLE001
                pass
        raise
    out.log(f"Test restore ok: {tables} tables, {indices} indices, {procedures} procedures; scratch database dropped")
    return {"ok": True, "seconds": round(time.time() - t0, 1), "tables": tables, "indices": indices,
            "procedures": procedures, "path": target}


# ===================================================================== restore
def open_backup(src: Path | None) -> tuple[Any, Any, int, str]:
    """(reader, raw file, raw size, compression) for a local backup or stdin (None); gzip/zstd by magic bytes."""
    if src is None:
        raw = sys.stdin.buffer
        size = 0
        head = raw.peek(4)[:4] if hasattr(raw, "peek") else b""
    else:
        if not src.is_file():
            raise CliError(f"No such backup file: {src}", USAGE)
        raw = open(src, "rb")  # noqa: SIM115 - closed by the caller
        size = src.stat().st_size
        head = raw.read(4)
        raw.seek(0)
    if head[:2] == GZIP_MAGIC:
        return gzip.GzipFile(fileobj=raw, mode="rb"), raw, size, "gzip"
    if head == ZSTD_MAGIC:
        return _zstd().ZstdFile(raw, mode="rb"), raw, size, "zstd"
    return raw, raw, size, "none"


def restore_local(svc, src: Path | None, db_path: str, o: ops.RestoreOptions, major: int, out: Out) -> int:
    """Stream a local backup (plain, gzip or zstd; None = stdin) to the server's gbak through stdin."""
    reader, raw, size, _kind = open_backup(src)
    sent = 0
    marks = [size * p // 10 for p in range(1, 11)] if size else []
    try:
        def read(n: int) -> bytes:
            nonlocal sent
            data = reader.read(n)
            sent += len(data)
            pos = raw.tell() if size else 0
            if marks and pos >= marks[0]:
                while marks and pos >= marks[0]:
                    marks.pop(0)
                if size >= 1 << 20:
                    out.log(f"Sent {pos * 100 // size}% of {_mb(size)}")
            return data

        S.start(svc, ops.restore_spb("stdin", db_path, o, major, verbose=True))
        S.feed_stdin(svc, read, out.detail)
    finally:
        if src is not None:
            raw.close()
    return sent


def restore_options(args: argparse.Namespace) -> ops.RestoreOptions:
    return ops.RestoreOptions(
        replace=args.replace, page_size=args.page_size, buffers=args.buffers,
        deactivate_indexes=args.deactivate_indexes, no_shadow=args.no_shadow, no_validity=args.no_validity,
        one_at_a_time=args.one_at_a_time, use_all_space=args.use_all_space, metadata_only=args.metadata_only,
        read_only=args.read_only, parallel_workers=args.parallel,
        skip_data=_pattern(args.skip_data, args.skip_table, "skip"),
        include_data=_pattern(args.include_data, args.include_table, "include"),
        crypt_plugin=args.crypt_plugin, keyholder=args.keyholder, key_name=args.key_name,
        fix_fss_data=args.fix_fss_data, fix_fss_metadata=args.fix_fss_metadata,
    )


def cmd_restore(args: argparse.Namespace, out: Out) -> int:
    pos = list(args.args)
    if args.server_file:
        if len(pos) > 1:
            raise CliError("With --server-file give only the database to restore into", USAGE)
        backup, db = None, (pos[0] if pos else None)
    else:
        if not pos:
            raise CliError("Give the backup file (or - for stdin, or --server-file PATH)", USAGE)
        if len(pos) > 2:
            raise CliError("Too many arguments: BACKUP [DATABASE]", USAGE)
        backup, db = pos[0], (pos[1] if len(pos) > 1 else None)
    t = resolve(args, db)
    o = restore_options(args)
    src = None if backup in (None, "-") else Path(backup).expanduser()
    if src is not None and not src.is_file():
        raise CliError(f"No such backup file: {src}", USAGE)
    started = time.time()
    res: dict[str, Any] = {"database": t.label, "replace": o.replace}
    with t.service() as svc:
        major = ops.engine_major(svc)
        res["engine_major"] = major
        try:
            if args.server_file:
                out.log(f"Restoring {args.server_file} (on the server) into {t.label}")
                ops.restore_server_file(svc, args.server_file, t.path, o, major, out.detail)
                res["server_file"] = args.server_file
            else:
                out.log(f"Restoring {src or 'stdin'} into {t.label}")
                res["bytes"] = restore_local(svc, src, t.path, o, major, out)
                res["file"] = str(src) if src else "-"
        except Exception as exc:
            msg = clean_error(exc)
            if not o.replace and "already exists" in msg.lower():
                raise CliError(f"{msg} (use --replace to overwrite it)", FAILED) from exc
            raise
    res["seconds"] = round(time.time() - started, 1)
    out.result(res, f"Restored {res.get('file') or res.get('server_file')} into {t.label} in {res['seconds']} s")
    return 0


# ===================================================================== parsers
def _data_filter_options(p: argparse.ArgumentParser) -> None:
    g = p.add_argument_group("data filters")
    g.add_argument("--skip-data", metavar="PATTERN", help="skip the data of tables matching this SIMILAR TO pattern")
    g.add_argument("--skip-table", metavar="NAME", action="append",
                   help="skip the data of this table (repeatable; shorthand for --skip-data NAME1|NAME2)")
    g.add_argument("--include-data", metavar="PATTERN",
                   help="only the data of tables matching this SIMILAR TO pattern (Firebird 4+)")
    g.add_argument("--include-table", metavar="NAME", action="append",
                   help="only the data of this table (repeatable, Firebird 4+)")
    g.add_argument("--parallel", type=int, metavar="N", help="parallel workers (Firebird 5+)")
    g.add_argument("--crypt-plugin", help="encryption plugin (Firebird 4+)")
    g.add_argument("--keyholder", help="key holder plugin (Firebird 4+)")
    g.add_argument("--key-name", help="encryption key name (Firebird 4+)")


def add_parsers(sub, common) -> None:
    p = sub.add_parser("backup", parents=common, help="back up a database (gbak through the Services API)",
                       description="Back up a database with gbak through the Firebird Services API. The backup is "
                                   "streamed to a file on this machine (-o/--dir) or written by the server "
                                   "(--server-file).")
    add_db_arg(p)
    d = p.add_argument_group("destination")
    d.add_argument("-o", "--output", metavar="FILE", help="local backup file (- for stdout; .gz/.zst compresses)")
    d.add_argument("--dir", metavar="DIRECTORY", help="local directory; the file is named NAME_YYYYmmdd-HHMMSS.fbk")
    d.add_argument("--name", help="file name prefix for --dir (default: the database file name)")
    d.add_argument("--server-file", metavar="PATH", help="backup file on the Firebird server (written by the server)")
    d.add_argument("--compress", choices=("none", "gzip", "zstd"),
                   help="compress the local file (default: from the -o suffix; zstd needs Python 3.14)")
    d.add_argument("--checksum", action="store_true", help="write FILE.sha256 (sha256sum format)")
    d.add_argument("--keep", type=int, metavar="N", help="with --dir: keep the N newest backups of this database, "
                                                          "delete older ones (after a successful backup/verify)")
    d.add_argument("--verify", action="store_true",
                   help="test-restore the backup into a scratch database on the server, open it, drop it")
    d.add_argument("--verify-path", metavar="PATH",
                   help="scratch database path on the server for --verify (default: next to the database)")
    o = p.add_argument_group("gbak options")
    o.add_argument("--metadata-only", action="store_true", help="metadata only, no data")
    o.add_argument("--no-gc", action="store_true", help="no garbage collection")
    o.add_argument("--non-transportable", action="store_true", help="non-transportable format")
    o.add_argument("--ignore-checksums", action="store_true", help="ignore checksum errors")
    o.add_argument("--ignore-limbo", action="store_true", help="ignore transactions in limbo")
    o.add_argument("--zip", action="store_true", help="compress inside the backup (gbak -zip, Firebird 4+)")
    _data_filter_options(p)
    p.set_defaults(func=cmd_backup)

    p = sub.add_parser("restore", parents=common, help="restore a backup into a database",
                       description="Restore a gbak backup. A local file (plain, .gz or .zst, or - for stdin) is "
                                   "streamed to the server; --server-file restores a file on the server.",
                       usage="fbadmin restore [options] BACKUP DATABASE\n"
                             "       fbadmin restore [options] --server-file PATH DATABASE")
    p.add_argument("args", nargs="*", metavar="BACKUP DATABASE", help=argparse.SUPPRESS)
    p.add_argument("--server-file", metavar="PATH", help="backup file on the Firebird server")
    o = p.add_argument_group("gbak options")
    o.add_argument("--replace", action="store_true", help="overwrite an existing database (otherwise it must not exist)")
    o.add_argument("--page-size", type=int, help="page size of the new database")
    o.add_argument("--buffers", type=int, help="page buffers of the new database")
    o.add_argument("--deactivate-indexes", action="store_true", help="do not build indices")
    o.add_argument("--no-shadow", action="store_true", help="do not restore shadows")
    o.add_argument("--no-validity", action="store_true", help="do not restore check constraints")
    o.add_argument("--one-at-a-time", action="store_true", help="commit after each table")
    o.add_argument("--use-all-space", action="store_true", help="fill data pages completely")
    o.add_argument("--metadata-only", action="store_true", help="metadata only, no data")
    o.add_argument("--read-only", action="store_true", help="restore as a read-only database")
    o.add_argument("--fix-fss-data", metavar="CHARSET", help="fix malformed UNICODE_FSS data")
    o.add_argument("--fix-fss-metadata", metavar="CHARSET", help="fix malformed UNICODE_FSS metadata")
    _data_filter_options(p)
    p.set_defaults(func=cmd_restore)
