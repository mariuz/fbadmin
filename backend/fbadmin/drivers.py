"""Which Firebird driver fbadmin uses for registered servers: the server-wide setting.

* ``firebirdsql`` (default): the pure-Python wire-protocol driver, no native library.
* ``native``: ``firebird-driver`` on top of ``libfbclient`` (see :mod:`fbadmin.native`).

The choice is stored in ``<data dir>/driver.json`` (``{"driver": ..., "fbclient": ...}``) and
edited in the UI (``GET/PUT /api/driver``, administrators only in server mode). The environment
variables ``FBADMIN_DRIVER`` and ``FBADMIN_FBCLIENT`` keep working and override the stored values.

Local (embedded) databases always use the native driver, whatever is chosen here. A process can
load only one ``libfbclient``: when the embedded engine's file set is present its client library
serves remote servers too, and once a library is loaded a different path takes effect only after
a restart. See docs/DRIVERS.md.
"""

from __future__ import annotations

import ctypes
import json
import os
import threading
from pathlib import Path

DRIVERS = ("firebirdsql", "native")
_NATIVE_ALIASES = ("native", "firebird-driver", "fbclient")
ENV_DRIVER = "FBADMIN_DRIVER"
ENV_FBCLIENT = "FBADMIN_FBCLIENT"
FILE_NAME = "driver.json"

_lock = threading.Lock()
_cache: tuple[str, float, dict] | None = None  # (path, mtime, data)


def _path() -> Path:
    from .config import settings

    return Path(settings.data_dir) / FILE_NAME


def stored() -> dict:
    """The saved setting (``{}`` when there is none). Cached until the file changes."""
    global _cache
    p = _path()
    try:
        mtime = p.stat().st_mtime
    except OSError:
        return {}
    c = _cache
    if c and c[0] == str(p) and c[1] == mtime:
        return c[2]
    try:
        data = json.loads(p.read_text("utf-8"))
        data = data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        data = {}
    _cache = (str(p), mtime, data)
    return data


def normalize(name: str | None) -> str:
    n = (name or "").strip().lower()
    if n in _NATIVE_ALIASES:
        return "native"
    if n in ("", "firebirdsql", "pure", "python"):
        return "firebirdsql"
    raise ValueError(f"Unknown driver {name!r}: use 'firebirdsql' or 'native'")


def selected() -> tuple[str, str]:
    """(driver, source): source is ``environment``, ``settings`` or ``default``."""
    env = os.environ.get(ENV_DRIVER, "").strip()
    if env:
        try:
            return normalize(env), "environment"
        except ValueError:
            return "firebirdsql", "environment"
    st = stored().get("driver")
    if st:
        try:
            return normalize(st), "settings"
        except ValueError:
            pass
    return "firebirdsql", "default"


def fbclient_setting() -> tuple[str | None, str]:
    """(client library path or None, source)."""
    env = os.environ.get(ENV_FBCLIENT, "").strip()
    if env:
        return env, "environment"
    st = (stored().get("fbclient") or "").strip()
    if st:
        return st, "settings"
    return None, "default"


def save(driver: str | None = None, fbclient: str | None = None) -> dict:
    """Store new values (``None`` keeps the current one, ``""`` clears the library path)."""
    data = dict(stored())
    if driver is not None:
        data["driver"] = normalize(driver)
    if fbclient is not None:
        if fbclient.strip():
            data["fbclient"] = fbclient.strip()
        else:
            data.pop("fbclient", None)
    restore(data)
    return data


def restore(data: dict) -> None:
    """Write ``data`` as the stored setting (``{}`` removes the file)."""
    global _cache
    p = _path()
    with _lock:
        _cache = None
        if not data:
            p.unlink(missing_ok=True)
            return
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2), "utf-8")
        os.replace(tmp, p)


def _client_version_string(api) -> str | None:
    """``isc_get_client_version`` text, "LI-V6.3.4.1812 Firebird 5.0", as "Firebird 5.0 (LI-V6.3.4.1812)"
    (the ``V6.3`` is the implementation's interface version, the last number the build)."""
    try:
        buf = ctypes.create_string_buffer(256)
        api.client_library.isc_get_client_version(buf)
        s = buf.value.decode("ascii", "replace").strip()
    except Exception:  # noqa: BLE001 - very old or unusual library
        return None
    head, _, tail = s.partition(" ")
    return f"{tail} ({head})" if tail.startswith("Firebird") else (s or None)


def native_status() -> dict:
    """Can the native driver be used here, with which client library, and if not, why."""
    out: dict = {"package": None, "available": False, "reason": "", "library": None,
                 "client_version": None, "client_major": None, "restart_required": False,
                 "embedded_library": None}
    try:
        import firebird.driver as fd
    except ImportError:
        out["reason"] = ("The Python package 'firebird-driver' is not installed "
                         "(pip install 'firebird-admin[native]').")
        return out
    try:
        from importlib.metadata import version

        out["package"] = version("firebird-driver")
    except Exception:  # noqa: BLE001
        out["package"] = getattr(fd, "__version__", "?")
    from . import native
    from .embedded import prepare_client

    out["embedded_library"] = prepare_client()
    wanted, _src = fbclient_setting()
    if wanted and not out["embedded_library"] and not Path(wanted).expanduser().is_file():
        out["reason"] = f"Client library not found: {wanted}"
        return out
    try:
        drv = native._load()
        drv.load_api()
    except Exception as exc:  # noqa: BLE001
        out["reason"] = f"Cannot load the Firebird client library (libfbclient): {exc}".strip()
        return out
    from firebird.driver import fbapi

    api = fbapi.api
    loaded = str(getattr(api, "client_library_name", "") or "") or None
    out["library"] = loaded
    try:
        v = int(api.util.get_client_version())
        out["client_major"] = v >> 8
        out["client_version"] = _client_version_string(api) or f"{v >> 8}.{v & 0xFF}"
    except Exception:  # noqa: BLE001
        out["client_version"] = _client_version_string(api)
    target = out["embedded_library"] or wanted
    if target and loaded and Path(target).expanduser().resolve() != Path(loaded).resolve():
        out["restart_required"] = True
    if out["client_major"] is not None and out["client_major"] < 3:
        out["reason"] = f"Client library {loaded} is older than Firebird 3; the native driver needs 3 or newer."
        return out
    out["available"] = True
    return out


def status() -> dict:
    """What ``GET /api/driver`` returns."""
    drv, src = selected()
    lib, lib_src = fbclient_setting()
    nat = native_status()
    return {
        "driver": drv,
        "source": src,
        "fbclient": lib,
        "fbclient_source": lib_src,
        "stored": stored(),
        "env": {ENV_DRIVER: os.environ.get(ENV_DRIVER) or None, ENV_FBCLIENT: os.environ.get(ENV_FBCLIENT) or None},
        "native": nat,
        "drivers": list(DRIVERS),
    }
