"""Security features that need a Firebird server: read-only servers, audit, wire options,
Firebird users (SQL and Services API), mappings and system privileges."""

from __future__ import annotations

import uuid

import pytest

from .conftest import HOST, PASSWORD, PORT, USER, requires_server

pytestmark = requires_server


def base(sid, dbid):
    return f"/api/servers/{sid}/databases/{dbid}"


def sec(sid, dbid):
    return base(sid, dbid) + "/security"


def uniq(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:6].upper()}"


def engine_major(client, sid, dbid) -> int:
    r = client.post(base(sid, dbid) + "/execute",
                    json={"sql": "SELECT RDB$GET_CONTEXT('SYSTEM', 'ENGINE_VERSION') FROM RDB$DATABASE"})
    return int(r.json()["results"][0]["rows"][0][0].split(".")[0])


@pytest.fixture
def audit_on(client):
    from fbadmin.api.deps import get_state

    get_state().security.audit.enabled = True
    return client


@pytest.fixture
def ro_server(client, database):
    """The test database registered a second time on a read-only server entry."""
    sid, dbid = database
    path = client.get(f"/api/servers/{sid}").json()["databases"][0]["path"]
    client.post(base(sid, dbid) + "/execute",
                json={"sql": "CREATE TABLE T (ID INTEGER NOT NULL PRIMARY KEY, NAME VARCHAR(20));"
                             "INSERT INTO T VALUES (1, 'one');"})
    r = client.post("/api/servers", json={"name": "prod", "host": HOST, "port": PORT, "user": USER,
                                          "password": PASSWORD, "read_only": True})
    ro = r.json()
    assert ro["read_only"] is True
    db = client.post(f"/api/servers/{ro['id']}/databases", json={"name": "prod-db", "path": path}).json()
    yield ro["id"], db["id"]
    client.post(f"/api/servers/{ro['id']}/databases/{db['id']}/disconnect")


def test_read_only_server(client, ro_server):
    sid, dbid = ro_server
    b = base(sid, dbid)
    r = client.post(b + "/execute", json={"sql": "SELECT NAME FROM T"})
    assert r.status_code == 200 and r.json()["results"][0]["rows"] == [["one"]]
    for sql in ("INSERT INTO T VALUES (2, 'two')", "CREATE TABLE X (A INTEGER)", "SELECT 1 FROM RDB$DATABASE; DELETE FROM T",
                "GRANT SELECT ON T TO PUBLIC", "SET TRANSACTION READ WRITE"):
        r = client.post(b + "/execute", json={"sql": sql})
        assert r.status_code == 403 and r.json()["code"] == "read_only", sql
    # writes hidden in a block pass the keyword check but Firebird refuses them (read-only transaction)
    r = client.post(b + "/execute", json={"sql": "EXECUTE BLOCK AS BEGIN UPDATE T SET NAME = 'x'; END"})
    assert r.status_code == 200 and r.json()["error"] and "read-only" in r.json()["error"]["message"]
    # grid edits, drops, DDL dialogs, database creation are refused
    r = client.post(b + "/tables/T/data", json={"updates": [{"key": {"ID": 1}, "values": {"NAME": "x"}}]})
    assert r.status_code == 403
    assert client.delete(b + "/objects/tables/T").status_code == 403
    assert client.post(f"/api/servers/{sid}/databases/create", json={"name": "n", "path": "/tmp/n.fdb"}).status_code == 403
    assert client.post(sec(sid, dbid) + "/roles", json={"name": "R1"}).status_code == 403
    # reading still works
    assert client.get(b + "/tables/T/data").status_code == 200
    assert client.get(b + "/objects/tables").status_code == 200

    # Query Tool sessions are read-only too
    sess = client.post("/api/query/sessions", json={"server_id": sid, "database_id": dbid}).json()
    assert sess["options"]["read_only"] is True
    q = f"/api/query/sessions/{sess['session_id']}"
    assert client.post(q + "/execute", json={"sql": "SELECT COUNT(*) FROM T"}).status_code == 200
    r = client.post(q + "/execute", json={"sql": "UPDATE T SET NAME = 'y'"})
    assert r.status_code == 403
    r = client.put(q + "/options", json={"isolation": "snapshot", "read_only": False})
    assert r.status_code == 400
    client.delete(q)
    # still unchanged
    r = client.post(b + "/execute", json={"sql": "SELECT NAME FROM T"})
    assert r.json()["results"][0]["rows"] == [["one"]]
    # turning read-only off is a registry change and allowed
    assert client.put(f"/api/servers/{sid}", json={"read_only": False}).json()["read_only"] is False
    r = client.post(b + "/execute", json={"sql": "UPDATE T SET NAME = 'z'"})
    assert r.status_code == 200 and r.json()["error"] is None


