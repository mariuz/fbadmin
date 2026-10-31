"""Database timeline (roadmap 2.3): changes recorded as they happen (DDL from the Query Tool and the dialogs,
maintenance jobs with their outcome, property changes, dropped objects), scheduled jobs, alerts, migrations,
engine upgrades, filters, and audit entries for administrators only."""

from __future__ import annotations

import time

import pytest

from fbadmin import timeline as T

from .conftest import requires_server
from .test_library import _switch, srv  # noqa: F401 - fixture
from .test_security import ADMIN_PW, login


def _wait(client, job_id, timeout=120):
    for _ in range(int(timeout / 0.2)):
        j = client.get(f"/api/jobs/{job_id}").json()
        if j["status"] != "running":
            return j
        time.sleep(0.2)
    raise AssertionError("job did not end")


def _tl(client, sid, dbid, **params):
    r = client.get(f"/api/servers/{sid}/databases/{dbid}/timeline", params=params)
    assert r.status_code == 200, r.text
    return r.json()


def test_request_rules():
    m = T._match("POST", "/api/servers/s1/databases/d1/maintenance/sweep")
    assert (m.server_id, m.db_id, m.category, m.action) == ("s1", "d1", "maintenance", "sweep")
    m = T._match("DELETE", "/api/servers/s1/databases/d1/objects/tables/ORDERS")
    assert m.action == "drop" and m.groups == ("tables", "ORDERS")
    assert T._match("GET", "/api/servers/s1/databases/d1/maintenance/sweep") is None
    assert T._match("POST", "/api/servers/s1/databases/d1/explain") is None
    assert T._match("POST", "/api/servers/s1/databases/d1/datacompare/abc/apply").category == "data"
    assert T._match("POST", "/api/query/sessions/nope/execute") is None  # unknown session
    assert T._summary(["CREATE TABLE T (A INTEGER)", "CREATE INDEX X ON T (A)"]) == "CREATE TABLE T (A INTEGER) (+1 more)"
    assert T._statements("SELECT 1 FROM RDB$DATABASE; CREATE TABLE U (B INT); UPDATE U SET B = 1", ("DDL",)) == \
        ["CREATE TABLE U (B INT)"]


def test_engine_upgrades_are_observed(tmp_path, monkeypatch):
    from fbadmin import config

    monkeypatch.setattr(config.settings, "data_dir", tmp_path)
    st = T.get_store()
    st.observe("s", "d", "4.0.4", "13.0")  # baseline: no event
    assert st.events("s", "d", None, None) == []
    st.observe("s", "d", "4.0.4", "13.0")
    st.observe("s", "d", "5.0.1", "13.0")  # the server was upgraded, the file not yet
    st.observe("s", "d", "5.0.1", "13.1")  # restored on the new version
    titles = [e["title"] for e in st.events("s", "d", None, None)]
    assert titles == ["On-disk structure (ODS) 13.0 → 13.1", "Firebird 4.0.4 → 5.0.1"] or \
        sorted(titles) == sorted(["On-disk structure (ODS) 13.0 → 13.1", "Firebird 4.0.4 → 5.0.1"])
    T._store = None
    st2 = T.get_store()  # the baseline survives a restart
    st2.observe("s", "d", "5.0.1", "13.1")
    assert len(st2.events("s", "d", None, None)) == 2


