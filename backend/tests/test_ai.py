"""AI assistant: disabled by default; with a fake provider, prompts include plan and DDL."""

from pathlib import Path

from fbadmin import ai
from fbadmin.ai.providers import AIProvider

from .conftest import requires_server

SCHEMA = (Path(__file__).parent / "sample_schema.sql").read_text()


class FakeProvider(AIProvider):
    name = "fake"
    last: dict = {}

    def complete(self, system, prompt):
        FakeProvider.last = {"system": system, "prompt": prompt}
        return "ok"


def test_disabled_by_default(client, monkeypatch):
    monkeypatch.delenv("FBADMIN_AI_PROVIDER", raising=False)
    assert client.get("/api/ai/status").json()["enabled"] is False
    assert client.post("/api/ai/ask", json={"task": "ask", "sql": "select 1"}).status_code == 400


def test_unknown_provider(client, monkeypatch):
    monkeypatch.setenv("FBADMIN_AI_PROVIDER", "nope")
    assert client.get("/api/ai/status").json()["enabled"] is False


def test_convert_without_db(client, monkeypatch):
    ai.register_provider("fake", FakeProvider)
    monkeypatch.setenv("FBADMIN_AI_PROVIDER", "fake")
    r = client.post("/api/ai/ask", json={"task": "convert_sql", "sql": "SELECT * FROM t LIMIT 5", "dialect": "PostgreSQL"})
    assert r.json()["answer"] == "ok"
    assert "PostgreSQL" in FakeProvider.last["prompt"] and "LIMIT 5" in FakeProvider.last["prompt"]


@requires_server
def test_plan_context(client, database, monkeypatch):
    ai.register_provider("fake", FakeProvider)
    monkeypatch.setenv("FBADMIN_AI_PROVIDER", "fake")
    sid, dbid = database
    client.post(f"/api/servers/{sid}/databases/{dbid}/execute", json={"sql": SCHEMA})
    r = client.post("/api/ai/ask", json={"task": "suggest_indexes", "server_id": sid, "database_id": dbid,
                                         "sql": "select * from orders where qty = :q"})
    assert r.status_code == 200, r.text
    p = FakeProvider.last["prompt"]
    assert "Detailed plan" in p and "CREATE TABLE ORDERS" in p
    assert "Firebird" in FakeProvider.last["system"]
