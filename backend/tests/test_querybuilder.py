"""Visual query builder metadata endpoints (milestone 2.0)."""

from __future__ import annotations

import pytest

from .conftest import requires_server

pytestmark = requires_server

SETUP = """
CREATE TABLE CUSTOMER (ID INTEGER NOT NULL PRIMARY KEY, NAME VARCHAR(50), CITY VARCHAR(30));
CREATE TABLE ORDERS (ID INTEGER NOT NULL PRIMARY KEY, CUSTOMER_ID INTEGER NOT NULL REFERENCES CUSTOMER (ID),
  AMOUNT NUMERIC(12,2), "POSITION" INTEGER);
CREATE VIEW V_BIG AS SELECT ID, AMOUNT FROM ORDERS WHERE AMOUNT > 100;
"""


@pytest.fixture
def qb(client, database):
    sid, dbid = database
    base = f"/api/servers/{sid}/databases/{dbid}"
    r = client.post(base + "/execute", json={"sql": SETUP}).json()
    assert r["error"] is None, r["error"]
    return client, base + "/query-builder", base


def test_relations_lists_tables_and_views(qb):
    client, qbase, _ = qb
    r = client.get(qbase + "/relations")
    assert r.status_code == 200, r.text
    d = r.json()
    assert [(x["name"], x["kind"]) for x in d["relations"]] == [("CUSTOMER", "table"), ("ORDERS", "table"), ("V_BIG", "view")]
    assert d["engine_major"] >= 3
    assert "POSITION" in d["reserved"] and "SELECT" in d["reserved"]
    if not d["schemas_supported"]:
        assert d["schema"] is None and d["schemas"] == []


def test_relation_columns_and_foreign_keys(qb):
    client, qbase, _ = qb
    d = client.get(qbase + "/relations/ORDERS").json()
    assert [c["name"] for c in d["columns"]] == ["ID", "CUSTOMER_ID", "AMOUNT", "POSITION"]
    by = {c["name"]: c for c in d["columns"]}
    assert by["ID"]["pk"] and by["CUSTOMER_ID"]["fk"] and not by["AMOUNT"]["fk"]
    assert by["AMOUNT"]["type"] == "NUMERIC(12,2)"
    assert [(k["columns"], k["ref_table"], k["ref_columns"]) for k in d["foreign_keys"]] == [(["CUSTOMER_ID"], "CUSTOMER", ["ID"])]
    v = client.get(qbase + "/relations/V_BIG").json()
    assert [c["name"] for c in v["columns"]] == ["ID", "AMOUNT"] and v["foreign_keys"] == []
    assert client.get(qbase + "/relations/NOPE").status_code == 404


def test_builder_sql_runs(qb):
    """The kind of statement the builder generates (aliases, join, aggregate, quoted reserved word)."""
    client, _, base = qb
    sql = """INSERT INTO CUSTOMER VALUES (1, 'Ann', 'Rome');
INSERT INTO ORDERS VALUES (1, 1, 150, 1);
INSERT INTO ORDERS VALUES (2, 1, 50, 2);
SELECT C.NAME, COUNT(O.ID) AS N, SUM(O.AMOUNT) AS TOTAL, MAX(O."POSITION") AS P
FROM CUSTOMER C
LEFT JOIN ORDERS O ON O.CUSTOMER_ID = C.ID
WHERE C.CITY = 'Rome'
GROUP BY C.NAME
HAVING SUM(O.AMOUNT) > 10
ORDER BY C.NAME ASC
OFFSET 0 ROWS FETCH NEXT 10 ROWS ONLY"""
    r = client.post(base + "/execute", json={"sql": sql}).json()
    assert r["error"] is None, r["error"]
    rows = r["results"][-1]["rows"]
    assert len(rows) == 1 and rows[0][:2] == ["Ann", 2] and float(rows[0][2]) == 200 and rows[0][3] == 2


def test_fb6_schema_scoping(client, database):
    sid, dbid = database
    base = f"/api/servers/{sid}/databases/{dbid}"
    if not client.get(base + "/schemas").json()["supported"]:
        pytest.skip("server has no SQL schemas (Firebird < 6)")
    r = client.post(base + "/execute", json={"sql": "CREATE SCHEMA APP; CREATE TABLE APP.T1 (ID INTEGER, NAME VARCHAR(9));"
                                                    " CREATE TABLE PUBLIC.T2 (CODE INTEGER);"}).json()
    assert r["error"] is None, r["error"]
    qbase = base + "/query-builder"
    app = client.get(qbase + "/relations", params={"schema": "APP"}).json()
    assert app["schema"] == "APP" and [x["name"] for x in app["relations"]] == ["T1"]
    assert {"APP", "PUBLIC"} <= set(app["schemas"])
    pub = client.get(qbase + "/relations", params={"schema": "PUBLIC"}).json()
    assert [x["name"] for x in pub["relations"]] == ["T2"]
    default = client.get(qbase + "/relations").json()  # no schema: PUBLIC, not all schemas mixed
    assert default["schema"] == "PUBLIC" and [x["name"] for x in default["relations"]] == ["T2"]
    assert [c["name"] for c in client.get(qbase + "/relations/T1", params={"schema": "APP"}).json()["columns"]] == ["ID", "NAME"]
    assert client.get(qbase + "/relations/T1", params={"schema": "PUBLIC"}).status_code == 404
    assert client.get(qbase + "/relations", params={"schema": 'bad"name'}).status_code == 400
