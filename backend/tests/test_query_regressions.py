"""Roadmap 2.2: query regression tracking - fingerprints, runs and plans over time, slower and changed-plan
statements, accept, alerts, the sampler and the Query Tool as sources."""

from __future__ import annotations

import threading
import time

import pytest

from fbadmin.monitoring import history, regressions as R

from .conftest import requires_server
from .test_alerts import add_webhook, flush, receiver  # noqa: F401 - fixture

S = {**R.DEFAULTS}
DAY = 86400.0


def test_fingerprints():
    a = R.normalize("SELECT * FROM ORDERS WHERE ID = 7 AND NOTE = 'x''y' -- why\n")
    assert a == R.normalize("select *\n  from orders where id=8 and note = 'other'") == \
        "select * from orders where id=? and note=?"
    assert R.normalize('SELECT "Mixed" FROM T') == 'select "Mixed" from t'  # quoted names keep their case
    assert R.normalize("SELECT A FROM T WHERE B IN (1, 2, 3)") == R.normalize("select a from t where b in (4)")
    assert R.normalize("UPDATE T SET A = :new WHERE B = ? /* c */") == "update t set a=? where b=?"
    assert R.normalize("SELECT X1, T2.C FROM T2") == "select x1,t2.c from t2"
    assert R.sql_hash("SELECT 1 FROM T") != R.sql_hash("SELECT 1 FROM U")
    assert R.plan_hash("PLAN (T INDEX (I))\n") == R.plan_hash("  PLAN (T INDEX (I))") and R.plan_hash("") is None


@pytest.fixture
def store(tmp_path):
    s = history.HistoryStore(tmp_path / "m.sqlite")
    yield s
    s.close()


def _runs(store, sql, seconds, plan, start, n, step=600.0, **kw):
    for i in range(n):
        R.record(store, "s", "d", sql, seconds, plan=plan, ts=start + i * step, **kw)


def test_analysis(store):
    now = time.time()
    base = now - 3 * DAY
    # slower, with a new plan: 0.2 s -> 2 s
    _runs(store, "SELECT * FROM ORDERS WHERE CUSTOMER = 1", 0.2, "PLAN (ORDERS INDEX (IX_CUST))", base, 5)
    _runs(store, "SELECT * FROM ORDERS WHERE CUSTOMER = 9", 2.0, "PLAN (ORDERS NATURAL)", now - 3600, 4, 60)
    # a new plan, not slower: shown, not a regression
    _runs(store, "SELECT * FROM ITEMS WHERE ID = 1", 1.0, "PLAN (ITEMS INDEX (A))", base, 4)
    _runs(store, "SELECT * FROM ITEMS WHERE ID = 2", 1.0, "PLAN (ITEMS INDEX (B))", now - 3600, 4, 60)
    # slower, same plan, but too few recent runs
    _runs(store, "SELECT COUNT(*) FROM LOGS", 1.0, None, base, 5)
    _runs(store, "SELECT COUNT(*) FROM LOGS", 9.0, None, now - 3600, 2, 60)
    # slower, same plan
    _runs(store, "DELETE FROM TMP WHERE TS < 5", 1.0, "PLAN (TMP NATURAL)", base, 3)
    _runs(store, "DELETE FROM TMP WHERE TS < 7", 3.5, "PLAN (TMP NATURAL)", now - 7200, 3, 60)
    items = R.analyze(store, "s", "d", S, now=now)
    by = {i["normalized"]: i for i in items}
    o = by["select * from orders where customer=?"]
    assert o["regression"] and o["flags"] == ["slower", "plan_changed"] and o["ratio"] == 10.0
    assert (o["baseline_runs"], o["recent_runs"], o["baseline_median"], o["recent_median"]) == (5, 4, 0.2, 2.0)
    assert o["example"] == "SELECT * FROM ORDERS WHERE CUSTOMER = 1"
    i = by["select * from items where id=?"]
    assert i["flags"] == ["plan_changed"] and not i["regression"]
    assert not by["select count(*) from logs"]["flags"]
    t = by["delete from tmp where ts<?"]
    assert t["regression"] and t["flags"] == ["slower"] and t["ratio"] == 3.5
    assert [x["normalized"] for x in items][:2] == ["select * from orders where customer=?", "delete from tmp where ts<?"]
    assert {x["normalized"] for x in R.analyze(store, "s", "d", S, now=now, only="regressions")} == \
        {"select * from orders where customer=?", "delete from tmp where ts<?"}
    assert len(R.analyze(store, "s", "d", S, now=now, only="flagged")) == 3
    # a stricter factor
    assert not {x["normalized"] for x in R.analyze(store, "s", "d", {**S, "regression_factor": 4.0}, now=now,
                                                   only="regressions")} - {"select * from orders where customer=?"}

    # accept: the current behaviour is the baseline now
    R.accept(store, "s", "d", o["sql_hash"], now=now - 10)
    assert not next(x for x in R.analyze(store, "s", "d", S, now=now) if x["sql_hash"] == o["sql_hash"])["regression"]
    d = R.detail(store, "s", "d", o["sql_hash"], S)
    assert len(d["runs"]) == 9 and [p["runs"] for p in d["plans"]] == [5, 4]
    assert d["plans"][0]["plan"] == "PLAN (ORDERS INDEX (IX_CUST))" and d["plans"][1]["median"] == 2.0
    with pytest.raises(KeyError):
        R.accept(store, "s", "d", "nope")


