"""Event monitor: wire format and source scanner (always), live events (FBADMIN_TEST_EVENTS_PORT).

The live tests need a Firebird server whose RemoteAuxPort is fixed and reachable from the test
host (scripts/events-test-server.sh starts such containers for Firebird 3/4/5/6). Set
FBADMIN_TEST_EVENTS_PORT to its main port; FBADMIN_TEST_HOST/USER/PASSWORD/DIR are shared with
the other integration tests.
"""

from __future__ import annotations

import os
import socket
import time
import uuid

import firebirdsql
import pytest

from fbadmin.events import scan, wire
from fbadmin.security.readonly import blocked_request

from .conftest import DB_DIR, HOST, PASSWORD, USER, requires_server

EVENTS_PORT = int(os.environ.get("FBADMIN_TEST_EVENTS_PORT", "0") or 0)
requires_events = pytest.mark.skipif(not EVENTS_PORT, reason="FBADMIN_TEST_EVENTS_PORT not set "
                                     "(start servers with scripts/events-test-server.sh)")


# --- units ---------------------------------------------------------------------------------------
def test_epb_round_trip():
    counts = {"A": 0, "ORDER_PAID": 7, "zażółć": 2 ** 31}
    epb = wire.build_epb(counts)
    assert epb[0] == 1
    assert wire.parse_epb(epb) == counts


def test_event_name_limits():
    with pytest.raises(ValueError):
        wire.encode_name("")
    with pytest.raises(ValueError):
        wire.encode_name("x" * 256)
    assert wire.encode_name("x" * 255)


def test_parse_sockaddr():
    buf = bytes([2, 0]) + (3051).to_bytes(2, "big") + bytes([172, 17, 0, 8]) + bytes(8)
    assert wire.parse_sockaddr(buf) == (3051, "172.17.0.8")
    buf6 = bytes([10, 0]) + (4000).to_bytes(2, "big") + bytes(4) + bytes(15) + b"\x01"
    assert wire.parse_sockaddr(buf6) == (4000, "::1")
    with pytest.raises(wire.AuxConnectionError):
        wire.parse_sockaddr(b"")


def test_find_post_events():
    src = """
    AS
      DECLARE name VARCHAR(30) = 'X';
    BEGIN
      -- POST_EVENT 'IN_LINE_COMMENT';
      /* POST_EVENT 'IN_BLOCK_COMMENT'; */
      POST_EVENT 'ORDER_PAID';
      post_event   'it''s';
      POST_EVENT "DIALECT1";
      POST_EVENT q'{QSTR}';
      POST_EVENT /* c */ 'AFTER_COMMENT' ;
      POST_EVENT :name;
      POST_EVENT 'A' || :name;
      INSERT INTO LOG VALUES ('POST_EVENT ''NOT_ME''');
      MY_POST_EVENT = 1;
    END"""
    hits = scan.find_post_events(src)
    names = [h["name"] for h in hits if h["name"]]
    assert names == ["ORDER_PAID", "it's", "DIALECT1", "QSTR", "AFTER_COMMENT"]
    exprs = [h["expression"] for h in hits if h["name"] is None]
    assert exprs == [":name", "'A' || :name"]
    assert scan.find_post_events(None) == []


def test_read_only_rules():
    base = "/api/servers/s1/databases/d1/event-monitor"
    for method, path in (("POST", "/sessions"), ("POST", "/sessions/x/register"), ("POST", "/sessions/x/reset"),
                         ("POST", "/sessions/x/unregister"), ("POST", "/sessions/x/reconnect"),
                         ("DELETE", "/sessions/x")):
        assert blocked_request(method, base + path) == (None, ""), path
    assert blocked_request("POST", base + "/post")[0] == "s1"


# --- live server ---------------------------------------------------------------------------------
@pytest.fixture
def ev_server(client):
    r = client.post("/api/servers", json={"name": "events", "host": HOST, "port": EVENTS_PORT,
                                          "user": USER, "password": PASSWORD})
    assert r.status_code == 201, r.text
    return r.json()


