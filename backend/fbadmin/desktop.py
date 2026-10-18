"""Desktop shell: ``fbadmin-desktop``.

Starts the fbadmin server on a free localhost port in a background thread and shows the UI
in a native window (pywebview: WebView2 on Windows, WKWebView on macOS, GTK/Qt WebKit on
Linux). Without pywebview, or with ``--browser``, it opens the default web browser
instead and keeps serving until Ctrl+C.

The server only listens on 127.0.0.1. Data (registered servers) lives in the usual data
directory (``FBADMIN_DATA_DIR`` or ``~/.fbadmin``), shared with the ``fbadmin`` command.
"""

from __future__ import annotations

import argparse
import logging
import socket
import sys
import threading
import time
import webbrowser

import uvicorn

from . import __version__
from .logsetup import LEVELS, configure_logging

log = logging.getLogger("fbadmin.desktop")


def free_port(host: str = "127.0.0.1") -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind((host, 0))
        return s.getsockname()[1]


class ServerThread(threading.Thread):
    """Runs uvicorn in a daemon thread; ``stop()`` asks it to exit."""

    def __init__(self, host: str, port: int, log_config: dict | None = None) -> None:
        super().__init__(name="fbadmin-server", daemon=True)
        from .app import app

        config = uvicorn.Config(app, host=host, port=port, log_config=log_config, access_log=False,
                                lifespan="on")
        self.server = uvicorn.Server(config)
        # Signals belong to the GUI main loop, not to the server thread.
        self.server.install_signal_handlers = lambda: None  # type: ignore[method-assign]
        self.url = f"http://{host}:{port}/"

    def run(self) -> None:
        self.server.run()

    def wait_started(self, timeout: float = 30) -> bool:
        end = time.time() + timeout
        while time.time() < end:
            if self.server.started:
                return True
            if not self.is_alive():
                return False
            time.sleep(0.05)
        return False

    def stop(self) -> None:
        self.server.should_exit = True


def _webview():
    try:
        import webview  # pywebview
    except Exception as exc:  # noqa: BLE001 - missing GUI toolkits raise various errors
        log.info("pywebview unavailable (%s); using the web browser", exc)
        return None
    return webview


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="fbadmin-desktop", description="fbadmin in a desktop window")
    p.add_argument("--port", type=int, default=0, help="port (default: a free port)")
    p.add_argument("--browser", action="store_true", help="open the web browser instead of a native window")
    p.add_argument("--log-level", choices=LEVELS, default=None)
    p.add_argument("--version", action="version", version=f"fbadmin {__version__}")
    args = p.parse_args(argv)

    log_config = configure_logging(args.log_level or "warning", None)
    import logging.config

    logging.config.dictConfig(log_config)
    host = "127.0.0.1"
    srv = ServerThread(host, args.port or free_port(host), log_config)
    srv.start()
    if not srv.wait_started():
        print("fbadmin: the server failed to start", file=sys.stderr)
        return 1

    webview = None if args.browser else _webview()
    if webview is not None:
        try:
            webview.create_window(f"fbadmin {__version__}", srv.url, width=1400, height=900,
                                  min_size=(900, 600))
            webview.start()  # blocks until the window is closed
        except Exception as exc:  # noqa: BLE001 - no GUI backend (e.g. headless Linux)
            log.warning("cannot open a native window (%s); using the web browser", exc)
        else:
            srv.stop()
            srv.join(5)
            return 0

    print(f"fbadmin is running at {srv.url} (press Ctrl+C to quit)")
    webbrowser.open(srv.url)
    try:
        while srv.is_alive():
            srv.join(0.5)
    except KeyboardInterrupt:
        srv.stop()
        srv.join(5)
    return 0


if __name__ == "__main__":
    sys.exit(main())
