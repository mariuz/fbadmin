"""Which Firebird driver fbadmin uses for registered servers: the server-wide setting.

* ``auto`` (default): the native driver when it can be used with a Firebird 4 or newer client
  library, otherwise the pure-Python driver. :func:`effective` says which one and why.
* ``native``: ``firebird-driver`` on top of ``libfbclient`` (see :mod:`fbadmin.native`).
* ``firebirdsql``: the pure-Python wire-protocol driver, no native library.

The choice is stored in ``<data dir>/driver.json`` (``{"driver": ..., "fbclient": ...}``) and
edited in the UI (``GET/PUT /api/driver``, administrators only in server mode). The environment
variables ``FBADMIN_DRIVER`` (``auto``, ``native`` or ``firebirdsql``) and ``FBADMIN_FBCLIENT``
override the stored values.

``auto`` is resolved once (cached, and again when the setting changes): ``native`` when
``firebird-driver`` is importable, a client library loads and it is a Firebird 4+ client (a
Firebird 3 client gets INT128, DECFLOAT and time zone values only as legacy types), else
``firebirdsql``. The client library is the embedded engine's when that file set is present (the
desktop app, ``FBADMIN_EMBEDDED_DIR``), else the configured path (``FBADMIN_FBCLIENT`` or the
setting), else firebird-driver's search of the system library path. When a native connection
later fails for a reason that is not a Firebird error (the client library or one of its plugins
cannot be loaded), ``auto`` uses ``firebirdsql`` for that connection and reports it
(:func:`note_native_failure`); Firebird errors (wrong password, unknown database...) are
reported as they are.

Local (embedded) databases always use the native driver, whatever is chosen here. A process can
load only one ``libfbclient``: when the embedded engine's file set is present its client library
serves remote servers too, and once a library is loaded a different path takes effect only after
a restart. See docs/DRIVERS.md.
"""

from __future__ import annotations

import ctypes
import json
import logging
import os
import threading
from pathlib import Path

DRIVERS = ("auto", "native", "firebirdsql")
_NATIVE_ALIASES = ("native", "firebird-driver", "fbclient")
_PURE_ALIASES = ("firebirdsql", "pure", "python")
_AUTO_ALIASES = ("auto", "automatic", "")
ENV_DRIVER = "FBADMIN_DRIVER"
ENV_FBCLIENT = "FBADMIN_FBCLIENT"
FILE_NAME = "driver.json"
# auto uses the native driver only with a client library of this Firebird version or newer
AUTO_MIN_CLIENT = 4

log = logging.getLogger("fbadmin.drivers")
_lock = threading.Lock()
_cache: tuple[str, float, dict] | None = None  # (path, mtime, data)
_resolve_lock = threading.Lock()
_resolved: dict[tuple, tuple[str, str]] = {}  # library sources -> (driver, reason) for auto
_fallback: dict = {"reason": None, "sticky": False}


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
    if n in _PURE_ALIASES:
        return "firebirdsql"
    if n in _AUTO_ALIASES:
        return "auto"
    raise ValueError(f"Unknown driver {name!r}: use 'auto', 'native' or 'firebirdsql'")


def selected() -> tuple[str, str]:
    """(setting, source): the setting is ``auto``, ``native`` or ``firebirdsql`` (see
    :func:`effective` for the driver actually used); the source ``environment``, ``settings``
    or ``default``."""
    env = os.environ.get(ENV_DRIVER, "").strip()
    if env:
        try:
            return normalize(env), "environment"
        except ValueError:
            return "auto", "environment"
    st = stored().get("driver")
    if st:
        try:
            return normalize(st), "settings"
        except ValueError:
            pass
    return "auto", "default"


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
        reset()  # resolve auto again and forget a runtime fallback
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


def _icu_check(api) -> tuple[bool, str | None]:
    """Can the Firebird 4+ client library convert time zone names? It needs ICU for that (the
    system's on Linux): without it, values in a named zone (``Europe/Prague``) are read without
    their zone and cannot be sent at all ("Could not find acceptable ICU library")."""
    import datetime

    try:
        from firebird.driver.types import get_timezone

        api.util.encode_timestamp_tz(datetime.datetime(2024, 7, 1, 12, 0, tzinfo=get_timezone("Europe/Prague")))
    except Exception as exc:  # noqa: BLE001
        return False, str(exc).strip() or type(exc).__name__
    return True, None


def native_status() -> dict:
    """Can the native driver be used here, with which client library, and if not, why."""
    out: dict = {"package": None, "available": False, "reason": "", "library": None,
                 "client_version": None, "client_major": None, "restart_required": False,
                 "embedded_library": None, "icu": None, "icu_error": None}
    try:
        import firebird.driver as fd
    except ImportError:
        out["reason"] = ("The Python package 'firebird-driver' is not installed "
                         "(pip install firebird-driver; it needs Python 3.11 or newer).")
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
        from .native import client_version

        major, minor = client_version(api.util)
        out["client_major"] = major
        out["client_version"] = _client_version_string(api) or f"{major}.{minor}"
    except Exception:  # noqa: BLE001
        out["client_version"] = _client_version_string(api)
    if (out["client_major"] or 0) >= 4:
        out["icu"], out["icu_error"] = _icu_check(api)
    target = out["embedded_library"] or wanted
    if target and loaded and Path(target).expanduser().resolve() != Path(loaded).resolve():
        out["restart_required"] = True
    if out["client_major"] is not None and out["client_major"] < 3:
        out["reason"] = f"Client library {loaded} is older than Firebird 3; the native driver needs 3 or newer."
        return out
    out["available"] = True
    return out


