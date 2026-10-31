"""Roadmap 2.3: the guided performance check-up - findings from synthetic facts, and a real database with
indices loaded while empty, a stuck OAT, record versions, sweep settings, a lock wait and the fix actions."""

from __future__ import annotations

import re
import threading
import time
from pathlib import Path

import firebirdsql
import pytest

from fbadmin.monitoring import checkup as C

from .conftest import HOST, PASSWORD, PORT, USER, requires_server


def _tx(**kw):
    base = {"id": 1, "attachment_id": 1, "username": "APP", "remote_process": "/usr/bin/app",
            "remote_address": "10.0.0.5", "age_seconds": 7200.0, "writes": 0, "is_self": False, "is_system": False}
    return base | kw


def _facts(**kw):
    f = {
        "database": {"page_size": 8192, "pages": 1_000_000, "page_buffers": 2048, "sweep_interval": 20000,
                     "backup_state": 0, "page_reads": 0, "page_fetches": 0, "record_waits": 0, "record_conflicts": 0},
        "transactions": {"gaps": {"next_oat": 5, "oat_oit": 1}, "oat_holders": [], "long_running": []},
        "locks": {"waiting": []},
        "indices": [],
        "config": {},
        "sweep_scheduled": False,
    }
    f.update(kw)
    return f


def codes(findings):
    return [x["code"] for x in findings]


def test_healthy_database_has_no_findings():
    assert C.analyze(_facts()) == []


def test_ranking_and_transactions():
    holder = _tx(id=100, attachment_id=7)
    other = _tx(id=120, attachment_id=8, age_seconds=4000.0)
    f = _facts(transactions={"gaps": {"next_oat": C.GAP_WARN * 20, "oat_oit": 1},
                             "oat_holders": [holder], "long_running": [holder, other]},
               indices=[{"schema": None, "name": "IX_A", "table": "T", "stored": 0.0, "inactive": False,
                         "has_rows": True, "segments": 1, "expression": False}],
               history={"tracked": False})
    out = C.analyze(f)
    assert codes(out) == ["oat_stuck", "selectivity_never", "long_transactions", "history_off"]
    assert [x["rank"] for x in out] == [1, 2, 3, 4]
    oat = out[0]
    assert oat["severity"] == "critical" and oat["params"]["gap"] == f"{C.GAP_WARN * 20:,}"
    assert "transaction 100" in oat["evidence"][0]["value"] and "APP (app) from 10.0.0.5" in oat["evidence"][0]["value"]
    assert {"kind": "disconnect", "label": "Disconnect attachment {attachment_id}",
            "confirm": "Disconnect attachment {attachment_id}? Its transaction is rolled back.",
            "params": {"attachment_id": 7}} in oat["actions"]
    # the holder is not reported twice
    assert out[2]["params"]["count"] == 1 and "transaction 120" in out[2]["evidence"][0]["label"]
    assert out[0]["text"]["title"] == f"The oldest active transaction is stuck {C.GAP_WARN * 20:,} transactions behind"
    rec = out[1]["actions"][0]
    assert rec["kind"] == "recompute_statistics" and rec["params"]["indices"] == [{"name": "IX_A", "schema": None}]


def test_sweep_settings():
    stuck = _facts(transactions={"gaps": {"next_oat": 5, "oat_oit": C.GAP_WARN * 2}, "oat_holders": [], "long_running": []})
    stuck["database"]["sweep_interval"] = 0
    f = C.analyze(stuck)
    assert codes(f) == ["oit_stuck"]
    assert [a["kind"] for a in f[0]["actions"]] == ["sweep", "set_sweep_interval"]
    assert f[0]["actions"][1]["params"]["value"] == C.SUGGESTED_SWEEP_INTERVAL
    # automatic sweep on but not keeping up: only "sweep now"
    stuck["database"]["sweep_interval"] = 1000
    f = C.analyze(stuck)
    assert [a["kind"] for a in f[0]["actions"]] == ["sweep"] and "1,000" in f[0]["text"]["advice"]
    # no gap, automatic sweep off, nothing scheduled
    off = _facts()
    off["database"]["sweep_interval"] = 0
    assert codes(C.analyze(off)) == ["sweep_off"]
    assert C.analyze({**off, "sweep_scheduled": True}) == []


