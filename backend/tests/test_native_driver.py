"""The native driver (firebird-driver + libfbclient) as an alternative to firebirdsql.

Tests that need it are skipped unless ``firebird-driver`` is installed and a ``libfbclient`` loads
(``FBADMIN_FBCLIENT=/path/to/libfbclient.so``; use a Firebird 5 client, e.g. the one from
``scripts/embedded-test-setup.sh``, for INT128/DECFLOAT/time zones). The whole suite can also run
with ``FBADMIN_DRIVER=native``; ``test_driver_equivalence.py`` compares both drivers result by result.
"""

from __future__ import annotations

import threading
import time
import uuid

import firebirdsql
import pytest

from .conftest import DB_DIR, HOST, PASSWORD, PORT, USER, requires_server
from .drivers_util import client_major, requires_native, use_driver

SLOW = "SELECT COUNT(*) FROM RDB$FIELDS a, RDB$FIELDS b, RDB$FIELDS c"


def base(sid, dbid):
    return f"/api/servers/{sid}/databases/{dbid}"


def one(client, sid, dbid, sql):
    r = client.post(base(sid, dbid) + "/execute", json={"sql": sql})
    assert r.status_code == 200 and not r.json()["error"], r.text
    return r.json()["results"][-1]["rows"][0]


def engine_major(client, sid, dbid) -> int:
    return int(str(one(client, sid, dbid, "SELECT RDB$GET_CONTEXT('SYSTEM', 'ENGINE_VERSION') FROM RDB$DATABASE")[0])
               .split(".")[0])


# ------------------------------------------------------------------ selection and setting
def test_driver_switch(monkeypatch, tmp_path):
    from fbadmin import config, native

    monkeypatch.setattr(config.settings, "data_dir", tmp_path)
    monkeypatch.delenv("FBADMIN_DRIVER", raising=False)
    assert not native.native_enabled()
    monkeypatch.setenv("FBADMIN_DRIVER", "native")
    assert native.native_enabled()


def test_stored_setting_and_environment_override(monkeypatch, tmp_path):
    from fbadmin import config, drivers

    monkeypatch.setattr(config.settings, "data_dir", tmp_path)
    monkeypatch.delenv("FBADMIN_DRIVER", raising=False)
    monkeypatch.delenv("FBADMIN_FBCLIENT", raising=False)
    assert drivers.selected() == ("firebirdsql", "default")
    drivers.save(driver="native", fbclient="/opt/fb/lib/libfbclient.so")
    assert (tmp_path / "driver.json").is_file()
    assert drivers.selected() == ("native", "settings")
    assert drivers.fbclient_setting() == ("/opt/fb/lib/libfbclient.so", "settings")
    monkeypatch.setenv("FBADMIN_DRIVER", "firebirdsql")
    monkeypatch.setenv("FBADMIN_FBCLIENT", "/usr/lib/libfbclient.so.2")
    assert drivers.selected() == ("firebirdsql", "environment")
    assert drivers.fbclient_setting() == ("/usr/lib/libfbclient.so.2", "environment")
    drivers.save(fbclient="")
    assert "fbclient" not in drivers.stored()
    with pytest.raises(ValueError):
        drivers.normalize("odbc")


