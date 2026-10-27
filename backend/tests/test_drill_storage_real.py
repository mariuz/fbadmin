"""Restore drills and downloads against the cloud storage emulators (scripts/storage-test-servers.sh).

Ranged downloads of each provider, and a drill whose backup comes from cloud storage: an encrypted scheduled
backup is uploaded, the drill downloads the newest one, decrypts and restores it, and checks it.
"""

from __future__ import annotations

import time
import uuid

import pytest

from fbadmin import storage
from fbadmin.storage import base

from .conftest import requires_server
from .test_maintenance import execute, wait_job
from .test_storage_real import E, MiB, PROVIDERS, _api, _file, data_dir  # noqa: F401 - fixture


@pytest.mark.parametrize("kind,settings,create,chunk", PROVIDERS)
def test_download_in_ranges(kind, settings, create, chunk, data_dir, monkeypatch):  # noqa: F811
    run = uuid.uuid4().hex[:8]
    t = storage.get_store().add(storage.Target(name=f"{kind}-dl", kind=kind,
                                               settings={**settings(), "prefix": f"dl-{run}/"}))
    client = storage.open_client(t)
    create(client)
    src = _file(data_dir / "db.fbk.enc", 3 * MiB + 777, run)
    key = f"dl-{run}/db.fbk.enc"
    client.upload_file(src, key)
    monkeypatch.setattr(base, "CHUNK_SIZE", MiB)  # four ranged GETs, the last one short
    logs: list[str] = []
    n = client.download_file(key, data_dir / "back.fbk.enc", logs.append)
    assert n == 3 * MiB + 777 and (data_dir / "back.fbk.enc").read_bytes() == src.read_bytes()
    assert len(logs) == 4 and not (data_dir / "back.fbk.enc.part").exists()
    client.delete(key)


@requires_server
@pytest.mark.parametrize("kind,settings,create,chunk", PROVIDERS)
def test_drill_from_cloud_storage(kind, settings, create, chunk, client, database, monkeypatch):
    monkeypatch.setenv("FBADMIN_TEST_S3_SECRET_KEY", E("FBADMIN_TEST_S3_SECRET_KEY", "fbadmin-secret-key"))
    monkeypatch.delenv("FBADMIN_BACKUP_KEY", raising=False)
    sid, dbid = database
    tid = _api(client, kind, settings)
    create(storage.open_client(storage.get_store().get(tid)))
    prefix = f"drill-{uuid.uuid4().hex[:8]}"
    execute(client, sid, dbid, "CREATE TABLE T (ID INTEGER NOT NULL PRIMARY KEY); INSERT INTO T VALUES (1);")
    client.post("/api/backup-keys")
    r = client.post("/api/schedules", json={"name": "cloud", "kind": "backup", "server_id": sid, "database_id": dbid,
                                            "cron": "0 2 * * *", "encrypt": True,
                                            "upload": {"target_id": tid, "prefix": prefix, "keep": 3}})
    assert r.status_code == 201, r.text
    backup = r.json()
    job = wait_job(client, client.post(f"/api/schedules/{backup['id']}/run").json(), timeout=300)
    assert job["status"] == "finished", job["log"]
    h = client.get(f"/api/schedules/{backup['id']}/history").json()[0]
    assert h["upload"]["ok"] and h["upload"]["key"].endswith(".fbk.enc")
    r = client.post("/api/schedules", json={"name": "cloud drill", "kind": "restore_drill", "server_id": sid,
                                            "database_id": dbid, "cron": "0 5 * * 0",
                                            "drill": {"source": "storage", "target_id": tid, "prefix": prefix}})
    assert r.status_code == 201, r.text
    drill = r.json()
    job = wait_job(client, client.post(f"/api/schedules/{drill['id']}/run").json(), timeout=300)
    assert job["status"] == "finished", (job.get("error"), job["log"])
    rep = client.get(f"/api/schedules/{drill['id']}/drill").json()
    assert rep["ok"] and rep["source"]["kind"] == "storage" and rep["source"]["encrypted"]
    assert rep["source"]["key"] == h["upload"]["key"] and rep["source"]["download_seconds"] >= 0
    assert rep["compare"]["tables"] == [{"table": "T", "backup_rows": 1, "live_rows": 1, "status": "same"}]
    c = storage.open_client(storage.get_store().get(tid))
    for o in c.list(storage.object_prefix(storage.get_store().get(tid), prefix) + "/"):
        c.delete(o["key"])
    assert time.time() - rep["finished_at"] < 300
