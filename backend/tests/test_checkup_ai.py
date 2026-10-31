"""AI inspection of the performance check-up: what is sent (findings, numbers, statements, DDL; no data,
no IP addresses), the job, follow-up questions, and the disabled / no-report cases."""

from __future__ import annotations

import time

import firebirdsql

from fbadmin import ai
from fbadmin.ai.providers import AIProvider
from fbadmin.monitoring import checkup as C
from fbadmin.monitoring import checkup_ai as CA

from .conftest import HOST, PASSWORD, PORT, USER, requires_server


class FakeProvider(AIProvider):
    name = "fake"
    calls: list[dict] = []

    def describe(self):
        return {"provider": "fake", "model": "fake-1"}

    def complete(self, system, prompt):
        FakeProvider.calls.append({"system": system, "prompt": prompt})
        return f"## Diagnosis\nAnswer {len(FakeProvider.calls)}\n```sql\nSET STATISTICS INDEX IX_G;\n```"


class Broken(AIProvider):
    name = "broken"

    def complete(self, system, prompt):
        raise ai.AIUnavailable("Rate limited by the Anthropic API; try again shortly")


def test_redact():
    assert CA.redact("APP (app) from 10.0.0.5 and 192.168.1.20/24") == "APP (app) from <ip> and <ip>"
    assert CA.redact("from fe80::1:2:3:4") == "from <ip>"
    assert CA.redact("version 5.0.1, at '10:30:00'") == "version 5.0.1, at '10:30:00'"
    assert CA.redact("2001:db8:0:0:1:0:0:1 and ::1") == "<ip> and <ip>"


def _job(client, job_id, status="finished"):
    for _ in range(300):
        j = client.get(f"/api/jobs/{job_id}").json()
        if j["status"] != "running":
            assert j["status"] == status, j
            return j
        time.sleep(0.2)
    raise AssertionError("job did not end")


@requires_server
def test_ai_inspection(client, database, monkeypatch):
    sid, dbid = database
    base = f"/api/servers/{sid}/databases/{dbid}/monitoring/checkup"
    path = client.get(f"/api/servers/{sid}").json()["databases"][0]["path"]
    FakeProvider.calls = []
    try:
        # off by default
        monkeypatch.delenv("FBADMIN_AI_PROVIDER", raising=False)
        r = client.post(base + "/ai")
        assert r.status_code == 503 and "FBADMIN_AI_PROVIDER" in r.json()["detail"]
        ai.register_provider("fake", FakeProvider)
        monkeypatch.setenv("FBADMIN_AI_PROVIDER", "fake")
        # no report yet
        r = client.post(base + "/ai")
        assert r.status_code == 400 and "check-up" in r.json()["detail"]

        con = firebirdsql.connect(host=HOST, port=PORT, database=path, user=USER, password=PASSWORD)
        cur = con.cursor()
        cur.execute("CREATE TABLE ORDERS (ID INTEGER NOT NULL PRIMARY KEY, CUSTOMER INTEGER, SECRET VARCHAR(20))")
        con.commit()
        cur.execute("CREATE INDEX IX_G ON ORDERS (CUSTOMER)")
        con.commit()
        cur.execute("EXECUTE BLOCK AS DECLARE I INTEGER = 0; BEGIN WHILE (I < 500) DO BEGIN "
                    "INSERT INTO ORDERS VALUES (:I, MOD(:I, 7), 'top-secret-value'); I = I + 1; END END")
        con.commit()
        con.close()
        _job(client, client.post(base, json={"gstat": True}).json()["id"])
        rep = client.get(base).json()["report"]
        assert rep["gstat_tables"][0]["name"] == "ORDERS" and rep["gstat_tables"][0]["total_records"] == 500

        # a slow statement in the history, with an address in the evidence of a finding
        from fbadmin.monitoring import history

        history.get_store().add_slow(sid, dbid, "SELECT * FROM ORDERS WHERE CUSTOMER = 3", "PLAN (ORDERS NATURAL)", 4.5)
        with C._lock:
            C._reports[(sid, dbid)]["findings"][0]["evidence"].append({"label": "Held by", "value": "APP from 10.1.2.3"})

        pv = client.post(base + "/ai/preview").json()
        p = pv["prompt"]
        assert "Firebird" in pv["system"] and "## Diagnosis" in pv["system"]
        assert "1. [warning] 2 index(es) have no selectivity although their table has rows" in p
        assert "fix buttons offered: Recompute selectivity of 2 index(es)" in p
        assert "CREATE TABLE ORDERS" in p and "IX_G" in p
        assert "SELECT * FROM ORDERS WHERE CUSTOMER = 3" in p and "PLAN (ORDERS NATURAL)" in p
        assert '"total_records": 500' in p
        assert "10.1.2.3" not in p and "<ip>" in p
        assert "top-secret-value" not in p  # never table data
        assert p.endswith("Inspect this check-up and give your diagnosis and recommendations.")

        j = client.post(base + "/ai")
        assert j.status_code == 202, j.text
        _job(client, j.json()["id"])
        assert FakeProvider.calls[-1]["prompt"] == p
        items = client.get(base).json()["report"]["ai"]
        assert len(items) == 1 and items[0]["answer"].startswith("## Diagnosis\nAnswer 1")
        assert items[0]["question"] is None and items[0]["model"] == "fake-1"

        # a follow-up sees the earlier answer
        _job(client, client.post(base + "/ai", json={"question": "Is the cache big enough?"}).json()["id"])
        last = FakeProvider.calls[-1]["prompt"]
        assert "Your earlier inspection:\n## Diagnosis\nAnswer 1" in last
        assert last.endswith("Follow-up question from the administrator: Is the cache big enough?")
        items = client.get(base).json()["report"]["ai"]
        assert [i["question"] for i in items] == [None, "Is the cache big enough?"]
        # a new inspection (no question) starts over
        _job(client, client.post(base + "/ai").json()["id"])
        assert len(client.get(base).json()["report"]["ai"]) == 1

        # a provider error fails the job with its message
        ai.register_provider("broken", Broken)
        monkeypatch.setenv("FBADMIN_AI_PROVIDER", "broken")
        f = _job(client, client.post(base + "/ai").json()["id"], status="failed")
        assert "Rate limited" in f["error"]
        # a new check-up drops the answers
        _job(client, client.post(base, json={"gstat": False}).json()["id"])
        assert "ai" not in client.get(base).json()["report"]
    finally:
        with C._lock:
            C._reports.clear()
            C._jobs.clear()
