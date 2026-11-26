"""Command line entry point: ``fbadmin`` or ``python -m fbadmin``.

Without a subcommand (or with ``serve``) the web server starts, with the options below. The subcommands
``backup``, ``restore``, ``extract``, ``diff``, ``sql``, ``check`` and ``servers`` are the command-line tool
in :mod:`fbadmin.cli` (docs/CLI.md).
"""

from __future__ import annotations

import argparse
import os
import sys
import threading
import webbrowser

import uvicorn

from . import __version__
from .config import reload_settings, settings
from .logsetup import LEVELS, configure_logging


REMOTE_OPTIONS = ("--url", "--token", "--token-env", "--ca-cert")


def _remote_command(argv: list[str], commands) -> bool:
    """``fbadmin --url URL [--token-env NAME] COMMAND ...``: remote options before the command."""
    i = 0
    while i < len(argv) and argv[i].split("=", 1)[0] in REMOTE_OPTIONS:
        i += 1 if "=" in argv[i] else 2
    return 0 < i < len(argv) and argv[i] in commands


def main(argv: list[str] | None = None) -> None:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] == "write-admin-file":  # internal: the Windows server MSI (fbadmin/admin_file.py)
        from .admin_file import main as write_admin_file

        sys.exit(write_admin_file())
    from .cli import COMMANDS

    if argv and (argv[0] in COMMANDS or _remote_command(argv, COMMANDS)):
        from .cli import main as cli_main

        sys.exit(cli_main(argv))
    if argv and argv[0] == "serve":
        argv = argv[1:]
    _load_config(argv)
    env = os.environ.get
    tools = ",".join(COMMANDS)
    p = argparse.ArgumentParser(
        prog="fbadmin", description="Web administration tool for Firebird SQL",
        usage=f"fbadmin [serve] [options]\n       fbadmin {{{tools}}} ... (command-line tool, see fbadmin COMMAND --help)")
    p.add_argument("--version", action="version", version=f"fbadmin {__version__}")
    p.add_argument("--host", default=settings.host, help="bind address (default %(default)s)")
    p.add_argument("--port", type=int, default=settings.port, help="port (default %(default)s)")
    p.add_argument("--config", metavar="PATH",
                   help="configuration file (default $FBADMIN_CONFIG, else fbadmin.toml in the data directory)")
    p.add_argument("--no-browser", action="store_true", default=not _flag(env("FBADMIN_OPEN_BROWSER"), True),
                   help="do not open a browser window (FBADMIN_OPEN_BROWSER=0)")
    p.add_argument("--reload", action="store_true", help="auto-reload on code changes (development)")
    p.add_argument("--log-level", choices=LEVELS, default=None,
                   help="log level (default $FBADMIN_LOG_LEVEL or info)")
    p.add_argument("--log-format", choices=("text", "json"), default=None,
                   help="log output format; json writes one object per line (default $FBADMIN_LOG_FORMAT or text)")
    p.add_argument("--log-file", default=None, metavar="PATH",
                   help="also log to this file, rotated by size (default $FBADMIN_LOG_FILE; relative: in the data "
                        "directory)")
    # --- security / server mode (docs/SECURITY.md)
    p.add_argument("--server-mode", action="store_true",
                   help="multi-user server mode with logins (same as FBADMIN_SERVER_MODE=1)")
    p.add_argument("--ssl-certfile", default=env("FBADMIN_SSL_CERTFILE"),
                   help="serve HTTPS with this certificate (PEM; FBADMIN_SSL_CERTFILE)")
    p.add_argument("--ssl-keyfile", default=env("FBADMIN_SSL_KEYFILE"),
                   help="private key for --ssl-certfile (FBADMIN_SSL_KEYFILE)")
    p.add_argument("--proxy-headers", action="store_true", default=_flag(env("FBADMIN_PROXY_HEADERS"), False),
                   help="trust X-Forwarded-Proto/For from --forwarded-allow-ips (behind a reverse proxy)")
    p.add_argument("--forwarded-allow-ips",
                   default=env("FBADMIN_FORWARDED_ALLOW_IPS") or env("FORWARDED_ALLOW_IPS", "127.0.0.1"),
                   help="proxy addresses trusted for X-Forwarded-* headers (default %(default)s)")
    args = p.parse_args(argv)
    log_config = configure_logging(args.log_level, args.log_format, args.log_file)
    if args.server_mode:
        os.environ["FBADMIN_SERVER_MODE"] = "1"
    https = bool(args.ssl_certfile)
    if not args.no_browser:
        host = "localhost" if args.host in ("0.0.0.0", "127.0.0.1") else args.host
        url = f"{'https' if https else 'http'}://{host}:{args.port}/"
        threading.Timer(1.5, lambda: webbrowser.open(url)).start()
    extra = {}
    if https:
        extra.update(ssl_certfile=args.ssl_certfile, ssl_keyfile=args.ssl_keyfile)
    if args.proxy_headers:
        extra.update(proxy_headers=True, forwarded_allow_ips=args.forwarded_allow_ips)
    # SIGTERM / Ctrl+C (service stop): uvicorn stops accepting requests, waits for running ones (at most
    # FBADMIN_SHUTDOWN_TIMEOUT seconds) and runs the app's shutdown (scheduler, monitors, connections).
    uvicorn.run("fbadmin.app:app", host=args.host, port=args.port, reload=args.reload,
                log_config=log_config, access_log=False,
                timeout_graceful_shutdown=int(env("FBADMIN_SHUTDOWN_TIMEOUT") or 10), **extra)


def _flag(value: str | None, default: bool) -> bool:
    if value is None or value == "":
        return default
    return value.strip().lower() not in ("0", "false", "no", "off")


def _load_config(argv: list[str]) -> None:
    """Apply the configuration file (issue #11) before the options read their defaults from the environment."""
    from . import configfile

    path = None
    for i, a in enumerate(argv):
        if a == "--config" and i + 1 < len(argv):
            path = argv[i + 1]
        elif a.startswith("--config="):
            path = a.split("=", 1)[1]
    try:
        configfile.apply(configfile.Path(path) if path else configfile.default_path())
    except configfile.ConfigError as exc:
        sys.exit(f"fbadmin: {exc}")
    reload_settings()


if __name__ == "__main__":
    main()
