"""The ``fbadmin`` command-line tool for CI and cron jobs: backup, restore, extract, diff, sql, check.

``fbadmin`` without a subcommand (or ``fbadmin serve``) starts the web server, see ``__main__``.
Every subcommand talks to Firebird directly with the same code the web app uses - or, with ``--url``,
runs through a fbadmin server's REST API with an API token (``remote.py``). See docs/CLI.md.
"""

from __future__ import annotations

import argparse
import sys

from .. import __version__
from .common import USAGE, CliError, Out, add_connection_options, add_output_options, clean_error

COMMANDS = ("backup", "restore", "extract", "diff", "sql", "check", "servers")

EXIT_CODES = """exit codes:
  0  success
  1  diff --exit-code: differences found; check: unhealthy
  2  usage error (bad arguments, missing file or password)
  3  cannot connect or log in
  4  the operation failed (gbak or script error)
  5  backup written, but its test restore (--verify) failed"""


def build_parser() -> argparse.ArgumentParser:
    from . import backup, misc, remote, schema

    conn = argparse.ArgumentParser(add_help=False)
    add_connection_options(conn)
    outp = argparse.ArgumentParser(add_help=False)
    add_output_options(outp)
    p = argparse.ArgumentParser(
        prog="fbadmin", formatter_class=argparse.RawDescriptionHelpFormatter, epilog=EXIT_CODES,
        description="fbadmin command-line tool. Without a subcommand, fbadmin starts the web server "
                    "(fbadmin serve --help).")
    p.add_argument("--version", action="version", version=f"fbadmin {__version__}")
    remote.add_remote_options(p, top=True)
    remote.add_remote_options(conn, top=False)
    sub = p.add_subparsers(dest="cmd", metavar="COMMAND")
    sub.add_parser("serve", help="start the web server (the default without a subcommand)", add_help=False)
    common = [conn, outp]
    backup.add_parsers(sub, common)
    schema.add_parsers(sub, common)
    misc.add_parsers(sub, common)
    for name, sp in sub.choices.items():
        if name != "serve":
            sp.formatter_class = argparse.RawDescriptionHelpFormatter
            sp.epilog = EXIT_CODES
    return p


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "func", None):
        parser.print_help()
        return USAGE
    out = Out(args)
    try:
        from . import remote

        if remote.url_from(args) and args.cmd != "serve":
            return int(remote.run(args, out) or 0)
        return int(args.func(args, out) or 0)
    except CliError as exc:
        if args.debug:
            import traceback

            traceback.print_exc()
        out.error(str(exc), exc.code, exc.data)
        return exc.code
    except KeyboardInterrupt:
        out.error("interrupted", 130)
        return 130
    except BrokenPipeError:  # `fbadmin extract db | head`
        return 0
    except Exception as exc:  # noqa: BLE001 - one clear line, the traceback only with --debug
        if args.debug:
            import traceback

            traceback.print_exc()
        out.error(clean_error(exc), 4)
        return 4
