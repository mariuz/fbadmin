"""Local databases through the Firebird embedded engine (no server needed).

Firebird "embedded" is the ordinary client library (``libfbclient`` / ``fbclient.dll``)
loading the engine provider plugin (``plugins/Engine13``) in-process: a connection string
that is a plain file path, with no host, is opened by the engine inside the fbadmin process.
See docs/EMBEDDED.md for the file set, licences and the design.

* **Where the engine comes from**: ``FBADMIN_EMBEDDED_DIR`` (an extracted file set, e.g.
  from ``scripts/embedded-test-setup.sh``), else the copy bundled with the desktop app
  (``<app>/firebird``, see packaging/embedded/fetch-embedded.py). ``FBADMIN_EMBEDDED=0``
  turns the feature off. It is never available in multi-user server mode: the engine runs
  with fbadmin's operating-system rights and does no authentication.
* **Registrations**: local databases live under one server entry with ``embedded=True``
  (created on demand, "Local databases"). Every connection of that entry - metadata,
  Query Tool, create/drop - goes through the native driver (``firebird-driver``) with
  ``Providers = Engine13`` forced in the DPB, so it can never reach a server by accident.
* **Services API** (backup/restore, gstat, validation, sweep, properties, nbackup...): the
  embedded service manager, wrapped by :class:`EmbeddedServices` so the existing
  ``firebirdsql``-based maintenance code runs unchanged.
* **Concurrency**: the engine runs in SuperServer mode, which opens a database file
  exclusively for the process. Any number of attachments *inside* fbadmin work (tree, data
  grid, several Query Tool tabs); a Firebird server or another program cannot open the file
  at the same time (and vice versa: a file a server has open cannot be opened here).
"""

from __future__ import annotations

import json
import os
import re
import sys
import threading
from contextlib import contextmanager
from pathlib import Path

import firebirdsql

from .native import NativeServices

ENV_DIR = "FBADMIN_EMBEDDED_DIR"
BUNDLED_SUBDIR = "firebird"  # inside the PyInstaller bundle
MANIFEST = "fbadmin-embedded.json"  # written by packaging/embedded/fetch-embedded.py
SERVER_NAME = "Local databases"
SERVER_GROUP = "Local"
EMBEDDED_USER = "SYSDBA"
# The embedded engine does not authenticate; registry code that insists on a password gets this.
EMBEDDED_PASSWORD = "embedded"

_lock = threading.Lock()
_env_done = False


class EmbeddedUnavailable(ValueError):
    """The embedded engine cannot be used (reported as HTTP 400 with the reason)."""


def _disabled() -> bool:
    return os.environ.get("FBADMIN_EMBEDDED", "").strip().lower() in ("0", "false", "no", "off")


def embedded_dir() -> Path | None:
    """Root of the embedded file set (``firebird.conf``, ``plugins/``, ``intl/``...), if any."""
    env = os.environ.get(ENV_DIR, "").strip()
    if env:
        return Path(env).expanduser().resolve()
    base = getattr(sys, "_MEIPASS", None)  # frozen desktop app
    if base and (Path(base) / BUNDLED_SUBDIR).is_dir():
        return Path(base) / BUNDLED_SUBDIR
    return None


def client_library(root: Path) -> Path | None:
    if sys.platform == "win32":
        names = ["fbclient.dll"]
    elif sys.platform == "darwin":
        names = ["lib/libfbclient.dylib"]
    else:
        names = ["lib/libfbclient.so.2", "lib/libfbclient.so"]
    for n in names:
        if (root / n).is_file():
            return root / n
    return None


def engine_plugin(root: Path) -> Path | None:
    for n in ("plugins/libEngine13.so", "plugins/engine13.dll", "plugins/libEngine13.dylib"):
        if (root / n).is_file():
            return root / n
    return None


def _lock_dir() -> Path:
    from .config import settings

    return settings.data_dir / "embedded" / "lock"


def prepare_client() -> str | None:
    """Point this process at the embedded file set; returns the client library to load.

    Must run before firebird-driver loads ``libfbclient`` (``native._load`` calls it).
    ``FIREBIRD`` makes the library use the file set as its root (configuration, messages,
    plugins, character sets). Lock files go to a per-user directory under fbadmin's data
    directory, so they never clash with a system Firebird server's ``/tmp/firebird``.
    """
    global _env_done
    if _disabled():
        return None
    root = embedded_dir()
    lib = client_library(root) if root else None
    if lib is None:
        return None
    with _lock:
        if not _env_done:
            os.environ["FIREBIRD"] = str(root)
            if not os.environ.get("FIREBIRD_LOCK"):
                lock = _lock_dir()
                lock.mkdir(parents=True, exist_ok=True)
                os.environ["FIREBIRD_LOCK"] = str(lock)
            if sys.platform == "darwin":
                # Firebird's macOS build dlopen()s ICU as "lib/libicuuc.<ver>.dylib", relative to the
                # working directory (other platforms resolve it from the root or the system)
                os.chdir(root)
            _env_done = True
    return str(lib)


def _server_mode() -> bool:
    try:
        from .api.deps import get_state

        sec = getattr(get_state(), "security", None)
        return bool(getattr(sec, "server_mode", False))
    except Exception:  # noqa: BLE001
        return False


def manifest(root: Path | None = None) -> dict:
    root = root or embedded_dir()
    try:
        return json.loads((root / MANIFEST).read_text("utf-8")) if root else {}
    except (OSError, ValueError):
        return {}


