"""Milestone 0.7 - monitoring and performance, against a live server."""

from __future__ import annotations

import re
import threading
import time

import firebirdsql
import pytest

from .conftest import HOST, PASSWORD, PORT, USER, requires_server

pytestmark = requires_server

PROC = """
CREATE OR ALTER PROCEDURE P_SUM (N INTEGER) RETURNS (S BIGINT) AS
DECLARE I INTEGER = 0;
BEGIN
  S = 0;
  WHILE (I < N) DO
  BEGIN
    S = S + I;
    I = I + 1;
  END
  SUSPEND;
END
"""


def _base(database):
    sid, dbid = database
    return f"/api/servers/{sid}/databases/{dbid}"


def _exec(client, database, sql):
    r = client.post(_base(database) + "/execute", json={"sql": sql})
    assert r.status_code == 200, r.text
    assert r.json()["error"] is None, r.json()["error"]
    return r.json()


def _major(client, database) -> int:
    return int(client.post(_base(database) + "/connect").json()["engine_version"].split(".")[0])


def _db_path(client, database) -> str:
    sid, dbid = database
    srv = next(s for s in client.get("/api/servers").json() if s["id"] == sid)
    return next(d for d in srv["databases"] if d["id"] == dbid)["path"]


def _direct(client, database):
    return firebirdsql.connect(host=HOST, port=PORT, database=_db_path(client, database), user=USER,
                               password=PASSWORD)


def test_snapshot_and_features(client, database):
    m = _base(database) + "/monitoring"
    f = client.get(m + "/features").json()
    major = _major(client, database)
    assert f["engine_major"] == major
    assert f["profiler"] == (major >= 5)
    assert f["compiled_statements"] == (major >= 5)
    assert f["replication"] == (major >= 4)
    a = client.get(m + "/snapshot").json()
    _exec(client, database, "CREATE TABLE T (ID INTEGER); COMMIT; INSERT INTO T VALUES (1); INSERT INTO T VALUES (2);")
    b = client.get(m + "/snapshot").json()
    assert b["attachments"] >= 1
    assert b["next_transaction"] > a["next_transaction"]
    assert b["inserts"] >= a["inserts"] + 2
    assert b["memory_used"] > 0
    assert b["ts"] > a["ts"]


def test_table_stats(client, database):
    _exec(client, database, "CREATE TABLE TS_A (ID INTEGER, V VARCHAR(10)); CREATE TABLE TS_B (ID INTEGER);")
    _exec(client, database, "EXECUTE BLOCK AS DECLARE I INT = 0; BEGIN WHILE (I < 300) DO BEGIN "
                            "INSERT INTO TS_A VALUES (:I, 'x'); I = I + 1; END END")
    _exec(client, database, "UPDATE TS_A SET V = 'y' WHERE ID < 10; DELETE FROM TS_A WHERE ID >= 290; "
                            "INSERT INTO TS_B VALUES (1); SELECT COUNT(*) FROM TS_A;")
    r = client.get(_base(database) + "/monitoring/tables", params={"order_by": "inserts"}).json()
    if not r["available"]:
        pytest.skip(r["reason"])
    rows = {t["table_name"]: t for t in r["tables"]}
    assert rows["TS_A"]["inserts"] >= 300
    assert rows["TS_A"]["updates"] >= 10
    assert rows["TS_A"]["deletes"] >= 10
    assert rows["TS_A"]["seq_reads"] > 0
    assert r["tables"][0]["table_name"] == "TS_A"  # ordered by inserts
    assert not any(t["table_name"].startswith("RDB$") for t in r["tables"])
    r = client.get(_base(database) + "/monitoring/tables", params={"include_system": True}).json()
    assert any(t["table_name"].startswith("RDB$") for t in r["tables"])


def test_compiled_statements(client, database):
    _exec(client, database, "CREATE TABLE CS (ID INTEGER); " + PROC)
    _exec(client, database, "SELECT COUNT(*) FROM CS WHERE ID > 5; SELECT * FROM P_SUM(10);")
    r = client.get(_base(database) + "/monitoring/compiled").json()
    if _major(client, database) < 5:
        assert r["available"] is False and "5.0" in r["reason"]
        return
    assert r["available"]
    texts = [s["sql_text"] or "" for s in r["statements"]]
    assert any("FROM CS WHERE ID > 5" in t for t in texts)
    assert any(s["object_name"] == "P_SUM" and s["object_type_name"] == "procedure" for s in r["statements"])
    r = client.get(_base(database) + "/monitoring/compiled", params={"search": "P_SUM"}).json()
    assert r["statements"] and all("P_SUM" in ((s["sql_text"] or "") + (s["object_name"] or "")).upper()
                                   for s in r["statements"])


