from pathlib import Path

from .conftest import requires_server

pytestmark = requires_server
SCHEMA = (Path(__file__).parent / "sample_schema.sql").read_text()


def base(sid, dbid):
    return f"/api/servers/{sid}/databases/{dbid}"


def load_schema(client, sid, dbid):
    r = client.post(base(sid, dbid) + "/execute", json={"sql": SCHEMA})
    assert r.status_code == 200, r.text
    assert r.json()["error"] is None, r.json()["error"]


def test_health(client):
    assert client.get("/api/health").json()["status"] == "ok"


def test_server_registry(client, server):
    servers = client.get("/api/servers").json()
    assert len(servers) == 1 and "password" not in servers[0]
    r = client.put(f"/api/servers/{server['id']}", json={"name": "renamed"})
    assert r.json()["name"] == "renamed"
    info = client.get(f"/api/servers/{server['id']}/info").json()
    assert "Firebird" in info["server_version"]


def test_password_required(client):
    r = client.post("/api/servers", json={"name": "x", "host": "127.0.0.1", "save_password": False})
    sid = r.json()["id"]
    r = client.get(f"/api/servers/{sid}/info")
    assert r.status_code == 401 and r.json()["code"] == "password_required"


def test_objects_and_ddl(client, database):
    sid, dbid = database
    load_schema(client, sid, dbid)
    b = base(sid, dbid)
    info = client.get(b + "/info").json()
    assert info["counts"]["tables"] == 2
    assert info["counts"]["procedures"] == 1

    names = {t["name"] for t in client.get(b + "/objects/tables").json()}
    assert names == {"CUSTOMER", "ORDERS"}

    cust = client.get(b + "/objects/tables/CUSTOMER").json()
    cols = {c["name"]: c for c in cust["columns"]}
    assert cols["ID"]["identity"] == "BY DEFAULT" and cols["ID"]["primary_key"]
    assert cols["NAME"]["domain"] == "D_NAME"
    assert cols["EMAIL"]["description"] == "Contact e-mail"
    assert "CREATE TABLE CUSTOMER" in cust["ddl"]
    assert '"lower case" INTEGER' in cust["ddl"]

    orders = client.get(b + "/objects/tables/ORDERS").json()
    fk = next(c for c in orders["constraints"] if c["type"] == "FOREIGN KEY")
    assert fk["ref_table"] == "CUSTOMER" and fk["on_delete"] == "CASCADE"
    assert any(c["type"] == "CHECK" for c in orders["constraints"])
    assert "COMPUTED BY (AMOUNT * QTY)" in orders["ddl"]
    assert "DESCENDING INDEX IX_ORDERS_AMOUNT" in orders["ddl"]
    assert orders["triggers"][0]["event"] == "BEFORE INSERT"

    proc = client.get(b + "/objects/procedures/SP_ORDERS_FOR").json()
    assert [p["direction"] for p in proc["parameters"]] == ["IN", "IN", "OUT", "OUT"]
    assert "TYPE OF COLUMN ORDERS.AMOUNT" in proc["ddl"]

    seq = client.get(b + "/objects/sequences/SEQ_ORDERS").json()
    assert seq["properties"]["Increment"] == 5

    view = client.get(b + "/objects/views/V_CUSTOMER_TOTALS").json()
    assert {d["name"] for d in view["dependencies"]["depends_on"]} >= {"CUSTOMER", "ORDERS"}

    for otype in ("functions", "packages", "triggers", "domains", "exceptions", "indices", "roles", "users"):
        items = client.get(b + f"/objects/{otype}").json()
        assert items, otype
        detail = client.get(b + f"/objects/{otype}/{items[0]['name']}")
        assert detail.status_code == 200, (otype, detail.text)
        assert detail.json()["ddl"]


def test_ddl_roundtrip(client, server, database):
    """Extract DDL from a database, run it on an empty one and compare the extracted DDL again."""
    sid, src = database
    load_schema(client, sid, src)
    b = base(sid, src)
    order = ["domains", "sequences", "exceptions", "roles", "tables", "functions", "procedures", "packages", "views"]
    ddl = {}
    for otype in order:
        for item in client.get(b + f"/objects/{otype}").json():
            ddl[(otype, item["name"])] = client.get(b + f"/objects/{otype}/{item['name']}").json()["ddl"]

    import uuid

    from .conftest import DB_DIR

    r = client.post(f"/api/servers/{sid}/databases/create",
                    json={"name": "copy", "path": f"{DB_DIR}/fbadmin_copy_{uuid.uuid4().hex[:8]}.fdb"})
    dst = r.json()["id"]
    try:
        script = "\n".join(ddl[k] for k in ddl)
        # FKs reference tables created later in the script: create tables first without them is
        # what a full extract will do; here tables are ordered so CUSTOMER precedes ORDERS.
        res = client.post(base(sid, dst) + "/execute", json={"sql": script}).json()
        assert res["error"] is None, res["error"]
        for (otype, name), text in ddl.items():
            again = client.get(base(sid, dst) + f"/objects/{otype}/{name}").json()["ddl"]
            assert again == text, f"{otype} {name} differs:\n{text}\n---\n{again}"
    finally:
        client.post(f"/api/servers/{sid}/databases/{dst}/drop")