def test_audit_log(audit_on, database):
    client = audit_on
    sid, dbid = database
    b = base(sid, dbid)
    client.post(b + "/execute", json={"sql": "CREATE TABLE AUD (ID INTEGER NOT NULL PRIMARY KEY, V VARCHAR(10));"
                                             "INSERT INTO AUD VALUES (1, 'a');"
                                             "SELECT * FROM AUD; CREATE TABLE AUD2 (A INTEGER);"})
    client.post(b + "/execute", json={"sql": "INSERT INTO AUD VALUES (1, 'dup')"})  # PK violation
    sess = client.post("/api/query/sessions", json={"server_id": sid, "database_id": dbid}).json()["session_id"]
    q = f"/api/query/sessions/{sess}"
    client.post(q + "/execute", json={"sql": "UPDATE AUD SET V = 'b'", "autocommit": False})
    client.post(q + "/commit")
    client.delete(q)
    client.post(b + "/tables/AUD/data", json={"inserts": [{"values": {"ID": 2, "V": "c"}}]})
    client.delete(b + "/objects/tables/AUD2")

    rows = [r for r in client.get("/api/admin/audit", params={"limit": 50}).json()["rows"]
            if r["category"] != "ACTION"]  # creating the test database is audited too
    stmts = [r["statement"] for r in reversed(rows)]
    assert stmts[0].startswith("CREATE TABLE AUD")
    assert stmts[1].startswith("INSERT INTO AUD VALUES (1, 'a')")
    assert not any(s.startswith("SELECT") for s in stmts)
    dup = next(r for r in rows if "dup" in r["statement"])
    assert dup["success"] is False and "PRIMARY" in dup["error"].upper()
    assert any(s.startswith("UPDATE AUD") for s in stmts) and "COMMIT" in stmts
    grid = next(r for r in rows if r["statement"].startswith("-- grid edit on AUD"))
    assert grid["row_count"] == 1 and grid["category"] == "DML"
    drop = rows[0]
    assert drop["statement"].startswith("DROP TABLE") and drop["category"] == "DDL"
    assert all(r["db_id"] == dbid and r["server_name"] == "test" and r["username"].startswith("desktop") for r in rows)
    # filters
    assert client.get("/api/admin/audit", params={"q": "'dup'"}).json()["total"] == 1
    assert client.get("/api/admin/audit", params={"success": False}).json()["total"] == 1


def test_wire_compression_and_auth_plugin(client, database):
    sid, dbid = database
    r = client.put(f"/api/servers/{sid}", json={"wire_compress": True, "auth_plugin": "Srp256"})
    assert r.status_code == 200 and r.json()["wire_compress"] and r.json()["auth_plugin"] == "Srp256"
    r = client.post(base(sid, dbid) + "/execute", json={
        "sql": "SELECT RDB$GET_CONTEXT('SYSTEM', 'WIRE_COMPRESSED'), RDB$GET_CONTEXT('SYSTEM', 'WIRE_ENCRYPTED') "
               "FROM RDB$DATABASE"})
    assert r.status_code == 200, r.text
    row = r.json()["results"][0]["rows"][0]
    assert row[0] == "TRUE" and row[1] == "TRUE"
    assert "Firebird" in client.get(f"/api/servers/{sid}/info").json()["server_version"]  # services too
    assert client.put(f"/api/servers/{sid}", json={"auth_plugin": "Bogus"}).status_code == 200
    r = client.post(base(sid, dbid) + "/connect")
    assert r.status_code == 400 and "Bogus" in r.json()["detail"]
    client.put(f"/api/servers/{sid}", json={"auth_plugin": None, "wire_compress": False})


