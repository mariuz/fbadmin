"""Cloud storage targets against real emulators: SeaweedFS (S3), Azurite (Azure Blob) and fake-gcs-server.

Start them with ``scripts/storage-test-servers.sh`` and export the variables it prints:
FBADMIN_TEST_S3_ENDPOINT (+ _ACCESS_KEY, _SECRET_KEY, _BUCKET), FBADMIN_TEST_AZURE_ENDPOINT
(+ _ACCOUNT, _KEY) and FBADMIN_TEST_GCS_ENDPOINT. Each provider is skipped when its endpoint is not set.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import time
import uuid
from datetime import datetime, timedelta, timezone
from urllib.parse import quote

import pytest

from fbadmin import storage
from fbadmin.storage import base
from fbadmin.storage.base import StorageError

E = os.environ.get
AZURITE_KEY = "Eby8vdM02xNOcqFlqUwJPLlmEtlCDXJ1OUzFT50uSRZ6IFsuFq2UVErCz4I6tq/K1SZFPTOtr/KBHBeksoGMGw=="
MiB = 1024 * 1024


def _s3_settings() -> dict:
    return {"bucket": E("FBADMIN_TEST_S3_BUCKET", "fbadmin-test"), "endpoint": E("FBADMIN_TEST_S3_ENDPOINT"),
            "access_key_id": E("FBADMIN_TEST_S3_ACCESS_KEY", "fbadmin"),
            "secret_access_key": "env:FBADMIN_TEST_S3_SECRET_KEY", "region": E("FBADMIN_TEST_S3_REGION", "")}


def _azure_settings() -> dict:
    return {"account": E("FBADMIN_TEST_AZURE_ACCOUNT", "devstoreaccount1"), "container": "fbadmin-test",
            "account_key": E("FBADMIN_TEST_AZURE_KEY", AZURITE_KEY), "endpoint": E("FBADMIN_TEST_AZURE_ENDPOINT")}


def _gcs_settings() -> dict:
    return {"bucket": "fbadmin-test", "endpoint": E("FBADMIN_TEST_GCS_ENDPOINT")}


def _create_s3_bucket(c) -> None:
    c._call("PUT", "", ok=(200, 409))  # 409 BucketAlreadyOwnedByYou


def _create_azure_container(c) -> None:
    c._call("PUT", "", [("restype", "container")], ok=(201, 409))


def _create_gcs_bucket(c) -> None:
    c._call("POST", f"{c.endpoint}/storage/v1/b", json.dumps({"name": c.bucket}).encode(),
            {"Content-Type": "application/json"}, ok=(200, 409))


PROVIDERS = [
    pytest.param("s3", _s3_settings, _create_s3_bucket, 5 * MiB, id="s3",
                 marks=pytest.mark.skipif(not E("FBADMIN_TEST_S3_ENDPOINT"), reason="FBADMIN_TEST_S3_ENDPOINT not set")),
    pytest.param("azure", _azure_settings, _create_azure_container, 5 * MiB, id="azure",
                 marks=pytest.mark.skipif(not E("FBADMIN_TEST_AZURE_ENDPOINT"),
                                          reason="FBADMIN_TEST_AZURE_ENDPOINT not set")),
    pytest.param("gcs", _gcs_settings, _create_gcs_bucket, 5 * MiB, id="gcs",
                 marks=pytest.mark.skipif(not E("FBADMIN_TEST_GCS_ENDPOINT"), reason="FBADMIN_TEST_GCS_ENDPOINT not set")),
]


@pytest.fixture
def data_dir(tmp_path, monkeypatch):
    from fbadmin.config import settings

    monkeypatch.setattr(settings, "data_dir", tmp_path)
    monkeypatch.setenv("FBADMIN_TEST_S3_SECRET_KEY", E("FBADMIN_TEST_S3_SECRET_KEY", "fbadmin-secret-key"))
    return tmp_path


def _file(path, size: int, seed: str):
    """A file of ``size`` pseudo-random bytes written in 1 MiB pieces."""
    with open(path, "wb") as f:
        n, block = 0, hashlib.sha256(seed.encode()).digest() * (MiB // 32)
        while n < size:
            f.write(block[: size - n])
            n += min(len(block), size - n)
    return path


@pytest.mark.parametrize("kind,settings,create,chunk", PROVIDERS)
def test_upload_list_retention_delete(kind, settings, create, chunk, data_dir, monkeypatch):
    monkeypatch.setattr(base, "CHUNK_SIZE", chunk)
    run = uuid.uuid4().hex[:8]
    t = storage.get_store().add(storage.Target(name=f"{kind}-it", kind=kind,
                                               settings={**settings(), "prefix": f"it-{run}/"}))
    client = storage.open_client(t)
    create(client)
    res = client.test()
    assert res["ok"] and f"it-{run}/.fbadmin-test-" in res["details"]

    # one multi-chunk upload (12 MiB + a bit: 3 parts/blocks/chunks, the last one short)
    big = _file(data_dir / "db-1.fbk", 12 * MiB + 12345, run)
    logs: list[str] = []
    r = storage.upload_with_retention(t.id, big, "sched", 2, logs.append)
    assert r == {"target": f"{kind}-it", "key": f"it-{run}/sched/db-1.fbk", "size": 12 * MiB + 12345,
                 "removed": [], "errors": []}
    assert sum(" 3/3 " in m for m in logs) == 1, logs
    objs = client.list(f"it-{run}/sched/")
    assert [(o["key"], o["size"]) for o in objs] == [(r["key"], 12 * MiB + 12345)]
    assert abs(objs[0]["modified"] - time.time()) < 300
    assert client.size_of(r["key"]) == 12 * MiB + 12345

    # two more small backups (plus a non-backup object) -> keep=2 removes the oldest .fbk only
    client.put_bytes(f"it-{run}/sched/notes.txt", b"not a backup")
    for i in (2, 3):
        time.sleep(1.1)  # provider timestamps have one second resolution
        r = storage.upload_with_retention(t.id, _file(data_dir / f"db-{i}.fbk", 1000 + i, run), "sched/", 2)
    assert r["removed"] == [f"it-{run}/sched/db-1.fbk"] and r["errors"] == []
    keys = [o["key"] for o in client.list(f"it-{run}/")]
    assert keys[:2] == [f"it-{run}/sched/db-3.fbk", f"it-{run}/sched/db-2.fbk"]
    assert sorted(keys) == sorted([f"it-{run}/sched/db-2.fbk", f"it-{run}/sched/db-3.fbk",
                                   f"it-{run}/sched/notes.txt"])
    for k in keys:
        client.delete(k)
    assert client.list(f"it-{run}/") == []


@pytest.mark.parametrize("kind,settings,create,chunk", PROVIDERS)
def test_pagination_and_errors(kind, settings, create, chunk, data_dir):
    run = uuid.uuid4().hex[:8]
    t = storage.get_store().add(storage.Target(name="p", kind=kind, settings=settings()))
    client = storage.open_client(t)
    create(client)
    keys = [f"page-{run}/k{i:02d}.fbk" for i in range(5)]
    for k in keys:
        client.put_bytes(k, b"x")
    # force two keys per page so that list() has to follow the continuation token/marker
    orig, pages = client._call, []

    def paged(method, *args, **kw):
        if method == "GET":
            pages.append(method)
            if kind == "gcs":
                args = (args[0] + "&maxResults=2",) + args[1:]
            else:
                key, params = (list(args) + ["", None])[:2]
                limit = ("max-keys", "2") if kind == "s3" else ("maxresults", "2")
                args = (key, (params or []) + [limit])
        return orig(method, *args, **kw)

    client._call = paged
    assert sorted(o["key"] for o in client.list(f"page-{run}/")) == keys
    assert len(pages) == 3, pages
    del client._call
    for k in keys:
        client.delete(k)
    with pytest.raises(StorageError) as e:
        client.delete(f"page-{run}/missing.fbk") if kind != "s3" else client.size_of(f"page-{run}/missing")
    assert e.value.status == 404


def _api(client, kind, settings_fn):
    r = client.post("/api/storage/targets", json={"name": kind, "kind": kind, "settings": settings_fn()})
    assert r.status_code == 201, r.text
    return r.json()["id"]


@pytest.mark.parametrize("kind,settings,create,chunk", PROVIDERS)
def test_api_test_and_objects(kind, settings, create, chunk, client, monkeypatch):
    monkeypatch.setenv("FBADMIN_TEST_S3_SECRET_KEY", E("FBADMIN_TEST_S3_SECRET_KEY", "fbadmin-secret-key"))
    tid = _api(client, kind, settings)
    create(storage.open_client(storage.get_store().get(tid)))
    r = client.post(f"/api/storage/targets/{tid}/test")
    assert r.status_code == 200 and r.json()["ok"] is True, r.text
    r = client.get(f"/api/storage/targets/{tid}/objects", params={"prefix": f"none-{uuid.uuid4().hex}"})
    assert r.status_code == 200 and r.json() == []


@pytest.mark.skipif(not E("FBADMIN_TEST_S3_ENDPOINT"), reason="FBADMIN_TEST_S3_ENDPOINT not set")
def test_s3_wrong_secret_is_reported(client):
    tid = _api(client, "s3", lambda: {**_s3_settings(), "secret_access_key": "wrong"})
    r = client.post(f"/api/storage/targets/{tid}/test")
    assert r.status_code == 502 and "SignatureDoesNotMatch" in r.json()["detail"]


@pytest.mark.skipif(not E("FBADMIN_TEST_AZURE_ENDPOINT"), reason="FBADMIN_TEST_AZURE_ENDPOINT not set")
def test_azure_wrong_key_is_reported(client):
    wrong = base64.b64encode(b"wrong-key" * 4).decode()
    tid = _api(client, "azure", lambda: {**_azure_settings(), "account_key": wrong})
    r = client.post(f"/api/storage/targets/{tid}/test")
    assert r.status_code == 502 and "Azure: AuthorizationFailure" in r.json()["detail"], r.text


def _service_sas(account: str, key: str, container: str, perms: str = "racwdl") -> str:
    """A container service SAS (signed version 2021-08-06), as the Azure portal would generate."""
    sv = "2021-08-06"
    se = (datetime.now(timezone.utc) + timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    fields = [perms, "", se, f"/blob/{account}/{container}", "", "", "", sv, "c", "", "", "", "", "", "", ""]
    sig = base64.b64encode(hmac.new(base64.b64decode(key), "\n".join(fields).encode(), "sha256").digest()).decode()
    return f"sv={sv}&sr=c&sp={perms}&se={quote(se, safe='')}&sig={quote(sig, safe='')}"


@pytest.mark.skipif(not E("FBADMIN_TEST_AZURE_ENDPOINT"), reason="FBADMIN_TEST_AZURE_ENDPOINT not set")
def test_azure_sas_token(data_dir, monkeypatch):
    s = _azure_settings()
    create = storage.open_client(storage.Target(name="k", kind="azure", settings=s))
    _create_azure_container(create)
    monkeypatch.setattr(base, "CHUNK_SIZE", 4 * MiB)
    sas = _service_sas(s["account"], s["account_key"], s["container"])
    t = storage.get_store().add(storage.Target(name="sas", kind="azure", settings={
        **{k: v for k, v in s.items() if k != "account_key"}, "sas_token": "?" + sas}))
    client = storage.open_client(t)
    assert client.test()["ok"]
    r = storage.upload_with_retention(t.id, _file(data_dir / "s.fbk", 9 * MiB, "sas"), "sas-it", 1)
    assert client.size_of(r["key"]) == 9 * MiB
    client.delete(r["key"])
    ro = storage.open_client(storage.Target(name="ro", kind="azure", settings={
        **{k: v for k, v in s.items() if k != "account_key"},
        "sas_token": _service_sas(s["account"], s["account_key"], s["container"], "rl")}))
    with pytest.raises(StorageError, match="Azure: AuthorizationPermissionMismatch"):
        ro.test()
