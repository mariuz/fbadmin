"""Opt-in "update available" check against the GitHub Releases API.

Nothing here runs on its own: the backend only contacts GitHub when the UI calls
``GET /api/version/latest``, and the UI only does that after the user ticked
"Check for updates" (stored in their browser). The request is a plain anonymous GET
of the public "latest release" document; no identifiers, settings or usage data are sent.
Administrators can forbid it completely with ``FBADMIN_UPDATE_CHECK=off``.
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
import urllib.request

from ..security.core import require_admin
from fastapi import APIRouter

from .. import __version__

router = APIRouter(prefix="/api/version", tags=["version"])

RELEASES_URL = os.environ.get(
    "FBADMIN_UPDATE_URL", "https://api.github.com/repos/mariuz/fbadmin/releases/latest"
)
CACHE_SECONDS = 6 * 3600
_cache: dict = {}
_lock = threading.Lock()


def check_enabled() -> bool:
    return os.environ.get("FBADMIN_UPDATE_CHECK", "ask").lower() not in ("off", "0", "false", "no", "never")


def parse_version(v: str) -> tuple:
    """'v1.2.3' / '1.2.3rc1' -> comparable tuple; a pre-release sorts before its release."""
    m = re.match(r"^v?(\d+(?:\.\d+)*)(.*)$", v.strip())
    if not m:
        return ()
    nums = tuple(int(x) for x in m.group(1).split("."))
    nums = nums + (0,) * (3 - len(nums))
    return nums + ((0, m.group(2)) if m.group(2) else (1, ""),)


def is_newer(latest: str, current: str) -> bool:
    a, b = parse_version(latest), parse_version(current)
    return bool(a) and bool(b) and a > b


def _fetch_latest() -> dict:
    req = urllib.request.Request(
        RELEASES_URL, headers={"Accept": "application/vnd.github+json", "User-Agent": "fbadmin-update-check"}
    )
    with urllib.request.urlopen(req, timeout=5) as r:  # noqa: S310 - fixed https URL
        data = json.loads(r.read().decode())
    return {"latest": str(data.get("tag_name", "")).lstrip("v"), "url": data.get("html_url"),
            "published_at": data.get("published_at")}


@router.get("")
def version():
    return {"version": __version__, "update_check": check_enabled()}


@router.get("/latest")
def latest(refresh: bool = False):
    out = {"current": __version__, "enabled": check_enabled(), "latest": None, "url": None,
           "published_at": None, "update_available": False, "error": None}
    if not out["enabled"]:
        return out
    with _lock:
        cached = _cache.get("v")
        if refresh or not cached or time.time() - cached[0] > CACHE_SECONDS:
            try:
                cached = (time.time(), _fetch_latest())
                _cache["v"] = cached
            except Exception as exc:  # noqa: BLE001 - being offline is normal, report softly
                out["error"] = f"Update check failed: {exc}"
                return out
    info = cached[1]
    out.update(latest=info["latest"], url=info["url"], published_at=info.get("published_at"),
               update_available=is_newer(info["latest"], __version__))
    return out


# ---------------------------------------------------------------- desktop auto-update
def _later(fn, delay: float = 1.0) -> None:
    """Run ``fn`` after the HTTP response went out."""
    t = threading.Timer(delay, fn)
    t.daemon = True
    t.start()


@router.get("/update")
def update_plan():
    """What "Download and install" would do on this install (desktop app only)."""
    from .. import updater

    try:
        return updater.plan()
    except Exception as exc:  # noqa: BLE001 - offline etc.
        return {"current": __version__, "supported": updater.install_kind() is not None, "error": str(exc)}


@router.post("/update/download")
def update_download():
    require_admin()  # desktop mode: allowed; server mode: administrators only
    from .. import updater
    from ..config import settings

    p = updater.plan()
    if not p["supported"]:
        raise ValueError(p.get("reason") or "This install cannot update itself.")
    if p.get("error"):
        raise ValueError(p["error"])
    if not p["update_available"]:
        raise ValueError(f"fbadmin {__version__} is up to date.")
    d = updater.start_download(p["asset"], settings.data_dir)
    return {**d.status(), "kind": p["kind"], "latest": p["latest"]}


@router.get("/update/status")
def update_status():
    from .. import updater

    d = updater.current()
    return d.status() if d else {"state": "idle"}


@router.post("/update/apply")
def update_apply():
    require_admin()  # desktop mode: allowed; server mode: administrators only
    from .. import updater

    kind = updater.install_kind()
    d = updater.current()
    if kind is None:
        raise ValueError("This install cannot update itself.")
    if not d or d.state != "done":
        raise ValueError("Download the update first.")
    return updater.apply(d.path, kind, restart=lambda: _later(updater.restart_appimage),
                         quit_app=lambda: _later(lambda: os._exit(0)))