def test_page_cache():
    f = _facts()
    f["database"].update(page_reads=30_000, page_fetches=100_000)
    out = C.analyze(f)
    assert codes(out) == ["page_cache"] and out[0]["severity"] == "warning"
    assert out[0]["params"]["miss"] == "30.0" and out[0]["params"]["when"] == "since the database was opened"
    a = out[0]["actions"][0]
    assert a["kind"] == "set_page_buffers" and a["params"]["value"] == 16384 and a["params"]["size"] == "128 MB"
    # the history window wins when it has enough activity
    out = C.analyze({**f, "window": {"page_reads": 100, "page_fetches": 1_000_000}})
    assert out == []
    # the whole database fits: no finding; too little activity: no finding
    f2 = _facts()
    f2["database"].update(page_buffers=2_000_000, page_reads=30_000, page_fetches=100_000)
    assert C.analyze(f2) == []
    f3 = _facts()
    f3["database"].update(page_reads=300, page_fetches=1000)
    assert C.analyze(f3) == []
    # classic: per-connection cache, a lower cap
    out = C.analyze({**f, "config": {"ServerMode": "Classic"}})
    assert out[0]["actions"][0]["params"]["value"] == 16384 and "Classic" in out[0]["text"]["advice"]


def test_locks():
    w = {"id": 5, "attachment_id": 3, "transaction_id": 9, "age_seconds": 12.0, "sql_text": "UPDATE T SET A = 1",
         "username": "BOB", "remote_process": None, "tables": ["T"], "lock_timeout": -1,
         "blockers": [{"id": 8, "attachment_id": 2, "username": "ALICE"}]}
    out = C.analyze(_facts(locks={"waiting": [w]}))
    assert codes(out) == ["lock_waits_now"]
    assert "blocked by transaction 8 (ALICE)" in out[0]["evidence"][0]["value"]
    assert [a["kind"] for a in out[0]["actions"]] == ["open", "disconnect"]
    f = _facts()
    f["database"].update(record_waits=5000, record_conflicts=20)
    out = C.analyze(f)
    assert codes(out) == ["lock_contention"] and out[0]["severity"] == "info"
    out = C.analyze({**f, "window": {"record_waits": 10, "record_conflicts": 50}})
    assert out[0]["severity"] == "warning" and out[0]["params"]["when"] == "in the last hour"


def test_index_selectivity_and_inactive():
    ix = [
        {"schema": None, "name": "OK", "table": "T", "stored": 0.02, "actual": 0.02, "nodes": 5000, "inactive": False},
        {"schema": None, "name": "OLD", "table": "T", "stored": 0.5, "actual": 0.001, "nodes": 5000, "inactive": False},
        {"schema": None, "name": "TINY", "table": "U", "stored": 0.5, "actual": 0.01, "nodes": 50, "inactive": False},
        {"schema": None, "name": "EMPTY", "table": "V", "stored": 0.0, "has_rows": False, "inactive": False},
        {"schema": "APP", "name": "OFF", "table": "W", "stored": 0.1, "inactive": True},
    ]
    out = C.analyze(_facts(indices=ix))
    assert codes(out) == ["selectivity_outdated", "inactive_indices"]
    assert out[0]["evidence"] == [{"label": "OLD", "value": "stored 0.5, actual 0.001"}]
    assert out[1]["actions"][0]["params"]["sql"] == 'ALTER INDEX APP.OFF ACTIVE;'
    assert out[1]["evidence"][0]["label"] == "APP.OFF"


