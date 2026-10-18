"""Command line entry point: ``fbadmin`` or ``python -m fbadmin``."""

from __future__ import annotations

import argparse
import threading
import webbrowser

import uvicorn

from .config import settings


def main() -> None:
    p = argparse.ArgumentParser(prog="fbadmin", description="Web administration tool for Firebird SQL")
    p.add_argument("--host", default=settings.host, help="bind address (default %(default)s)")
    p.add_argument("--port", type=int, default=settings.port, help="port (default %(default)s)")
    p.add_argument("--no-browser", action="store_true", help="do not open a browser window")
    p.add_argument("--reload", action="store_true", help="auto-reload on code changes (development)")
    args = p.parse_args()
    if not args.no_browser:
        url = f"http://{'localhost' if args.host in ('0.0.0.0', '127.0.0.1') else args.host}:{args.port}/"
        threading.Timer(1.5, lambda: webbrowser.open(url)).start()
    uvicorn.run("fbadmin.app:app", host=args.host, port=args.port, reload=args.reload)


if __name__ == "__main__":
    main()
