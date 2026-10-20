"""Multi-server overview (milestone 2.0): reachable, unreachable and password-less registrations,
timeouts, caching, alert/schedule information and server-mode visibility."""

from __future__ import annotations

import threading
import time

import firebirdsql
import pytest

from tests.conftest import HOST, PASSWORD, PORT, USER, requires_server
from tests.test_security import ADMIN_PW, _client, login


@pytest.fixture(autouse=True)
def _fresh_cache():
    from fbadmin.monitoring import overview

    overview.clear_cache()
    yield
    overview.clear_cache()


def _srv(ov: dict, name: str) -> dict:
    return next(s for s in ov["servers"] if s["name"] == name)


def test_unreachable_and_password_not_saved(client):
    bad = client.post("/api/servers", json={"name": "bad port", "host": "127.0.0.1", "port": 1,
                                            "password": "x"}).json()
    client.post(f"/api/servers/{bad['id']}/databases", json={"name": "gone", "path": "/nowhere.fdb"})
    nopw = client.post("/api/servers", json={"name": "no password", "host": "127.0.0.1", "port": 1,
                                             "save_password": False}).json()
    client.post(f"/api/servers/{nopw['id']}/databases", json={"name": "db", "path": "/x.fdb"})
    t0 = time.monotonic()
    r = client.get("/api/overview")
    assert r.status_code == 200, r.text  # never a 401 password prompt
    assert time.monotonic() - t0 < 5
    ov = r.json()
    s = _srv(ov, "bad port")
    assert s["status"] == "critical" and s["code"] == "unreachable" and s["error"]
    assert s["databases"][0]["status"] == "critical" and s["databases"][0]["code"] == "unreachable"
    assert "refused" in s["databases"][0]["error"].lower() or s["databases"][0]["error"]
    s = _srv(ov, "no password")
    assert s["status"] == "unknown" and s["code"] == "password_not_saved"
    assert s["databases"][0]["code"] == "password_not_saved" and s["databases"][0]["status"] == "unknown"


def test_timeout_does_not_stall_and_reuses_running_check(client, monkeypatch):
    from fbadmin.monitoring import overview

    calls = []
    release = threading.Event()

    def slow(mgr, srv, db, pw):
        calls.append(db.id)
        release.wait(10)
        return {"engine_version": "x"}

    def slow_server(mgr, srv):
        release.wait(10)
        return {"server_version": "x"}

    monkeypatch.setattr(overview, "_probe_database", slow)
    monkeypatch.setattr(overview, "_probe_server", slow_server)
    monkeypatch.setattr(overview, "TIMEOUT", 0.5)
    s = client.post("/api/servers", json={"name": "slow", "host": "10.255.255.1", "password": "x"}).json()
    client.post(f"/api/servers/{s['id']}/databases", json={"name": "a", "path": "/a.fdb"})
    client.post(f"/api/servers/{s['id']}/databases", json={"name": "b", "path": "/b.fdb"})
    try:
        t0 = time.monotonic()
        ov = client.get("/api/overview").json()
        assert time.monotonic() - t0 < 2.5  # one shared deadline, not one per check
        srv = _srv(ov, "slow")
        assert srv["code"] == "timeout" and srv["status"] == "critical"
        assert [d["code"] for d in srv["databases"]] == ["timeout", "timeout"]
        # cached for a short while
        assert client.get("/api/overview").json()["cached"] is True
        # a refresh while the checks still run waits for them instead of starting new ones
        ov = client.get("/api/overview?refresh=true").json()
        assert ov["cached"] is False and len(calls) == 2
    finally:
        release.set()
    time.sleep(0.2)
    ov = client.get("/api/overview?refresh=true").json()
    assert [d["status"] for d in _srv(ov, "slow")["databases"]] == ["ok", "ok"]
    assert len(calls) == 4


@requires_server
def test_reachable_server_and_database(client, database):
    sid, dbid = database
    client.post(f"/api/servers/{sid}/disconnect")  # the overview opens its own short-lived attachment
    ov = client.get("/api/overview").json()
    assert ov["cached"] is False
    s = next(x for x in ov["servers"] if x["id"] == sid)
    assert s["status"] == "ok", s
    assert "Firebird" in s["server_version"] or s["server_version"].startswith(("LI-", "WI-"))
    d = s["databases"][0]
    assert d["status"] == "ok" and d["code"] is None and d["issues"] == []
    assert int(d["engine_version"].split(".")[0]) >= 3
    assert d["page_size"] > 0 and d["pages"] > 0 and d["size_bytes"] == d["page_size"] * d["pages"]
    assert d["attachments"] == 0 and d["transactions"] == 0  # fbadmin's own check is not counted
    assert d["gaps"]["next_oat"] >= 0 and d["long_transactions"] == 0
    assert d["nbackup_state"] == "normal" and d["ods"]
    major = int(d["engine_version"].split(".")[0])
    if major >= 4:
        assert d["replication"]["replica_mode"] == "none"
    else:
        assert d["replication"] is None
    # the check did not leave a metadata connection behind (the tree stays disconnected)
    assert not any(x["connected"] for x in client.get(f"/api/servers/{sid}").json()["databases"])
    assert client.get("/api/overview").json()["cached"] is True
    # with the tree connected, the overview reuses its metadata connection
    assert client.post(f"/api/servers/{sid}/databases/{dbid}/connect").status_code == 200
    d = client.get("/api/overview?refresh=true").json()["servers"][0]["databases"][0]
    assert d["status"] == "ok" and d["attachments"] == 0