def test_long_running_transactions_and_oat(client, database):
    _exec(client, database, "CREATE TABLE LR (ID INTEGER)")
    other = _direct(client, database)
    try:
        cur = other.cursor()
        cur.execute("INSERT INTO LR VALUES (1)")  # keeps a write transaction open
        cur.execute("SELECT CURRENT_TRANSACTION, CURRENT_CONNECTION FROM RDB$DATABASE")
        tx, att = cur.fetchone()
        time.sleep(1.3)
        # create some transactions after it so the gap is visible
        for i in range(5):
            _exec(client, database, f"INSERT INTO LR VALUES ({i})")
        r = client.get(_base(database) + "/monitoring/transactions",
                       params={"long_running": 1, "gap_warn": 3}).json()
        ids = [t["id"] for t in r["long_running"]]
        assert tx in ids
        t = next(t for t in r["transactions"] if t["id"] == tx)
        assert t["attachment_id"] == att and t["age_seconds"] >= 1 and t["writes"] >= 1
        assert r["oat_holders"] and r["oat_holders"][0]["id"] == tx
        assert r["gaps"]["next_oat"] >= 5
        msgs = " ".join(a["message"] for a in r["alerts"])
        assert f"held by transaction {tx}" in msgs
        assert f"Transaction {tx}" in msgs
    finally:
        other.rollback()
        other.close()


def test_lock_wait_view(client, database):
    _exec(client, database, "CREATE TABLE LK (ID INTEGER NOT NULL PRIMARY KEY, V INTEGER); COMMIT; "
                            "INSERT INTO LK VALUES (1, 0);")
    holder, waiter = _direct(client, database), _direct(client, database)
    try:
        hc = holder.cursor()
        hc.execute("UPDATE LK SET V = V + 1 WHERE ID = 1")
        hc.execute("SELECT CURRENT_TRANSACTION FROM RDB$DATABASE")
        holder_tx = hc.fetchone()[0]
        done = []

        def wait_update():
            try:
                waiter.cursor().execute("UPDATE LK SET V = V + 10 WHERE ID = 1")
                done.append("ok")
            except Exception as exc:  # noqa: BLE001
                done.append(str(exc))

        th = threading.Thread(target=wait_update)
        th.start()
        r = None
        for _ in range(30):
            time.sleep(0.2)
            r = client.get(_base(database) + "/monitoring/locks").json()
            if r["waiting"]:
                break
        assert r and r["waiting"], r
        w = r["waiting"][0]
        assert "UPDATE LK" in w["sql_text"].upper()
        assert w["waits"] >= 1
        assert r["lock_print_available"] is False
        if r["table_stats_available"]:
            assert w["tables"] == ["LK"]
            assert [b["id"] for b in w["blockers"]] == [holder_tx]
        holder.commit()
        th.join(10)
        assert done
    finally:
        for c in (holder, waiter):
            try:
                c.rollback()
                c.close()
            except Exception:  # noqa: BLE001
                pass


def test_history_store_and_sampler(client, database):
    sid, dbid = database
    m = _base(database) + "/monitoring"
    r = client.get(m + "/history").json()
    assert r["tracking"] is None and r["samples"] == []
    r = client.put(m + "/history/tracking", json={"enabled": True}).json()
    assert r["tracking"]["db_id"] == dbid
    _exec(client, database, "CREATE TABLE H (ID INTEGER); COMMIT; INSERT INTO H VALUES (1);")
    time.sleep(0.05)
    client.post(m + "/history/sample").raise_for_status()
    r = client.get(m + "/history", params={"hours": 1}).json()
    assert len(r["samples"]) == 2
    s1, s2 = r["samples"]
    assert s2["ts"] > s1["ts"] and s2["inserts"] >= s1["inserts"] + 1
    assert s1["rates"]["inserts"] is None and s2["rates"]["inserts"] > 0
    assert r["tracking"]["last_error"] is None

    # settings + background sampler thread
    st = client.put("/api/monitoring/settings", json={"interval": 1, "retention_hours": 2}).json()
    assert st["settings"]["interval"] == 1 and st["settings"]["retention_hours"] == 2
    assert any(t["db_id"] == dbid for t in st["tracked"])
    deadline = time.time() + 10
    n = 2
    while time.time() < deadline and n < 4:
        time.sleep(0.5)
        n = len(client.get(m + "/history", params={"hours": 1}).json()["samples"])
    assert n >= 4, "background sampler did not store samples"
    client.put("/api/monitoring/settings", json={"interval": 0})
    assert client.put("/api/monitoring/settings", json={"interval": -1}).status_code == 400

    # retention pruning and down-sampling
    from fbadmin.monitoring import history

    store = history.get_store()
    old = {k: 0 for k in history.COLUMNS}
    old["ts"] = time.time() - 5 * 3600
    store.add_sample(sid, dbid, old)
    assert len(store.samples(sid, dbid)) >= 5
    assert store.prune(2) == 1
    assert len(store.samples(sid, dbid, max_points=2)) == 2

    # slow statement log (workload of the index advisor)
    store.add_slow(sid, dbid, "SELECT * FROM H WHERE ID = 1", None, 7.5)
    store.add_slow(sid, dbid, "SELECT  *  FROM H WHERE ID = 1", None, 3.0)
    slow = client.get(m + "/history/slow").json()["statements"]
    assert len(slow) == 1 and slow[0]["seen"] == 2 and slow[0]["max_seconds"] == 7.5

    client.put(m + "/history/tracking", json={"enabled": False})
    client.delete(m + "/history")
    r = client.get(m + "/history").json()
    assert r["tracking"] is None and r["samples"] == []