def test_query_session(client, database):
    sid, dbid = database
    load_schema(client, sid, dbid)
    s = client.post("/api/query/sessions", json={"server_id": sid, "database_id": dbid}).json()["session_id"]
    q = f"/api/query/sessions/{s}"

    r = client.post(q + "/execute", json={"sql": "insert into customer (name) values ('Ann'); "
                                                 "select id, name, balance from customer;", "with_plan": True}).json()
    assert r["error"] is None
    assert r["results"][0]["row_count"] == 1
    sel = r["results"][1]
    assert sel["columns"][1]["name"] == "NAME" and sel["rows"][0][1] == "Ann"
    assert sel["rows"][0][2] == "0.00"
    assert "PLAN" in (sel["plan"] or "")

    # manual transaction + rollback
    r = client.post(q + "/execute", json={"sql": "delete from customer", "autocommit": False}).json()
    assert r["transaction_open"]
    client.post(q + "/rollback")
    r = client.post(q + "/execute", json={"sql": "select count(*) from customer"}).json()
    assert r["results"][0]["rows"][0][0] == 1

    # errors stop the script and report the line
    r = client.post(q + "/execute", json={"sql": "select 1 from rdb$database;\nselect nope from customer;"}).json()
    assert r["error"]["line"] == 2 and len(r["results"]) == 2

    # truncation
    r = client.post(q + "/execute", json={"sql": "select * from rdb$fields", "max_rows": 5}).json()
    assert r["results"][0]["truncated"] and r["results"][0]["row_count"] == 5

    assert client.post(q + "/explain", json={"sql": "select * from customer where id = 1"}).json()["plan"]
    assert client.delete(q).status_code == 204


def test_table_data_and_edit(client, database):
    sid, dbid = database
    load_schema(client, sid, dbid)
    b = base(sid, dbid)
    r = client.post(b + "/tables/CUSTOMER/data", json={"inserts": [{"values": {"NAME": "A"}}, {"values": {"NAME": "B"}}]})
    assert r.json()["statements"] == 2
    data = client.get(b + "/tables/CUSTOMER/data?limit=1").json()
    assert data["has_more"] and data["primary_key"] == ["ID"] and len(data["rows"]) == 1
    first_id = data["rows"][0][0]
    client.post(b + "/tables/CUSTOMER/data", json={"updates": [{"key": {"ID": first_id}, "values": {"EMAIL": "a@x"}}]})
    data = client.get(b + "/tables/CUSTOMER/data", params={"where": "EMAIL = 'a@x'"}).json()
    assert len(data["rows"]) == 1
    client.post(b + "/tables/CUSTOMER/data", json={"deletes": [{"key": {"ID": first_id}}]})
    assert client.get(b + "/tables/CUSTOMER/count").json()["count"] == 1
    r = client.post(b + "/tables/CUSTOMER/data", json={"deletes": [{"key": {"ID": 999999}}]})
    assert r.status_code == 400


def test_scripts_and_drop(client, database):
    sid, dbid = database
    load_schema(client, sid, dbid)
    b = base(sid, dbid)
    for kind in ("select", "insert", "update", "delete", "create", "drop"):
        r = client.get(b + "/objects/tables/CUSTOMER/script", params={"kind": kind})
        assert r.status_code == 200 and r.json()["sql"], kind
    assert "EXECUTE PROCEDURE" in client.get(
        b + "/objects/procedures/SP_ORDERS_FOR/script", params={"kind": "execute"}).json()["sql"]
    assert client.delete(b + "/objects/views/V_CUSTOMER_TOTALS").status_code == 200
    assert client.get(b + "/objects/views/V_CUSTOMER_TOTALS").status_code == 404


def test_monitor(client, database):
    sid, dbid = database
    m = client.get(base(sid, dbid) + "/monitor").json()
    assert any(a["is_self"] for a in m["attachments"])
    assert m["database"]["page_size"]


def test_jobs(client, server, database):
    import time

    sid, dbid = database
    job = client.post(f"/api/servers/{sid}/databases/{dbid}/statistics?header_only=true").json()
    for _ in range(50):
        j = client.get(f"/api/jobs/{job['id']}").json()
        if j["status"] != "running":
            break
        time.sleep(0.1)
    assert j["status"] == "finished", j
    assert any("Database header" in line or "Attributes" in line or "Page size" in line for line in j["log"])