def test_gstat_findings():
    stats = {"header": {}, "tables": [
        {"name": "HOT", "total_records": 1000, "version_ratio": 1.5, "total_versions": 1500, "max_versions": 9,
         "fragment_ratio": 0, "indices": [{"name": "IX_FLAG", "depth": 2, "nodes": 5000, "max_dup": 4900}]},
        {"name": "WIDE", "total_records": 1000, "version_ratio": 0, "fragment_ratio": 0.5, "total_fragments": 500,
         "indices": []},
    ]}
    out = C.analyze(_facts(gstat=stats))
    assert codes(out) == ["record_versions", "storage", "unselective_indices"]
    assert out[0]["severity"] == "critical" and out[0]["params"]["count"] == 1
    assert [a["kind"] for a in out[0]["actions"]] == ["sweep", "open"]
    assert out[2]["evidence"][0]["label"] == "IX_FLAG"


def test_history_findings():
    regs = [{"normalized": "select * from t where a=?", "ratio": 8.0}]
    sugg = [{"table": "ORDERS", "columns": ["CUSTOMER"], "ddl": "CREATE INDEX IX ON ORDERS (CUSTOMER)",
             "total_seconds": 12.0}]
    out = C.analyze(_facts(history={"tracked": True, "regressions": regs, "advisor": sugg}))
    assert codes(out) == ["regressions", "missing_indices"]
    assert out[1]["actions"][1] == {"kind": "query_tool", "label": "Open the CREATE INDEX statements in the Query Tool",
                                    "confirm": None, "params": {"sql": "CREATE INDEX IX ON ORDERS (CUSTOMER);"}}


def test_nbackup():
    f = _facts()
    f["database"]["backup_state"] = 1
    assert codes(C.analyze(f)) == ["nbackup_locked"]


def _all_findings():
    """Every finding and wording variant the analysis can produce."""
    holder = _tx(id=100, attachment_id=7)
    w = {"id": 5, "attachment_id": 3, "sql_text": "U", "username": "B", "tables": ["T"],
         "blockers": [{"id": 8, "attachment_id": 2, "username": "A"}]}
    stats = {"header": {}, "tables": [
        {"name": "HOT", "total_records": 1000, "version_ratio": 1.5, "max_versions": 9, "fragment_ratio": 0.5,
         "indices": [{"name": "IX", "depth": 5, "nodes": 5000, "max_dup": 4900}]}]}
    ix = [{"schema": None, "name": "A", "table": "T", "stored": 0.0, "has_rows": True, "inactive": False},
          {"schema": None, "name": "B", "table": "T", "stored": 0.5, "actual": 0.001, "nodes": 5000, "inactive": False},
          {"schema": None, "name": "C", "table": "T", "stored": 0.5, "inactive": True}]
    hist = {"tracked": False, "regressions": [{"normalized": "x", "ratio": 2}],
            "advisor": [{"table": "T", "columns": ["A"], "ddl": "CREATE INDEX I ON T (A)"}]}
    out = []
    for oat_gap, sweep, mode, state in ((C.GAP_WARN * 20, 0, "Classic", 1), (1, 1000, "", 2), (1, 0, "", 0)):
        f = _facts(transactions={"gaps": {"next_oat": oat_gap, "oat_oit": C.GAP_WARN * 2 if state != 0 else 1},
                                 "oat_holders": [holder], "long_running": [holder, _tx(id=120)]},
                   locks={"waiting": [w]}, indices=ix, gstat=stats, history=hist, config={"ServerMode": mode})
        f["database"].update(sweep_interval=sweep, backup_state=state, page_reads=30_000, page_fetches=100_000,
                             record_waits=5000, record_conflicts=500)
        out += C.analyze(f)
    return out


def test_ui_has_every_message():
    """Titles, advice and action labels are translated in the UI from frontend/src/features/checkup/messages.ts."""
    ui = (Path(__file__).parents[2] / "frontend/src/features/checkup/messages.ts").read_text()
    ui_keys = {m.group(1).replace('\\"', '"') for m in re.finditer(r'N_\("((?:[^"\\]|\\.)*)"\)', ui)}
    used = set()
    found = _all_findings()
    for f in found:
        used |= {f["title"], f["advice"]} | {a["label"] for a in f["actions"]} | {a["confirm"] for a in f["actions"] if a["confirm"]}
        used |= {e["label"] for e in f["evidence"] if e["label"] in ("Held by", "Cache", "Database", "Reads / fetches")}
        used.add(f["params"].get("when", "in the last hour"))
    assert set(C.WEIGHT) == {f["code"] for f in found}
    assert not used - ui_keys, sorted(used - ui_keys)


