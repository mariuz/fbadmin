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
from .config import settings
from .logsetup import LEVELS, configure_logging


def main(argv: list[str] | None = None) -> None:
    argv = list(sys.argv[1:] if argv is None else argv)
    from .cli import COMMANDS

    if argv and argv[0] in COMMANDS:
        from .cli import main as cli_main

        sys.exit(cli_main(argv))
    if argv and argv[0] == "serve":
        argv = argv[1:]
    tools = ",".join(COMMANDS)
    p = argparse.ArgumentParser(
        prog="fbadmin", description="Web administration tool for Firebird SQL",
        usage=f"fbadmin [serve] [options]\n       fbadmin {{{tools}}} ... (command-line tool, see fbadmin COMMAND --help)")
    p.add_argument("--version", action="version", version=f"fbadmin {__version__}")
    p.add_argument("--host", default=settings.host, help="bind address (default %(default)s)")
    p.add_argument("--port", type=int, default=settings.port, help="port (default %(default)s)")
    p.add_argument("--no-browser", action="store_true", help="do not open a browser window")
    p.add_argument("--reload", action="store_true", help="auto-reload on code changes (development)")
    p.add_argument("--log-level", choices=LEVELS, default=None,
                   help="log level (default $FBADMIN_LOG_LEVEL or info)")
    p.add_argument("--log-format", choices=("text", "json"), default=None,
                   help="log output format; json writes one object per line (default $FBADMIN_LOG_FORMAT or text)")
    # --- security / server mode (docs/SECURITY.md)
    p.add_argument("--server-mode", action="store_true",
                   help="multi-user server mode with logins (same as FBADMIN_SERVER_MODE=1)")
    p.add_argument("--ssl-certfile", help="serve HTTPS with this certificate (PEM)")
    p.add_argument("--ssl-keyfile", help="private key for --ssl-certfile")
    p.add_argument("--proxy-headers", action="store_true",
                   help="trust X-Forwarded-Proto/For from --forwarded-allow-ips (behind a reverse proxy)")
    p.add_argument("--forwarded-allow-ips", default=os.environ.get("FORWARDED_ALLOW_IPS", "127.0.0.1"),
                   help="proxy addresses trusted for X-Forwarded-* headers (default %(default)s)")
    args = p.parse_args(argv)
    log_config = configure_logging(args.log_level, args.log_format)
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
    uvicorn.run("fbadmin.app:app", host=args.host, port=args.port, reload=args.reload,
                log_config=log_config, access_log=False, **extra)


if __name__ == "__main__":
    main()
