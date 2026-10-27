"""Schema drift detection (roadmap 2.2): scheduled comparison with the schema history or another database,
ignored types and names, the report and its script, the schema.drift alert (new, changed, resolved)."""

from __future__ import annotations

import subprocess
import uuid
from pathlib import Path

import pytest
from pydantic import ValidationError

from fbadmin.maintenance import drift

from .conftest import DB_DIR, requires_server
from .test_alerts import add_webhook, flush, receiver  # noqa: F401 - fixture
from .test_maintenance import execute, wait_job
from .test_schema_history import configure, snap


# ------------------------------------------------------------------ units (no server)
def test_strip_comments_and_options():
    ddl = ("CREATE TABLE T (ID INTEGER);\nCOMMENT ON TABLE T IS 'one\nline two';\n"
           "COMMENT ON COLUMN T.ID IS 'x';\nALTER TABLE T ADD CHECK (ID > 0);\n")
    assert drift._strip_comments(ddl) == "CREATE TABLE T (ID INTEGER);\nALTER TABLE T ADD CHECK (ID > 0);\n"
    with pytest.raises(ValidationError):
        drift.DriftOptions(ignore_types=["tablez"])
    with pytest.raises(ValidationError):
        drift.DriftOptions(ref="--upload-pack=x")
    with pytest.raises(ValidationError):
        drift.DriftOptions(ref="main..other")
    o = drift.DriftOptions(ignore_types=["grants", "comments", "grants"], ignore_names=[" tmp_* ", ""], ref=" v1.0 ")
    assert o.ignore_types == ["comments", "grants"] and o.ignore_names == ["tmp_*"] and o.ref == "v1.0"
    assert drift._ignored(("tables", "TMP_LOAD"), o) and drift._ignored(("grants", "T"), o)
    assert not drift._ignored(("tables", "T_TMP"), o)
    items = [{"schema": None, "type": "tables", "name": "A", "status": "extra", "baseline_ddl": None,
              "database_ddl": "CREATE TABLE A (ID INTEGER);\n"}]
    assert drift.fingerprint(items) == drift.fingerprint([dict(items[0])]) != drift.fingerprint([])
    assert drift.fingerprint([{**items[0], "database_ddl": "CREATE TABLE A (ID BIGINT);\n"}]) != drift.fingerprint(items)


# ------------------------------------------------------------------ integration
def schedule(client, sid, dbid, **opts):
    r = client.post("/api/schedules", json={"name": "drift", "kind": "schema_drift", "server_id": sid,
                                            "database_id": dbid, "cron": "0 6 * * *", "drift": opts})
    assert r.status_code == 201, r.text
    return r.json()


def run(client, sched) -> tuple[dict, dict]:
    job = wait_job(client, client.post(f"/api/schedules/{sched['id']}/run").json())
    assert job["status"] == "finished", job["log"]
    h = client.get(f"/api/schedules/{sched['id']}/history").json()[0]
    r = client.get(f"/api/schedules/{sched['id']}/drift")
    assert r.status_code == 200, r.text
    return h, r.json()


def by_name(report) -> dict[str, str]:
    return {i["name"]: i["status"] for i in report["items"]}


@pytest.fixture
def other_db(client, server):
    path = f"{DB_DIR}/fbadmin_test_{uuid.uuid4().hex[:8]}.fdb"
    r = client.post(f"/api/servers/{server['id']}/databases/create", json={"name": "baseline", "path": path})
    assert r.status_code == 201, r.text
    db = r.json()
    yield db["id"]
    client.post(f"/api/servers/{server['id']}/databases/{db['id']}/drop")