# ============================================================ real server
def _connect(path):
    return firebirdsql.connect(host=HOST, port=PORT, database=path, user=USER, password=PASSWORD)


def _wait(client, sid, dbid, job_id, timeout=120):
    end = time.time() + timeout
    while time.time() < end:
        j = client.get(f"/api/jobs/{job_id}").json()
        if j["status"] != "running":
            assert j["status"] == "finished", j
            return j
        time.sleep(0.3)
    raise AssertionError("job did not finish")


def _checkup(client, sid, dbid, gstat=True):
    r = client.post(f"/api/servers/{sid}/databases/{dbid}/monitoring/checkup", json={"gstat": gstat})
    assert r.status_code == 202, r.text
    _wait(client, sid, dbid, r.json()["id"])
    rep = client.get(f"/api/servers/{sid}/databases/{dbid}/monitoring/checkup").json()["report"]
    assert all(c["status"] == "done" for c in rep["checks"]), rep["checks"]
    return rep, {f["code"]: f for f in rep["findings"]}


@requires_server
def test_checkup_on_a_real_database(client, database, monkeypatch):
    sid, dbid = database
    base = f"/api/servers/{sid}/databases/{dbid}"
    path = client.get(f"/api/servers/{sid}").json()["databases"][0]["path"]
    assert client.get(base + "/monitoring/checkup").json() == {"report": None, "job": None}
    con = _connect(path)
    cur = con.cursor()
    cur.execute("CREATE TABLE T (ID INTEGER NOT NULL PRIMARY KEY, G INTEGER, A INTEGER, B INTEGER)")
    con.commit()
    cur.execute("CREATE INDEX IX_G ON T (G)")
    cur.execute("CREATE INDEX IX_AB ON T (A, B)")
    cur.execute("CREATE TABLE EMPTY_T (ID INTEGER)")
    cur.execute("CREATE INDEX IX_EMPTY ON EMPTY_T (ID)")
    con.commit()
    cur.execute("EXECUTE BLOCK AS DECLARE I INTEGER = 0; BEGIN WHILE (I < 3000) DO BEGIN "
                "INSERT INTO T VALUES (:I, MOD(:I, 50), MOD(:I, 10), :I); I = I + 1; END END")
    con.commit()
    con.close()

    # quick check (no gstat): selectivity 0 on a table with rows, history off
    rep, f = _checkup(client, sid, dbid, gstat=False)
    assert not rep["gstat"] and rep["facts"]["indices"] == 4 and rep["facts"]["page_buffers"] > 0
    never = f["selectivity_never"]
    names = {e["label"] for e in never["evidence"]}
    assert names == {"RDB$PRIMARY1", "IX_G", "IX_AB"}  # not IX_EMPTY: its table is empty
    assert "history_off" in f and "oat_stuck" not in f
    assert rep["summary"]["warning"] >= 1

    # the fix action
    act = never["actions"][0]
    r = client.post(base + "/monitoring/checkup/statistics", json={"indices": act["params"]["indices"]})
    assert r.status_code == 202, r.text
    _wait(client, sid, dbid, r.json()["id"])
    bad = client.post(base + "/monitoring/checkup/statistics", json={"indices": [{"name": "NOPE"}]})
    j = _wait_failed(client, bad.json()["id"])
    assert "Unknown index" in j["error"]

    # with gstat: the recomputed selectivity matches the data
    rep, f = _checkup(client, sid, dbid)
    assert rep["gstat"] and "selectivity_never" not in f and "selectivity_outdated" not in f

    # the data changes a lot: stored selectivity is outdated (only gstat sees it)
    con = _connect(path)
    cur = con.cursor()
    cur.execute("UPDATE T SET G = ID")
    con.commit()
    con.close()
    rep, f = _checkup(client, sid, dbid)
    out = f["selectivity_outdated"]
    assert [e["label"] for e in out["evidence"]] == ["IX_G"]
    assert "stored 0.02" in out["evidence"][0]["value"]

    # sweep off and nothing scheduled
    r = client.put(base + "/maintenance/properties", json={"sweep_interval": 0})
    assert r.status_code == 200, r.text
    rep, f = _checkup(client, sid, dbid, gstat=False)
    assert rep["facts"]["sweep_interval"] == 0 and "sweep_off" in f
    sch = client.post("/api/schedules", json={"name": "nightly sweep", "kind": "sweep", "server_id": sid,
                                              "database_id": dbid, "cron": "0 3 * * *"})
    assert sch.status_code in (200, 201), sch.text
    rep, f = _checkup(client, sid, dbid, gstat=False)
    assert "sweep_off" not in f
    client.delete(f"/api/schedules/{sch.json()['id']}")

    # a stuck OAT with record versions behind it
    monkeypatch.setattr(C, "GAP_WARN", 5)
    holder = _connect(path)
    try:
        hc = holder.cursor()
        hc.execute("SELECT COUNT(*) FROM T")
        hc.fetchall()
        con = _connect(path)
        cur = con.cursor()
        for i in range(10):
            cur.execute(f"UPDATE T SET B = B + {i + 1}")
            con.commit()
        con.close()
        rep, f = _checkup(client, sid, dbid)
        assert "oat_stuck" in f, (rep["facts"], list(f))
        oat = f["oat_stuck"]
        assert rep["findings"][0]["code"] == "oat_stuck"
        disc = next(a for a in oat["actions"] if a["kind"] == "disconnect")
        assert disc["params"]["attachment_id"] > 0
        rv = f["record_versions"]
        assert rv["evidence"][0]["label"] == "T"
        assert "only once the oldest active transaction moves on" in rv["text"]["advice"]
    finally:
        holder.close()