@requires_server
def test_timeline_of_a_database(client, database):
    sid, dbid = database
    base = f"/api/servers/{sid}/databases/{dbid}"
    t0 = time.time() - 1

    # DDL from an object dialog and from the Query Tool (a failing one too); queries are not recorded
    r = client.post(base + "/execute", json={"sql": "CREATE TABLE ORDERS (ID INTEGER NOT NULL PRIMARY KEY, AMOUNT INTEGER)"})
    assert r.status_code == 200 and r.json()["error"] is None
    s = client.post("/api/query/sessions", json={"server_id": sid, "database_id": dbid}).json()["session_id"]
    for sql in ("CREATE INDEX IX_AMOUNT ON ORDERS (AMOUNT)", "SELECT * FROM ORDERS",
                "INSERT INTO ORDERS VALUES (1, 10)", "CREATE TABLE ORDERS (X INTEGER)"):
        client.post(f"/api/query/sessions/{s}/execute", json={"sql": sql, "autocommit": True})
    client.delete(f"/api/query/sessions/{s}")
    client.post(base + "/execute", json={"sql": "CREATE TABLE TMP (A INTEGER)"})
    assert client.delete(base + "/objects/tables/TMP").status_code in (200, 204)

    # maintenance: a sweep job (its outcome arrives when it ends) and a property change
    job = client.post(base + "/maintenance/sweep").json()
    assert _wait(client, job["id"])["status"] == "finished"
    r = client.put(base + "/maintenance/properties", json={"sweep_interval": 12345})
    assert r.status_code == 200, r.text
    # a refused request records nothing
    assert client.put(base + "/maintenance/properties", json={"dialect": 9}).status_code >= 400

    # a scheduled backup
    sch = client.post("/api/schedules", json={"name": "nightly", "kind": "backup", "server_id": sid,
                                              "database_id": dbid, "cron": "0 3 * * *"}).json()
    run = client.post(f"/api/schedules/{sch['id']}/run").json()
    _wait(client, run["id"])
    for _ in range(50):  # the scheduler records the run right after the job
        if client.get(f"/api/schedules/{sch['id']}/history").json():
            break
        time.sleep(0.1)

    # an alert sent for the database
    from fbadmin.alerts.engine import get_engine

    ch = client.post("/api/alerts/channels", json={"name": "c", "kind": "webhook",
                                                   "settings": {"url": "http://127.0.0.1:9/x"}}).json()
    client.post("/api/alerts/rules", json={"name": "slow", "event": "query.regression", "server_id": sid,
                                           "database_id": dbid, "channels": [ch["id"]]})
    get_engine().emit("query.regression", "warning", "A statement got slower", "select * from orders 5x slower",
                      server_id=sid, database_id=dbid)

    # an engine upgrade: pretend fbadmin last saw an older version
    T.get_store().observe(sid, dbid, "2.5.9", "11.2")

    # a migration recorded in the database
    from fbadmin.migrations import DDL

    s = client.post("/api/query/sessions", json={"server_id": sid, "database_id": dbid}).json()["session_id"]
    for sql in DDL[:1] + ["INSERT INTO FBADMIN_MIGRATIONS (MIGRATION_VERSION, DESCR, KIND, SUCCESS, INSTALLED_BY, "
                          "EXECUTION_MS) VALUES ('1', 'orders', 'versioned', 1, 'SYSDBA', 12)"]:
        r = client.post(f"/api/query/sessions/{s}/execute", json={"sql": sql, "autocommit": True}).json()
        assert r["error"] is None, r
    client.delete(f"/api/query/sessions/{s}")

    try:
        tl = _tl(client, sid, dbid)
        assert tl["problems"] == [] and tl["engine_version"] and tl["audit"] is True  # desktop: the user is admin
        ev = tl["events"]
        titles = [e["title"] for e in ev]
        by = {e["title"]: e for e in ev}
        assert [e["ts"] for e in ev] == sorted((e["ts"] for e in ev), reverse=True)
        assert "DDL: CREATE TABLE ORDERS (ID INTEGER NOT NULL PRIMARY KEY, AMOUNT INTEGER)" in titles
        assert by["DDL: CREATE INDEX IX_AMOUNT ON ORDERS (AMOUNT)"]["status"] == "ok"
        failed = by["DDL: CREATE TABLE ORDERS (X INTEGER)"]
        assert failed["status"] == "failed" and "ORDERS" in failed["error"]
        assert not any("SELECT" in x or "INSERT INTO ORDERS" in x for x in titles)
        assert "Dropped table TMP" in titles
        sweep = by["Sweep"]
        assert sweep["status"] == "ok" and sweep["category"] == "maintenance" and sweep["ref"]["job_id"] == job["id"]
        props = next(e for e in ev if e["title"].startswith("Properties changed"))
        assert "12345" in props["title"] or "12345" in (props["detail"] or "")
        assert sum(1 for e in ev if e["title"].startswith("Properties changed")) == 1
        backup = by["Scheduled backup: nightly"]
        assert backup["category"] == "backup" and backup["source"] == "schedule" and backup["ref"]["schedule_id"] == sch["id"]
        alert = by["A statement got slower"]
        assert alert["category"] == "alert" and alert["status"] == "warning"
        ups = sorted(e["title"] for e in ev if e["category"] == "upgrade")
        assert len(ups) == 2 and ups[0].startswith("Firebird 2.5.9 → ") and ups[1].startswith("On-disk structure (ODS) 11.2 → ")
        mig = by["Migration V1 — orders"]
        assert mig["category"] == "schema" and mig["user"] == "SYSDBA"
        assert tl["counts"]["schema"] >= 4 and tl["counts"]["maintenance"] >= 2

        # filters
        only = _tl(client, sid, dbid, category="backup,alert")["events"]
        assert {e["category"] for e in only} == {"backup", "alert"}
        assert [e["title"] for e in _tl(client, sid, dbid, q="ix_amount")["events"]] == \
            ["DDL: CREATE INDEX IX_AMOUNT ON ORDERS (AMOUNT)"]
        assert _tl(client, sid, dbid, until=t0)["events"] == []
        assert len(_tl(client, sid, dbid, since=t0, limit=3)["events"]) == 3
        assert _tl(client, sid, dbid, limit=3)["has_more"] is True
        assert client.get(f"/api/servers/{sid}/databases/nope/timeline").status_code == 404
    finally:
        get_engine().history.clear()
        for r in client.get("/api/alerts/rules").json():
            client.delete(f"/api/alerts/rules/{r['id']}")
        for c in client.get("/api/alerts/channels").json():
            client.delete(f"/api/alerts/channels/{c['id']}")
        client.delete(f"/api/schedules/{sch['id']}")


