"""FBADMIN_SERVERS_FILE: servers declared by the deployment (Kubernetes Secrets for passwords)."""

from __future__ import annotations

from fbadmin import preconfig
from fbadmin.api.deps import reset_state

from .conftest import HOST, PASSWORD, PORT, USER, requires_server


def _write(tmp_path, body: str, name="servers.yaml"):
    p = tmp_path / name
    p.write_text(body)
    return p


def test_apply_and_update(tmp_path, monkeypatch):
    from fbadmin.store import ServerStore

    store = ServerStore(tmp_path / "servers.json")
    specs = [{"name": "Prod DB", "host": "fb.svc", "port": 3051, "password_env": "FB_PW",
              "databases": [{"name": "main", "path": "/data/main.fdb"}, "/data/other.fdb"]}]
    assert preconfig.apply(store, specs) == ["cfg-prod-db"]
    srv = store.get("cfg-prod-db")
    assert (srv.host, srv.port, srv.save_password) == ("fb.svc", 3051, False)
    assert [(d.name, d.path) for d in srv.databases] == [("main", "/data/main.fdb"), ("other", "/data/other.fdb")]
    assert store.password_for("cfg-prod-db") is None
    monkeypatch.setenv("FB_PW", "s3cret")
    assert store.password_for("cfg-prod-db") == "s3cret"
    assert "s3cret" not in (tmp_path / "servers.json").read_text()
    # hand-added database survives a re-apply; changed host is applied; ids are stable
    store.add_database("cfg-prod-db", __import__("fbadmin.store", fromlist=["DatabaseEntry"]).DatabaseEntry(name="x", path="/x.fdb"))
    specs[0]["host"] = "fb2.svc"
    preconfig.apply(ServerStore(tmp_path / "servers.json"), specs)
    reloaded = ServerStore(tmp_path / "servers.json").get("cfg-prod-db")
    assert reloaded.host == "fb2.svc" and {d.path for d in reloaded.databases} == {"/data/main.fdb", "/data/other.fdb", "/x.fdb"}
    # removed from the file -> removed from the registry
    s2 = ServerStore(tmp_path / "servers.json")
    preconfig.apply(s2, [])
    assert not [x for x in s2.list() if x.id.startswith("cfg-")]


@requires_server
def test_servers_file_end_to_end(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from fbadmin.app import create_app

    monkeypatch.setenv("FBADMIN_TEST_FB_PW", PASSWORD)
    f = _write(tmp_path, f"""
servers:
  - name: Test Firebird
    host: {HOST}
    port: {PORT}
    user: {USER}
    password_env: FBADMIN_TEST_FB_PW
    databases:
      - {{name: sec, path: security.db}}
""")
    monkeypatch.setenv("FBADMIN_SERVERS_FILE", str(f))
    monkeypatch.setenv("FBADMIN_DATA_DIR", str(tmp_path / "data"))
    from fbadmin.config import settings
    monkeypatch.setattr(settings, "data_dir", tmp_path / "data")
    reset_state()
    try:
        client = TestClient(create_app())
        servers = client.get("/api/servers").json()
        srv = next(s for s in servers if s["id"] == "cfg-test-firebird")
        assert srv["password_known"] is True and srv["has_password"] is False
        assert client.post("/api/servers/cfg-test-firebird/connect", json={}).status_code == 200
        info = client.get("/api/servers/cfg-test-firebird/info")
        assert info.status_code == 200, info.text
    finally:
        monkeypatch.delenv("FBADMIN_SERVERS_FILE")
        reset_state()
