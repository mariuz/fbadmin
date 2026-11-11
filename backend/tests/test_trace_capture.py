"""Continuous trace capture (roadmap 2.4): the always-on trace session of a database feeds query regressions and
the slow statement log; the sampler and the Query Tool leave runs to it; it needs the history, restarts after
a failure with a backoff, and stops with the history."""

from __future__ import annotations

import time
import uuid

from fbadmin.monitoring import regressions as R
from fbadmin.monitoring import trace_capture as TC

from .conftest import requires_server
from .test_maintenance import execute


def test_on_event_filters_limits_and_records(monkeypatch):
    calls: list[tuple] = []

    class Store:
        def get_settings(self):
            return {"slow_seconds": 2}

        def add_slow(self, *a):
            calls.append(("slow", *a))

    monkeypatch.setattr("fbadmin.monitoring.history.get_store", lambda: Store())
    monkeypatch.setattr(R, "record", lambda store, sid, dbid, sql, sec, **kw: calls.append(("run", sql, sec, kw["source"])))
    monkeypatch.setattr(TC, "PER_MINUTE", 2)
    cap = TC.Capture("s", "d")
    cap.threshold_ms = 100
    ev = {"type": "EXECUTE_STATEMENT_FINISH", "sql": "SELECT 1 FROM T", "duration_ms": 2500, "plan": "Select Expression"}
    cap.on_event({**ev, "type": "FAILED EXECUTE_STATEMENT_FINISH"})
    cap.on_event({**ev, "duration_ms": 50})        # below the threshold
    cap.on_event({**ev, "sql": None})               # no text
    cap.on_event(ev)                                # recorded, and slow
    cap.on_event({**ev, "duration_ms": 300})        # recorded, not slow
    cap.on_event(ev)                                # over the per-minute limit
    assert [c[0] for c in calls] == ["run", "slow", "run"]
    assert calls[0] == ("run", "SELECT 1 FROM T", 2.5, "trace")
    assert (cap.captured, cap.slow, cap.dropped) == (2, 1, 1)


def test_trace_config():
    cfg = TC.trace_config("/data/app.fdb", 250)
    assert "log_statement_finish = true" in cfg and "explain_plan = true" in cfg and "time_threshold = 250" in cfg
    assert cfg.startswith("database = %[\\\\/]app.fdb")


@requires_server
def test_continuous_capture(client, database, monkeypatch):
    sid, dbid = database
    base = f"/api/servers/{sid}/databases/{dbid}/monitoring"
    r = client.get(base + "/trace-capture").json()
    assert r["enabled"] is False and r["state"] == "stopped" and r["threshold_ms"] == TC.DEFAULT_THRESHOLD_MS
    r = client.put(base + "/trace-capture", json={"enabled": True, "threshold_ms": 1})
    assert r.status_code == 400 and "history" in r.json()["detail"]  # needs the history

    client.put(base + "/history/tracking", json={"enabled": True})
    try:
        r = client.put(base + "/trace-capture", json={"enabled": True, "threshold_ms": 1})
        assert r.status_code == 200, r.text
        cap = r.json()
        assert cap["enabled"] and cap["state"] == "running" and cap["session_id"], cap
        assert TC.active(sid, dbid)
        sessions = client.get(f"/api/servers/{sid}/trace/sessions").json()["server"]
        assert any((s["name"] or "").startswith("fbadmin continuous capture") for s in sessions)

        marker = f"m{uuid.uuid4().hex[:6]}"
        sql = f"SELECT COUNT(*) AS {marker} FROM RDB$FIELDS a CROSS JOIN RDB$FIELDS b"
        execute(client, sid, dbid, sql)
        execute(client, sid, dbid, sql)
        found = None
        for _ in range(60):
            items = client.get(base + "/regressions", params={"only": "all"}).json()["statements"]
            found = next((i for i in items if marker.lower() in (i.get("example") or i.get("normalized") or "").lower()),
                         None)
            if found:
                d = client.get(base + f"/regressions/{found['sql_hash']}").json()
                if len(d["runs"]) >= 2:
                    break
            time.sleep(0.25)
        assert found, "the trace did not record the statement"
        assert {r["source"] for r in d["runs"]} == {"trace"}  # the Query Tool left them to the trace
        assert len(d["runs"]) == 2
        assert d["plans"] and "RDB$FIELDS" in d["plans"][0]["plan"]
        status = client.get(base + "/trace-capture").json()
        assert status["captured"] >= 2 and status["last_event_at"]

        # another threshold restarts the session
        r = client.put(base + "/trace-capture", json={"enabled": True, "threshold_ms": 60000}).json()
        assert r["threshold_ms"] == 60000 and r["state"] == "running" and r["session_id"] != cap["session_id"]

        # the session ends from outside: the supervisor starts a new one
        old = r["session_id"]
        with TC._lock:
            TC._caps[(sid, dbid)].started_at -= TC.MIN_LIFE + 1  # it lived long enough: no backoff
        client.post(f"/api/servers/{sid}/trace/server-sessions/{old}/stop")
        for _ in range(50):
            if not TC.active(sid, dbid):
                break
            time.sleep(0.2)
        assert not TC.active(sid, dbid)
        TC.check_all()
        st = client.get(base + "/trace-capture").json()
        assert st["state"] == "running" and st["session_id"] != old and st["sessions"] == 2

        # turning the history off stops it
        client.put(base + "/history/tracking", json={"enabled": False})
        assert not TC.active(sid, dbid)
        assert client.get(base + "/trace-capture").json()["state"] == "stopped"
    finally:
        TC.stop(sid, dbid)
        client.delete(base + "/history")
    assert TC.config(sid, dbid)["enabled"] is False


@requires_server
def test_start_failure_backs_off(client, database, monkeypatch):
    sid, dbid = database
    base = f"/api/servers/{sid}/databases/{dbid}/monitoring"
    client.put(base + "/history/tracking", json={"enabled": True})
    try:
        monkeypatch.setattr(TC, "trace_config", lambda path, ms: "database\n{\n  enabled = maybe\n}\n")
        r = client.put(base + "/trace-capture", json={"enabled": True, "threshold_ms": 500}).json()
        assert r["state"] in ("waiting", "running")
        if r["state"] == "waiting":  # rejected at start: retried later
            assert r["error"] and r["retry_at"] > time.time() + 20
            TC.check_all()  # not yet
            assert client.get(base + "/trace-capture").json()["sessions"] == 1
    finally:
        TC.stop(sid, dbid)
        client.delete(base + "/history")