@pytest.fixture
def ev_db(client, ev_server):
    path = f"{DB_DIR}/fbadmin_events_{uuid.uuid4().hex[:8]}.fdb"
    r = client.post(f"/api/servers/{ev_server['id']}/databases/create", json={"name": "ev", "path": path})
    assert r.status_code == 201, r.text
    db = r.json()
    yield ev_server["id"], db["id"], path
    from fbadmin.events.monitor import get_event_manager

    get_event_manager().close_server(ev_server["id"])
    client.post(f"/api/servers/{ev_server['id']}/databases/{db['id']}/drop")


def other_conn(path):
    return firebirdsql.connect(host=HOST, port=EVENTS_PORT, database=path, user=USER, password=PASSWORD, timeout=30)


def post(conn, *names, commit=True):
    cur = conn.cursor()
    cur.execute("EXECUTE BLOCK AS BEGIN " + " ".join(f"POST_EVENT '{n}';" for n in names) + " END")
    if commit:
        conn.commit()
    else:
        conn.rollback()


def base(sid, dbid):
    return f"/api/servers/{sid}/databases/{dbid}/event-monitor"


def wait_counts(client, url, expected: dict, timeout=10):
    end = time.time() + timeout
    data = None
    while time.time() < end:
        data = client.get(url).json()
        got = {n["name"]: n["count"] for n in data["session"]["names"]}
        if all(got.get(k) == v for k, v in expected.items()):
            return data
        time.sleep(0.1)
    raise AssertionError(f"counts {expected} not reached: {data}")


def wait_listening(client, url, names, timeout=10):
    """Wait until every name has its baseline (the server answered the first queue request)."""
    end = time.time() + timeout
    while time.time() < end:
        data = client.get(url).json()
        state = {n["name"]: n["listening"] for n in data["session"]["names"]}
        if all(state.get(n) for n in names):
            return data
        time.sleep(0.05)
    raise AssertionError(f"not listening to {names}: {data}")


@requires_events
def test_events_live(client, ev_db):
    sid, dbid, path = ev_db
    r = client.post(base(sid, dbid) + "/sessions", json={"names": ["A", "B"]})
    assert r.status_code == 201, r.text
    s = r.json()["session"]
    assert s["status"] == "listening"
    url = f"{base(sid, dbid)}/sessions/{s['id']}"
    wait_listening(client, url, ["A", "B"])
    oc = other_conn(path)
    try:
        post(oc, "A", "A", "B", "C")  # C is not registered
        data = wait_counts(client, url, {"A": 2, "B": 1})
        assert {e["name"]: e["count"] for e in data["log"]} == {"A": 2, "B": 1}
        assert all(e["time"] > 0 for e in data["log"])
        last = data["session"]["last_seq"]

        # rolled back posts are never delivered
        post(oc, "A", commit=False)
        # the request was re-queued after the first notification: more posts keep arriving
        for _ in range(3):
            post(oc, "B")
        data = wait_counts(client, url, {"A": 2, "B": 4})
        time.sleep(0.5)
        data = client.get(url, params={"after": last}).json()
        assert {n["name"]: n["count"] for n in data["session"]["names"]} == {"A": 2, "B": 4}
        assert all(e["name"] == "B" for e in data["log"])
        assert sum(e["count"] for e in data["log"]) == 3

        # uncommitted posts are delivered only on commit
        cur = oc.cursor()
        cur.execute("EXECUTE BLOCK AS BEGIN POST_EVENT 'A'; END")
        time.sleep(0.7)
        assert {n["name"]: n["count"] for n in client.get(url).json()["session"]["names"]}["A"] == 2
        oc.commit()
        wait_counts(client, url, {"A": 3})

        # add a name later, remove one, reset counters
        r = client.post(url + "/register", json={"names": ["C", "A"]})
        assert r.status_code == 200 and r.json()["added"] == ["C"]
        wait_listening(client, url, ["A", "B", "C"])
        post(oc, "C", "A")
        wait_counts(client, url, {"A": 4, "B": 4, "C": 1})
        assert client.post(url + "/unregister", json={"names": ["B"]}).status_code == 200
        assert client.post(url + "/reset").status_code == 200
        wait_listening(client, url, ["A", "C"])
        post(oc, "A", "B", "C")
        data = wait_counts(client, url, {"A": 1, "C": 1})
        assert [n["name"] for n in data["session"]["names"]] == ["A", "C"]
        assert data["session"]["names"][0]["last_at"] is not None
        assert sum(data["session"]["names"][0]["rate"]) == 1
    finally:
        oc.close()

    # close: the attachment goes away
    assert client.delete(url).status_code == 204
    assert client.get(url).status_code == 404


