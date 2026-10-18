"""Integration tests need a Firebird server.

Set FBADMIN_TEST_HOST (default 127.0.0.1), FBADMIN_TEST_PORT (3050),
FBADMIN_TEST_PASSWORD (masterkey) and FBADMIN_TEST_DIR, the directory *on the
server* where temporary test databases are created (default /var/lib/firebird/data,
which matches the official firebirdsql/firebird Docker image).
"""

from __future__ import annotations

import os
import socket
import uuid

import pytest

HOST = os.environ.get("FBADMIN_TEST_HOST", "127.0.0.1")
PORT = int(os.environ.get("FBADMIN_TEST_PORT", "3050"))
USER = os.environ.get("FBADMIN_TEST_USER", "SYSDBA")
PASSWORD = os.environ.get("FBADMIN_TEST_PASSWORD", "masterkey")
DB_DIR = os.environ.get("FBADMIN_TEST_DIR", "/var/lib/firebird/data")


def _server_up() -> bool:
    try:
        with socket.create_connection((HOST, PORT), timeout=1):
            return True
    except OSError:
        return False


requires_server = pytest.mark.skipif(not _server_up(), reason=f"no Firebird server at {HOST}:{PORT}")


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("FBADMIN_DATA_DIR", str(tmp_path))
    from fastapi.testclient import TestClient

    from fbadmin import config
    from fbadmin.api import deps

    monkeypatch.setattr(config.settings, "data_dir", tmp_path)
    deps.reset_state()
    from fbadmin.app import create_app

    with TestClient(create_app()) as c:
        yield c
    deps.reset_state()


@pytest.fixture
def server(client):
    r = client.post(
        "/api/servers",
        json={"name": "test", "host": HOST, "port": PORT, "user": USER, "password": PASSWORD},
    )
    assert r.status_code == 201, r.text
    return r.json()


@pytest.fixture
def database(client, server):
    """A fresh, empty database created through the API and dropped afterwards."""
    path = f"{DB_DIR}/fbadmin_test_{uuid.uuid4().hex[:8]}.fdb"
    r = client.post(f"/api/servers/{server['id']}/databases/create", json={"name": "t", "path": path})
    assert r.status_code == 201, r.text
    db = r.json()
    yield server["id"], db["id"]
    client.post(f"/api/servers/{server['id']}/databases/{db['id']}/drop")
