"""Desktop auto-update: asset choice, checksum verification, AppImage replacement (no network)."""

from __future__ import annotations

import hashlib
import http.server
import json
import threading
from pathlib import Path

import pytest

from fbadmin import updater


def test_install_kind():
    assert updater.install_kind(env={}, plat="win32", frozen=True) is None  # not the desktop app
    d = {"FBADMIN_DESKTOP": "1"}
    assert updater.install_kind(env=d, plat="win32", frozen=True) == "msi"
    assert updater.install_kind(env=d, plat="darwin", frozen=True) == "dmg"
    assert updater.install_kind(env=d, plat="win32", frozen=False) is None  # pip-installed
    assert updater.install_kind(env={**d, "APPIMAGE": "/x/fbadmin.AppImage"}, plat="linux", frozen=True) == "appimage"
    assert updater.install_kind(env=d, plat="linux", frozen=True) is None  # deb/rpm


def test_pick_asset_and_sums():
    names = ["fbadmin-1.2.0-x64.msi", "fbadmin-1.2.0-macos-arm64.dmg", "fbadmin-1.2.0-macos-x86_64.dmg",
             "fbadmin-1.2.0-x86_64.AppImage", "fbadmin-1.2.0-aarch64.AppImage", "fbadmin_1.2.0_amd64.deb"]
    assets = [{"name": n} for n in names]
    assert updater.pick_asset(assets, "1.2.0", "msi", "AMD64")["name"] == names[0]
    assert updater.pick_asset(assets, "1.2.0", "dmg", "arm64")["name"] == names[1]
    assert updater.pick_asset(assets, "1.2.0", "dmg", "x86_64")["name"] == names[2]
    assert updater.pick_asset(assets, "1.2.0", "appimage", "aarch64")["name"] == names[4]
    assert updater.pick_asset(assets, "1.3.0", "msi", "AMD64") is None
    a, b = "a" * 64, "B" * 64
    sums = updater.parse_sums(f"{a}  fbadmin-1.2.0-x64.msi\n{b} *dist/fbadmin-1.2.0-x86_64.AppImage\n")
    assert sums == {"fbadmin-1.2.0-x64.msi": "a" * 64, "fbadmin-1.2.0-x86_64.AppImage": "b" * 64}


@pytest.fixture
def release_server(tmp_path):
    """A fake GitHub "latest release" with an AppImage and SHA256SUMS on localhost."""
    files: dict[str, bytes] = {}

    class H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            body = files.get(self.path)
            if body is None:
                self.send_response(404)
                self.end_headers()
                return
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass

    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    threading.Thread(target=srv.serve_forever, daemon=True).start()

    def publish(version: str, payload: bytes, good_sum: bool = True):
        name = f"fbadmin-{version}-x86_64.AppImage"
        digest = hashlib.sha256(payload if good_sum else b"other").hexdigest()
        files[f"/dl/{name}"] = payload
        files["/dl/SHA256SUMS"] = f"{digest}  {name}\n".encode()
        files["/latest"] = json.dumps({"tag_name": f"v{version}", "assets": [
            {"name": name, "browser_download_url": f"{base}/dl/{name}", "size": len(payload)},
            {"name": "SHA256SUMS", "browser_download_url": f"{base}/dl/SHA256SUMS"},
        ]}).encode()
        return f"{base}/latest"

    yield publish
    srv.shutdown()


def _desktop_appimage(monkeypatch, tmp_path) -> Path:
    app = tmp_path / "fbadmin.AppImage"
    app.write_bytes(b"old build")
    monkeypatch.setenv("FBADMIN_DESKTOP", "1")
    monkeypatch.setenv("APPIMAGE", str(app))
    monkeypatch.setattr(updater.sys, "platform", "linux")
    monkeypatch.setattr(updater.platform, "machine", lambda: "x86_64")
    return app


def test_download_verify_and_replace_appimage(monkeypatch, tmp_path, release_server):
    app = _desktop_appimage(monkeypatch, tmp_path)
    monkeypatch.setattr(updater, "RELEASES_URL", release_server("99.0.0", b"new build " * 1000))
    p = updater.plan()
    assert p["supported"] and p["update_available"] and p["kind"] == "appimage"
    d = updater.Download(**{k: p["asset"][k] for k in ("url", "name", "sha256", "size")}, dest_dir=tmp_path / "upd").start()
    d.join(10)
    assert d.state == "done" and d.received == 10000
    restarted = []
    r = updater.apply(d.path, "appimage", restart=lambda: restarted.append(True))
    assert app.read_bytes() == b"new build " * 1000 and restarted and "replaced" in r["action"]
    assert app.stat().st_mode & 0o111


def test_checksum_mismatch_is_refused(monkeypatch, tmp_path, release_server):
    _desktop_appimage(monkeypatch, tmp_path)
    monkeypatch.setattr(updater, "RELEASES_URL", release_server("99.0.0", b"tampered", good_sum=False))
    p = updater.plan()
    d = updater.Download(**{k: p["asset"][k] for k in ("url", "name", "sha256", "size")}, dest_dir=tmp_path / "upd").start()
    d.join(10)
    assert d.state == "failed" and "checksum mismatch" in d.error
    assert not (tmp_path / "upd" / p["asset"]["name"]).exists()


def test_update_api(monkeypatch, tmp_path, client, release_server):
    # not the desktop app: nothing to do
    monkeypatch.delenv("FBADMIN_DESKTOP", raising=False)
    assert client.get("/api/version/update").json()["supported"] is False
    assert client.post("/api/version/update/download").status_code == 400

    app = _desktop_appimage(monkeypatch, tmp_path)
    monkeypatch.setattr(updater, "RELEASES_URL", release_server("99.0.0", b"via api"))
    from fbadmin.config import settings
    monkeypatch.setattr(settings, "data_dir", tmp_path / "data")
    monkeypatch.setattr(updater, "restart_appimage", lambda: None)
    p = client.get("/api/version/update").json()
    assert p["update_available"] and p["asset"]["name"] == "fbadmin-99.0.0-x86_64.AppImage"
    assert client.post("/api/version/update/download").json()["state"] in ("running", "done")
    updater.current().join(10)
    assert client.get("/api/version/update/status").json()["state"] == "done"
    assert "replaced" in client.post("/api/version/update/apply").json()["action"]
    assert app.read_bytes() == b"via api"
