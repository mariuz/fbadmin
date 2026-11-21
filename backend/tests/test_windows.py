"""Maintenance windows (roadmap 2.5): recurring and one-off windows, their scope, alerts muted while one is open
(recorded, not sent), and an approved change request applied when the next window opens."""

from __future__ import annotations

import time
from datetime import datetime

import pytest
from pydantic import ValidationError

from fbadmin import windows as W
from fbadmin.alerts import emit

from .conftest import requires_server
from .test_alerts import add_webhook, flush, receiver  # noqa: F401 - fixture
from .test_changes import _job
from .test_library import srv  # noqa: F401 - fixture
from .test_maintenance import wait_job


def _ts(s: str) -> float:
    return datetime.fromisoformat(s).timestamp()


def test_occurrences():
    w = W.Window(name="nightly", kind="recurring", cron="0 22 * * *", duration_minutes=120)
    assert w.current(_ts("2026-11-21T21:59:00")) is None
    assert w.current(_ts("2026-11-21T22:00:00")) == (_ts("2026-11-21T22:00:00"), _ts("2026-11-22T00:00:00"))
    assert w.current(_ts("2026-11-21T23:30:00"))[0] == _ts("2026-11-21T22:00:00")
    assert w.current(_ts("2026-11-22T00:00:00")) is None
    assert w.next_start(_ts("2026-11-21T10:00:00"))[0] == _ts("2026-11-21T22:00:00")
    assert w.next_start(_ts("2026-11-21T23:00:00"))[0] == _ts("2026-11-21T22:00:00")  # the open one
    once = W.Window(name="upgrade", kind="once", start=_ts("2026-12-01T20:00:00"), end=_ts("2026-12-01T23:00:00"))
    assert once.next_start(_ts("2026-11-21T10:00:00")) == (_ts("2026-12-01T20:00:00"), _ts("2026-12-01T23:00:00"))
    assert once.next_start(_ts("2026-12-02T10:00:00")) is None
    assert W.Window(name="x", server_id="s", database_id="d", cron="@daily").covers("s", "d")
    assert not W.Window(name="x", server_id="s", database_id="d", cron="@daily").covers("s", "e")
    assert W.Window(name="x", server_id="s", cron="@daily").covers("s", "e")
    for bad in ({"cron": "nope"}, {"kind": "once", "start": 2, "end": 1}, {"database_id": "d", "cron": "@daily"}):
        with pytest.raises(ValidationError):
            W.Window(name="x", **bad)


def test_alerts_muted_in_a_window(client, receiver):  # noqa: F811
    s = client.post("/api/servers", json={"name": "prod", "host": "h"}).json()
    db = client.post(f"/api/servers/{s['id']}/databases", json={"name": "shop", "path": "/db/shop.fdb"}).json()
    other = client.post(f"/api/servers/{s['id']}/databases", json={"name": "hr", "path": "/db/hr.fdb"}).json()
    hook = add_webhook(client, receiver)
    client.post("/api/alerts/rules", json={"name": "fails", "event": "schedule.failed", "server_id": s["id"],
                                           "channels": [hook["id"]], "cooldown_minutes": 0})
    now = time.time()
    r = client.post("/api/maintenance-windows", json={"name": "Upgrade", "server_id": s["id"], "database_id": db["id"],
                                                      "kind": "once", "start": now - 60, "end": now + 3600})
    assert r.status_code == 201, r.text
    w = r.json()
    assert w["open"] and w["open_until"] == now + 3600
    listed = client.get("/api/maintenance-windows", params={"server_id": s["id"], "database_id": db["id"]}).json()
    assert [x["name"] for x in listed] == ["Upgrade"]
    assert client.get("/api/maintenance-windows", params={"server_id": s["id"], "database_id": other["id"]}).json() == []

    emit("schedule.failed", "critical", "Backup failed: shop", "x", server_id=s["id"], database_id=db["id"], key="a")
    emit("schedule.failed", "critical", "Backup failed: hr", "x", server_id=s["id"], database_id=other["id"], key="b")
    flush()
    titles = [receiver.json(i)["title"] for i in range(len(receiver.requests))]
    assert titles == ["Backup failed: hr"]  # shop's is muted
    hist = {a["title"]: a for a in client.get("/api/alerts/history").json()}
    assert hist["Backup failed: shop"]["muted"] == "Upgrade" and "muted" not in hist["Backup failed: hr"]

    # closed: sent again
    client.put(f"/api/maintenance-windows/{w['id']}", json={**{k: w[k] for k in ("name", "server_id", "database_id",
                                                                                  "kind", "start")}, "end": now - 1})
    emit("schedule.failed", "critical", "Backup failed again: shop", "x", server_id=s["id"], database_id=db["id"], key="c")
    flush()
    assert receiver.json(len(receiver.requests) - 1)["title"] == "Backup failed again: shop"
    assert client.delete(f"/api/maintenance-windows/{w['id']}").status_code == 204
    for rule in client.get("/api/alerts/rules").json():
        client.delete(f"/api/alerts/rules/{rule['id']}")
    client.delete(f"/api/alerts/channels/{hook['id']}")


