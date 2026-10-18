"""Plugin loading.

Backend plugins are Python packages exposing an object in the ``fbadmin.plugins`` entry
point group. The object may provide any of:

* ``router``         – a FastAPI ``APIRouter`` to include
* ``setup(app)``     – called with the FastAPI app (add middleware, startup hooks, ...)
* ``static_dir``     – a directory served at ``/plugins/<name>/``
* ``frontend_entry`` – a JS module inside ``static_dir`` loaded by the web UI

Frontend-only plugins can also be dropped into ``<data_dir>/plugins/*.js``.
A frontend module exports ``default function (fbadmin) {...}`` and receives the SDK
(registry hooks, API helpers, React, t()). See docs/PLUGINS.md.
"""

from __future__ import annotations

import logging
from importlib.metadata import entry_points
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from .config import settings

log = logging.getLogger("fbadmin.plugins")


def _entry_points():
    return entry_points(group="fbadmin.plugins")


def load_plugins(app: FastAPI) -> list[dict]:
    loaded: list[dict] = []
    for ep in _entry_points():
        info: dict = {"name": ep.name, "source": "package", "frontend": None}
        try:
            obj = ep.load()
            if getattr(obj, "router", None) is not None:
                app.include_router(obj.router)
            setup = getattr(obj, "setup", None)
            if callable(setup):
                setup(app)
            static = getattr(obj, "static_dir", None)
            if static:
                app.mount(f"/plugins/{ep.name}", StaticFiles(directory=str(static)), name=f"plugin-{ep.name}")
                entry = getattr(obj, "frontend_entry", None)
                if entry:
                    info["frontend"] = f"/plugins/{ep.name}/{entry}"
            info["version"] = getattr(obj, "version", None)
        except Exception as exc:  # noqa: BLE001 - one bad plugin must not stop the app
            log.exception("plugin %s failed to load", ep.name)
            info["error"] = str(exc)
        loaded.append(info)

    local = Path(settings.data_dir) / "plugins"
    if local.is_dir():
        files = sorted(p.name for p in local.glob("*.js"))
        if files:
            app.mount("/plugins/local", StaticFiles(directory=str(local)), name="plugin-local")
            for f in files:
                loaded.append({"name": f[:-3], "source": "data-dir", "frontend": f"/plugins/local/{f}"})
    return loaded