def test_firebird_users_sql(audit_on, database):
    client = audit_on
    sid, dbid = database
    s = sec(sid, dbid)
    name = uniq("FBA_U")
    body = {"name": name.lower(), "password": "pw12345", "first_name": "Ann", "last_name": "Lee",
            "plugin": "Srp", "tags": {"dept": "sales", "floor": "3"}, "admin": True, "db_admin": True,
            "description": "test user"}
    r = client.post(s + "/users", json=body, params={"preview": True})
    assert r.status_code == 201 and r.json()["executed"] is False
    sql = r.json()["sql"]
    assert sql[0] == (f"CREATE USER {name} PASSWORD 'pw12345' FIRSTNAME 'Ann' LASTNAME 'Lee' ACTIVE GRANT ADMIN ROLE "
                      "USING PLUGIN Srp TAGS (dept = 'sales', floor = '3')")
    assert sql[-1] == f"GRANT RDB$ADMIN TO USER {name}"
    try:
        r = client.post(s + "/users", json=body)
        assert r.status_code == 201, r.text
        data = client.get(s + "/users").json()
        assert "Srp" in data["plugins"] and data["security_database"]
        u = next(x for x in data["users"] if x["name"] == name)
        assert u["plugin"] == "Srp" and u["tags"] == {"DEPT": "sales", "FLOOR": "3"} or \
            u["tags"] == {"dept": "sales", "floor": "3"}
        assert u["admin"] and u["db_admin"] and u["first_name"] == "Ann" and u["description"] == "test user"
        r = client.put(f"{s}/users/{name}", json={"active": False, "admin": False, "db_admin": False,
                                                    "drop_tags": ["floor"], "tags": {"dept": "it"}, "plugin": "Srp"})
        assert r.status_code == 200, r.text
        u = next(x for x in client.get(s + "/users").json()["users"] if x["name"] == name)
        assert not u["active"] and not u["admin"] and not u["db_admin"]
        assert list(u["tags"].values()) == ["it"]
    finally:
        r = client.delete(f"{s}/users/{name}", params={"plugin": "Srp"})
    assert r.status_code == 200
    assert all(x["name"] != name for x in client.get(s + "/users").json()["users"])
    audit = client.get("/api/admin/audit", params={"q": name}).json()["rows"]
    assert any(r["statement"].startswith("DROP USER") for r in audit)
    assert not any("pw12345" in r["statement"] for r in audit)  # passwords are redacted
    assert any("PASSWORD '***'" in r["statement"] for r in audit)


def test_firebird_users_services_api(client, server):
    sid = server["id"]
    name = uniq("FBA_S")
    r = client.post(f"/api/servers/{sid}/fbusers", json={"name": name, "password": "pw1", "first_name": "Svc",
                                                         "admin": True})
    assert r.status_code == 201, r.text
    try:
        users = client.get(f"/api/servers/{sid}/fbusers").json()["users"]
        u = next(x for x in users if x["user_name"] == name)
        assert u["first_name"] == "Svc" and u["admin"] is True
        r = client.put(f"/api/servers/{sid}/fbusers/{name}", json={"name": name, "last_name": "Account",
                                                                   "admin": False})
        assert r.status_code == 200
        u = next(x for x in client.get(f"/api/servers/{sid}/fbusers").json()["users"] if x["user_name"] == name)
        assert u["last_name"] == "Account" and u["admin"] is False
    finally:
        assert client.delete(f"/api/servers/{sid}/fbusers/{name}").status_code == 200
    assert all(x["user_name"] != name for x in client.get(f"/api/servers/{sid}/fbusers").json()["users"])