def test_one_run_per_execution_and_pruning(store, monkeypatch):
    for sec in (5.0, 7.5, 6.0):  # the sampler sees the same execution three times
        R.record(store, "s", "d", "SELECT 1 FROM T", sec, exec_key="12:3:2026-10-30T10:00:00")
    R.record(store, "s", "d", "SELECT 1 FROM T", 5.5, exec_key="12:4:2026-10-30T10:05:00")
    d = R.detail(store, "s", "d", R.sql_hash("SELECT 1 FROM T"), S)
    assert [r["seconds"] for r in d["runs"]] == [7.5, 5.5]
    monkeypatch.setattr(R, "MAX_RUNS", 3)
    _runs(store, "SELECT 2 FROM T2", 1.0, None, time.time() - 100, 5, 1)
    R.record(store, "s", "d", "SELECT 3 FROM T3", 1.0, ts=time.time() - 40 * DAY)
    R.prune(store, 30)
    assert len(R.detail(store, "s", "d", R.sql_hash("SELECT 2 FROM T2"), S)["runs"]) == 3
    with pytest.raises(KeyError):
        R.detail(store, "s", "d", R.sql_hash("SELECT 3 FROM T3"), S)
    store.forget("s", "d")
    assert R.analyze(store, "s", "d", S) == []


def test_alerts(client, receiver, tmp_path):  # noqa: F811
    hook = add_webhook(client, receiver)
    assert client.post("/api/alerts/rules", json={"name": "slow", "event": "query.regression", "channels": [hook["id"]],
                                                  "cooldown_minutes": 0}).status_code == 201
    store = history.get_store()
    now = time.time()
    _runs(store, "SELECT * FROM T WHERE A = 1", 0.1, "PLAN (T INDEX (A))", now - 2 * DAY, 4)
    _runs(store, "SELECT * FROM T WHERE A = 2", 1.0, "PLAN (T NATURAL)", now - 600, 3, 60)
    sent = R.check_alerts(store, "s", "d", S, "app")
    flush()
    assert len(sent) == 1 and len(receiver.requests) == 1
    a = receiver.json()
    assert a["event"] == "query.regression" and a["level"] == "warning" and "10.0x slower" in a["message"]
    assert "different plan" in a["message"]
    assert R.check_alerts(store, "s", "d", S, "app") == []  # the same regression: once
    R.accept(store, "s", "d", R.sql_hash("SELECT * FROM T WHERE A = 1"), now=now - 700)
    R.check_alerts(store, "s", "d", S, "app")
    flush()
    assert len(receiver.requests) == 2 and receiver.json()["level"] == "resolved"


# ------------------------------------------------------------------ with Firebird
def _q(client, sid, dbid):
    s = client.post("/api/query/sessions", json={"server_id": sid, "database_id": dbid})
    assert s.status_code in (200, 201), s.text
    return s.json()["session_id"]


