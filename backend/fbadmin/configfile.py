"""Configuration file ``fbadmin.toml`` (issue #11), for service installs (Windows service, systemd, containers).

Every setting of fbadmin is an environment variable (``FBADMIN_*``, docs/admin/configuration.md). The file sets
them in one place; an environment variable that is already set wins, and command-line flags win over both::

    # fbadmin.toml
    [server]
    host = "0.0.0.0"          # FBADMIN_HOST
    port = 5050               # FBADMIN_PORT
    server_mode = true        # FBADMIN_SERVER_MODE=1
    log_file = "logs/fbadmin.log"   # FBADMIN_LOG_FILE (relative: in the data directory)

    [env]                     # any other variable, by its full name
    FBADMIN_MAX_UPLOAD_BYTES = 2147483648

Keys of ``[server]`` (or at the top level) are setting names: ``key`` sets ``FBADMIN_KEY``. ``true`` / ``false``
become ``1`` / ``0``. The file is ``--config PATH``, else ``$FBADMIN_CONFIG``, else ``fbadmin.toml`` in the data
directory when it exists.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

try:  # Python 3.11+
    import tomllib as _toml
except ModuleNotFoundError:  # pragma: no cover - Python 3.10
    import tomli as _toml  # type: ignore[no-redef]

FILE_NAME = "fbadmin.toml"
_NAME = re.compile(r"^[A-Za-z][A-Za-z0-9_]*$")


class ConfigError(ValueError):
    """The configuration file cannot be read or has an invalid entry."""


def default_path(env: dict[str, str] | None = None) -> Path | None:
    """``$FBADMIN_CONFIG``, else ``<data dir>/fbadmin.toml`` when it exists."""
    env = os.environ if env is None else env
    if env.get("FBADMIN_CONFIG"):
        return Path(env["FBADMIN_CONFIG"])
    data = Path(env.get("FBADMIN_DATA_DIR") or Path.home() / ".fbadmin")
    p = data / FILE_NAME
    return p if p.is_file() else None


def _value(v: Any, where: str) -> str:
    if isinstance(v, bool):
        return "1" if v else "0"
    if isinstance(v, (int, float, str)):
        return str(v)
    raise ConfigError(f"{where}: expected a string, number or boolean")


def read(path: Path) -> dict[str, str]:
    """The environment variables the file sets (name -> value)."""
    try:
        data = _toml.loads(path.read_text("utf-8-sig"))  # utf-8-sig: Notepad may add a BOM
    except FileNotFoundError:
        raise ConfigError(f"Configuration file not found: {path}") from None
    except OSError as exc:  # e.g. a service account that may not read it
        raise ConfigError(f"Cannot read the configuration file {path}: {exc.strerror or exc}") from None
    except _toml.TOMLDecodeError as exc:
        raise ConfigError(f"{path}: {exc}") from None
    out: dict[str, str] = {}
    tables = {k: v for k, v in data.items() if isinstance(v, dict)}
    unknown = set(tables) - {"server", "env"}
    if unknown:
        raise ConfigError(f"{path}: unknown table [{sorted(unknown)[0]}] (use [server] or [env])")
    settings = {k: v for k, v in data.items() if not isinstance(v, dict)} | tables.get("server", {})
    for key, v in settings.items():
        if not _NAME.match(key):
            raise ConfigError(f"{path}: invalid setting name {key!r}")
        out["FBADMIN_" + key.upper()] = _value(v, f"{path}: {key}")
    for key, v in tables.get("env", {}).items():
        if not _NAME.match(key):
            raise ConfigError(f"{path}: invalid variable name {key!r}")
        out[key] = _value(v, f"{path}: [env] {key}")
    return out


def apply(path: Path | None, env: dict[str, str] | None = None) -> dict[str, str]:
    """Set the file's variables that are not set yet; returns the ones it set. ``FBADMIN_CONFIG`` records the file."""
    env = os.environ if env is None else env  # type: ignore[assignment]
    if path is None:
        return {}
    applied = {k: v for k, v in read(path).items() if k not in env}
    env.update(applied)
    env["FBADMIN_CONFIG"] = str(path)
    return applied