def test_driver_api(client, monkeypatch):
    monkeypatch.delenv("FBADMIN_DRIVER", raising=False)
    st = client.get("/api/driver").json()
    assert st["driver"] in ("firebirdsql", "native") and set(st["drivers"]) == {"firebirdsql", "native"}
    assert {"available", "reason", "library", "client_version", "package"} <= set(st["native"])
    # an unknown driver, and a client library that does not exist, are refused
    assert client.put("/api/driver", json={"driver": "odbc"}).status_code == 400
    r = client.put("/api/driver", json={"driver": "native", "fbclient": "/nonexistent/libfbclient.so"})
    if not st["native"]["embedded_library"] and st["fbclient_source"] != "environment":
        assert r.status_code == 400 and "/nonexistent/libfbclient.so" in r.json()["detail"]
        assert client.get("/api/driver").json()["stored"] == {}  # nothing saved
    # with the driver set by the environment, the API cannot change it
    monkeypatch.setenv("FBADMIN_DRIVER", "firebirdsql")
    r = client.put("/api/driver", json={"driver": "native"})
    assert r.status_code == 400 and "FBADMIN_DRIVER" in r.json()["detail"]
    st = client.get("/api/driver").json()
    assert st["source"] == "environment" and st["env"]["FBADMIN_DRIVER"] == "firebirdsql"
    assert client.put("/api/driver", json={"driver": "firebirdsql"}).status_code == 200  # no change: fine


def test_error_translation():
    from fbadmin.native import _translate

    class DatabaseError(Exception):
        sqlcode = -204
        gds_codes = (335544569, 335544580)

    class UniqueError(Exception):
        sqlcode = -803
        gds_codes = (335544665, 335545072)

    e = _translate(DatabaseError("Table unknown\n-NO_SUCH\n-At line 1, column 15"))
    assert type(e) is firebirdsql.OperationalError and e.sql_code == -204  # what firebirdsql raises
    assert type(_translate(UniqueError("violation of PRIMARY or UNIQUE KEY"))) is firebirdsql.IntegrityError
    assert str(e).splitlines() == ["Table unknown", "NO_SUCH", "At line 1, column 15"]  # like firebirdsql
    assert isinstance(_translate(Exception("Unable to complete network request to host")), OSError)


def test_client_config():
    from fbadmin.native import _host_port, client_config

    assert client_config(wire_crypt=False, wire_compress=True, auth_plugin_name="Srp256", timeout=30).splitlines() == [
        "WireCrypt = Disabled", "WireCompression = true", "AuthClient = Srp256, Srp", "ConnectionTimeout = 30"]
    assert client_config() == ""
    assert _host_port("::1", 3050) == "[::1]/3050" and _host_port("db.example", None) == "db.example"


# ------------------------------------------------------------------ against a server
@requires_native
@requires_server
def test_api_switch_closes_pools_and_sessions_keep_their_driver(client, database, monkeypatch, tmp_path):
    from fbadmin.api.deps import get_state
    from fbadmin.native import NativeConnection

    monkeypatch.delenv("FBADMIN_DRIVER", raising=False)
    sid, dbid = database
    mgr = get_state().manager
    r = client.put("/api/driver", json={"driver": "firebirdsql"})
    assert r.status_code == 200
    one(client, sid, dbid, "SELECT 1 FROM RDB$DATABASE")
    assert mgr.is_connected(sid, dbid)
    qs = client.post("/api/query/sessions", json={"server_id": sid, "database_id": dbid}).json()["session_id"]
    r = client.put("/api/driver", json={"driver": "native"})
    assert r.status_code == 200, r.text
    assert r.json()["driver"] == "native" and r.json()["source"] == "settings"
    assert r.json()["closed_connections"] >= 1 and not mgr.is_connected(sid, dbid)
    with mgr.meta(sid, dbid) as conn:
        assert isinstance(conn, NativeConnection)
    # the open Query Tool session keeps its firebirdsql connection until it is closed
    assert not isinstance(mgr.session(qs).conn, NativeConnection)
    res = client.post(f"/api/query/sessions/{qs}/execute", json={"sql": "SELECT 1 FROM RDB$DATABASE"}).json()
    assert res["error"] is None
    client.delete(f"/api/query/sessions/{qs}")
    qs = client.post("/api/query/sessions", json={"server_id": sid, "database_id": dbid}).json()["session_id"]
    assert isinstance(mgr.session(qs).conn, NativeConnection)
    client.delete(f"/api/query/sessions/{qs}")
    client.put("/api/driver", json={"driver": "firebirdsql"})