def test_audit_entries_for_administrators_only(srv, monkeypatch):  # noqa: F811
    c = srv
    login(c, "admin@example.org", ADMIN_PW)
    assert c.post("/api/admin/users", json={"username": "erin", "password": "erin-password",
                                            "role": "user"}).status_code == 201
    s1 = c.post("/api/servers", json={"name": "prod", "host": "127.0.0.1", "port": 9, "shared": True}).json()
    d1 = c.post(f"/api/servers/{s1['id']}/databases", json={"name": "sales", "path": "/db/sales.fdb"}).json()
    from fbadmin.api.deps import get_state

    audit = get_state().security.audit
    monkeypatch.setattr(audit, "enabled", True)
    audit.record(server_id=s1["id"], db_id=d1["id"], statement="ALTER TABLE ORDERS ADD NOTE VARCHAR(20)", success=True)
    audit.record(server_id=s1["id"], db_id=d1["id"], statement="SELECT * FROM ORDERS", success=True)
    audit.record(server_id=s1["id"], db_id=d1["id"], statement="DROP TABLE X", success=False, error="no such table")

    tl = _tl(c, s1["id"], d1["id"])
    assert tl["audit"] is True and [p["source"] for p in tl["problems"]] == ["database"]  # nothing listens on :9
    titles = [e["title"] for e in tl["events"]]
    assert titles == ["DDL: DROP TABLE X", "DDL: ALTER TABLE ORDERS ADD NOTE VARCHAR(20)"]
    assert tl["events"][0]["status"] == "failed" and tl["events"][0]["error"] == "no such table"
    assert len(_tl(c, s1["id"], d1["id"], audit_queries="true")["events"]) == 3

    _switch(c, "erin", "erin-password")
    tl = _tl(c, s1["id"], d1["id"])
    assert tl["audit"] is False and tl["events"] == []


@pytest.fixture(autouse=True)
def _fresh_store():
    yield
    T._store = None