@requires_server
def test_drift_against_schema_history(client, database, tmp_path, receiver):  # noqa: F811
    sid, dbid = database
    execute(client, sid, dbid, "CREATE TABLE KEEP_T (ID INTEGER)")
    execute(client, sid, dbid, "CREATE TABLE GONE_T (ID INTEGER NOT NULL PRIMARY KEY)")
    execute(client, sid, dbid, "CREATE TABLE ALTER_T (ID INTEGER)")
    # no history repository yet: refused when the schedule is created
    r = client.post("/api/schedules", json={"name": "d", "kind": "schema_drift", "server_id": sid,
                                            "database_id": dbid, "cron": "0 6 * * *"})
    assert r.status_code == 400 and "schema history" in r.json()["detail"]
    cfg = configure(client, sid, dbid, tmp_path)
    assert snap(client, sid, dbid)["committed"]
    hook = add_webhook(client, receiver)
    rule = client.post("/api/alerts/rules", json={"name": "drift", "event": "schema.drift", "channels": [hook["id"]],
                                                  "cooldown_minutes": 600})
    assert rule.status_code == 201, rule.text
    sched = schedule(client, sid, dbid)
    assert client.get(f"/api/schedules/{sched['id']}/drift").status_code == 404  # never checked

    h, rep = run(client, sched)
    assert h["status"] == "finished" and h["drift"] is False and rep["drift"] is False
    assert rep["baseline"]["kind"] == "history" and rep["baseline"]["ref"] == "main"
    flush()
    assert receiver.requests == []  # no drift, no alert

    # changes made outside the recorded history
    execute(client, sid, dbid, "CREATE TABLE EXTRA_T (ID INTEGER)")
    execute(client, sid, dbid, "DROP TABLE GONE_T")
    execute(client, sid, dbid, "ALTER TABLE ALTER_T ADD X INTEGER")
    h, rep = run(client, sched)
    assert h["status"] == "drift" and h["drift"] is True
    assert h["summary"] == {"missing": 1, "extra": 1, "changed": 1} == rep["summary"]
    names = by_name(rep)
    assert names["EXTRA_T"] == "extra" and names["GONE_T"] == "missing" and names["ALTER_T"] == "changed"
    assert "KEEP_T" not in names
    item = next(i for i in rep["items"] if i["name"] == "ALTER_T")
    assert "X" not in item["baseline_ddl"] and "X INTEGER" in item["database_ddl"]
    assert "DROP TABLE EXTRA_T" in rep["script"] and "CREATE TABLE GONE_T" in rep["script"]
    flush()
    assert len(receiver.requests) == 1
    alert = receiver.json()
    assert alert["event"] == "schema.drift" and alert["level"] == "warning" and "1 missing" in alert["message"]

    # the same drift again: cooling down, no second alert
    run(client, sched)
    flush()
    assert len(receiver.requests) == 1
    # the drift changed: news, despite the cool-down
    execute(client, sid, dbid, "CREATE TABLE EXTRA2_T (ID INTEGER)")
    h, rep = run(client, sched)
    assert h["summary"]["extra"] == 2
    flush()
    assert len(receiver.requests) == 2

    # the report's script makes the database match the baseline: drift resolved
    res = execute(client, sid, dbid, rep["script"])
    assert res.get("error") is None, res
    h, rep = run(client, sched)
    assert h["status"] == "finished" and rep["drift"] is False
    flush()
    assert len(receiver.requests) == 3 and receiver.json()["level"] == "resolved"
    run(client, sched)
    flush()
    assert len(receiver.requests) == 3  # resolved only once

    # a tag as the baseline; the history's own branch moved on
    repo = Path(cfg["resolved_path"])
    subprocess.run(["git", "-C", str(repo), "tag", "approved"], check=True)
    execute(client, sid, dbid, "CREATE TABLE LATER_T (ID INTEGER)")
    assert snap(client, sid, dbid)["committed"]
    r = client.put(f"/api/schedules/{sched['id']}", json={"drift": {"ref": "approved"}})
    assert r.status_code == 200 and r.json()["drift"]["ref"] == "approved"
    h, rep = run(client, sched)
    assert by_name(rep) == {"LATER_T": "extra"} and rep["baseline"]["ref"] == "approved"
    r = client.put(f"/api/schedules/{sched['id']}", json={"drift": {"ref": "no-such-tag"}})
    assert r.status_code == 200
    job = wait_job(client, client.post(f"/api/schedules/{sched['id']}/run").json())
    assert job["status"] == "failed" and "no-such-tag" in "\n".join(job["log"])
    assert client.put(f"/api/schedules/{sched['id']}", json={"drift": {"ref": "-x"}}).status_code == 422

    # deleting the schedule deletes its report
    assert client.delete(f"/api/schedules/{sched['id']}").status_code == 204
    assert drift.load_report(sched["id"]) is None