@requires_native
@requires_server
def test_native_metadata_endpoints(client, database, monkeypatch):
    use_driver(monkeypatch, "native")
    from fbadmin.api.deps import get_state
    from fbadmin.native import NativeConnection

    sid, dbid = database
    b = base(sid, dbid)
    r = client.post(f"{b}/execute", json={"sql": "CREATE TABLE NT (ID INTEGER NOT NULL PRIMARY KEY, "
                                                "NAME VARCHAR(20), N NUMERIC(10,2), B BLOB SUB_TYPE TEXT)"})
    assert r.status_code == 200, r.text
    with get_state().manager.meta(sid, dbid) as conn:
        assert isinstance(conn, NativeConnection)
        cur = conn.cursor()
        cur.execute("INSERT INTO NT VALUES (1, 'a', 1.25, 'blob text')")
        assert cur.rowcount == 1
    names = [o["name"] for o in client.get(f"{b}/objects/tables").json()]
    assert "NT" in names
    ddl = client.get(f"{b}/objects/tables/NT").json()["ddl"]
    assert "CREATE TABLE NT" in ddl and "NUMERIC(10, 2)" in ddl.replace("NUMERIC(10,2)", "NUMERIC(10, 2)")
    data = client.get(f"{b}/tables/NT/data").json()
    assert data["rows"][0][1] == "a" and data["rows"][0][3] == "blob text"
    assert data["columns"][2]["type"].startswith(("NUMERIC", "INTEGER"))
    r = client.get(f"{b}/tables/NO_SUCH_TABLE/count")
    assert r.status_code == 400 and r.json()["code"] == "firebird_error"


@requires_native
@requires_server
def test_native_query_tool_session(client, database, monkeypatch):
    """Transactions, isolation, read-only, statement timeout, plans, parameters, fetch more, cancel."""
    use_driver(monkeypatch, "native")
    from fbadmin.api.deps import get_state
    from fbadmin.native import NativeConnection

    sid, dbid = database
    one(client, sid, dbid, "CREATE TABLE QT (ID INTEGER NOT NULL PRIMARY KEY, S VARCHAR(20)); COMMIT; "
                           "INSERT INTO QT VALUES (1, 'one'); INSERT INTO QT VALUES (2, 'two'); "
                           "SELECT COUNT(*) FROM QT")
    r = client.post("/api/query/sessions", json={"server_id": sid, "database_id": dbid})
    assert r.status_code == 201
    opts = r.json()["options"]
    q = f"/api/query/sessions/{r.json()['session_id']}"
    assert isinstance(get_state().manager.session(r.json()["session_id"]).conn, NativeConnection)
    # manual transaction: invisible to others until committed, rollback undoes it
    res = client.post(q + "/execute", json={"sql": "INSERT INTO QT VALUES (3, 'three')", "autocommit": False}).json()
    assert res["transaction_open"]
    assert one(client, sid, dbid, "SELECT COUNT(*) FROM QT")[0] == 2
    client.post(q + "/rollback")
    assert one(client, sid, dbid, "SELECT COUNT(*) FROM QT")[0] == 2
    # parameters
    res = client.post(q + "/execute", json={"sql": "SELECT S FROM QT WHERE ID = :ID", "params": {"ID": 2}}).json()
    assert res["results"][0]["rows"] == [["two"]]
    # read-only snapshot
    assert client.put(q + "/options", json={"isolation": "snapshot", "read_only": True}).status_code == 200
    res = client.post(q + "/execute", json={"sql": "INSERT INTO QT VALUES (4, 'x')"}).json()
    assert "read-only" in res["error"]["message"].lower()
    client.put(q + "/options", json={"isolation": "read_committed"})
    # plans
    p = client.post(q + "/explain", json={"sql": "SELECT * FROM QT WHERE ID = 1"}).json()
    assert "QT" in p["plan"] and "INDEX" in p["plan"].upper()
    if opts["engine_major"] >= 3:
        assert p["detailed"] and "QT" in p["detailed"]
    res = client.post(q + "/execute", json={"sql": "SELECT * FROM QT", "with_plan": True, "with_stats": True}).json()
    assert "QT" in res["results"][0]["plan"] and res["results"][0]["stats"]["seq_reads"] >= 2
    # fetch more
    gen = "WITH RECURSIVE R(N) AS (SELECT 1 FROM RDB$DATABASE UNION ALL SELECT N + 1 FROM R WHERE N < 30) SELECT N FROM R"
    res = client.post(q + "/execute", json={"sql": gen, "max_rows": 10}).json()
    assert res["cursor_open"] and len(res["results"][0]["rows"]) == 10
    more = client.post(q + "/fetch", json={"rows": 50}).json()
    assert [r[0] for r in more["rows"]] == list(range(11, 31)) and not more["has_more"]
    # statement timeout (Firebird 4+)
    r = client.put(q + "/options", json={"statement_timeout": 300})
    if opts["engine_major"] >= 4:
        res = client.post(q + "/execute", json={"sql": SLOW}).json()
        msg = res["error"]["message"].lower()
        assert "timeout" in msg or "cancel" in msg
        client.put(q + "/options", json={"statement_timeout": 0})
    else:
        assert r.status_code == 400
    # cancel from another attachment while the statement runs (the native call releases the GIL)
    out: dict = {}
    t = threading.Thread(target=lambda: out.update(r=client.post(q + "/execute", json={"sql": SLOW}).json()))
    t.start()
    cancelled = 0
    for _ in range(50):
        time.sleep(0.2)
        cancelled = client.post(q + "/cancel").json()["cancelled"]
        if cancelled:
            break
    t.join(60)
    assert cancelled == 1 and "cancel" in out["r"]["error"]["message"].lower()
    assert client.post(q + "/execute", json={"sql": "SELECT 1 FROM RDB$DATABASE"}).json()["error"] is None
    client.delete(q)