@requires_server
def test_change_applied_in_the_next_window(client, database):
    sid, dbid = database
    cr = client.post("/api/changes", json={"server_id": sid, "database_id": dbid, "title": "Later",
                                           "sql": "CREATE TABLE IN_WINDOW (X INTEGER)"}).json()
    _job(client, client.post(f"/api/changes/{cr['id']}/dry-run"))
    client.post(f"/api/changes/{cr['id']}/approve")
    r = client.post(f"/api/changes/{cr['id']}/schedule")
    assert r.status_code == 400 and "No maintenance window" in r.json()["detail"]
    start = time.time() + 3600
    w = client.post("/api/maintenance-windows", json={"name": "Tonight", "server_id": sid, "database_id": dbid,
                                                      "kind": "once", "start": start, "end": start + 3600}).json()
    got = client.post(f"/api/changes/{cr['id']}/schedule").json()
    assert got["apply_in_window"]["at"] == start and got["apply_in_window"]["window"] == "Tonight"
    assert "run_as" not in got["apply_in_window"] and got["can"]["unschedule"] and not got["can"]["schedule"]
    assert W.apply_due(time.time()) == []  # not yet
    assert W.apply_due(start + 60) == [cr["id"]]
    cur = client.get(f"/api/changes/{cr['id']}").json()
    assert cur["apply_in_window"] is None and cur["history"][-1]["action"] in ("window_apply", "applying", "applied")
    job = wait_job(client, {"id": cur["apply_job"]})
    assert job["status"] == "finished", job["log"]
    assert client.get(f"/api/changes/{cr['id']}").json()["status"] == "applied"
    client.delete(f"/api/maintenance-windows/{w['id']}")


def test_who_may_manage_windows(srv):  # noqa: F811
    from .test_library import _switch
    from .test_security import ADMIN_PW, login

    c = srv
    login(c, "admin@example.org", ADMIN_PW)
    assert c.post("/api/admin/users", json={"username": "dave", "password": "dave-password", "role": "user"}).status_code == 201
    s = c.post("/api/servers", json={"name": "prod", "host": "h1", "shared": True}).json()
    db = c.post(f"/api/servers/{s['id']}/databases", json={"name": "sales", "path": "/db/sales.fdb"}).json()
    assert c.post("/api/admin/access/grants", json={"subject_type": "user", "subject": "dave", "server_id": s["id"],
                                                    "db_id": db["id"], "level": "read"}).status_code == 201
    w = c.post("/api/maintenance-windows", json={"name": "Nightly", "server_id": s["id"], "database_id": db["id"],
                                                 "cron": "0 22 * * *"}).json()
    _switch(c, "dave", "dave-password")
    assert [x["name"] for x in c.get("/api/maintenance-windows").json()] == ["Nightly"]
    for body in ({"name": "Mine", "server_id": s["id"], "database_id": db["id"], "cron": "@daily"},
                 {"name": "All", "cron": "@daily"}):
        assert c.post("/api/maintenance-windows", json=body).status_code == 403
    assert c.delete(f"/api/maintenance-windows/{w['id']}").status_code == 403