def test_mappings(client, database):
    sid, dbid = database
    s = sec(sid, dbid)
    fb5 = engine_major(client, sid, dbid) >= 5
    r = client.post(s + "/mappings", json={"name": "m_plugin", "using": "plugin", "plugin": "Srp",
                                           "from_type": "USER", "from_name": "ext_user", "to_type": "USER",
                                           "to_name": "INT_USER", "description": "one to one" if fb5 else None})
    assert r.status_code == 201, r.text
    assert r.json()["sql"][0] == "CREATE MAPPING M_PLUGIN USING PLUGIN Srp FROM USER EXT_USER TO USER INT_USER"
    r = client.post(s + "/mappings", json={"name": "M_ANY", "using": "any_plugin", "from_type": "USER",
                                           "to_type": "ROLE", "to_name": "RDB$ADMIN"})
    assert r.status_code == 201, r.text
    r = client.post(s + "/mappings", json={"name": "M_STAR", "using": "all", "from_type": "USER", "to_type": "USER"})
    assert r.status_code == 201, r.text
    r = client.post(s + "/mappings", json={"name": "M_WIDE", "using": "serverwide", "from_type": "USER",
                                           "to_type": "USER"})
    assert r.status_code == 201, r.text
    data = client.get(s + "/mappings").json()
    local = {m["name"]: m for m in data["local"]}
    assert local["M_PLUGIN"]["using"] == "plugin" and local["M_PLUGIN"]["plugin"] == "SRP"
    assert local["M_PLUGIN"]["from_name"] == "EXT_USER" and local["M_PLUGIN"]["to_name"] == "INT_USER"
    assert local["M_PLUGIN"]["description"] == ("one to one" if fb5 else None)
    assert local["M_ANY"]["using"] == "any_plugin" and local["M_ANY"]["to_type"] == "ROLE"
    assert local["M_STAR"]["using"] == "all" and local["M_WIDE"]["using"] == "serverwide"
    assert local["M_ANY"]["from_name"] == "*"
    r = client.put(s + "/mappings/M_PLUGIN", json={"name": "M_PLUGIN", "using": "plugin", "plugin": "Srp",
                                                   "from_type": "USER", "from_name": "other", "to_type": "USER"})
    assert r.status_code == 200, r.text
    assert next(m for m in client.get(s + "/mappings").json()["local"] if m["name"] == "M_PLUGIN")["from_name"] == "OTHER"
    for n in ("M_PLUGIN", "M_ANY", "M_STAR", "M_WIDE"):
        assert client.delete(f"{s}/mappings/{n}").status_code == 200
    assert client.get(s + "/mappings").json()["local"] == []

    # global mapping (security database)
    g = uniq("GM")
    r = client.post(s + "/mappings", json={"name": g, "is_global": True, "using": "plugin", "plugin": "Srp",
                                           "from_type": "USER", "from_name": "someone", "to_type": "USER"})
    assert r.status_code == 201, r.text
    try:
        assert any(m["name"] == g and m["global"] for m in client.get(s + "/mappings").json()["global"])
    finally:
        assert client.delete(f"{s}/mappings/{g}", params={"is_global": True}).status_code == 200
    with pytest.raises(ValueError):
        from fbadmin.security.fbsql import mapping_sql

        mapping_sql("CREATE", "X", using="nope")


def test_system_privileges(client, database):
    sid, dbid = database
    s = sec(sid, dbid)
    major = engine_major(client, sid, dbid)
    data = client.get(s + "/roles").json()
    if major < 4:
        assert data["supported"] is False and data["privileges"] == []
        r = client.post(s + "/roles", json={"name": "R_FB3", "privileges": ["USER_MANAGEMENT"]})
        assert r.status_code == 400
        r = client.post(s + "/roles", json={"name": "R_FB3"})
        assert r.status_code == 201
        return
    assert data["supported"] and "USER_MANAGEMENT" in data["privileges"]
    assert ("PROFILE_ANY_ATTACHMENT" in data["privileges"]) == (major >= 5)
    admin = next(r for r in data["roles"] if r["name"] == "RDB$ADMIN")
    assert set(admin["privileges"]) == set(data["privileges"])
    r = client.post(s + "/roles", json={"name": "r_ops", "privileges": ["USER_MANAGEMENT", "CREATE_DATABASE",
                                                                       "MONITOR_ANY_ATTACHMENT"]})
    assert r.status_code == 201, r.text
    assert r.json()["sql"][0] == ("CREATE ROLE R_OPS SET SYSTEM PRIVILEGES TO USER_MANAGEMENT, CREATE_DATABASE, "
                                  "MONITOR_ANY_ATTACHMENT")
    role = next(x for x in client.get(s + "/roles").json()["roles"] if x["name"] == "R_OPS")
    assert set(role["privileges"]) == {"USER_MANAGEMENT", "CREATE_DATABASE", "MONITOR_ANY_ATTACHMENT"}
    r = client.put(s + "/roles/R_OPS", json={"privileges": ["READ_RAW_PAGES"]})
    assert r.status_code == 200
    role = next(x for x in client.get(s + "/roles").json()["roles"] if x["name"] == "R_OPS")
    assert role["privileges"] == ["READ_RAW_PAGES"]
    r = client.post(s + "/roles/R_OPS/members", json={"user": "sysdba", "default": True})
    assert r.status_code == 200, r.text
    role = next(x for x in client.get(s + "/roles").json()["roles"] if x["name"] == "R_OPS")
    assert role["members"][0]["user"] == "SYSDBA"
    client.post(s + "/roles/R_OPS/members", json={"user": "SYSDBA", "grant": False})
    r = client.put(s + "/roles/R_OPS", json={"privileges": []})
    assert r.json()["sql"] == ["ALTER ROLE R_OPS DROP SYSTEM PRIVILEGES"]
    assert client.post(s + "/roles", json={"name": "R_BAD", "privileges": ["FLY"]}).status_code == 400


