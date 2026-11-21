"""Error log from the continuous trace (roadmap 2.5): parsing ERROR events, grouping by code, the statement that
failed, lock conflicts, the trace.errors alert (new kind, spike), and a real capture."""

from __future__ import annotations

import time

import pytest

from fbadmin.monitoring import trace_capture as TC
from fbadmin.monitoring import trace_errors as TE

from .conftest import requires_server
from .test_alerts import add_webhook, flush, receiver  # noqa: F401 - fixture
from .test_maintenance import execute

TEXT = """2026-11-21T18:56:38.6950 (21:0x7f) ERROR AT JStatement::prepare
\t/db/app.fdb (ATT_4, SYSDBA:NONE, UTF8, TCPv4:172.17.0.1/53052)
335544569 : Dynamic SQL Error
335544436 : SQL error code = -204
335544580 : Table unknown
335544382 : NOPE
336397208 : At line 1, column 15"""


def test_parse_and_categories():
    e = TE.parse({"type": "ERROR AT JStatement::prepare", "text": TEXT})
    assert e["code"] == 335544580 and e["message"] == "Table unknown" and e["at_function"] == "JStatement::prepare"
    assert e["category"] == "error" and "335544382 : NOPE" in e["detail"]
    dl = TE.parse({"type": "ERROR AT JStatement::execute", "text": "h\n335544336 : deadlock\n335544451 : update conflicts"})
    assert dl["code"] == 335544336 and dl["category"] == "lock"
    assert TE.parse({"type": "ERROR AT x", "text": "nothing here"}) is None


def test_capture_links_the_failed_statement(monkeypatch):
    rec = []
    monkeypatch.setattr(TE, "record", lambda sid, dbid, err, **kw: rec.append((err["code"], kw["sql"], kw["user"])))
    cap = TC.Capture("s", "d")
    cap.on_event({"type": "FAILED EXECUTE_STATEMENT_FINISH", "attachment": 4, "sql": "INSERT INTO T VALUES (1)"})
    cap.on_event({"type": "ERROR AT JStatement::execute", "attachment": 4, "user": "SYSDBA",
                  "text": "h\n335544665 : violation of PRIMARY or UNIQUE KEY constraint \"INTEG_2\" on table \"T\""})
    cap.on_event({"type": "ERROR AT JStatement::prepare", "attachment": 5, "user": "APP", "text": TEXT})
    assert rec == [(335544665, "INSERT INTO T VALUES (1)", "SYSDBA"), (335544580, None, "APP")]
    assert cap.errors == 2 and cap.captured == 0


@pytest.fixture
def fresh(client, database):  # noqa: F811 - a database whose history store is clean
    sid, dbid = database
    TE.forget(sid, dbid)
    yield sid, dbid
    TE.forget(sid, dbid)


@requires_server
def test_groups_and_alerts(client, fresh, receiver):  # noqa: F811
    sid, dbid = fresh
    now = time.time()
    unknown = TE.parse({"type": "ERROR AT JStatement::prepare", "text": TEXT})
    deadlock = TE.parse({"type": "ERROR AT JStatement::execute", "text": "h\n335544336 : deadlock"})
    # table unknown: usual (twice an hour all day yesterday); deadlocks: none before
    for i in range(46):
        TE.record(sid, dbid, unknown, sql="SELECT * FROM NOPE", user="APP", attachment=1, ts=now - 3700 - i * 1800)
    for i in range(3):
        TE.record(sid, dbid, unknown, sql=None, user="APP", attachment=1, ts=now - 60 * (i + 1))
    for i in range(25):
        TE.record(sid, dbid, deadlock, sql="UPDATE T SET X = 1", user="APP", attachment=2, ts=now - 30 * (i + 1))
    g = client.get(f"/api/servers/{sid}/databases/{dbid}/monitoring/errors", params={"hours": 1}).json()
    assert g["total"] == 28 and g["lock"] == 25
    assert [(x["code"], x["count"], x["category"], x["new"]) for x in g["groups"]] == [
        (335544336, 25, "lock", True), (335544580, 3, "error", False)]
    assert g["groups"][1]["example_sql"] is None or g["groups"][1]["example_sql"] == "SELECT * FROM NOPE"
    assert sum(h["count"] for h in g["hourly"]) == 28

    hook = add_webhook(client, receiver)
    client.post("/api/alerts/rules", json={"name": "errors", "event": "trace.errors", "server_id": sid,
                                           "database_id": dbid, "channels": [hook["id"]]})
    raised = TE.check_alerts(sid, dbid, "app", now)
    assert raised == ["335544336:new"]  # the usual error does not alert
    assert TE.check_alerts(sid, dbid, "app", now + 60) == []  # once
    flush()
    assert receiver.json(len(receiver.requests) - 1)["title"] == "New lock conflict in app: deadlock"
    # a spike of the usual error: 40 in the last hour against about 2 an hour
    for i in range(40):
        TE.record(sid, dbid, unknown, sql=None, user="APP", attachment=1, ts=now - 20 * (i + 1))
    assert "335544580:spike" in TE.check_alerts(sid, dbid, "app", now + 120)
    for r in client.get("/api/alerts/rules").json():
        client.delete(f"/api/alerts/rules/{r['id']}")
    client.delete(f"/api/alerts/channels/{hook['id']}")


@requires_server
def test_real_capture_of_errors(client, fresh):
    sid, dbid = fresh
    base = f"/api/servers/{sid}/databases/{dbid}/monitoring"
    execute(client, sid, dbid, "CREATE TABLE T (ID INTEGER NOT NULL PRIMARY KEY)")
    execute(client, sid, dbid, "INSERT INTO T VALUES (1)")
    client.put(base + "/history/tracking", json={"enabled": True})
    try:
        r = client.put(base + "/trace-capture", json={"enabled": True, "threshold_ms": 0})
        assert r.status_code == 200 and r.json()["state"] == "running", r.text
        for sql in ("INSERT INTO T VALUES (1)", "SELECT * FROM NOPE"):
            res = client.post(f"/api/servers/{sid}/databases/{dbid}/execute", json={"sql": sql}).json()
            assert res["error"] is not None
        got = {}
        for _ in range(60):
            got = {x["code"]: x for x in client.get(base + "/errors").json()["groups"]}
            if 335544665 in got and 335544580 in got:
                break
            time.sleep(0.25)
        assert got[335544665]["category"] == "constraint" and got[335544665]["example_sql"] == "INSERT INTO T VALUES (1)"
        assert got[335544580]["message"] == "Table unknown"
        assert client.get(base + "/trace-capture").json()["errors"] >= 2
    finally:
        TC.stop(sid, dbid)
        client.delete(base + "/history")
