"""Fleet health (roadmap 2.5): one row per visible database from fbadmin's own records (no connection), its
status from the worst signal, and only the databases the user may read. No Firebird server needed."""

from __future__ import annotations

import time

from fbadmin import changes as CR
from fbadmin import fleet as F
from fbadmin.maintenance.scheduler import get_scheduler
from fbadmin.monitoring import forecast

from .test_library import _switch, desktop, srv  # noqa: F401 - fixtures
from .test_security import ADMIN_PW, login


def _schedule(c, sid, dbid, kind):
    if kind == "schema_drift":  # a drift schedule needs a schema history repository: add it directly
        from fbadmin.maintenance.scheduler import Schedule

        return get_scheduler().add(Schedule(name="drift", kind=kind, server_id=sid, database_id=dbid, cron="0 3 * * *"))
    r = c.post("/api/schedules", json={"name": f"{kind} {dbid}", "kind": kind, "server_id": sid, "database_id": dbid,
                                       "cron": "0 3 * * *"})
    assert r.status_code == 201, r.text
    return get_scheduler().get(r.json()["id"])


def test_fleet_rows_and_status(desktop):  # noqa: F811
    c = desktop
    s = c.post("/api/servers", json={"name": "prod", "host": "h"}).json()
    ok = c.post(f"/api/servers/{s['id']}/databases", json={"name": "ok", "path": "/db/ok.fdb"}).json()
    bad = c.post(f"/api/servers/{s['id']}/databases", json={"name": "bad", "path": "/db/bad.fdb"}).json()
    c.post(f"/api/servers/{s['id']}/databases", json={"name": "new", "path": "/db/new.fdb"}).json()
    now = time.time()
    sch = get_scheduler()

    def rec(x, at, status, error=None, **extra):
        sch._record(x, None, at, status, error, **extra)
        sch.history[-1]["finished_at"] = at
    # ok: a backup an hour ago, a clean scheduled check-up, a quiet forecast
    b = _schedule(c, s["id"], ok["id"], "backup")
    rec(b, now - 3700, "finished", file="ok-1.fbk")
    cu = _schedule(c, s["id"], ok["id"], "checkup")
    rec(cu, now - 600, "finished", summary={"critical": 0, "warning": 0, "info": 2})
    forecast._remember(s["id"], ok["id"], {"findings": [], "enough": True, "size": {"bytes": 10 << 20}}, now)
    # bad: the backup is old and the newest one failed; drift; a sequence near its limit; a failed change
    b2 = _schedule(c, s["id"], bad["id"], "backup")
    rec(b2, now - 10 * 86400, "finished", file="bad-1.fbk")
    rec(b2, now - 3600, "failed", "disk full")
    d = _schedule(c, s["id"], bad["id"], "schema_drift")
    rec(d, now - 1800, "drift", summary={"missing": 1, "extra": 0, "changed": 0})
    forecast._remember(s["id"], bad["id"], {"findings": [{"key": "seq:S", "severity": "critical",
                                                          "title": "Sequence S feeding T.ID (INTEGER) is at 97 %"}],
                                            "enough": True, "size": {"bytes": 1}}, now)
    cr = CR.get_store().add(CR.ChangeRequest(server_id=s["id"], database_id=bad["id"], title="x", sql="SELECT 1 FROM RDB$DATABASE"))
    CR.get_store().update(cr.id, lambda x: setattr(x, "status", "failed"))

    data = c.get("/api/fleet").json()
    rows = {r["database"]: r for r in data["databases"]}
    assert [r["database"] for r in data["databases"]][0] == "bad"  # the worst first
    assert data["counts"]["critical"] == 1 and data["counts"]["ok"] == 1 and data["counts"]["unknown"] == 1

    r = rows["ok"]
    assert r["status"] == "ok" and r["problems"] == []
    assert r["backup"]["file"] == "ok-1.fbk" and 0.9 < r["backup"]["age_hours"] < 1.2 and r["backup"]["error"] is None
    assert r["checkup"]["source"] == "scheduled" and r["checkup"]["info"] == 2
    assert r["forecast"]["severity"] == "ok" and r["forecast"]["size_bytes"] == 10 << 20
    assert r["schedules"] == ["backup", "checkup"]

    r = rows["bad"]
    assert r["status"] == "critical"
    assert r["backup"]["error"] == "disk full" and r["backup"]["file"] == "bad-1.fbk"
    assert r["drift"]["drift"] is True and r["changes"]["failed"] == 1
    assert r["problems"][0] in ("the last backup failed", "forecast: Sequence S feeding T.ID (INTEGER) is at 97 %")
    assert "schema drift" in r["problems"] and "1 change request(s) failed" in r["problems"]
    assert any(p.startswith("forecast: Sequence S") for p in r["problems"])

    r = rows["new"]
    assert r["status"] == "unknown" and r["backup"] is None and r["checkup"] is None and r["forecast"] is None

    # an old backup alone is a warning, then critical after a week
    assert F.row(s["id"], type("S", (), {"name": "prod"}), type("D", (), {"id": ok["id"], "name": "ok"}),
                 hist=[{"kind": "backup", "status": "finished", "finished_at": now - 48 * 3600, "file": "f"}],
                 changes=[], alerts=[], now=now)["status"] == "warning"
    for x in (b, cu, b2, d):
        c.delete(f"/api/schedules/{x.id}")


def test_fleet_shows_only_readable_databases(srv):  # noqa: F811
    c = srv
    login(c, "admin@example.org", ADMIN_PW)
    assert c.post("/api/admin/users", json={"username": "dave", "password": "dave-password", "role": "user"}).status_code == 201
    s = c.post("/api/servers", json={"name": "prod", "host": "h1", "shared": True}).json()
    sales = c.post(f"/api/servers/{s['id']}/databases", json={"name": "sales", "path": "/db/sales.fdb"}).json()
    c.post(f"/api/servers/{s['id']}/databases", json={"name": "hr", "path": "/db/hr.fdb"})
    assert {r["database"] for r in c.get("/api/fleet").json()["databases"]} == {"sales", "hr"}
    assert c.post("/api/admin/access/grants", json={"subject_type": "user", "subject": "dave", "server_id": s["id"],
                                                    "db_id": sales["id"], "level": "read"}).status_code == 201
    _switch(c, "dave", "dave-password")
    assert [r["database"] for r in c.get("/api/fleet").json()["databases"]] == ["sales"]
