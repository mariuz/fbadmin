"""Alerts (milestone 1.1): channels, rules, delivery, condition checks and the scheduler hooks
(test restores, validation errors). Webhook and Slack deliveries go to a real local HTTP server;
email goes to a real SMTP server when FBADMIN_TEST_SMTP_HOST is set (scripts/smtp-test-server.sh)."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import threading
import time
import urllib.request
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import firebirdsql
import pytest

from .conftest import DB_DIR, HOST, PASSWORD, PORT, USER, requires_server
from .test_maintenance import fill, ok_job, wait_job


class Receiver:
    """A local HTTP endpoint that records what it is sent."""

    def __init__(self, status: int = 200):
        self.requests: list[dict] = []
        self.status = status
        outer = self

        class H(BaseHTTPRequestHandler):
            def do_POST(self):  # noqa: N802
                body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
                outer.requests.append({"path": self.path, "headers": dict(self.headers), "body": body})
                self.send_response(outer.status)
                self.end_headers()
                self.wfile.write(b"ok" if outer.status < 400 else b"invalid_token")

            def log_message(self, *a):
                pass

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def json(self, i: int = -1) -> dict:
        return json.loads(self.requests[i]["body"])

    def close(self) -> None:
        self.httpd.shutdown()


@pytest.fixture
def receiver():
    r = Receiver()
    yield r
    r.close()


def flush():
    from fbadmin.alerts.engine import get_engine

    get_engine().flush()


def add_webhook(client, receiver, secret="s3cret"):
    r = client.post("/api/alerts/channels", json={"name": "hook", "kind": "webhook",
                                                  "settings": {"url": receiver.url + "/hook", "secret": secret}})
    assert r.status_code == 201, r.text
    return r.json()


# ------------------------------------------------------------------ units
def test_channels_masking_validation_and_env(client, monkeypatch):
    assert client.post("/api/alerts/channels", json={"name": "x", "kind": "slack", "settings": {}}).status_code == 400
    assert client.post("/api/alerts/channels", json={"name": "x", "kind": "slack",
                                                     "settings": {"webhook_url": "ftp://x"}}).status_code == 400
    c = client.post("/api/alerts/channels", json={"name": "ops", "kind": "slack",
                                                  "settings": {"webhook_url": "https://hooks.example/T1"}}).json()
    assert c["settings"]["webhook_url"] == "********" and c["has_webhook_url"] is True
    # the mask sent back keeps the stored secret
    c2 = client.put(f"/api/alerts/channels/{c['id']}", json={"name": "ops2", "settings": {"webhook_url": "********"}}).json()
    assert c2["name"] == "ops2"
    from fbadmin.alerts.store import get_store, resolve_value

    assert get_store().get_channel(c["id"]).settings["webhook_url"] == "https://hooks.example/T1"
    raw = json.loads((get_store().file).read_text())
    assert oct(os.stat(get_store().file).st_mode & 0o777) == "0o600" and raw["channels"][0]["kind"] == "slack"
    monkeypatch.setenv("FBADMIN_TEST_HOOK", "https://hooks.example/T2")
    assert resolve_value("env:FBADMIN_TEST_HOOK") == "https://hooks.example/T2"
    with pytest.raises(ValueError):
        resolve_value("env:FBADMIN_NOT_SET_ANYWHERE")
    email = {"host": "smtp.example", "from_addr": "a@example", "to": "b@example", "security": "tls"}
    assert client.post("/api/alerts/channels", json={"name": "m", "kind": "email", "settings": email}).status_code == 400
    # deleting a channel removes it from rules
    r = client.post("/api/alerts/rules", json={"name": "r", "event": "schedule.failed", "channels": [c["id"]]}).json()
    assert client.delete(f"/api/alerts/channels/{c['id']}").status_code == 204
    assert client.get("/api/alerts/rules").json()[0]["channels"] == [] and r["channels"] == [c["id"]]


def test_rule_validation(client):
    ev = {e["id"]: e for e in client.get("/api/alerts/events").json()}
    assert ev["oat_gap"]["kind"] == "condition" and ev["schedule.failed"]["kind"] == "event"
    assert client.post("/api/alerts/rules", json={"name": "x", "event": "nope"}).status_code == 400
    assert client.post("/api/alerts/rules", json={"name": "x", "event": "oat_gap"}).status_code == 400  # needs a db
    assert client.post("/api/alerts/rules", json={"name": "x", "event": "schedule.failed",
                                                  "channels": ["missing"]}).status_code == 400
    assert client.post("/api/alerts/rules", json={"name": "x", "event": "replication_lag", "server_id": "a",
                                                  "database_id": "b"}).status_code == 400  # needs the peer


def test_webhook_signature_slack_text_cooldown(client, receiver):
    from fbadmin import alerts

    hook = add_webhook(client, receiver)
    slack = client.post("/api/alerts/channels", json={"name": "slack", "kind": "slack",
                                                      "settings": {"webhook_url": receiver.url + "/slack"}}).json()
    rule = client.post("/api/alerts/rules", json={"name": "jobs", "event": "schedule.failed",
                                                  "channels": [hook["id"], slack["id"]], "cooldown_minutes": 60}).json()
    sent = alerts.emit("schedule.failed", "critical", "Backup failed", "disk full", key="k1")
    assert len(sent) == 1
    flush()
    by_path = {r["path"]: r for r in receiver.requests}
    body = by_path["/hook"]["body"]
    assert by_path["/hook"]["headers"]["X-Fbadmin-Signature"] == \
        "sha256=" + hmac.new(b"s3cret", body, hashlib.sha256).hexdigest()
    p = json.loads(body)
    assert p["event"] == "schedule.failed" and p["level"] == "critical" and p["rule"] == "jobs"
    assert "*Backup failed*" in json.loads(by_path["/slack"]["body"])["text"]
    # the same alert inside the cool-down is not repeated; a different key is
    assert alerts.emit("schedule.failed", "critical", "Backup failed", "disk full", key="k1") == []
    assert len(alerts.emit("schedule.failed", "critical", "Other", "x", key="k2")) == 1
    flush()
    hist = client.get("/api/alerts/history").json()
    assert len(hist) == 2 and all(d["ok"] for d in hist[-1]["deliveries"])
    # scoped rules only match their server/database
    client.put(f"/api/alerts/rules/{rule['id']}", json={"server_id": "other"})
    assert alerts.emit("schedule.failed", "critical", "x", "y", server_id="mine", key="k3") == []
    # a failing endpoint is recorded, not raised
    receiver.status = 403
    client.put(f"/api/alerts/rules/{rule['id']}", json={"server_id": None})
    alerts.emit("schedule.failed", "critical", "x", "y", key="k4")
    flush()
    d = client.get("/api/alerts/history").json()[0]["deliveries"]
    assert not d[0]["ok"] and "403" in d[0]["error"] and "invalid_token" in d[0]["error"]
    t = client.post(f"/api/alerts/channels/{hook['id']}/test").json()
    assert t["ok"] is False and "403" in t["error"]
    receiver.status = 200
    assert client.post(f"/api/alerts/channels/{hook['id']}/test").json()["ok"] is True
    assert client.delete("/api/alerts/history").status_code == 204 and client.get("/api/alerts/history").json() == []


def test_validation_error_parser():
    from fbadmin.maintenance.verify import scratch_path, validation_errors

    ok = ["03:52:41.90 Validation started", "03:52:41.90 Relation 128 (T) is ok", "03:52:41.90 Validation finished"]
    assert validation_errors(ok) == 0
    # real output of Firebird 5 for a data page whose type byte was overwritten
    bad = ["03:56:14.63 Validation started", "03:56:14.63 Relation 128 (T)",
           "03:56:14.63   process pointer page    0 of    1",
           "03:56:14.63 Error: Page 229 wrong type (expected data encountered unknown (92))",
           "03:56:14.63 Error: Data page 229 {sequence 0} marked as secondary but contains primary record versions",
           "03:56:14.63 Warning: Pointer page 224 {sequence 0} bits {0x00 } are not consistent with data page 229",
           "03:56:14.63 Index 1 (RDB$PRIMARY1)", "03:56:14.63 Relation 128 (T) : 2 ERRORS found",
           "03:56:14.63 Validation finished"]
    assert validation_errors(bad) == 2
    assert validation_errors([*bad, "Index 3 (IX) : 1 ERRORS found"]) == 3
    assert scratch_path("/data/app.fdb", "abc") == "/data/fbadmin_verify_abc.fdb"
    assert scratch_path(r"C:\db\app.fdb", "abc") == r"C:\db\fbadmin_verify_abc.fdb"
    assert scratch_path("employee", "abc", "/tmp/x.fdb") == "/tmp/x.fdb"
    with pytest.raises(ValueError):
        scratch_path("employee", "abc")


def test_email_channel_real_smtp(client, monkeypatch):
    host = os.environ.get("FBADMIN_TEST_SMTP_HOST")
    if not host:
        pytest.skip("FBADMIN_TEST_SMTP_HOST not set (scripts/smtp-test-server.sh)")
    port, api = os.environ.get("FBADMIN_TEST_SMTP_PORT", "1025"), os.environ["FBADMIN_TEST_SMTP_API"]
    to = f"dba-{uuid.uuid4().hex[:6]}@example.com"
    c = client.post("/api/alerts/channels", json={"name": "mail", "kind": "email", "settings": {
        "host": host, "port": port, "security": "none", "username": "fbadmin", "password": "env:FBADMIN_TEST_SMTP_PASS",
        "from_addr": "fbadmin@example.com", "to": to}}).json()
    monkeypatch.setenv("FBADMIN_TEST_SMTP_PASS", "secret")
    r = client.post(f"/api/alerts/channels/{c['id']}/test").json()
    assert r["ok"], r
    with urllib.request.urlopen(f"{api}/api/v1/search?query=to:{to}") as resp:
        msgs = json.loads(resp.read())["messages"]
    assert len(msgs) == 1 and msgs[0]["Subject"] == "[fbadmin] INFO: Test alert from fbadmin"


# ----------------------------------------------------------- integration
def _hold_transaction(path: str):
    """A connection with an open, writing snapshot transaction: pins the OAT."""
    c = firebirdsql.connect(host=HOST, port=PORT, user=USER, password=PASSWORD, database=path)
    cur = c.cursor()
    cur.execute("UPDATE ITEMS SET NAME = NAME WHERE ID = 1")
    return c


def _db_path(client, sid, dbid):
    return next(d["path"] for d in client.get(f"/api/servers/{sid}").json()["databases"] if d["id"] == dbid)


@requires_server
def test_condition_rules_fire_and_resolve(client, database, receiver):
    sid, dbid = database
    fill(client, sid, dbid, 5)
    hook = add_webhook(client, receiver)
    gap = client.post("/api/alerts/rules", json={"name": "gap", "event": "oat_gap", "server_id": sid,
                                                 "database_id": dbid, "threshold": 3, "channels": [hook["id"]]}).json()
    lng = client.post("/api/alerts/rules", json={"name": "long", "event": "long_transaction", "server_id": sid,
                                                 "database_id": dbid, "threshold": 1, "channels": [hook["id"]]}).json()
    assert client.post(f"/api/alerts/rules/{gap['id']}/check").json()["status"]["state"] == "ok"
    held = _hold_transaction(_db_path(client, sid, dbid))
    try:
        for _ in range(5):  # move the next transaction past the held one
            c = firebirdsql.connect(host=HOST, port=PORT, user=USER, password=PASSWORD,
                                    database=_db_path(client, sid, dbid))
            c.cursor().execute("SELECT 1 FROM RDB$DATABASE")
            c.commit()
            c.close()
        time.sleep(1.5)
        st = client.post(f"/api/alerts/rules/{gap['id']}/check").json()["status"]
        assert st["state"] == "firing" and st["value"] >= 3, st
        st = client.post(f"/api/alerts/rules/{lng['id']}/check").json()["status"]
        assert st["state"] == "firing", st
        flush()
        events = [receiver.json(i)["event"] for i in range(len(receiver.requests))]
        assert "oat_gap" in events and "long_transaction" in events
        gap_alert = next(receiver.json(i) for i in range(len(receiver.requests)) if receiver.json(i)["event"] == "oat_gap")
        assert gap_alert["details"]["oat_holder"]["username"] == USER
        # still bad: no repeat inside the cool-down
        n = len(receiver.requests)
        client.post(f"/api/alerts/rules/{gap['id']}/check")
        flush()
        assert len(receiver.requests) == n
    finally:
        held.rollback()
        held.close()
    st = client.post(f"/api/alerts/rules/{lng['id']}/check").json()["status"]
    assert st["state"] == "ok"
    flush()
    assert receiver.json()["level"] == "resolved" and receiver.json()["event"] == "long_transaction"
    rules = {r["id"]: r for r in client.get("/api/alerts/rules").json()}
    assert rules[lng["id"]]["status"]["state"] == "ok"


@requires_server
def test_unreachable_and_replication_rules(client, server, receiver):
    sid = server["id"]
    bogus = client.post(f"/api/servers/{sid}/databases", json={"name": "gone", "path": f"{DB_DIR}/nope_{uuid.uuid4().hex[:6]}.fdb"}).json()
    hook = add_webhook(client, receiver)
    r = client.post("/api/alerts/rules", json={"name": "down", "event": "database.unreachable", "server_id": sid,
                                               "database_id": bogus["id"], "channels": [hook["id"]]}).json()
    st = client.post(f"/api/alerts/rules/{r['id']}/check").json()["status"]
    assert st["state"] == "firing"
    flush()
    assert receiver.json()["event"] == "database.unreachable" and receiver.json()["level"] == "critical"
    # replication lag between two plain databases: sequence 0 on both sides (Firebird 4+), an error on Firebird 3
    a = client.post(f"/api/servers/{sid}/databases/create", json={"name": "a", "path": f"{DB_DIR}/fbadmin_al_{uuid.uuid4().hex[:6]}.fdb"}).json()
    b = client.post(f"/api/servers/{sid}/databases/create", json={"name": "b", "path": f"{DB_DIR}/fbadmin_al_{uuid.uuid4().hex[:6]}.fdb"}).json()
    try:
        lag = client.post("/api/alerts/rules", json={"name": "lag", "event": "replication_lag", "server_id": sid,
                                                     "database_id": a["id"], "peer_server_id": sid,
                                                     "peer_database_id": b["id"]}).json()
        st = client.post(f"/api/alerts/rules/{lag['id']}/check").json()["status"]
        if st["state"] == "error":  # Firebird 3
            assert "4.0" in st["error"], st
        else:
            assert st["state"] == "ok" and st["value"] == 0, st
    finally:
        for d in (a, b):
            client.post(f"/api/servers/{sid}/databases/{d['id']}/drop")


@requires_server
def test_scheduled_backup_test_restore(client, database, receiver):
    sid, dbid = database
    fill(client, sid, dbid, 20)
    sch = client.post("/api/schedules", json={"name": "verified", "kind": "backup", "server_id": sid,
                                              "database_id": dbid, "cron": "0 3 * * *", "keep": 1,
                                              "verify": True}).json()
    assert sch["verify"] is True
    ok_job(client, client.post(f"/api/schedules/{sch['id']}/run"))
    h = client.get(f"/api/schedules/{sch['id']}/history").json()[0]
    assert h["status"] == "finished" and h["verify"]["ok"] and h["verify"]["tables"] == 2, h
    scratch = h["verify"]["path"]
    assert scratch.endswith(f"fbadmin_verify_{sch['id']}.fdb")
    with pytest.raises(firebirdsql.Error):  # dropped again
        firebirdsql.connect(host=HOST, port=PORT, user=USER, password=PASSWORD, database=scratch).close()

    # on-demand test restore of an archived backup
    b = client.get(f"/api/schedules/{sch['id']}/backups").json()[0]
    ok_job(client, client.post(f"/api/schedules/{sch['id']}/backups/verify", json={"path": b["path"]}))
    assert client.post(f"/api/schedules/{sch['id']}/backups/verify", json={"path": "../x.fbk"}).status_code == 400

    # a test restore that cannot run (the scratch path is the live database) fails the run, keeps the
    # older backup (no retention) and raises both alerts
    hook = add_webhook(client, receiver)
    for ev in ("schedule.failed", "backup.verify_failed"):
        client.post("/api/alerts/rules", json={"name": ev, "event": ev, "server_id": sid, "channels": [hook["id"]]})
    client.put(f"/api/schedules/{sch['id']}", json={"verify_path": _db_path(client, sid, dbid)})
    time.sleep(1.1)
    j = wait_job(client, client.post(f"/api/schedules/{sch['id']}/run").json())
    assert j["status"] == "failed"
    h = client.get(f"/api/schedules/{sch['id']}/history").json()[0]
    assert h["status"] == "failed" and "registered database" in h["error"] and h["verify"]["ok"] is False
    assert len(client.get(f"/api/schedules/{sch['id']}/backups").json()) == 2  # keep=1 not applied
    flush()
    assert sorted(receiver.json(i)["event"] for i in range(len(receiver.requests))) == \
        ["backup.verify_failed", "schedule.failed"]
    # clearing the path goes back to the default scratch database
    assert client.put(f"/api/schedules/{sch['id']}", json={"verify_path": None}).json()["verify_path"] is None


@requires_server
def test_scheduled_validation_reports_errors(client, database, receiver):
    sid, dbid = database
    fill(client, sid, dbid, 5)
    sch = client.post("/api/schedules", json={"name": "val", "kind": "validate", "server_id": sid,
                                              "database_id": dbid, "cron": "0 3 * * *"}).json()
    ok_job(client, client.post(f"/api/schedules/{sch['id']}/run"))
    h = client.get(f"/api/schedules/{sch['id']}/history").json()[0]
    assert h["status"] == "finished" and h["validation_errors"] == 0


@requires_server
def test_scheduled_backup_upload_to_s3(client, database, receiver):
    """Backup -> test restore -> upload with cloud retention, against a real S3 API (SeaweedFS in CI)."""
    endpoint = os.environ.get("FBADMIN_TEST_S3_ENDPOINT")
    if not endpoint:
        pytest.skip("FBADMIN_TEST_S3_ENDPOINT not set (scripts/storage-test-servers.sh)")
    sid, dbid = database
    fill(client, sid, dbid, 10)
    tg = client.post("/api/storage/targets", json={"name": "s3", "kind": "s3", "settings": {
        "bucket": os.environ["FBADMIN_TEST_S3_BUCKET"], "endpoint": endpoint,
        "access_key_id": os.environ["FBADMIN_TEST_S3_ACCESS_KEY"],
        "secret_access_key": os.environ["FBADMIN_TEST_S3_SECRET_KEY"]}})
    assert tg.status_code == 201, tg.text
    tid = tg.json()["id"]
    prefix = f"sched-{uuid.uuid4().hex[:6]}"
    sch = client.post("/api/schedules", json={"name": "cloud", "kind": "backup", "server_id": sid, "database_id": dbid,
                                              "cron": "0 3 * * *", "keep": 0, "verify": True,
                                              "upload": {"target_id": tid, "prefix": prefix, "keep": 2}}).json()
    for _ in range(3):
        ok_job(client, client.post(f"/api/schedules/{sch['id']}/run"))
        time.sleep(1.1)
    h = client.get(f"/api/schedules/{sch['id']}/history").json()
    assert all(x["status"] == "finished" and x["verify"]["ok"] and x["upload"]["ok"] for x in h), h
    assert h[0]["upload"]["key"].startswith(prefix + "/") and len(h[0]["upload"]["removed"]) == 1
    objs = client.get(f"/api/storage/targets/{tid}/objects?prefix={prefix}").json()
    assert len(objs) == 2 and {o["key"] for o in objs} == {x["upload"]["key"] for x in h[:2]}
    local = {b["name"]: b["size"] for b in client.get(f"/api/schedules/{sch['id']}/backups").json()}
    assert all(local[o["key"].split("/")[-1]] == o["size"] for o in objs)

    # an upload that fails (bad credentials) fails the run and raises the alert; the backup stays local
    hook = add_webhook(client, receiver)
    client.post("/api/alerts/rules", json={"name": "up", "event": "upload.failed", "channels": [hook["id"]]})
    client.put(f"/api/storage/targets/{tid}", json={"settings": {"secret_access_key": "wrong"}})
    j = wait_job(client, client.post(f"/api/schedules/{sch['id']}/run").json())
    assert j["status"] == "failed"
    last = client.get(f"/api/schedules/{sch['id']}/history").json()[0]
    assert last["upload"]["ok"] is False and "SignatureDoesNotMatch" in last["error"], last
    flush()
    assert receiver.json()["event"] == "upload.failed"