@requires_native
@requires_server
def test_native_wire_options_and_auth_plugin(client, database, monkeypatch):
    """Per-server wire compression / encryption and auth plugin reach libfbclient (isc_dpb_config)."""
    use_driver(monkeypatch, "native")
    sid, dbid = database
    ctx = ("SELECT RDB$GET_CONTEXT('SYSTEM', 'WIRE_COMPRESSED'), RDB$GET_CONTEXT('SYSTEM', 'WIRE_ENCRYPTED'), "
           "a.MON$AUTH_METHOD FROM MON$ATTACHMENTS a WHERE a.MON$ATTACHMENT_ID = CURRENT_CONNECTION")
    assert client.put(f"/api/servers/{sid}", json={"wire_compress": True, "wire_crypt": True,
                                                   "auth_plugin": "Srp256"}).status_code == 200
    row = one(client, sid, dbid, ctx)
    assert row[0] == "TRUE" and row[1] == "TRUE" and row[2].strip() in ("Srp256", "Srp")  # Srp: a Firebird 3 server without Srp256
    assert "Firebird" in client.get(f"/api/servers/{sid}/info").json()["server_version"]  # services too
    assert client.put(f"/api/servers/{sid}", json={"wire_compress": False, "auth_plugin": None}).status_code == 200
    assert one(client, sid, dbid, ctx)[0] == "FALSE"
    # no encryption: refused by a server with WireCrypt = Required (the Docker default), else plain
    assert client.put(f"/api/servers/{sid}", json={"wire_crypt": False}).status_code == 200
    r = client.post(base(sid, dbid) + "/execute", json={"sql": ctx})
    if r.status_code == 200:
        assert r.json()["results"][0]["rows"][0][1] == "FALSE"
    else:
        assert "encryption" in r.json()["detail"].lower()
    client.put(f"/api/servers/{sid}", json={"wire_crypt": True})