def test_profiler(client, database):
    m = _base(database) + "/monitoring/profiler"
    _exec(client, database, PROC)
    ov = client.get(m).json()
    if _major(client, database) < 5:
        assert ov["available"] is False and "5.0" in ov["reason"]
        r = client.post(m + "/run", json={"sql": "SELECT * FROM P_SUM(10)"})
        assert r.status_code == 400
        return
    assert ov["available"]
    r = client.post(m + "/run", json={"sql": "SELECT * FROM P_SUM(20000);\nSELECT COUNT(*) FROM RDB$RELATIONS",
                                      "description": "pytest"}).json()
    assert r["error"] is None, r["error"]
    assert r["results"][0]["rows"] == [[sum(range(20000))]]
    pid = r["profile_id"]
    rep = client.get(f"{m}/{pid}").json()
    assert rep["session"]["description"] == "pytest"
    routines = [s for s in rep["statements"] if s["routine_name"] == "P_SUM"]
    assert routines and routines[0]["statement_type"] == "PROCEDURE"
    blocks = [s for s in rep["statements"] if s["statement_type"] == "BLOCK" and "P_SUM" in (s["sql_text"] or "")]
    assert blocks and blocks[0]["total_ns"] > 0
    assert blocks[0]["statement_id"] in rep["roots"]
    assert routines[0]["statement_id"] in blocks[0]["calls_to"] + blocks[0]["children"]
    lines = [ln for ln in rep["lines"] if ln["routine_name"] == "P_SUM"]
    hot = max(lines, key=lambda ln: ln["counter"])
    assert hot["counter"] >= 20000 and hot["total_ns"] > 0
    src = next(s for s in rep["routines"] if s["routine_name"] == "P_SUM")
    assert "WHILE" in src["source"].upper()
    # line numbers map onto the stored source: the most executed line is the loop condition (N + 1 times)
    body = src["source"].split("\n")
    assert "WHILE" in body[hot["line_num"] - 1 - src["line_offset"]].upper()
    assert hot["counter"] == 20001
    others = {body[ln["line_num"] - 1 - src["line_offset"]].strip() for ln in lines if ln["counter"] == 20000}
    assert others == {"S = S + I;", "I = I + 1;"}
    assert any("RDB$RELATIONS" in (x["access_path"] or "") for x in rep["record_sources"])
    assert any(s["profile_id"] == pid for s in client.get(m).json()["sessions"])

    # profile another attachment (e.g. a Query Tool tab)
    other = _direct(client, database)
    try:
        cur = other.cursor()
        cur.execute("SELECT CURRENT_CONNECTION FROM RDB$DATABASE")
        att = cur.fetchone()[0]
        other.commit()
        ov = client.get(m).json()
        assert any(a["id"] == att for a in ov["attachments"])
        pid2 = client.post(m + "/start", json={"attachment_id": att}).json()["profile_id"]
        cur.execute("SELECT * FROM P_SUM(500)")
        cur.fetchall()
        other.commit()
        client.post(m + "/finish", json={"attachment_id": att}).raise_for_status()
        rep2 = client.get(f"{m}/{pid2}").json()
        assert rep2["session"]["attachment_id"] == att
        assert any(ln["routine_name"] == "P_SUM" and ln["counter"] >= 500 for ln in rep2["lines"])
    finally:
        other.close()

    # errors are reported and the session is still flushed
    r = client.post(m + "/run", json={"sql": "SELECT * FROM NO_SUCH_TABLE"}).json()
    assert r["error"] and "NO_SUCH_TABLE" in r["error"]
    assert client.get(f"{m}/{r['profile_id']}").status_code == 200
    client.delete(f"{m}/{pid}").raise_for_status()
    assert client.get(f"{m}/{pid}").status_code == 404
    client.delete(m).raise_for_status()
    assert client.get(m).json()["sessions"] == []