@requires_events
def test_more_names_than_one_request(client, ev_db):
    sid, dbid, path = ev_db
    names = [f"EV_{i:02d}" for i in range(40)]
    r = client.post(base(sid, dbid) + "/sessions", json={"names": names[:20]})
    assert r.status_code == 201, r.text
    url = f"{base(sid, dbid)}/sessions/{r.json()['session']['id']}"
    assert client.post(url + "/register", json={"names": names[20:]}).status_code == 200
    data = wait_listening(client, url, names)
    assert data["session"]["requests"] == 3  # 15 + 15 + 10
    oc = other_conn(path)
    try:
        post(oc, *names)
        post(oc, names[0], names[39])
        wait_counts(client, url, {**{n: 1 for n in names}, names[0]: 2, names[39]: 2})
    finally:
        oc.close()
    client.delete(url)


@requires_events
def test_post_endpoint_and_suggestions(client, ev_db):
    sid, dbid, path = ev_db
    q = f"/api/servers/{sid}/databases/{dbid}/execute"
    r = client.post(q, json={"sql": """
CREATE TABLE T (ID INTEGER);
CREATE PROCEDURE P AS BEGIN POST_EVENT 'FROM_PROC'; END;
CREATE TRIGGER T_AI FOR T AFTER INSERT AS BEGIN POST_EVENT 'FROM_TRIGGER'; POST_EVENT 'FROM_PROC'; END;
CREATE FUNCTION F RETURNS INTEGER AS DECLARE n VARCHAR(10) = 'X'; BEGIN POST_EVENT :n; RETURN 1; END;
CREATE PACKAGE PK AS BEGIN PROCEDURE PP; END;
CREATE PACKAGE BODY PK AS BEGIN PROCEDURE PP AS BEGIN POST_EVENT 'FROM_PACKAGE'; END END;
COMMIT;"""})
    assert r.status_code == 200 and r.json()["error"] is None, r.text
    sug = client.get(base(sid, dbid) + "/suggestions").json()
    by = {s["name"]: {(x["type"], x["name"]) for x in s["sources"]} for s in sug["names"]}
    assert by == {"FROM_PROC": {("procedure", "P"), ("trigger", "T_AI")}, "FROM_TRIGGER": {("trigger", "T_AI")},
                  "FROM_PACKAGE": {("package", "PK")}}
    assert [(d["type"], d["name"], d["expression"]) for d in sug["dynamic"]] == [("function", "F", ":n")]

    r = client.post(base(sid, dbid) + "/sessions", json={"names": ["FROM_TRIGGER", "it's"]})
    url = f"{base(sid, dbid)}/sessions/{r.json()['session']['id']}"
    wait_listening(client, url, ["FROM_TRIGGER", "it's"])
    assert client.post(base(sid, dbid) + "/post", json={"name": "it's", "count": 3}).status_code == 200
    r = client.post(q, json={"sql": "INSERT INTO T VALUES (1); COMMIT;"})
    assert r.status_code == 200 and r.json()["error"] is None, r.text
    wait_counts(client, url, {"it's": 3, "FROM_TRIGGER": 1})
    client.delete(url)

    # read-only servers may listen but not post
    assert client.put(f"/api/servers/{sid}", json={"read_only": True}).status_code == 200
    r = client.post(base(sid, dbid) + "/sessions", json={"names": ["X"]})
    assert r.status_code == 201, r.text
    client.delete(f"{base(sid, dbid)}/sessions/{r.json()['session']['id']}")
    r = client.post(base(sid, dbid) + "/post", json={"name": "X"})
    assert r.status_code == 403
    assert "read-only" in r.json()["detail"]