@requires_native
@requires_server
def test_native_services(client, database, monkeypatch):
    """Services API through firebird-driver's service manager: info, log, users, backup/restore, gstat."""
    use_driver(monkeypatch, "native")
    from fbadmin.api.deps import get_state
    from fbadmin.native import NativeServices

    sid, dbid = database
    with get_state().manager.service(sid) as svc:
        assert isinstance(svc, NativeServices)
    assert "Firebird" in client.get(f"/api/servers/{sid}/info").json()["server_version"]
    assert isinstance(client.get(f"/api/servers/{sid}/log").json()["log"], str)
    assert any(u["user_name"] == USER for u in client.get(f"/api/servers/{sid}/fbusers").json()["users"])
    one(client, sid, dbid, "CREATE TABLE BK (ID INTEGER NOT NULL PRIMARY KEY, S VARCHAR(100)); COMMIT; "
                           "INSERT INTO BK SELECT r, 'row ' || r FROM (SELECT ROW_NUMBER() OVER () AS r "
                           "FROM RDB$FIELDS) t; COMMIT; SELECT COUNT(*) FROM BK")
    expected = one(client, sid, dbid, "SELECT COUNT(*) FROM BK")[0]
    r = client.post(f"/api/servers/{sid}/databases/{dbid}/maintenance/backup/stream", json={})
    assert r.status_code == 200, r.text
    with client.stream("GET", r.json()["url"]) as resp:
        data = b"".join(resp.iter_bytes())
    assert len(data) > 4000 and expected > 100
    target = f"{DB_DIR}/fbadmin_native_{uuid.uuid4().hex[:8]}.fdb"
    r = client.post(f"/api/servers/{sid}/maintenance/restore/upload",
                    params={"database_path": target, "filename": "n.fbk", "register_as": "native-restored"},
                    content=data, headers={"Content-Type": "application/octet-stream"})
    assert r.status_code == 202, r.text
    job = r.json()
    for _ in range(300):
        job = client.get(f"/api/jobs/{job['id']}").json()
        if job["status"] != "running":
            break
        time.sleep(0.2)
    assert job["status"] == "finished", job
    new_id = next(d["id"] for d in client.get(f"/api/servers/{sid}").json()["databases"] if d["name"] == "native-restored")
    try:
        assert one(client, sid, new_id, "SELECT COUNT(*) FROM BK")[0] == expected
    finally:
        client.post(f"/api/servers/{sid}/databases/{new_id}/drop")


@requires_native
@requires_server
def test_error_classes_match_firebirdsql(client, database):
    from fbadmin.connections import connect
    from fbadmin.store import ServerEntry

    sid, dbid = database
    path = client.get(f"/api/servers/{sid}").json()["databases"][0]["path"]
    srv = ServerEntry(name="t", host=HOST, port=PORT, user=USER)
    one(client, sid, dbid, "CREATE TABLE EC (ID INTEGER NOT NULL PRIMARY KEY); COMMIT; "
                           "INSERT INTO EC VALUES (1); SELECT 1 FROM RDB$DATABASE")
    cases = ["SELECT * FROM NO_SUCH_TABLE", "SELECT FROM", "INSERT INTO EC VALUES (1)", "SELECT 1/0 FROM RDB$DATABASE",
             "SELECT CAST('x' AS INTEGER) FROM RDB$DATABASE"]
    seen: dict[str, list] = {}
    for drv in ("firebirdsql", "native"):
        conn = connect(srv, path, PASSWORD, driver=drv)
        try:
            for sql in cases:
                cur = conn.cursor()
                with pytest.raises(firebirdsql.Error) as ei:
                    cur.execute(sql)
                    cur.fetchall()
                seen.setdefault(sql, []).append((type(ei.value).__name__, str(ei.value).strip()))
                conn.rollback()
        finally:
            conn.close()
    for sql, (pure, nat) in seen.items():
        assert pure == nat, sql