@requires_server
def test_long_transaction_alerts_and_jobs(client, database, monkeypatch):
    from fbadmin.alerts.engine import get_engine
    from fbadmin.maintenance.scheduler import get_scheduler
    from fbadmin.monitoring import overview

    sid, dbid = database
    path = client.get(f"/api/servers/{sid}").json()["databases"][0]["path"]
    monkeypatch.setattr(overview, "LONG_TX", 1)
    other = firebirdsql.connect(host=HOST, port=PORT, database=path, user=USER, password=PASSWORD)
    try:
        cur = other.cursor()
        cur.execute("SELECT 1 FROM RDB$DATABASE")  # keeps a transaction open
        cur.fetchall()
        time.sleep(1.5)
        # a firing condition rule and a failed scheduled job of this database
        ch = client.post("/api/alerts/channels", json={"name": "c", "kind": "webhook",
                                                       "settings": {"url": "http://127.0.0.1:9/x"}}).json()
        rule = client.post("/api/alerts/rules", json={"name": "gap rule", "event": "oat_gap", "server_id": sid,
                                                      "database_id": dbid, "channels": [ch["id"]]}).json()
        get_engine().status[rule["id"]] = {"checked_at": time.time(), "state": "firing", "value": 12345,
                                           "message": "gap too large", "error": None}
        sch = client.post("/api/schedules", json={"name": "nightly", "server_id": sid, "database_id": dbid,
                                                  "cron": "0 3 * * *"}).json()
        s = get_scheduler().get(sch["id"])
        get_scheduler()._record(s, None, time.time() - 60, "finished")
        get_scheduler()._record(s, None, time.time(), "failed", "disk full")

        d = client.get("/api/overview").json()["servers"][0]["databases"][0]
        assert d["attachments"] >= 1 and d["long_transactions"] >= 1 and d["oldest_transaction_age"] >= 1
        assert d["oat_holder"] is not None
        codes = {i["code"] for i in d["issues"]}
        assert {"long_transactions", "alert", "job_failed"} <= codes
        assert d["status"] == "warning"
        assert d["alerts"][0]["rule"] == "gap rule" and d["alerts"][0]["message"] == "gap too large"
        assert d["job_failures"][0]["schedule"] == "nightly" and d["job_failures"][0]["error"] == "disk full"
        assert d["last_backup"]["schedule"] == "nightly" and d["last_backup"]["at"] > time.time() - 120
        assert client.get("/api/overview").json()["servers"][0]["status"] == "warning"
    finally:
        other.close()
        get_engine().status.clear()
        for r in client.get("/api/alerts/rules").json():
            client.delete(f"/api/alerts/rules/{r['id']}")
        for c in client.get("/api/alerts/channels").json():
            client.delete(f"/api/alerts/channels/{c['id']}")
        for s in client.get("/api/schedules").json():
            client.delete(f"/api/schedules/{s['id']}")


@pytest.fixture
def srv_mode(tmp_path, monkeypatch):
    with _client(tmp_path, monkeypatch, SERVER_MODE="1", ADMIN_EMAIL="admin@example.org",
                 ADMIN_PASSWORD=ADMIN_PW) as c:
        yield c
    from fbadmin.api import deps

    deps.reset_state()


def test_server_mode_shows_only_visible_servers(srv_mode):
    c = srv_mode
    login(c, "admin@example.org", ADMIN_PW)
    c.post("/api/admin/users", json={"username": "alice", "password": "alice-password", "role": "user"})
    c.post("/api/servers", json={"name": "admin private", "host": "127.0.0.1", "port": 1, "password": "x"})
    c.post("/api/servers", json={"name": "team", "host": "127.0.0.1", "port": 1, "shared": True, "password": "x"})
    assert {s["name"] for s in c.get("/api/overview").json()["servers"]} == {"admin private", "team"}
    c.post("/api/auth/logout")
    c.headers.pop("X-CSRF-Token", None)
    login(c, "alice", "alice-password")
    c.post("/api/servers", json={"name": "alice own", "host": "127.0.0.1", "port": 1, "password": "y"})
    ov = c.get("/api/overview").json()
    assert {s["name"]: s["code"] for s in ov["servers"]} == {"alice own": "unreachable",
                                                             "team": "password_not_saved"}
    c.post("/api/auth/logout")
    c.headers.pop("X-CSRF-Token", None)
    assert c.get("/api/overview").status_code == 401

