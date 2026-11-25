"""Large uploads (issue #10): the restore upload and data import files are streamed to disk, so the 32 MB request
limit does not apply to them (nor the server-mode one); they are bounded by FBADMIN_MAX_UPLOAD_BYTES and the free
space in fbadmin's data directory, checked before the upload starts. These tests do not need a Firebird server."""

from __future__ import annotations

from fbadmin import uploads

from .test_library import _switch, desktop, srv  # noqa: F401 - fixtures
from .test_security import ADMIN_PW, login

BIG = b"x" * (40 * 1024 * 1024)  # over the 32 MiB request limit


def test_streamed_routes():
    assert uploads.is_streamed("/api/servers/abc/maintenance/restore/upload")
    assert uploads.is_streamed("/api/schema/uploads")
    assert not uploads.is_streamed("/api/servers/abc/databases/d/execute")


def test_large_uploads_pass_the_request_limit(desktop, monkeypatch):  # noqa: F811
    c = desktop
    s = c.post("/api/servers", json={"name": "fb", "host": "127.0.0.1", "port": 9, "password": "x"}).json()
    # an ordinary request over 32 MiB is still refused, naming the setting
    r = c.post(f"/api/servers/{s['id']}/databases/x/execute", content=BIG, headers={"content-type": "application/json"})
    assert r.status_code == 413 and "FBADMIN_MAX_REQUEST_BYTES" in r.json()["detail"]
    # the restore upload is accepted (the restore job then fails: the bytes are not a backup)
    r = c.post(f"/api/servers/{s['id']}/maintenance/restore/upload", params={"database_path": "/tmp/x.fdb"},
               content=BIG, headers={"content-type": "application/octet-stream"})
    assert r.status_code == 202, r.text
    r = c.post("/api/schema/uploads", params={"filename": "big.csv"}, content=BIG)
    assert r.status_code == 201, r.text

    lim = c.get(f"/api/servers/{s['id']}/maintenance/upload-limits").json()
    assert lim["max_upload_bytes"] == 0 and lim["free_bytes"] > 0
    # not enough room: refused before anything is written
    monkeypatch.setenv("FBADMIN_UPLOAD_FREE_MARGIN", str(lim["free_bytes"]))
    r = c.post(f"/api/servers/{s['id']}/maintenance/restore/upload", params={"database_path": "/tmp/x.fdb"},
               content=BIG, headers={"content-type": "application/octet-stream"})
    assert r.status_code == 507 and "free" in r.json()["detail"]
    monkeypatch.delenv("FBADMIN_UPLOAD_FREE_MARGIN")
    # an explicit limit
    monkeypatch.setenv("FBADMIN_MAX_UPLOAD_BYTES", str(10 * 1024 * 1024))
    r = c.post("/api/schema/uploads", params={"filename": "big.csv"}, content=BIG)
    assert r.status_code == 400 and "FBADMIN_MAX_UPLOAD_BYTES" in r.json()["detail"]


def test_large_upload_in_server_mode(srv):  # noqa: F811
    c = srv
    login(c, "admin@example.org", ADMIN_PW)
    s = c.post("/api/servers", json={"name": "fb", "host": "127.0.0.1", "port": 9, "password": "x"}).json()
    r = c.post(f"/api/servers/{s['id']}/maintenance/restore/upload", params={"database_path": "/tmp/x.fdb"},
               content=BIG, headers={"content-type": "application/octet-stream"})
    assert r.status_code == 202, r.text  # neither the 16 MB server-mode limit nor a read into memory
