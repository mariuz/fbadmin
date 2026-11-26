"""The one-time administrator file (FBADMIN_ADMIN_FILE) the Windows server MSI writes (ADMIN_USER, ADMIN_PASSWORD)."""

from __future__ import annotations

from fbadmin.security.core import read_admin_file

from .test_security import _client, login


def test_read_admin_file(tmp_path):
    f = tmp_path / "initial-admin.ini"
    f.write_text('﻿[admin]\n; written by the installer\nusername="ops"\npassword="p=ss"w\\ord"\nother=x\n', "utf-8")
    assert read_admin_file(f) == {"username": "ops", "password": 'p=ss"w\\ord'}


def test_first_admin_from_the_file_then_deleted(tmp_path, monkeypatch):
    f = tmp_path / "initial-admin.ini"
    f.write_text('[admin]\nusername="ops"\npassword="Install-Time-Pw1"\n', "utf-8")
    with _client(tmp_path, monkeypatch, SERVER_MODE="1", ADMIN_FILE="initial-admin.ini") as c:
        assert not f.exists()  # it held a password: deleted once the account exists
        assert c.get("/api/auth/status").json().get("needs_setup") is False
        login(c, "ops", "Install-Time-Pw1")
    from fbadmin.api import deps

    deps.reset_state()


def test_weak_password_keeps_the_setup_page(tmp_path, monkeypatch):
    f = tmp_path / "initial-admin.ini"
    f.write_text('password="short"\n', "utf-8")
    with _client(tmp_path, monkeypatch, SERVER_MODE="1", ADMIN_FILE=str(f)) as c:
        assert f.exists()  # no account: kept, and the error is logged
        assert c.get("/api/auth/status").json().get("needs_setup") is True
    from fbadmin.api import deps

    deps.reset_state()


def test_desktop_mode_leaves_the_file(tmp_path, monkeypatch):
    f = tmp_path / "initial-admin.ini"
    f.write_text('password="Install-Time-Pw1"\n', "utf-8")
    with _client(tmp_path, monkeypatch, ADMIN_FILE=str(f)):
        assert f.exists()
    from fbadmin.api import deps

    deps.reset_state()


def test_write_admin_file_takes_the_password_literally(tmp_path):
    from fbadmin import admin_file

    target = tmp_path / "initial-admin.ini"
    pw = 'a "quoted" pass\\word --user=x '
    raw = f'"C:\\Program Files\\fbadmin-server\\fbadmin-server.exe" write-admin-file "{target}" --user=ops ' \
          f'--email= --password={pw}'
    assert admin_file.main(raw) == 0
    assert read_admin_file(target) == {"username": "ops", "password": pw}
    # nothing to write without a password; a missing --password= is an error
    other = tmp_path / "other.ini"
    assert admin_file.main(f'x write-admin-file "{other}" --user=ops --password=') == 0 and not other.exists()
    assert admin_file.main(f'x write-admin-file "{other}" --user=ops') == 2