@requires_events
def test_idle_sessions_expire(client, ev_db, monkeypatch):
    from fbadmin.events import monitor

    sid, dbid, path = ev_db
    r = client.post(base(sid, dbid) + "/sessions", json={"names": ["A"]})
    url = f"{base(sid, dbid)}/sessions/{r.json()['session']['id']}"
    monkeypatch.setattr(monitor, "IDLE_TIMEOUT", 0.5)
    time.sleep(2.5)
    sess = monitor.get_event_manager().get(r.json()["session"]["id"])
    assert sess.status == "expired"
    assert sess._conn is None and sess._channel is None
    monkeypatch.setattr(monitor, "IDLE_TIMEOUT", 300)
    # reconnect brings it back with the same names
    r = client.post(url + "/reconnect")
    assert r.status_code == 200, r.text
    assert r.json()["session"]["status"] == "listening"
    wait_listening(client, url, ["A"])
    oc = other_conn(path)
    try:
        post(oc, "A")
        wait_counts(client, url, {"A": 1})
    finally:
        oc.close()
    client.delete(url)


@requires_server
def test_unreachable_aux_port(client, database):
    """The plain test servers (scripts/test-servers.sh) only publish port 3050, so the random
    RemoteAuxPort they advertise is unreachable: the error must explain it and nothing may hang."""
    sid, dbid = database
    t = time.time()
    r = client.post(base(sid, dbid) + "/sessions", json={"names": ["A"]})
    if r.status_code == 201:
        client.delete(f"{base(sid, dbid)}/sessions/{r.json()['session']['id']}")
        pytest.skip("the auxiliary port of the FBADMIN_TEST_PORT server is reachable")
    assert r.status_code == 502, r.text
    assert r.json()["code"] == "aux_unreachable"
    assert "RemoteAuxPort" in r.json()["detail"]
    assert time.time() - t < 15
    # nothing is left behind and the database is still usable
    from fbadmin.events.monitor import get_event_manager

    assert not [s for s in get_event_manager().sessions.values() if s.server_id == sid]
    r = client.get(base(sid, dbid) + "/suggestions")
    assert r.status_code == 200, r.text


@requires_events
def test_connection_lost(client, ev_db):
    sid, dbid, path = ev_db
    r = client.post(base(sid, dbid) + "/sessions", json={"names": ["A"]})
    sess_id = r.json()["session"]["id"]
    url = f"{base(sid, dbid)}/sessions/{sess_id}"
    wait_listening(client, url, ["A"])
    from fbadmin.events.monitor import get_event_manager

    get_event_manager().get(sess_id)._channel.sock.shutdown(socket.SHUT_RDWR)
    end = time.time() + 10
    while time.time() < end and client.get(url).json()["session"]["status"] != "error":
        time.sleep(0.1)
    s = client.get(url).json()["session"]
    assert s["status"] == "error" and s["error_code"] == "connection_lost"
    client.delete(url)


@requires_events
def test_busy_aux_port_recovers(client, ev_db, monkeypatch):
    """A fixed RemoteAuxPort serves one auxiliary connection request at a time. While another client
    holds it we report "aux_busy", and our queued request is completed in the background so the port
    is free again as soon as the other client is done (not ConnectionTimeout later)."""
    from fbadmin.events import monitor

    sid, dbid, path = ev_db
    monkeypatch.setattr(wire, "ANSWER_TIMEOUT", 2.0)
    blocker = other_conn(path)
    blocker._op_connect_request()
    _, _, buf = blocker._op_response()  # the server now waits for us on its aux port
    port, _ = wire.parse_sockaddr(buf)
    try:
        r = client.post(base(sid, dbid) + "/sessions", json={"names": ["A"]})
        assert r.status_code == 502, r.text
        assert r.json()["code"] == "aux_busy"
        assert not [s for s in monitor.get_event_manager().sessions.values() if s.server_id == sid]
    finally:
        socket.create_connection((HOST, port), timeout=5).close()  # let the server go on
        blocker.sock.close()
    end = time.time() + 30
    while True:
        r = client.post(base(sid, dbid) + "/sessions", json={"names": ["A"]})
        if r.status_code == 201 or time.time() > end:
            break
        time.sleep(1)
    assert r.status_code == 201, r.text
    client.delete(f"{base(sid, dbid)}/sessions/{r.json()['session']['id']}")
