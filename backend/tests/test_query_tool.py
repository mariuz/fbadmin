"""Milestone 0.2 Query Tool features against a live server."""

import threading
import time
from pathlib import Path

import pytest

from .conftest import requires_server

pytestmark = requires_server
SCHEMA = (Path(__file__).parent / "sample_schema.sql").read_text()
SLOW = "SELECT COUNT(*) FROM RDB$FIELDS a, RDB$FIELDS b, RDB$FIELDS c"


@pytest.fixture
def qs(client, database):
    sid, dbid = database
    r = client.post(f"/api/servers/{sid}/databases/{dbid}/execute", json={"sql": SCHEMA})
    assert r.json()["error"] is None
    r = client.post("/api/query/sessions", json={"server_id": sid, "database_id": dbid})
    assert r.status_code == 201
    data = r.json()
    yield client, f"/api/query/sessions/{data['session_id']}", data["options"], (sid, dbid)
    client.delete(f"/api/query/sessions/{data['session_id']}")


def test_params(qs):
    client, q, _, _ = qs
    assert client.post("/api/query/params", json={"sql": "select :a, ? from rdb$database"}).json()["params"] == ["A", "$1"]
    r = client.post(q + "/execute", json={
        "sql": "insert into customer (name, email) values (:name, ?); "
               "select name, email from customer where name = :name",
        "params": {"NAME": "Bob", "$1": "bob@x"},
    }).json()
    assert r["error"] is None, r["error"]
    assert r["results"][1]["rows"] == [["Bob", "bob@x"]]
    r = client.post(q + "/execute", json={"sql": "select :missing from rdb$database"}).json()
    assert "MISSING" in r["error"]["message"]
    # NULL parameter
    r = client.post(q + "/execute", json={"sql": "select coalesce(cast(:v as integer), -1) from rdb$database",
                                          "params": {"V": None}}).json()
    assert r["results"][0]["rows"] == [[-1]]


def test_error_position(qs):
    client, q, _, _ = qs
    r = client.post(q + "/execute", json={"sql": "select 1 from rdb$database;\n  select nope\n  from customer;"}).json()
    pos = r["error"]["position"]
    assert pos["line"] == 2 and pos["column"] == 10, r["error"]


def test_fetch_more(qs):
    client, q, _, _ = qs
    r = client.post(q + "/execute", json={"sql": "select rdb$field_name from rdb$fields order by 1", "max_rows": 10}).json()
    first = r["results"][0]
    assert first["truncated"] and r["cursor_open"] and not r["transaction_open"]
    seen = [x[0] for x in first["rows"]]
    while True:
        f = client.post(q + "/fetch", json={"rows": 50}).json()
        seen += [x[0] for x in f["rows"]]
        if not f["has_more"]:
            break
    total = client.post(q + "/execute", json={"sql": "select count(*) from rdb$fields"}).json()["results"][0]["rows"][0][0]
    assert len(seen) == total and seen == sorted(seen)
    assert client.post(q + "/fetch", json={"rows": 5}).status_code == 400


def test_cancel(qs):
    client, q, _, _ = qs
    out = {}
    t = threading.Thread(target=lambda: out.update(r=client.post(q + "/execute", json={"sql": SLOW}).json()))
    t.start()
    cancelled = 0
    for _ in range(50):
        time.sleep(0.2)
        cancelled = client.post(q + "/cancel").json()["cancelled"]
        if cancelled:
            break
    t.join(30)
    assert cancelled == 1
    assert "cancel" in out["r"]["error"]["message"].lower()
    # session still usable
    assert client.post(q + "/execute", json={"sql": "select 1 from rdb$database"}).json()["error"] is None


def test_transaction_options(qs):
    client, q, opts, (sid, dbid) = qs
    assert opts["isolation"] == "read_committed" and opts["attachment_id"]
    r = client.put(q + "/options", json={"isolation": "snapshot", "read_only": True}).json()
    assert r["isolation"] == "snapshot" and r["read_only"]
    r = client.post(q + "/execute", json={"sql": "insert into customer (name) values ('x')"}).json()
    assert "read-only" in r["error"]["message"].lower()
    # snapshot: a manual transaction does not see rows committed later by others
    client.put(q + "/options", json={"isolation": "snapshot"})
    r = client.post(q + "/execute", json={"sql": "select count(*) from customer", "autocommit": False}).json()
    before = r["results"][0]["rows"][0][0]
    client.post(f"/api/servers/{sid}/databases/{dbid}/execute", json={"sql": "insert into customer (name) values ('y')"})
    r = client.post(q + "/execute", json={"sql": "select count(*) from customer", "autocommit": False}).json()
    assert r["results"][0]["rows"][0][0] == before
    assert client.put(q + "/options", json={"isolation": "read_committed"}).status_code == 400  # tx open
    client.post(q + "/rollback")
    r = client.post(q + "/execute", json={"sql": "select count(*) from customer"}).json()
    assert r["results"][0]["rows"][0][0] == before + 1


def test_statement_timeout(qs):
    client, q, opts, _ = qs
    r = client.put(q + "/options", json={"statement_timeout": 300})
    if opts["engine_major"] < 4:
        assert r.status_code == 400
        return
    assert r.status_code == 200
    res = client.post(q + "/execute", json={"sql": SLOW}).json()
    assert "timeout" in res["error"]["message"].lower() or "cancel" in res["error"]["message"].lower()


def test_detailed_plan_and_stats(qs):
    client, q, _, _ = qs
    r = client.post(q + "/explain", json={"sql": "select * from orders o join customer c on c.id = o.customer_id where o.id = :id"}).json()
    assert r["plan"].startswith("PLAN")
    assert "Select Expression" in r["detailed"]
    r = client.post(q + "/execute", json={"sql": "select count(*) from rdb$relations", "with_stats": True}).json()
    s = r["results"][0]["stats"]
    assert s["seq_reads"] > 0 and s["fetches"] > 0


def test_completion(client, database):
    sid, dbid = database
    client.post(f"/api/servers/{sid}/databases/{dbid}/execute", json={"sql": SCHEMA})
    c = client.get(f"/api/servers/{sid}/databases/{dbid}/completion").json()
    assert c["relations"]["CUSTOMER"][:2] == ["ID", "NAME"]
    assert "MON$ATTACHMENTS" in c["relations"]
    assert "SP_ORDERS_FOR" in c["procedures"] and c["packages"]["PK_UTIL"] == ["ADD_ONE"]