@requires_server
def test_drift_against_database_and_ignores(client, database, other_db):
    sid, dbid = database
    for d in (dbid, other_db):
        execute(client, sid, d, "CREATE TABLE SHARED_T (ID INTEGER NOT NULL PRIMARY KEY, NAME VARCHAR(20))")
        execute(client, sid, d, "CREATE INDEX IX_SHARED_NAME ON SHARED_T (NAME)")
    # bad baselines are refused
    for opts in ({"baseline": "database"}, {"baseline": "database", "server_id": sid, "database_id": dbid},
                 {"baseline": "database", "server_id": sid, "database_id": "nope"}):
        r = client.post("/api/schedules", json={"name": "d", "kind": "schema_drift", "server_id": sid,
                                                "database_id": dbid, "cron": "0 6 * * *", "drift": opts})
        assert r.status_code in (400, 404), (opts, r.text)
    sched = schedule(client, sid, dbid, baseline="database", server_id=sid, database_id=other_db)
    h, rep = run(client, sched)
    assert h["status"] == "finished" and rep["drift"] is False and rep["baseline"]["kind"] == "database"

    # differences that are ignored: a temporary table, a comment, an extra index
    execute(client, sid, dbid, "CREATE TABLE TMP_LOAD (ID INTEGER)")
    execute(client, sid, dbid, "COMMENT ON TABLE SHARED_T IS 'only here'")
    execute(client, sid, dbid, "CREATE INDEX IX_EXTRA ON SHARED_T (ID, NAME)")
    client.put(f"/api/schedules/{sched['id']}", json={"drift": {
        "baseline": "database", "server_id": sid, "database_id": other_db,
        "ignore_types": ["comments", "indices"], "ignore_names": ["tmp_*"]}})
    h, rep = run(client, sched)
    # the comment is no difference once comments are ignored; the table and the index are ignored ones
    assert rep["drift"] is False and rep["ignored"] == 2 and h["status"] == "finished"
    # without the ignores all three are drift; the comment shows as a changed table
    client.put(f"/api/schedules/{sched['id']}", json={"drift": {
        "baseline": "database", "server_id": sid, "database_id": other_db}})
    h, rep = run(client, sched)
    assert by_name(rep) == {"TMP_LOAD": "extra", "SHARED_T": "changed", "IX_EXTRA": "extra"}
    assert rep["ignored"] == 0 and h["status"] == "drift"
    # a schedule of another kind does not need drift options and ignores them
    r = client.post("/api/schedules", json={"name": "s", "kind": "sweep", "server_id": sid, "database_id": dbid,
                                            "cron": "0 6 * * *"})
    assert r.status_code == 201 and r.json()["drift"] is None


@requires_server
def test_drift_in_server_mode_needs_baseline_access(tmp_path, monkeypatch):
    """A drift check reads the baseline's schema: its author needs read access to the baseline database."""
    from .conftest import HOST, PASSWORD, PORT, USER
    from .test_access_fb import logout
    from .test_security import ADMIN_PW, _client, login

    with _client(tmp_path, monkeypatch, SERVER_MODE="1", ADMIN_USERNAME="root", ADMIN_PASSWORD=ADMIN_PW,
                 LOGIN_RATE="50/60") as c:
        login(c, "root", ADMIN_PW)
        srv = c.post("/api/servers", json={"name": "fb", "host": HOST, "port": PORT, "user": USER,
                                           "password": PASSWORD, "save_password": True, "shared": True}).json()
        sid, dbs = srv["id"], {}
        for name in ("prod", "staging"):
            r = c.post(f"/api/servers/{sid}/databases/create",
                       json={"name": name, "path": f"{DB_DIR}/fbadmin_test_{uuid.uuid4().hex[:8]}.fdb"})
            assert r.status_code == 201, r.text
            dbs[name] = r.json()["id"]
        try:
            for u in ("ops", "lead"):
                assert c.post("/api/admin/users", json={"username": u, "password": f"{u}-password"}).status_code == 201
            for user, db, level in (("ops", dbs["prod"], "admin"), ("ops", dbs["staging"], "none"),
                                    ("lead", dbs["prod"], "admin"), ("lead", dbs["staging"], "read")):
                r = c.post("/api/admin/access/grants", json={"subject_type": "user", "subject": user, "server_id": sid,
                                                              "db_id": db, "level": level})
                assert r.status_code == 201, r.text
            body = {"name": "d", "kind": "schema_drift", "server_id": sid, "database_id": dbs["prod"],
                    "cron": "0 6 * * *", "drift": {"baseline": "database", "server_id": sid,
                                                   "database_id": dbs["staging"]}}
            logout(c)
            login(c, "ops", "ops-password")
            assert c.post("/api/schedules", json=body).status_code in (403, 404)
            logout(c)
            login(c, "lead", "lead-password")
            # passwords of shared servers are never shared: each user saves their own
            assert c.put(f"/api/servers/{sid}", json={"password": PASSWORD}).status_code == 200
            r = c.post("/api/schedules", json=body)
            assert r.status_code == 201, r.text
            h, rep = run(c, r.json())
            assert h["status"] == "finished" and rep["drift"] is False
        finally:
            logout(c)
            login(c, "root", ADMIN_PW)
            for d in dbs.values():
                c.post(f"/api/servers/{sid}/databases/{d}/drop")
    from fbadmin.api import deps

    monkeypatch.undo()  # leave server mode before the state is rebuilt, or it leaks into later tests
    deps.reset_state()