@requires_native
@requires_server
def test_native_big_values_and_blobs(client, database):
    """BLOBs of 1 and 10 MB (text and binary) and big parameters round-trip unchanged."""
    from fbadmin.connections import connect
    from fbadmin.store import ServerEntry

    sid, dbid = database
    path = client.get(f"/api/servers/{sid}").json()["databases"][0]["path"]
    one(client, sid, dbid, "CREATE TABLE BL (ID INTEGER NOT NULL PRIMARY KEY, T BLOB SUB_TYPE TEXT, "
                           "B BLOB SUB_TYPE BINARY, V VARCHAR(8000)); COMMIT; SELECT 1 FROM RDB$DATABASE")
    conn = connect(ServerEntry(name="t", host=HOST, port=PORT, user=USER), path, PASSWORD, driver="native")
    try:
        text = ("Firebird ✓ ünïcödé " * 60000)[:1_000_000]
        binary = bytes(range(256)) * (10 * 1024 * 1024 // 256)
        cur = conn.cursor()
        cur.execute("INSERT INTO BL VALUES (?, ?, ?, ?)", [1, text, binary, "€" * 8000])
        conn.commit()
        cur.execute("SELECT T, B, V, OCTET_LENGTH(B) FROM BL WHERE ID = 1")
        t, b, v, n = cur.fetchone()
        assert t == text and b == binary and v == "€" * 8000 and n == len(binary)
    finally:
        conn.close()


@requires_native
@requires_server
def test_fb4_types_need_a_new_client(client, database):
    """INT128 / DECFLOAT / time zones work through a Firebird 4+ client; a Firebird 3 client gets them
    as BIGINT / DOUBLE PRECISION / without time zone (SET BIND ... TO LEGACY) instead of an error."""
    sid, dbid = database
    if engine_major(client, sid, dbid) < 4:
        pytest.skip("Firebird 3 server: no INT128/DECFLOAT/time zones")
    from fbadmin.connections import connect
    from fbadmin.store import ServerEntry

    path = client.get(f"/api/servers/{sid}").json()["databases"][0]["path"]
    conn = connect(ServerEntry(name="t", host=HOST, port=PORT, user=USER), path, PASSWORD, driver="native")
    try:
        cur = conn.cursor()
        if (client_major() or 0) < 4:  # e.g. FBADMIN_FBCLIENT=Debian's libfbclient2 (3.0)
            cur.execute("SELECT CAST(12 AS INT128), CAST('1.5' AS DECFLOAT(34)), "
                        "CAST('2024-01-01 10:00 UTC' AS TIMESTAMP WITH TIME ZONE) FROM RDB$DATABASE")
            i, d, ts = cur.fetchone()
            assert i == 12 and d == 1.5 and ts.tzinfo is None
            return
        cur.execute("SELECT CAST(? AS INT128), CAST(? AS DECFLOAT(34)), CAST(? AS TIMESTAMP WITH TIME ZONE) "
                    "FROM RDB$DATABASE", ["-170141183460469231731687303715884105728", "1.5E-6000",
                                         "2024-03-31 02:30:00 Europe/Berlin"])
        i, d, ts = cur.fetchone()
        assert i == -170141183460469231731687303715884105728 and isinstance(i, int)
        assert str(d) == "1.5E-6000"
        assert ts.tzinfo is not None and ts.utcoffset().total_seconds() == 7200  # CEST after the switch
    finally:
        conn.close()


@requires_native
@requires_server
def test_event_monitor_stays_on_firebirdsql(client, database, monkeypatch):
    use_driver(monkeypatch, "native")
    from fbadmin.api.events import _connector

    sid, dbid = database
    conn = _connector(sid, dbid)()
    try:
        assert isinstance(conn, firebirdsql.Connection)
    finally:
        conn.close()