@requires_server
def test_query_tool_and_sampler_sources(client, database):
    sid, dbid = database
    m = f"/api/servers/{sid}/databases/{dbid}/monitoring"
    assert client.get(m + "/regressions").json()["tracking"] is False
    client.put(m + "/history/tracking", json={"enabled": True}).raise_for_status()
    client.put("/api/monitoring/settings", json={"regression_query_ms": 0, "slow_seconds": 1}).raise_for_status()
    q = _q(client, sid, dbid)
    r = client.post(f"/api/query/sessions/{q}/execute", json={"sql": (
        "CREATE TABLE RT (ID INTEGER NOT NULL PRIMARY KEY, V VARCHAR(10));\n"
        "INSERT INTO RT VALUES (1, 'a');\nINSERT INTO RT VALUES (2, 'b');\n"
        "SELECT * FROM RT WHERE ID = 1;\nSELECT * FROM RT WHERE ID = 2;\n"), "autocommit": True})
    assert r.status_code == 200 and r.json()["error"] is None, r.text
    R.wait_idle()
    items = client.get(m + "/regressions").json()["statements"]
    sel = next(i for i in items if i["normalized"] == "select * from rt where id=?")
    assert sel["runs"] == 2 and not sel["flags"]  # DDL isn't tracked; two runs of one statement
    assert {i["normalized"] for i in items} == {"select * from rt where id=?", "insert into rt values(?,?)"}
    d = client.get(m + f"/regressions/{sel['sql_hash']}").json()
    assert {x["source"] for x in d["runs"]} == {"query_tool"}
    assert d["plans"] and "RT" in d["plans"][0]["plan"].upper()  # explained after the run

    # an older baseline with another plan and faster runs: the Query Tool's runs are a regression now
    store = history.get_store()
    for i in range(3):
        R.record(store, sid, dbid, "SELECT * FROM RT WHERE ID = 5", 0.0001, plan="PLAN (RT NATURAL)",
                 ts=time.time() - 2 * DAY + i)
    for _ in range(2):
        client.post(f"/api/query/sessions/{q}/execute", json={"sql": "SELECT * FROM RT WHERE ID = 3", "autocommit": True})
    R.wait_idle()
    reg = client.get(m + "/regressions", params={"only": "flagged"}).json()
    hit = next(i for i in reg["statements"] if i["sql_hash"] == sel["sql_hash"])
    assert "plan_changed" in hit["flags"] and hit["baseline_runs"] == 3 and hit["recent_runs"] == 4
    assert client.post(m + f"/regressions/{sel['sql_hash']}/accept").json() == {"ok": True}
    assert client.get(m + f"/regressions/{sel['sql_hash']}").json()["accepted_at"]

    # the sampler: a statement running for a while, seen by two samples, is one run
    from fbadmin.api.deps import get_state

    path = get_state().store.get_database(sid, dbid).path
    from fbadmin.connections import connect

    conn = connect(get_state().store.get(sid), path, get_state().store.password_for(sid))

    def slow():
        cur = conn.cursor()
        # runs for 3 s whatever the server's speed ('NOW' is the time at each evaluation)
        cur.execute("EXECUTE BLOCK AS DECLARE T0 TIMESTAMP; BEGIN T0 = CAST('NOW' AS TIMESTAMP); "
                    "WHILE (DATEDIFF(MILLISECOND FROM T0 TO CAST('NOW' AS TIMESTAMP)) < 3000) DO T0 = T0; END")
        conn.commit()

    th = threading.Thread(target=slow)
    th.start()
    seen = 0
    end = time.time() + 30
    while th.is_alive() and time.time() < end:
        time.sleep(1.2)
        client.post(m + "/history/sample")
        seen += 1
    th.join(60)
    conn.close()
    items = client.get(m + "/regressions").json()["statements"]
    blk = [i for i in items if i["normalized"].startswith("execute block")]
    assert seen >= 2 and blk, (seen, items)
    assert blk[0]["runs"] == 1 and blk[0]["max_seconds"] >= 1  # one execution, the longest duration seen
    d = client.get(m + f"/regressions/{blk[0]['sql_hash']}").json()
    assert d["runs"][0]["source"] == "sampler" and d["plans"] == []  # an EXECUTE BLOCK has no plan of its own
    client.put("/api/monitoring/settings", json={"regression_query_ms": 200, "slow_seconds": 5})