def _wait_failed(client, job_id):
    for _ in range(100):
        j = client.get(f"/api/jobs/{job_id}").json()
        if j["status"] != "running":
            assert j["status"] == "failed", j
            return j
        time.sleep(0.2)
    raise AssertionError("job did not end")


@requires_server
def test_lock_wait_is_reported(client, database):
    sid, dbid = database
    base = f"/api/servers/{sid}/databases/{dbid}"
    path = client.get(f"/api/servers/{sid}").json()["databases"][0]["path"]
    con = _connect(path)
    cur = con.cursor()
    cur.execute("CREATE TABLE T (ID INTEGER NOT NULL PRIMARY KEY, A INTEGER)")
    con.commit()
    cur.execute("INSERT INTO T VALUES (1, 0)")
    con.commit()
    cur.execute("UPDATE T SET A = 1 WHERE ID = 1")  # not committed
    waiter = _connect(path)
    done = threading.Event()

    def wait():
        try:
            c = waiter.cursor()
            c.execute("UPDATE T SET A = 2 WHERE ID = 1")  # waits (default: WAIT, no timeout)
        except Exception:  # noqa: BLE001 - conflict after the holder rolls back... or commits
            pass
        finally:
            done.set()

    th = threading.Thread(target=wait, daemon=True)
    th.start()
    try:
        time.sleep(1.5)
        rep = None
        for _ in range(5):
            rep, f = _checkup(client, sid, dbid, gstat=False)
            if "lock_waits_now" in f:
                break
            time.sleep(1)
        w = f["lock_waits_now"]
        assert w["params"]["count"] == 1
        assert "UPDATE T SET A = 2" in w["evidence"][0]["sql"]
        assert any(a["kind"] == "disconnect" for a in w["actions"])
    finally:
        con.rollback()
        done.wait(30)
        th.join(5)
        for c in (waiter, con):  # the pure-Python driver may report the waiter's socket as gone
            try:
                c.rollback()
                c.close()
            except Exception:  # noqa: BLE001
                pass
    _ = base


@pytest.fixture(autouse=True)
def _forget_reports():
    yield
    with C._lock:
        C._reports.clear()
        C._jobs.clear()
