"""Finer permissions and API tokens against a real Firebird server (server mode)."""

from __future__ import annotations

import uuid

import pytest

from .conftest import DB_DIR, HOST, PASSWORD, PORT, USER, requires_server
from .test_security import ADMIN_PW, _client, login

pytestmark = requires_server

SCHEMA = """
CREATE TABLE CUSTOMER (ID INTEGER NOT NULL PRIMARY KEY, NAME VARCHAR(30));
CREATE TABLE SECRET_PAY (ID INTEGER, AMOUNT INTEGER);
CREATE TABLE AUDIT_LOG (MSG VARCHAR(50));
INSERT INTO CUSTOMER VALUES (1, 'Ann');
INSERT INTO SECRET_PAY VALUES (1, 1000);
"""


def logout(c):
    c.post("/api/auth/logout")
    c.headers.pop("X-CSRF-Token", None)
    c.cookies.clear()


@pytest.fixture
def env(tmp_path, monkeypatch):
    """Server mode with a shared Firebird server holding two databases, and four users."""
    with _client(tmp_path, monkeypatch, SERVER_MODE="1", ADMIN_USERNAME="root", ADMIN_PASSWORD=ADMIN_PW,
                 LOGIN_RATE="50/60") as c:
        login(c, "root", ADMIN_PW)
        srv = c.post("/api/servers", json={"name": "fb", "host": HOST, "port": PORT, "user": USER,
                                           "password": PASSWORD, "shared": True}).json()
        dbs = {}
        for name in ("main", "other"):
            path = f"{DB_DIR}/fbadmin_test_{uuid.uuid4().hex[:8]}.fdb"
            r = c.post(f"/api/servers/{srv['id']}/databases/create", json={"name": name, "path": path})
            assert r.status_code == 201, r.text
            dbs[name] = r.json()["id"]
        r = c.post(f"/api/servers/{srv['id']}/databases/{dbs['main']}/execute", json={"sql": SCHEMA})
        assert r.json()["error"] is None, r.text
        users = {}
        for u in ("reader", "writer", "builder", "plain"):
            users[u] = c.post("/api/admin/users", json={"username": u, "password": f"{u}-password"}).json()["id"]
        sid, main = srv["id"], dbs["main"]
        grants = [
            ("reader", None, None, "read"),
            ("writer", main, None, "write"),
            ("writer", main, "SECRET*", "none"),
            ("builder", main, None, "ddl"),
        ]
        for user, db, pattern, level in grants:
            r = c.post("/api/admin/access/grants", json={"subject_type": "user", "subject": user, "server_id": sid,
                                                          "db_id": db, "object_pattern": pattern, "level": level})
            assert r.status_code == 201, r.text
        logout(c)
        try:
            yield c, sid, dbs
        finally:
            logout(c)
            login(c, "root", ADMIN_PW)
            for db in dbs.values():
                c.post(f"/api/servers/{sid}/databases/{db}/drop")
    from fbadmin.api import deps

    deps.reset_state()


def as_user(c, sid, name):
    logout(c)
    login(c, name, f"{name}-password" if name != "root" else ADMIN_PW)
    if name != "root":  # passwords of shared servers are never shared: each user enters their own
        assert c.put(f"/api/servers/{sid}", json={"password": PASSWORD}).status_code == 200


def run(c, sid, db, sql, **kw):
    return c.post(f"/api/servers/{sid}/databases/{db}/execute", json={"sql": sql}, **kw)


def test_read_level(env):
    c, sid, dbs = env
    as_user(c, sid, "reader")
    srv = c.get("/api/servers").json()[0]
    assert {d["name"] for d in srv["databases"]} == {"main", "other"} and srv["read_only"]
    r = run(c, sid, dbs["main"], "SELECT NAME FROM CUSTOMER")
    assert r.status_code == 200 and r.json()["error"] is None
    for sql in ("INSERT INTO CUSTOMER VALUES (2, 'Bob')", "CREATE TABLE X (A INT)",
                "EXECUTE BLOCK AS BEGIN INSERT INTO CUSTOMER VALUES (3, 'Cid'); END"):
        r = run(c, sid, dbs["main"], sql)
        assert r.status_code == 403 and r.json()["code"] in ("permission_denied", "read_only"), sql
    r = c.post(f"/api/servers/{sid}/databases/{dbs['main']}/tables/CUSTOMER/data",
               json={"inserts": [{"values": {"ID": 9, "NAME": "x"}}]})
    assert r.status_code == 403
    # Query Tool sessions are opened with read-only transactions (Firebird refuses writes too)
    sess = c.post("/api/query/sessions", json={"server_id": sid, "database_id": dbs["main"]}).json()
    assert c.get(f"/api/query/sessions/{sess['session_id']}/options").json()["read_only"] is True
    r = c.post(f"/api/query/sessions/{sess['session_id']}/execute", json={"sql": "DELETE FROM CUSTOMER"})
    assert r.status_code == 403
    # admin tools
    assert c.post(f"/api/servers/{sid}/databases/{dbs['main']}/sweep").status_code == 403
    assert c.get(f"/api/servers/{sid}/databases/{dbs['main']}/maintenance/properties").status_code == 403
    assert c.get(f"/api/servers/{sid}/fbusers").status_code == 403


