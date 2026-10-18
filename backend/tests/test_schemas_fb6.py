"""Firebird 6 SQL schemas (milestone 0.9). Skipped on servers without schemas."""

import pytest

from .conftest import requires_server

pytestmark = requires_server

SETUP = """
CREATE SCHEMA APP;
CREATE TABLE APP.T1 (ID INTEGER NOT NULL PRIMARY KEY, NAME VARCHAR(20));
CREATE TABLE PUBLIC.T1 (CODE VARCHAR(5));
CREATE SEQUENCE APP.S1;
CREATE DOMAIN APP.D_POS AS INTEGER CHECK (VALUE > 0);
CREATE VIEW APP.V1 AS SELECT ID, NAME FROM APP.T1;
INSERT INTO APP.T1 VALUES (1, 'app row');
INSERT INTO PUBLIC.T1 VALUES ('pub');
"""


@pytest.fixture
def fb6(client, database):
    sid, dbid = database
    base = f"/api/servers/{sid}/databases/{dbid}"
    if not client.get(base + "/schemas").json()["supported"]:
        pytest.skip("server has no SQL schemas (Firebird < 6)")
    r = client.post(base + "/execute", json={"sql": SETUP}).json()
    assert r["error"] is None, r["error"]
    return client, base, sid, dbid


def h(schema):
    return {"X-Fbadmin-Schema": schema}


def test_schema_listing_and_scoping(fb6):
    client, base, _, _ = fb6
    names = [s["name"] for s in client.get(base + "/schemas").json()["schemas"]]
    assert {"APP", "PUBLIC", "SYSTEM"} <= set(names)
    app_tables = {t["name"] for t in client.get(base + "/objects/tables", headers=h("APP")).json()}
    pub_tables = {t["name"] for t in client.get(base + "/objects/tables", headers=h("PUBLIC")).json()}
    assert app_tables == {"T1"} and pub_tables == {"T1"}
    app_t1 = client.get(base + "/objects/tables/T1", headers=h("APP")).json()
    pub_t1 = client.get(base + "/objects/tables/T1", headers=h("PUBLIC")).json()
    assert [c["name"] for c in app_t1["columns"]] == ["ID", "NAME"]
    assert [c["name"] for c in pub_t1["columns"]] == ["CODE"]
    assert app_t1["schema"] == "APP" and app_t1["ddl"].startswith('SET SEARCH_PATH TO APP, SYSTEM;')
    assert not pub_t1["ddl"].startswith("SET SEARCH_PATH")
    assert {d["name"] for d in client.get(base + "/objects/domains", headers=h("APP")).json()} == {"D_POS"}
    assert client.get(base + "/objects/domains", headers=h("PUBLIC")).json() == []
    assert [s["name"] for s in client.get(base + "/objects/sequences", headers=h("APP")).json()] == ["S1"]
    deps = client.get(base + "/objects/views/V1", headers=h("APP")).json()["dependencies"]["depends_on"]
    assert any(d["name"] == "T1" for d in deps)
    counts = client.get(base + "/info", headers=h("APP")).json()["counts"]
    assert counts["tables"] == 1 and counts["views"] == 1
    sys_tables = client.get(base + "/objects/system_tables", headers=h("SYSTEM")).json()
    assert any(t["name"] == "RDB$RELATIONS" for t in sys_tables)


def test_data_drop_and_ddl_roundtrip_in_schema(fb6):
    client, base, _, _ = fb6
    assert client.get(base + "/tables/T1/data", headers=h("APP")).json()["rows"] == [[1, "app row"]]
    assert [r[0] for r in client.get(base + "/tables/T1/data", headers=h("PUBLIC")).json()["rows"]] == ["pub"]
    ddl = client.get(base + "/objects/views/V1", headers=h("APP")).json()["ddl"]
    assert client.delete(base + "/objects/views/V1", headers=h("APP")).status_code == 200
    assert client.get(base + "/objects/views/V1", headers=h("APP")).status_code == 404
    # re-run the extracted DDL (it carries SET SEARCH_PATH) without any schema context
    assert client.post(base + "/execute", json={"sql": ddl}).json()["error"] is None
    assert client.get(base + "/objects/views/V1", headers=h("APP")).json()["ddl"] == ddl
    # PUBLIC.T1 untouched
    assert client.get(base + "/tables/T1/count", headers=h("PUBLIC")).json()["count"] == 1


def test_query_tool_search_path(fb6):
    client, base, sid, dbid = fb6
    r = client.post("/api/query/sessions", json={"server_id": sid, "database_id": dbid}, headers=h("APP")).json()
    q = f"/api/query/sessions/{r['session_id']}"
    assert r["options"]["search_path"].startswith('"APP"')
    assert client.post(q + "/execute", json={"sql": "select name from t1"}).json()["results"][0]["rows"] == [["app row"]]
    opts = client.put(q + "/options", json={"search_schema": "PUBLIC"}).json()
    assert opts["search_path"].startswith('"PUBLIC"')
    assert client.post(q + "/execute", json={"sql": "select code from t1"}).json()["results"][0]["rows"] == [["pub"]]
    client.delete(q)


MORE = """
CREATE TABLE APP.T2 (ID INTEGER NOT NULL PRIMARY KEY, T1_ID INTEGER REFERENCES APP.T1 (ID), QTY APP.D_POS);
CREATE INDEX IX_T2_QTY ON APP.T2 (QTY);
SET TERM ^ ;
CREATE PROCEDURE APP.P_COUNT RETURNS (N INTEGER) AS
BEGIN
  SELECT COUNT(*) FROM T2 INTO :N;
  SUSPEND;
END^
SET TERM ; ^
CREATE ROLE R_APP;
GRANT SELECT ON TABLE APP.T1 TO ROLE R_APP;
COMMENT ON TABLE APP.T2 IS 'detail rows';
"""


def _strip_header(sql: str) -> str:
    return "\n".join(ln for ln in sql.splitlines() if not ln.startswith("/* DDL extracted") and "CREATE DATABASE" not in ln)


def test_extract_whole_database_with_schemas(fb6):
    client, base, sid, _ = fb6
    assert client.post(base + "/execute", json={"sql": MORE}).json()["error"] is None
    ex = client.get(base + "/schema/extract").json()
    assert "sql" in ex, ex
    sql = ex["sql"]
    assert "CREATE SCHEMA APP;" in sql and "SET SEARCH_PATH TO APP, SYSTEM;" in sql
    assert sql.index("Schema PUBLIC") < sql.index("Schema APP")
    assert sql.count("CREATE ROLE R_APP") == 1
    # replay into an empty database: the same DDL comes back out
    import uuid

    from .conftest import DB_DIR
    path = f"{DB_DIR}/fbadmin_s6_{uuid.uuid4().hex[:8]}.fdb"
    r = client.post(f"/api/servers/{sid}/databases/create", json={"name": "s6copy", "path": path})
    assert r.status_code == 201, r.text
    copy = f"/api/servers/{sid}/databases/{r.json()['id']}"
    try:
        res = client.post(copy + "/execute", json={"sql": sql}).json()
        assert res["error"] is None, res["error"]
        again = client.get(copy + "/schema/extract").json()["sql"]
        assert _strip_header(again) == _strip_header(sql)
    finally:
        client.post(copy + "/disconnect")
        client.post(copy + "/drop")