def test_decode_privileges():
    from fbadmin.security.fbsql import decode_privileges, encode_privileges

    assert decode_privileges("0200000000000000") == ["USER_MANAGEMENT"]
    assert decode_privileges("0002000800000000") == ["CREATE_DATABASE", "PROFILE_ANY_ATTACHMENT"]
    assert decode_privileges(encode_privileges(["READ_RAW_PAGES", "DROP_DATABASE"])) == ["READ_RAW_PAGES",
                                                                                          "DROP_DATABASE"]
    assert decode_privileges("FFFFFFFFFFFFFFFF", 4)[-1] == "REPLICATE_INTO_DATABASE"


def test_server_mode_isolation_and_audit(tmp_path, monkeypatch):
    from .test_security import ADMIN_PW, _client, login

    with _client(tmp_path, monkeypatch, SERVER_MODE="1", ADMIN_USERNAME="root", ADMIN_PASSWORD=ADMIN_PW) as c:
        login(c, "root", ADMIN_PW)
        c.post("/api/admin/users", json={"username": "bob", "password": "bob-password"})
        srv = c.post("/api/servers", json={"name": "shared-fb", "host": HOST, "port": PORT, "user": USER,
                                           "shared": True, "password": PASSWORD}).json()
        path = f"/var/lib/firebird/data/fbadmin_test_{uuid.uuid4().hex[:8]}.fdb"
        r = c.post(f"/api/servers/{srv['id']}/databases/create", json={"name": "t", "path": path})
        assert r.status_code == 201, r.text
        dbid = r.json()["id"]
        try:
            sess = c.post("/api/query/sessions", json={"server_id": srv["id"], "database_id": dbid}).json()
            r = c.post(f"/api/query/sessions/{sess['session_id']}/execute",
                       json={"sql": "CREATE TABLE BYROOT (A INT)"})
            assert r.json()["error"] is None
            c.post("/api/auth/logout")
            c.headers.pop("X-CSRF-Token")

            login(c, "bob", "bob-password")
            # bob sees the shared server, but not root's password, query session or connections
            assert c.get(f"/api/query/sessions/{sess['session_id']}/options").status_code == 404
            r = c.post(base(srv["id"], dbid) + "/connect")
            assert r.status_code == 401 and r.json()["code"] == "password_required"
            assert c.post(f"/api/servers/{srv['id']}/connect", json={"password": PASSWORD}).status_code == 200
            r = c.post(base(srv["id"], dbid) + "/execute", json={"sql": "INSERT INTO BYROOT VALUES (1)"})
            assert r.json()["error"] is None
            assert c.get("/api/jobs").json() == []
            assert c.get("/api/admin/audit").status_code == 403
            c.post("/api/auth/logout")
            c.headers.pop("X-CSRF-Token")

            login(c, "root", ADMIN_PW)
            rows = c.get("/api/admin/audit").json()["rows"]
            who = {r["statement"]: r["username"] for r in rows}
            assert who["CREATE TABLE BYROOT (A INT)"] == "root"
            assert who["INSERT INTO BYROOT VALUES (1)"] == "bob"
        finally:
            c.post(f"/api/servers/{srv['id']}/databases/{dbid}/drop")
