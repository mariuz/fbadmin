"""Logging, request limits, the opt-in update check and the metadata connection pool."""

from __future__ import annotations

import json
import logging
import threading
import time

import pytest

from .conftest import requires_server


# --- structured logging -------------------------------------------------------
def test_json_formatter_includes_extra_fields():
    from fbadmin.logsetup import JsonFormatter

    rec = logging.LogRecord("fbadmin.access", logging.INFO, __file__, 1, "GET %s", ("/x",), None)
    rec.duration_ms = 1.5
    out = json.loads(JsonFormatter().format(rec))
    assert out["msg"] == "GET /x" and out["level"] == "info" and out["duration_ms"] == 1.5
    assert out["logger"] == "fbadmin.access" and "ts" in out


def test_configure_logging_validates_level():
    from fbadmin.logsetup import configure_logging

    cfg = configure_logging("debug", "json")
    assert cfg["loggers"][""]["level"] == "DEBUG"
    assert cfg["handlers"]["default"]["formatter"] == "json"
    with pytest.raises(ValueError):
        configure_logging("verbose", "text")


def test_request_log_has_timing(client, caplog):
    with caplog.at_level(logging.INFO, logger="fbadmin.access"):
        assert client.get("/api/health").status_code == 200
    recs = [r for r in caplog.records if r.name == "fbadmin.access" and r.path == "/api/health"]
    assert recs and recs[0].status == 200 and recs[0].duration_ms >= 0 and recs[0].method == "GET"


# --- limits ---------------------------------------------------------------------
def test_rate_limiter_token_bucket():
    from fbadmin.limits import RateLimiter

    rl = RateLimiter(3, per=60)
    assert [rl.hit("a") for _ in range(3)] == [0, 0, 0]
    assert rl.hit("a") > 0          # bucket empty
    assert rl.hit("b") == 0         # other clients are independent


def _app(monkeypatch, **env):
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    from fastapi.testclient import TestClient

    from fbadmin.app import create_app

    return TestClient(create_app())


def test_rate_limit_middleware(monkeypatch, tmp_path):
    monkeypatch.setenv("FBADMIN_DATA_DIR", str(tmp_path))
    with _app(monkeypatch, FBADMIN_RATE_LIMIT="5") as c:
        codes = [c.get("/api/health").status_code for _ in range(8)]
    assert codes[:5] == [200] * 5 and codes[-1] == 429


def test_body_size_limit(monkeypatch, tmp_path):
    monkeypatch.setenv("FBADMIN_DATA_DIR", str(tmp_path))
    with _app(monkeypatch, FBADMIN_MAX_REQUEST_BYTES="1000") as c:
        r = c.post("/api/servers", content=b"x" * 5000, headers={"content-type": "application/json"})
        assert r.status_code == 413 and r.json()["code"] == "request_too_large"

        # streamed body without Content-Length
        def gen():
            for _ in range(10):
                yield b"y" * 500

        r = c.post("/api/servers", content=gen(), headers={"content-type": "application/json"})
        assert r.status_code == 413
        # small bodies still reach the endpoint (422: invalid server payload)
        assert c.post("/api/servers", json={}).status_code == 422


# --- update check ---------------------------------------------------------------
def test_version_compare():
    from fbadmin.api.version import is_newer

    assert is_newer("1.0.0", "0.2.0")
    assert is_newer("v0.10.0", "0.9.9")
    assert not is_newer("0.2.0", "0.2.0")
    assert is_newer("1.0.0", "1.0.0rc1") and not is_newer("1.0.0rc1", "1.0.0")
    assert not is_newer("garbage", "1.0")


def test_update_check_endpoint(client, monkeypatch):
    from fbadmin.api import version

    calls = []

    def fake():
        calls.append(1)
        return {"latest": "99.0.0", "url": "https://example.invalid/r", "published_at": None}

    monkeypatch.setattr(version, "_fetch_latest", fake)
    monkeypatch.setattr(version, "_cache", {})
    r = client.get("/api/version/latest").json()
    assert r["update_available"] and r["latest"] == "99.0.0"
    client.get("/api/version/latest")
    assert len(calls) == 1  # cached
    monkeypatch.setenv("FBADMIN_UPDATE_CHECK", "off")
    r = client.get("/api/version/latest").json()
    assert r["enabled"] is False and r["latest"] is None and len(calls) == 1


def test_update_check_offline(client, monkeypatch):
    from fbadmin.api import version

    def boom():
        raise OSError("network unreachable")

    monkeypatch.setattr(version, "_fetch_latest", boom)
    monkeypatch.setattr(version, "_cache", {})
    r = client.get("/api/version/latest").json()
    assert r["error"] and not r["update_available"]


# --- metadata connection pool ---------------------------------------------------
@requires_server
def test_meta_pool_slow_request_does_not_block(client, database):
    from fbadmin.api.deps import get_state

    sid, dbid = database
    mgr = get_state().manager
    holding = threading.Event()
    release = threading.Event()

    def slow():
        with mgr.meta(sid, dbid) as conn:
            holding.set()
            release.wait(10)
            conn.cursor().execute("SELECT 1 FROM RDB$DATABASE")

    t = threading.Thread(target=slow)
    t.start()
    try:
        assert holding.wait(10)
        t0 = time.time()
        with mgr.meta(sid, dbid) as conn:
            cur = conn.cursor()
            cur.execute("SELECT CURRENT_CONNECTION FROM RDB$DATABASE")
            cur.fetchone()
        assert time.time() - t0 < 5
        assert len(mgr._meta[(sid, dbid)]) == 2
        # the same request through the API also works while the primary is busy
        assert client.get(f"/api/servers/{sid}/databases/{dbid}/objects/tables").status_code == 200
    finally:
        release.set()
        t.join()
    # sequential use prefers the primary connection
    with mgr.meta(sid, dbid) as c1:
        pass
    assert c1 is mgr._meta[(sid, dbid)][0].conn
    mgr._reap_meta_extras(0)
    assert len(mgr._meta[(sid, dbid)]) == 1
    assert mgr.is_connected(sid, dbid)
    mgr.disconnect(sid, dbid)
    assert not mgr.is_connected(sid, dbid)
