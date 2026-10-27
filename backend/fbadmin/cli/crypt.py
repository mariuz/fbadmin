"""``fbadmin backup-decrypt`` and ``fbadmin backup-key``: encrypted backups without the web server.

For disaster recovery: an encrypted backup (``*.fbk.enc``) from fbadmin's archive or from cloud storage is
decrypted with the recovery key (``--key``, ``--key-file``, FBADMIN_BACKUP_KEY, or the keys in fbadmin's
data directory), then restored with ``fbadmin restore`` or gbak.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from .common import FAILED, USAGE, CliError, Out


def _key(args: argparse.Namespace) -> bytes | None:
    from ..maintenance import backupcrypt

    text = args.key
    if args.key_file:
        text = Path(args.key_file).expanduser().read_text().strip()
    return backupcrypt.parse_key(text) if text else None


def cmd_decrypt(args: argparse.Namespace, out: Out) -> int:
    from ..maintenance import backupcrypt
    from . import registry

    src = Path(args.file).expanduser()
    if not src.is_file():
        raise CliError(f"{src} doesn't exist", USAGE)
    h = backupcrypt.info(src)
    if h is None:
        raise CliError(f"{src} is not an encrypted fbadmin backup", USAGE)
    dest = Path(args.output).expanduser() if args.output else src.with_name(src.name.removesuffix(".enc"))
    if dest.exists() and not args.force:
        raise CliError(f"{dest} exists (use --force to overwrite)", USAGE)
    try:
        key = _key(args)
        ring = backupcrypt.Keyring(registry.data_dir(args))
        n = backupcrypt.decrypt_file(src, dest, key=key, ring=ring)
    except backupcrypt.BackupKeyError as e:
        raise CliError(str(e), FAILED) from None
    out.result({"file": str(dest), "bytes": n, "key_id": h["key_id"]},
               f"Decrypted {src.name} (key {h['key_id']}) into {dest} ({n} bytes)")
    return 0


def cmd_key(args: argparse.Namespace, out: Out) -> int:
    from ..maintenance import backupcrypt

    key = os.urandom(32)
    text = backupcrypt.format_key(key)
    out.result({"key": text, "key_id": backupcrypt.key_id(key)},
               f"{text}\n# key id {backupcrypt.key_id(key)}: keep it apart from the backups; "
               "set FBADMIN_BACKUP_KEY to it to encrypt with it", to_stderr=False)
    if not out.json and sys.stdout.isatty():
        out.log("Store this key in a password manager or secret store: without it the backups can't be read.")
    return 0


def add_parsers(sub, common) -> None:
    p = sub.add_parser("backup-decrypt", parents=common, help="decrypt an encrypted backup (*.fbk.enc)",
                       description="Decrypt a backup that fbadmin encrypted. The key comes from --key or --key-file, "
                                   "else from FBADMIN_BACKUP_KEY or fbadmin's data directory (--data-dir).")
    p.add_argument("file", metavar="FILE", help="the encrypted backup (*.fbk.enc)")
    p.add_argument("-o", "--output", metavar="FILE", help="the decrypted backup (default: FILE without .enc)")
    p.add_argument("--key", metavar="fbk1:…", help="the backup key")
    p.add_argument("--key-file", metavar="FILE", help="a file holding the backup key")
    p.add_argument("--force", action="store_true", help="overwrite the output file")
    p.set_defaults(func=cmd_decrypt)

    p = sub.add_parser("backup-key", parents=common, help="create a backup encryption key",
                       description="Print a new random backup key (for FBADMIN_BACKUP_KEY). It isn't stored anywhere.")
    p.set_defaults(func=cmd_key)
