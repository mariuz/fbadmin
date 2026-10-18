"""Desktop auto-update (milestone 1.0).

Only the desktop app (``fbadmin-desktop``, the packaged builds) updates itself; a server install
is updated with pip, Docker or Helm. Like the update check, nothing runs unless the user asks:
the UI offers "Download and install" after the opt-in check found a newer release.

Flow:
1. ``plan()`` reads the latest GitHub release, picks the package for this platform
   (MSI on Windows, dmg on macOS, AppImage on Linux) and its SHA-256 from ``SHA256SUMS``.
2. ``start_download()`` streams it into ``<data dir>/updates`` in a background thread and
   verifies the checksum; a package without a published checksum is refused.
3. ``apply()`` installs it:
   * Windows: runs the MSI (``msiexec /i … /passive``) and quits, so the installer can
     replace the program files;
   * Linux AppImage: atomically replaces the running AppImage (``$APPIMAGE``) and restarts;
   * macOS: opens the dmg (drag fbadmin to Applications; apps outside the App Store cannot
     replace themselves without a signed helper).
   deb/rpm installs and pip installs are updated by their package manager.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import platform
import re
import subprocess
import sys
import threading
import urllib.request
from pathlib import Path
from typing import Any
from collections.abc import Callable

from . import __version__

log = logging.getLogger("fbadmin.updater")

RELEASES_URL = os.environ.get(
    "FBADMIN_UPDATE_URL", "https://api.github.com/repos/mariuz/fbadmin/releases/latest"
)


def install_kind(env: dict[str, str] | None = None, plat: str | None = None, frozen: bool | None = None) -> str | None:
    """How this copy was installed, if it can update itself: "msi", "dmg", "appimage" or None."""
    env = os.environ if env is None else env
    plat = sys.platform if plat is None else plat
    frozen = bool(getattr(sys, "frozen", False)) if frozen is None else frozen
    if env.get("FBADMIN_DESKTOP") != "1":
        return None  # the web server: updated by pip / Docker / Helm
    if plat.startswith("linux"):
        return "appimage" if env.get("APPIMAGE") else None  # deb/rpm: the package manager
    if not frozen:
        return None  # pip-installed fbadmin-desktop
    if plat == "win32":
        return "msi"
    if plat == "darwin":
        return "dmg"
    return None


def _arch_names(machine: str) -> list[str]:
    m = machine.lower()
    if m in ("x86_64", "amd64", "x64"):
        return ["x86_64", "x64", "amd64"]
    if m in ("arm64", "aarch64"):
        return ["arm64", "aarch64"]
    return [m]


def pick_asset(assets: list[dict], version: str, kind: str, machine: str) -> dict | None:
    """The release asset for this platform, e.g. fbadmin-1.2.0-x64.msi, fbadmin-1.2.0-macos-arm64.dmg,
    fbadmin-1.2.0-x86_64.AppImage."""
    ext = {"msi": ".msi", "dmg": ".dmg", "appimage": ".AppImage"}[kind]
    archs = _arch_names(machine)
    for a in assets:
        name = a.get("name", "")
        if not name.startswith(f"fbadmin-{version}") or not name.endswith(ext):
            continue
        stem = name[len(f"fbadmin-{version}"):-len(ext)].lower()
        if any(re.search(rf"(^|[-_]){re.escape(x)}($|[-_])", stem) for x in archs):
            return a
    return None


def parse_sums(text: str) -> dict[str, str]:
    """``sha256sum`` output -> {file name: hex digest}."""
    out = {}
    for line in text.splitlines():
        m = re.match(r"^([0-9a-fA-F]{64})\s+\*?(.+?)\s*$", line)
        if m:
            out[Path(m.group(2)).name] = m.group(1).lower()
    return out


def _get(url: str, timeout: float = 15):
    req = urllib.request.Request(url, headers={"User-Agent": "fbadmin-updater", "Accept": "application/octet-stream"})
    return urllib.request.urlopen(req, timeout=timeout)  # noqa: S310 - https release URLs (or a test override)


def plan(machine: str | None = None) -> dict[str, Any]:
    """What an update would install (no download yet)."""
    from .api.version import check_enabled, is_newer

    kind = install_kind()
    out: dict[str, Any] = {"current": __version__, "supported": kind is not None, "kind": kind,
                           "latest": None, "update_available": False, "asset": None, "error": None}
    if kind is None:
        out["reason"] = ("Only the desktop app (MSI, dmg or AppImage) updates itself; update this install "
                         "with pip, Docker, Helm or your package manager.")
        return out
    if not check_enabled():
        out["supported"] = False
        out["reason"] = "Update checks are disabled (FBADMIN_UPDATE_CHECK=off)."
        return out
    req = urllib.request.Request(RELEASES_URL, headers={"Accept": "application/vnd.github+json",
                                                        "User-Agent": "fbadmin-updater"})
    with urllib.request.urlopen(req, timeout=10) as r:  # noqa: S310
        rel = json.loads(r.read().decode())
    latest = str(rel.get("tag_name", "")).lstrip("v")
    out["latest"] = latest
    out["update_available"] = is_newer(latest, __version__)
    assets = rel.get("assets") or []
    asset = pick_asset(assets, latest, kind, machine or platform.machine())
    sums_asset = next((a for a in assets if a.get("name") == "SHA256SUMS"), None)
    if asset is None:
        out["error"] = f"The release has no {kind} package for this platform."
        return out
    if sums_asset is None:
        out["error"] = "The release publishes no SHA256SUMS; refusing to install an unverified package."
        return out
    with _get(sums_asset["browser_download_url"]) as r:
        sums = parse_sums(r.read().decode())
    digest = sums.get(asset["name"])
    if not digest:
        out["error"] = f"SHA256SUMS has no entry for {asset['name']}."
        return out
    out["asset"] = {"name": asset["name"], "url": asset["browser_download_url"], "size": asset.get("size"),
                    "sha256": digest}
    return out


class Download:
    """One background download with progress, verified against the expected SHA-256."""

    def __init__(self, url: str, name: str, sha256: str, dest_dir: Path, size: int | None = None) -> None:
        self.url, self.name, self.sha256, self.size = url, name, sha256.lower(), size
        self.path = dest_dir / name
        self.received = 0
        self.state = "running"  # running | done | failed
        self.error: str | None = None
        dest_dir.mkdir(parents=True, exist_ok=True)
        self._thread = threading.Thread(target=self._run, name="fbadmin-update-download", daemon=True)

    def start(self) -> Download:
        self._thread.start()
        return self

    def join(self, timeout: float | None = None) -> None:
        self._thread.join(timeout)

    def _run(self) -> None:
        tmp = self.path.with_suffix(self.path.suffix + ".part")
        h = hashlib.sha256()
        try:
            with _get(self.url, timeout=60) as r, open(tmp, "wb") as f:
                if not self.size:
                    self.size = int(r.headers.get("Content-Length") or 0) or None
                while chunk := r.read(1 << 16):
                    f.write(chunk)
                    h.update(chunk)
                    self.received += len(chunk)
            if h.hexdigest() != self.sha256:
                raise ValueError(f"checksum mismatch for {self.name} (expected {self.sha256}, got {h.hexdigest()})")
            os.replace(tmp, self.path)
            self.state = "done"
        except Exception as exc:  # noqa: BLE001
            tmp.unlink(missing_ok=True)
            self.state, self.error = "failed", str(exc)
            log.warning("update download failed: %s", exc)

    def status(self) -> dict[str, Any]:
        return {"name": self.name, "state": self.state, "received": self.received, "size": self.size,
                "error": self.error}


_current: Download | None = None
_lock = threading.Lock()


def start_download(asset: dict[str, Any], data_dir: Path) -> Download:
    global _current
    with _lock:
        if _current and _current.state == "running":
            return _current
        _current = Download(asset["url"], asset["name"], asset["sha256"], data_dir / "updates", asset.get("size")).start()
        return _current


def current() -> Download | None:
    return _current


def apply(path: Path, kind: str, *, env: dict[str, str] | None = None,
          run: Callable[..., Any] = subprocess.Popen, restart: Callable[[], None] | None = None,
          quit_app: Callable[[], None] | None = None) -> dict[str, Any]:
    """Install a downloaded, verified package. ``run``/``restart``/``quit_app`` are injectable for tests."""
    env = os.environ if env is None else env
    if kind == "msi":
        run(["msiexec", "/i", str(path), "/passive", "/norestart"])
        if quit_app:
            quit_app()
        return {"action": "installer started; fbadmin closes so it can be replaced"}
    if kind == "dmg":
        run(["open", str(path)])
        return {"action": "the disk image is open: drag fbadmin to Applications, then restart fbadmin"}
    if kind == "appimage":
        target = Path(env["APPIMAGE"])
        staged = target.with_name(target.name + ".new")
        staged.write_bytes(path.read_bytes())
        staged.chmod(0o755)
        os.replace(staged, target)  # atomic on the same file system; the running image stays mapped
        if restart:
            restart()
        return {"action": f"{target.name} replaced; fbadmin restarts"}
    raise ValueError(f"cannot apply an update for install kind {kind!r}")


def restart_appimage() -> None:
    """Re-exec the (replaced) AppImage."""
    appimage = os.environ["APPIMAGE"]
    os.execv(appimage, [appimage, *sys.argv[1:]])  # noqa: S606
