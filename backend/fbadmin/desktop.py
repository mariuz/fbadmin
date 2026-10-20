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
import os
import signal
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


class DesktopApi:
    """Exposed to the page as ``window.pywebview.api``: native file dialogs for local databases
    (features/embedded in the frontend). Public methods only; pywebview calls them from JS."""

    def __init__(self, webview) -> None:
        self._webview = webview

    def choose_database_file(self, save: bool = False, suggested: str = "") -> str | None:
        wv = self._webview
        dialogs = getattr(wv, "FileDialog", None)  # pywebview >= 5.3; older: OPEN_DIALOG/SAVE_DIALOG
        kind = getattr(dialogs, "SAVE" if save else "OPEN", None) if dialogs else None
        if kind is None:
            kind = wv.SAVE_DIALOG if save else wv.OPEN_DIALOG
        kwargs: dict = {"file_types": ("Firebird databases (*.fdb;*.gdb)", "All files (*.*)")}
        if suggested:
            folder, name = os.path.split(suggested)
            if folder and os.path.isdir(folder):
                kwargs["directory"] = folder
            if save and name:
                kwargs["save_filename"] = name
        result = wv.windows[0].create_file_dialog(kind, **kwargs)
        if not result:
            return None
        return result if isinstance(result, str) else result[0]


def _handle_sigterm(webview) -> None:
    """Quit cleanly on SIGTERM (logout, ``kill``): close the window / leave the wait loop.

    Besides being polite, this matters with the embedded engine: the Firebird client library
    installs its own SIGTERM handler (it shuts the engine down) and only chains to a handler
    that is already there, so with the default action the process would keep running.
    """
    def on_term(_signum, _frame):
        if webview is not None and getattr(webview, "windows", None):
            for w in list(webview.windows):
                try:
                    w.destroy()
                except Exception:  # noqa: BLE001
                    pass
            return
        raise KeyboardInterrupt

    if threading.current_thread() is threading.main_thread() and hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, on_term)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="fbadmin-desktop", description="fbadmin in a desktop window")
    p.add_argument("--port", type=int, default=0, help="port (default: a free port)")
    p.add_argument("--browser", action="store_true", help="open the web browser instead of a native window")
    p.add_argument("--log-level", choices=LEVELS, default=None)
    p.add_argument("--version", action="version", version=f"fbadmin {__version__}")
    args = p.parse_args(argv)
    os.environ["FBADMIN_DESKTOP"] = "1"  # enables self-update for packaged builds (see updater.py)

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
    _handle_sigterm(webview)
    if webview is not None:
        try:
            webview.create_window(f"fbadmin {__version__}", srv.url, width=1400, height=900,
                                  min_size=(900, 600), js_api=DesktopApi(webview))
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