def test_index_advisor(client, database):
    _exec(client, database, """
        CREATE TABLE CUST (ID INTEGER NOT NULL PRIMARY KEY, NAME VARCHAR(40), CITY VARCHAR(40));
        CREATE TABLE ORD (ID INTEGER NOT NULL PRIMARY KEY, CUST_ID INTEGER, STATUS INTEGER, CREATED DATE);
        CREATE TABLE TINY (ID INTEGER, X INTEGER);
    """)
    _exec(client, database, """
        EXECUTE BLOCK AS DECLARE I INT = 0; BEGIN
          WHILE (I < 3000) DO BEGIN
            INSERT INTO CUST VALUES (:I, 'name' || :I, 'city' || MOD(:I, 50));
            INSERT INTO ORD VALUES (:I, MOD(:I, 3000), MOD(:I, 5), DATEADD(:I MINUTE TO DATE '2024-01-01'));
            I = I + 1;
          END
          INSERT INTO TINY VALUES (1, 1);
        END
    """)
    workload = (
        "SELECT * FROM ORD WHERE STATUS = 3 AND CREATED > DATE '2024-01-02';\n"
        "SELECT c.NAME FROM CUST c JOIN ORD o ON o.CUST_ID = c.ID WHERE c.CITY = 'city7';\n"
        "SELECT * FROM CUST WHERE UPPER(NAME) = 'NAME5';\n"
        "SELECT * FROM CUST WHERE NAME LIKE '%5';\n"
        "SELECT * FROM TINY WHERE X = 1;\n"
        "SELECT * FROM CUST WHERE ID = 5;\n"
    )
    r = client.post(_base(database) + "/monitoring/advisor",
                    json={"sources": [], "sql": workload, "min_rows": 1000}).json()
    ddl = {s["ddl"] for s in r["suggestions"]}
    assert "CREATE INDEX IX_ORD_STATUS_CREATED ON ORD (STATUS, CREATED);" in ddl
    # the join is driven differently by the FB3/4 and FB5 optimizers: whichever table is scanned gets an index
    assert ddl & {"CREATE INDEX IX_CUST_CITY ON CUST (CITY);", "CREATE INDEX IX_ORD_CUST_ID ON ORD (CUST_ID);"}
    assert any("COMPUTED BY (UPPER(NAME))" in d for d in ddl)
    assert not any(" ON TINY " in d for d in ddl)
    assert not any("(ID)" in d for d in ddl)
    s = next(s for s in r["suggestions"] if s["table"] == "ORD")
    assert "NATURAL" in s["reason"] and s["rows"] >= 1000
    kinds = {n["kind"] for n in r["notes"]}
    assert "small_table" in kinds and "unsargable" in kinds
    assert len(r["statements"]) == 6

    # apply a suggestion: the statement is no longer reported
    _exec(client, database, "CREATE INDEX IX_CUST_CITY ON CUST (CITY)")
    r = client.post(_base(database) + "/monitoring/advisor",
                    json={"sources": [], "sql": "SELECT c.NAME FROM CUST c WHERE c.CITY = 'city7'"}).json()
    assert r["suggestions"] == []

    # slow statements from the history are part of the workload
    from fbadmin.monitoring import history

    sid, dbid = database
    history.get_store().add_slow(sid, dbid, "SELECT * FROM ORD WHERE CUST_ID = 17", None, 12.0)
    r = client.post(_base(database) + "/monitoring/advisor", json={"sources": ["history", "running"]}).json()
    s = next(s for s in r["suggestions"] if s["table"] == "ORD")
    assert s["columns"] == ["CUST_ID"] and s["total_seconds"] == 12.0


def test_metrics_exporter(client, database):
    sid, dbid = database
    assert client.get("/metrics").status_code == 404  # disabled by default
    client.put("/api/monitoring/settings", json={"metrics_enabled": True}).raise_for_status()
    client.put(_base(database) + "/monitoring/history/tracking", json={"enabled": True})
    r = client.get("/metrics")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/plain")
    text = r.text
    assert "# TYPE firebird_attachments gauge" in text
    assert "# TYPE firebird_page_reads_total counter" in text
    line = re.compile(r'^[a-z_]+\{server="[^"]*",host="[^"]*",database="[^"]*"\} -?[0-9.e+]+$')
    for ln in text.strip().split("\n"):
        assert ln.startswith("# HELP ") or ln.startswith("# TYPE ") or line.match(ln), ln
    assert re.search(r"^fbadmin_database_up\{.*\} 1$", text, re.M)
    assert re.search(r"^firebird_attachments\{.*\} [1-9]\d*$", text, re.M)
    assert client.get("/metrics", params={"all": True}).status_code == 200
    client.put("/api/monitoring/settings", json={"metrics_enabled": False})
    assert client.get("/metrics").status_code == 404
