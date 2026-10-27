"""Roadmap 2.2: encryption of the backups fbadmin keeps and uploads, and scheduled restore drills."""

from __future__ import annotations

import io
import os
import subprocess
import sys
import uuid
from pathlib import Path

import pytest

from fbadmin.maintenance import backupcrypt as B

from .conftest import DB_DIR, requires_server
from .test_alerts import add_webhook, flush, receiver  # noqa: F401 - fixture
from .test_maintenance import execute, wait_job


# ------------------------------------------------------------------ format and keys
def _enc(data: bytes, key: bytes, chunk: int = B.CHUNK) -> bytes:
    f = io.BytesIO()
    w = B.EncryptingWriter(f, key, chunk)
    for i in range(0, len(data), 7777):
        w.write(data[i:i + 7777])
    w.close()
    return f.getvalue()


def _dec(ct: bytes, key: bytes) -> bytes:
    r = B.DecryptingReader(io.BytesIO(ct), key=key)
    return b"".join(iter(lambda: r.read(10_000), b""))


@pytest.mark.parametrize("n", [0, 1, 4095, 4096, 4097, 3 * 4096, 3 * 4096 + 5])
def test_stream_round_trip_and_tampering(n):
    key = os.urandom(32)
    data = os.urandom(n)
    ct = _enc(data, key, chunk=4096)
    assert ct.startswith(B.MAGIC) and _dec(ct, key) == data
    chunks = -(-max(n, 1) // 4096) if n else 1
    assert len(ct) == B.HEADER_LEN + n + B.TAG * chunks
    bad = [ct[:-1], ct + b"x", ct[:B.HEADER_LEN], ct[:B.HEADER_LEN + 3] + bytes([ct[B.HEADER_LEN + 3] ^ 1])
           + ct[B.HEADER_LEN + 4:]]
    if n > 4096:  # drop the final chunk: the previous one is not marked final
        bad.append(ct[:B.HEADER_LEN + 4096 + B.TAG])
        bad.append(ct[:B.HEADER_LEN] + ct[B.HEADER_LEN + 4096 + B.TAG:])  # reorder/drop the first chunk
    for b in bad:
        with pytest.raises(B.BackupKeyError):
            _dec(b, key)
    with pytest.raises(B.BackupKeyError, match="needs key"):
        _dec(ct, os.urandom(32))


def test_keys(tmp_path, monkeypatch):
    monkeypatch.delenv("FBADMIN_BACKUP_KEY", raising=False)
    ring = B.Keyring(tmp_path)
    assert ring.list() == [] and ring.active() is None
    text = ring.generate()
    kid = B.key_id(B.parse_key(text))
    assert ring.active_id() == kid and ring.list()[0]["source"] == "stored"
    assert oct((tmp_path / "backup-keys.json").stat().st_mode & 0o777) == "0o600"
    second = ring.generate()
    assert ring.active_id() == B.key_id(B.parse_key(second))
    ring.activate(kid)
    assert ring.active_id() == kid and ring.get(B.key_id(B.parse_key(second)))
    ring.remove(B.key_id(B.parse_key(second)))
    with pytest.raises(B.BackupKeyError, match="doesn't have"):
        ring.get(B.key_id(B.parse_key(second)))
    for bad in ("", "abc", "fbk1:short", "fbk1:" + "!" * 43):
        with pytest.raises(B.BackupKeyError):
            B.parse_key(bad)
    # FBADMIN_BACKUP_KEY: its first key encrypts; stored keys still decrypt
    env1, env2 = B.format_key(os.urandom(32)), B.format_key(os.urandom(32))
    monkeypatch.setenv("FBADMIN_BACKUP_KEY", f"{env1}, {env2}")
    assert ring.active_id() == B.key_id(B.parse_key(env1))
    assert [k["source"] for k in ring.list()] == ["environment", "environment", "stored"]
    assert ring.get(kid) and ring.get(B.key_id(B.parse_key(env2)))


# ------------------------------------------------------------------ encrypted backups
def _schedule(client, **body):
    r = client.post("/api/schedules", json={"cron": "0 2 * * *", **body})
    assert r.status_code == 201, r.text
    return r.json()


def _run(client, sched, ok=True):
    job = wait_job(client, client.post(f"/api/schedules/{sched['id']}/run").json(), timeout=300)
    if ok:
        assert job["status"] == "finished", (job.get("error"), job["log"])
    return job, client.get(f"/api/schedules/{sched['id']}/history").json()[0]


@requires_server
def test_encrypted_backups(client, database, tmp_path, monkeypatch):
    monkeypatch.delenv("FBADMIN_BACKUP_KEY", raising=False)
    sid, dbid = database
    execute(client, sid, dbid, "CREATE TABLE SECRET (ID INTEGER NOT NULL PRIMARY KEY, NOTE VARCHAR(40))")
    execute(client, sid, dbid, "INSERT INTO SECRET VALUES (1, 'top secret note')")
    body = {"name": "enc", "kind": "backup", "server_id": sid, "database_id": dbid, "encrypt": True, "verify": True}
    r = client.post("/api/schedules", json={"cron": "0 2 * * *", **body})
    assert r.status_code == 400 and "no backup key" in r.json()["detail"]
    assert client.get("/api/backup-keys").json() == {"keys": [], "active": None, "missing": []}
    k = client.post("/api/backup-keys")
    assert k.status_code == 201 and k.json()["active"] and k.json()["key"].startswith("fbk1:")
    kid, key_text = k.json()["id"], k.json()["key"]
    sched = _schedule(client, **body)
    job, h = _run(client, sched)
    assert h["file"].endswith(".fbk.enc") and h["encrypted"] == kid
    assert h["verify"]["ok"], h  # the test restore decrypts
    assert any("Decrypting" in line for line in client.get(f"/api/jobs/{job['id']}").json()["log"])
    [item] = client.get(f"/api/schedules/{sched['id']}/backups").json()
    assert item["encrypted"] and item["key_id"] == kid
    raw = client.get(f"/api/servers/{sid}/maintenance/archive/download", params={"path": item["path"]})
    assert raw.status_code == 200 and raw.content.startswith(B.MAGIC) and b"top secret note" not in raw.content
    plain = client.get(f"/api/servers/{sid}/maintenance/archive/download",
                       params={"path": item["path"], "decrypt": "true"})
    assert plain.status_code == 200 and b"top secret note" in plain.content
    assert 'filename="' in plain.headers["content-disposition"] and ".enc" not in plain.headers["content-disposition"]
    assert len(plain.content) == B.info(Path(os.environ["FBADMIN_DATA_DIR"]) / "backups" / item["path"])["plain_size"]

    # a restore from the archive decrypts it as well
    target = f"{DB_DIR}/fbadmin_test_dec_{uuid.uuid4().hex[:8]}.fdb"
    r = client.post(f"/api/servers/{sid}/maintenance/restore", json={
        "source": "archive", "backup_file": item["path"], "database_path": target, "register_as": "decrypted"})
    job = wait_job(client, r.json(), timeout=300)
    assert job["status"] == "finished", job["log"]
    srv = client.get(f"/api/servers/{sid}").json()
    rid = next(d["id"] for d in srv["databases"] if d["name"] == "decrypted")
    try:
        rows = client.get(f"/api/servers/{sid}/databases/{rid}/tables/SECRET/data").json()["rows"]
        assert rows == [[1, "top secret note"]]
    finally:
        client.post(f"/api/servers/{sid}/databases/{rid}/drop")

    # the recovery key decrypts it without fbadmin running
    src = Path(os.environ["FBADMIN_DATA_DIR"]) / "backups" / item["path"]
    out = tmp_path / "plain.fbk"
    env = {k: v for k, v in os.environ.items() if k != "FBADMIN_BACKUP_KEY"}
    cli = subprocess.run([sys.executable, "-m", "fbadmin", "backup-decrypt", str(src), "-o", str(out), "--key",
                          key_text, "--json"], capture_output=True, text=True, env=env)
    assert cli.returncode == 0, cli.stderr
    assert out.read_bytes() == plain.content
    wrong = subprocess.run([sys.executable, "-m", "fbadmin", "backup-decrypt", str(src), "-o", str(tmp_path / "x"),
                            "--key", B.format_key(os.urandom(32))], capture_output=True, text=True, env=env)
    assert wrong.returncode == 4 and "needs key" in wrong.stderr

    # a key that archived backups need is only removed on purpose
    keys = client.get("/api/backup-keys").json()["keys"]
    assert keys[0]["archived_backups"] == 1
    assert client.delete(f"/api/backup-keys/{kid}").status_code == 409
    assert client.post(f"/api/backup-keys/{kid}/export").json()["key"] == key_text
    assert client.delete(f"/api/backup-keys/{kid}", params={"force": "true"}).status_code == 204
    listing = client.get("/api/backup-keys").json()
    assert listing["keys"] == [] and listing["missing"] == [{"id": kid, "archived_backups": 1}]
    r = client.post(f"/api/servers/{sid}/maintenance/restore", json={
        "source": "archive", "backup_file": item["path"], "database_path": target})
    job = wait_job(client, r.json(), timeout=120)
    assert job["status"] == "failed" and kid in job["error"]
    assert client.post("/api/backup-keys/import", json={"key": key_text}).json()["id"] == kid
    assert client.post("/api/backup-keys/import", json={"key": "nope"}).status_code == 400


# ------------------------------------------------------------------ restore drills
@requires_server
def test_restore_drill(client, database, receiver, monkeypatch):  # noqa: F811
    monkeypatch.delenv("FBADMIN_BACKUP_KEY", raising=False)
    sid, dbid = database
    execute(client, sid, dbid, "CREATE TABLE ORDERS (ID INTEGER NOT NULL PRIMARY KEY)")
    execute(client, sid, dbid, "INSERT INTO ORDERS VALUES (1)")
    execute(client, sid, dbid, "INSERT INTO ORDERS VALUES (2)")
    key = client.post("/api/backup-keys").json()
    backup = _schedule(client, name="nightly", kind="backup", server_id=sid, database_id=dbid, encrypt=True)
    hook = add_webhook(client, receiver)
    assert client.post("/api/alerts/rules", json={"name": "drills", "event": "restore.drill_failed",
                                                  "channels": [hook["id"]], "cooldown_minutes": 0}).status_code == 201
    r = client.post("/api/schedules", json={"name": "drill", "kind": "restore_drill", "server_id": sid,
                                            "database_id": dbid, "cron": "0 5 * * 0", "drill": {}})
    assert r.status_code == 400 and "backup schedule" in r.json()["detail"]
    drill = _schedule(client, name="weekly drill", kind="restore_drill", server_id=sid, database_id=dbid,
                      drill={"backup_schedule_id": backup["id"], "max_age_hours": 24})
    assert client.get(f"/api/schedules/{drill['id']}/drill").status_code == 404

    # no backup yet: the drill fails (and says so)
    _job, h = _run(client, drill, ok=False)
    assert h["status"] == "failed" and "no backups" in h["error"]
    flush()
    assert receiver.json()["event"] == "restore.drill_failed" and receiver.json()["level"] == "critical"

    _run(client, backup)
    execute(client, sid, dbid, "INSERT INTO ORDERS VALUES (3)")  # after the backup
    _job, h = _run(client, drill)
    assert h["status"] == "finished" and h["drill_ok"] and h["rows"] == 2 and h["failed_checks"] == []
    rep = client.get(f"/api/schedules/{drill['id']}/drill").json()
    codes = {c["code"]: c["ok"] for c in rep["checks"]}
    assert codes == {"age": True, "key": True, "restore": True, "validation": True, "open": True, "read": True,
                     "tables": True}
    assert rep["source"]["encrypted"] and rep["source"]["key_id"] == key["id"] and rep["source"]["kind"] == "archive"
    assert rep["restored"]["tables"] == 1 and rep["recovery_seconds"] >= 0
    orders = next(t for t in rep["compare"]["tables"] if t["table"] == "ORDERS")
    assert orders == {"table": "ORDERS", "backup_rows": 2, "live_rows": 3, "status": "differs"}
    assert rep["restore"]["path"].endswith(f"fbadmin_drill_{drill['id']}.fdb")
    srv = client.get(f"/api/servers/{sid}").json()
    assert not any("drill" in d["path"] for d in srv["databases"])  # dropped, not registered
    flush()
    assert receiver.json()["level"] == "resolved"  # it works again

    # without the key, the drill fails before restoring
    assert client.delete(f"/api/backup-keys/{key['id']}", params={"force": "true"}).status_code == 204
    _job, h = _run(client, drill, ok=False)
    assert h["failed_checks"] == ["key"]
    flush()
    assert receiver.json()["level"] == "critical" and key["id"] in receiver.json()["message"]
    client.post("/api/backup-keys/import", json={"key": key["key"]})

    # a stale backup fails the age limit; keep: the restored database stays, registered
    client.put(f"/api/schedules/{drill['id']}", json={"drill": {"backup_schedule_id": backup["id"],
                                                                 "max_age_hours": 0.00001, "keep": True}})
    _job, h = _run(client, drill, ok=False)
    assert h["failed_checks"] == ["age"]
    srv = client.get(f"/api/servers/{sid}").json()
    kept = [d for d in srv["databases"] if d["name"].endswith("(restore drill)")]
    assert len(kept) == 1
    client.post(f"/api/servers/{sid}/databases/{kept[0]['id']}/drop")
    client.delete(f"/api/schedules/{drill['id']}")
    assert client.get(f"/api/schedules/{drill['id']}/drill").status_code == 404