def test_write_level_and_hidden_objects(env):
    c, sid, dbs = env
    as_user(c, sid, "writer")
    srv = c.get("/api/servers").json()[0]
    assert [d["name"] for d in srv["databases"]] == ["main"]  # "other" is not granted
    assert c.post(f"/api/servers/{sid}/databases/{dbs['other']}/connect").status_code == 404
    base = f"/api/servers/{sid}/databases/{dbs['main']}"
    assert run(c, sid, dbs["main"], "INSERT INTO CUSTOMER VALUES (2, 'Bob')").json()["error"] is None
    r = run(c, sid, dbs["main"], "CREATE TABLE X (A INT)")
    assert r.status_code == 403 and "ddl" in r.json()["detail"]
    r = run(c, sid, dbs["main"], "SELECT * FROM SECRET_PAY")
    assert r.status_code == 403 and "SECRET_PAY" in r.json()["detail"]
    r = run(c, sid, dbs["main"], "select c.name from customer c join secret_pay s on s.id = c.id")
    assert r.status_code == 403
    tables = [t["name"] for t in c.get(base + "/objects/tables").json()]
    assert "CUSTOMER" in tables and "SECRET_PAY" not in tables
    assert "SECRET_PAY" not in c.get(base + "/completion").json()["relations"]
    assert c.get(base + "/objects/tables/SECRET_PAY").status_code == 403
    assert c.get(base + "/tables/SECRET_PAY/data").status_code == 403
    assert c.get(base + "/tables/CUSTOMER/data").status_code == 200
    # whole-database tools would reveal hidden objects
    assert c.get(base + "/schema/extract").status_code == 403
    assert c.get(base + "/schema/export", params={"table": "CUSTOMER"}).status_code == 200
    assert c.delete(base + "/objects/tables/AUDIT_LOG").status_code == 403


def test_ddl_level_and_admin_unaffected(env):
    c, sid, dbs = env
    as_user(c, sid, "builder")
    r = run(c, sid, dbs["main"], "CREATE TABLE BUILT (A INT)")
    assert r.status_code == 200 and r.json()["error"] is None
    assert c.post(f"/api/servers/{sid}/databases/{dbs['main']}/maintenance/sweep").status_code == 403
    assert c.post(f"/api/servers/{sid}/backup", json={"database_id": dbs["main"], "backup_file": "/tmp/x.fbk"}) \
        .status_code == 403
    # a user without grants keeps the old behaviour
    as_user(c, sid, "plain")
    assert run(c, sid, dbs["other"], "CREATE TABLE FREE_T (A INT)").json()["error"] is None
    # the administrator is never restricted
    as_user(c, sid, "root")
    assert run(c, sid, dbs["main"], "SELECT * FROM SECRET_PAY").json()["error"] is None
    assert c.get(f"/api/servers/{sid}/databases/{dbs['main']}/schema/extract").status_code == 200


def test_tokens_against_firebird(env):
    c, sid, dbs = env
    as_user(c, sid, "root")
    c.put(f"/api/servers/{sid}", json={"password": PASSWORD, "save_password": True})
    ro = c.post("/api/access/me/tokens", json={"name": "report-bot", "scope": "read-only"}).json()["token"]
    rw = c.post("/api/access/me/tokens", json={"name": "etl", "scope": "read-write"}).json()["token"]
    logout(c)  # tokens work while their owner is signed out (saved passwords through the token)
    h_ro = {"Authorization": f"Bearer {ro}"}
    h_rw = {"Authorization": f"Bearer {rw}"}
    r = run(c, sid, dbs["main"], "SELECT COUNT(*) AS N FROM CUSTOMER", headers=h_ro)
    assert r.status_code == 200 and r.json()["error"] is None, r.text
    r = run(c, sid, dbs["main"], "UPDATE CUSTOMER SET NAME = 'X'", headers=h_ro)
    assert r.status_code == 403 and r.json()["code"] in ("token_read_only", "read_only")
    sess = c.post("/api/query/sessions", json={"server_id": sid, "database_id": dbs["main"]}, headers=h_ro).json()
    r = c.post(f"/api/query/sessions/{sess['session_id']}/execute", json={"sql": "DELETE FROM AUDIT_LOG"},
               headers=h_ro)
    assert r.status_code == 403
    r = run(c, sid, dbs["main"], "INSERT INTO AUDIT_LOG VALUES ('from etl')", headers=h_rw)
    assert r.status_code == 200 and r.json()["error"] is None
    # the audit log names the token
    login(c, "root", ADMIN_PW)
    rows = c.get("/api/admin/audit", params={"q": "from etl"}).json()["rows"]
    assert rows and rows[0]["username"] == "root" and rows[0]["token"] == "etl"