def status() -> dict:
    """What the UI shows: is the feature usable, and if not, why."""
    root = embedded_dir()
    out: dict = {"available": False, "reason": "", "directory": str(root) if root else None,
                 "version": manifest(root).get("version") if root else None,
                 "data_directory": str(default_database_dir())}
    if _server_mode():
        out["reason"] = "Local (embedded) databases are not available in multi-user server mode."
    elif _disabled():
        out["reason"] = "Local databases are turned off (FBADMIN_EMBEDDED=0)."
    elif root is None:
        out["reason"] = ("No Firebird embedded engine found. The desktop app bundles one; when running from "
                         f"source, set {ENV_DIR} to an extracted file set (see scripts/embedded-test-setup.sh).")
    elif client_library(root) is None:
        out["reason"] = f"No Firebird client library in {root}."
    elif engine_plugin(root) is None:
        out["reason"] = f"No engine plugin (plugins/Engine13) in {root}."
    else:
        try:
            from .native import _load

            driver = _load()
            driver.load_api()
        except Exception as exc:  # noqa: BLE001
            out["reason"] = f"Cannot load the Firebird client library: {exc}"
        else:
            loaded = driver.driver_config.fb_client_library.value
            if loaded and Path(loaded).resolve() != client_library(root).resolve():
                out["reason"] = f"Another Firebird client library is already loaded ({loaded})."
            else:
                out["available"] = True
    return out


def require_available() -> None:
    st = status()
    if not st["available"]:
        raise EmbeddedUnavailable(st["reason"])


def default_database_dir() -> Path:
    """Suggested folder for new local databases."""
    from .config import settings

    return settings.data_dir / "databases"


_REMOTE = re.compile(r"^(inet[46]?|xnet|wnet)://|^[^/\\]{2,}:|^\\\\|^//", re.IGNORECASE)


def check_local_path(path: str) -> str:
    """An absolute local file path (no ``host:``, ``inet://``, UNC or alias)."""
    p = (path or "").strip()
    if not p:
        raise ValueError("Enter the path of the database file")
    if _REMOTE.match(p):
        raise ValueError("A local database must be a file path on this computer, not a server connection string")
    expanded = os.path.expanduser(p)
    if not os.path.isabs(expanded):
        raise ValueError("Enter an absolute file path (e.g. /home/me/data/app.fdb or C:\\data\\app.fdb)")
    return os.path.normpath(expanded)


def connect_local(database: str, *, charset: str = "UTF8", role: str | None = None,
                  create: bool = False, page_size: int | None = None, user: str = EMBEDDED_USER):
    """A :class:`fbadmin.native.NativeConnection` to a local file through the embedded engine."""
    require_available()
    from .native import connect_native

    path = check_local_path(database)
    if create:
        if os.path.exists(path):
            raise ValueError(f"{path} already exists")
        os.makedirs(os.path.dirname(path), exist_ok=True)
    elif not os.path.isfile(path):
        raise LookupError(f"Database file not found: {path}")
    try:
        return connect_native(host="", port=0, database=path, user=user or EMBEDDED_USER, password="",
                              role=role, charset=charset or "UTF8", embedded=True, create=create,
                              page_size=page_size)
    except firebirdsql.Error as exc:
        if '"lock" operation' in str(exc):  # SuperServer embedded: the file is held by another process
            raise firebirdsql.OperationalError(
                f"{path} is in use by another program (a Firebird server or another fbadmin): the embedded "
                f"engine needs exclusive access to the file.\n{str(exc).strip()}") from exc
        raise


# ----------------------------------------------------------------------------- services
# Service actions that make no sense (or have no plugin) for a local file in this process.
_UNSUPPORTED_ACTIONS = {
    **dict.fromkeys((4, 5, 6, 7, 29), "User management is not available for local (embedded) databases: "
                                       "the embedded engine does not authenticate users."),
    **dict.fromkeys((22, 23, 24, 25, 26), "Trace is not available for local (embedded) databases: the trace "
                                           "plugin is not part of the bundled engine."),
    **dict.fromkeys((27, 28), "Security mappings are not available for local (embedded) databases."),
}


class EmbeddedServices(NativeServices):
    """:class:`fbadmin.native.NativeServices` on the embedded service manager: the same mapping
    of the wire primitives to ``IService.start`` / ``IService.query`` used for remote servers
    with the native driver, minus the actions a local file cannot support."""

    def _check_action(self, param: bytes) -> None:
        if param and param[0] in _UNSUPPORTED_ACTIONS:
            raise EmbeddedUnavailable(_UNSUPPORTED_ACTIONS[param[0]])

    def getLog(self) -> str:  # noqa: N802 - firebirdsql naming
        """firebird.log of the embedded engine (in its root directory, if it was writable)."""
        root = embedded_dir()
        log = root / "firebird.log" if root else None
        if log and log.is_file():
            return log.read_text("utf-8", "replace")
        return ""


_svc_config: str | None = None


@contextmanager
def services():
    """Context manager yielding an :class:`EmbeddedServices` (like ``ConnectionManager.service``)."""
    global _svc_config
    require_available()
    from .native import _load, _translate

    driver = _load()
    with _lock:
        if _svc_config is None:
            name = "fbadmin-embedded-services"
            if driver.driver_config.get_server(name) is None:
                cfg = driver.driver_config.register_server(name)
                cfg.config.value = "Providers = Engine13"  # never a server via Loopback
            _svc_config = name
    try:
        srv = driver.connect_server(_svc_config, user=EMBEDDED_USER, password="")
    except driver.Error as exc:
        raise _translate(exc) from exc
    svc = EmbeddedServices(srv, (driver.Error,))
    try:
        yield svc
    finally:
        svc.close()