def resolve(setting: str, nat: dict | None = None) -> tuple[str, str]:
    """(driver, reason) for ``setting``; ``auto`` decides from ``nat`` (:func:`native_status`)."""
    if setting == "firebirdsql":
        return "firebirdsql", "The pure Python driver is selected."
    if setting == "native":
        return "native", "The native driver is selected."
    nat = nat or {}
    if not nat.get("package"):
        return "firebirdsql", "firebird-driver is not installed."
    lib = nat.get("library")
    if not lib:
        why = nat.get("reason") or "The Firebird client library cannot be loaded."
        if "could not be determined" in why or "not found" in why:  # firebird-driver's / our wording
            return "firebirdsql", ("No Firebird client library found (install a Firebird 4 or newer client, "
                                   f"or set {ENV_FBCLIENT}). {why}")
        return "firebirdsql", why
    major = nat.get("client_major")
    if major is None or major < AUTO_MIN_CLIENT:
        return "firebirdsql", (f"The client library is Firebird {major if major is not None else '(unknown version)'}: "
                               f"{lib}. With it the native driver gets INT128, DECFLOAT and time zone values of "
                               "Firebird 4+ servers only as legacy types; the pure Python driver reads them exactly. "
                               "Install a Firebird 4 or newer client to use the native driver.")
    if not nat.get("available"):
        return "firebirdsql", nat.get("reason") or "The native driver is not available."
    if nat.get("icu") is False:
        return "firebirdsql", (f"The client library {lib} cannot load ICU ({nat.get('icu_error')}), which it needs "
                               "for time zone names; install ICU (Debian/Ubuntu: libicu) to use the native driver.")
    return "native", f"{nat.get('client_version') or f'Firebird {major}'} client library: {lib}"


def _sources_key() -> tuple:
    return (fbclient_setting()[0], os.environ.get("FBADMIN_EMBEDDED_DIR", ""),
            os.environ.get("FBADMIN_EMBEDDED", ""))


def effective() -> tuple[str, str]:
    """(driver used for new connections, reason): ``native`` or ``firebirdsql``.

    Cheap: ``auto`` is resolved once per library source (and again after :func:`reset`)."""
    setting, _src = selected()
    if setting != "auto":
        return resolve(setting)
    if _fallback["sticky"] and _fallback["reason"]:
        return "firebirdsql", _fallback["reason"]
    key = _sources_key()
    r = _resolved.get(key)
    if r is None:
        with _resolve_lock:
            r = _resolved.get(key)
            if r is None:
                try:
                    r = resolve("auto", native_status())
                except Exception as exc:  # noqa: BLE001 - never break connecting over the probe
                    r = ("firebirdsql", f"Cannot check the native driver: {exc}")
                _resolved[key] = r
    return r


def reset() -> None:
    """Forget the resolution of ``auto`` and a runtime fallback (the setting changed)."""
    _resolved.clear()
    _fallback.update(reason=None, sticky=False)


def note_native_failure(exc: BaseException, *, sticky: bool = False) -> str:
    """A native connection failed for a reason that is not a Firebird error and ``auto`` uses
    ``firebirdsql`` for it instead. Logged once (a warning); shown in the driver status.
    ``sticky``: the client library itself cannot be loaded, so ``auto`` stops trying it."""
    msg = f"Native driver failed, using firebirdsql: {exc}"
    first = _fallback["reason"] is None
    _fallback["reason"] = msg
    if sticky:
        _fallback["sticky"] = True
    if first:
        log.warning("%s", msg)
    return msg


def fallback_reason() -> str | None:
    return _fallback["reason"]


def may_fall_back() -> bool:
    """Whether a client-library failure of the native driver falls back to firebirdsql: only
    with ``auto`` (an explicitly selected driver reports the failure)."""
    return selected()[0] == "auto"


def log_startup() -> None:
    """Log the effective driver and why (once, at startup)."""
    try:
        setting, src = selected()
        drv, reason = effective()
    except Exception as exc:  # noqa: BLE001
        log.warning("Cannot determine the database driver: %s", exc)
        return
    log.info("Database driver: %s (setting %s, %s). %s", drv, setting, src, reason)


def status() -> dict:
    """What ``GET /api/driver`` returns."""
    drv, src = selected()
    eff, reason = effective()
    lib, lib_src = fbclient_setting()
    nat = native_status()
    return {
        "driver": drv,
        "source": src,
        "effective": eff,
        "reason": reason,
        "fallback": fallback_reason(),
        "fbclient": lib,
        "fbclient_source": lib_src,
        "stored": stored(),
        "env": {ENV_DRIVER: os.environ.get(ENV_DRIVER) or None, ENV_FBCLIENT: os.environ.get(ENV_FBCLIENT) or None},
        "native": nat,
        "drivers": list(DRIVERS),
    }
