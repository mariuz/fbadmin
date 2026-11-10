"""Change requests with approval (roadmap 2.4): propose, dry run with the schema diff, approve, apply; edits and
a later schema change invalidate the review; migration scripts; who may do what in server mode; the alert,
the timeline and the audit trail."""

from __future__ import annotations

from fbadmin import changes as C

from .conftest import DB_DIR, HOST, PASSWORD, PORT, USER, requires_server
from .test_alerts import add_webhook, flush, receiver  # noqa: F401 - fixture
from .test_library import _switch, srv  # noqa: F401 - fixture
from .test_maintenance import execute, wait_job
from .test_security import ADMIN_PW, login


def _job(client, r):
    assert r.status_code == 202, r.text
    return wait_job(client, r.json())


@requires_server
def test_change_request_flow(client, database, receiver):  # noqa: F811
    sid, dbid = database
    execute(client, sid, dbid, "CREATE TABLE ORDERS (ID INTEGER NOT NULL PRIMARY KEY, NOTE VARCHAR(20))")
    hook = add_webhook(client, receiver)
    client.post("/api/alerts/rules", json={"name": "changes", "event": "change.request", "server_id": sid,
                                           "database_id": dbid, "channels": [hook["id"]]})
    sql = "ALTER TABLE ORDERS ADD SHIPPED TIMESTAMP;\nCREATE INDEX IX_ORDERS_SHIPPED ON ORDERS (SHIPPED);\n"
    assert client.post("/api/changes", json={"server_id": sid, "database_id": dbid, "title": "x",
                                             "sql": "  "}).status_code == 400
    r = client.post("/api/changes", json={"server_id": sid, "database_id": dbid, "title": "Track shipping",
                                          "description": "For the courier report", "sql": sql})
    assert r.status_code == 201, r.text
    cr = r.json()
    cid = cr["id"]
    assert cr["status"] == "open" and cr["number"] >= 1 and cr["statements"] == 2
    assert cr["can"]["approve"] is False and "dry run" in cr["approve_blocked"]
    assert client.post(f"/api/changes/{cid}/approve").status_code == 409
    assert client.post(f"/api/changes/{cid}/apply").status_code == 403  # not approved

    # the dry run: on a copy, with the schema diff
    job = _job(client, client.post(f"/api/changes/{cid}/dry-run"))
    assert job["status"] == "finished", job["log"]
    cr = client.get(f"/api/changes/{cid}").json()
    d = cr["dry_run"]
    assert d["ok"] and d["stale"] is False
    chg = {(c["type"], c["name"]): c for c in d["changes"]}
    assert chg[("tables", "ORDERS")]["change"] == "altered" and "SHIPPED" in chg[("tables", "ORDERS")]["after"]
    assert any(c["change"] == "created" and c["name"] == "IX_ORDERS_SHIPPED" for c in d["changes"])
    cols = execute(client, sid, dbid, "SELECT COUNT(*) FROM RDB$RELATION_FIELDS WHERE RDB$RELATION_NAME = 'ORDERS' "
                                      "AND RDB$FIELD_NAME = 'SHIPPED'")["results"][0]["rows"][0][0]
    assert cols == 0  # the database itself is unchanged

    # an edit clears the review; the next dry run is needed again
    r = client.post(f"/api/changes/{cid}/approve", json={"text": "looks fine"})
    assert r.status_code == 200 and r.json()["status"] == "approved"
    r = client.put(f"/api/changes/{cid}", json={"sql": sql + "COMMENT ON COLUMN ORDERS.SHIPPED IS 'courier';\n"})
    cr = r.json()
    assert cr["status"] == "open" and cr["approvals"] == [] and cr["revision"] == 2 and cr["dry_run"]["stale"]
    _job(client, client.post(f"/api/changes/{cid}/dry-run"))
    assert client.post(f"/api/changes/{cid}/approve").json()["status"] == "approved"

    # someone changes the schema meanwhile: applying is refused until a new dry run
    execute(client, sid, dbid, "CREATE TABLE OTHER_CHANGE (X INTEGER)")
    r = client.post(f"/api/changes/{cid}/apply")
    assert r.status_code == 409 and "changed since the dry run" in r.json()["detail"]
    _job(client, client.post(f"/api/changes/{cid}/dry-run"))
    cr = client.get(f"/api/changes/{cid}").json()
    assert cr["status"] == "approved"  # the same schema changes: the approval stays
    job = _job(client, client.post(f"/api/changes/{cid}/apply"))
    assert job["status"] == "finished", job["log"]
    cr = client.get(f"/api/changes/{cid}").json()
    assert cr["status"] == "applied" and cr["applied"]["ok"] and len(cr["applied"]["results"]) == 3
    assert [e["action"] for e in cr["history"]] == ["submitted", "dry_run", "approved", "revised", "dry_run",
                                                    "approved", "dry_run", "applying", "applied"]
    cols = execute(client, sid, dbid, "SELECT COUNT(*) FROM RDB$RELATION_FIELDS WHERE RDB$RELATION_NAME = 'ORDERS' "
                                      "AND RDB$FIELD_NAME = 'SHIPPED'")["results"][0]["rows"][0][0]
    assert cols == 1
    assert client.post(f"/api/changes/{cid}/apply").status_code == 403  # once
    assert client.put(f"/api/changes/{cid}", json={"title": "y"}).status_code == 403

    # the alert and the timeline
    flush()
    titles = [receiver.json(i)["title"] for i in range(len(receiver.requests))]
    assert any(t.startswith("Change request #") and "for " in t for t in titles)  # submitted
    assert any("applied to" in t for t in titles)
    tl = client.get(f"/api/servers/{sid}/databases/{dbid}/timeline").json()["events"]
    acts = [e["action"] for e in tl]
    assert "change_applied" in acts and "change_submitted" in acts and "change_approved" in acts

    # lists, a rejection with its reason, a withdrawal
    r2 = client.post("/api/changes", json={"server_id": sid, "database_id": dbid, "title": "Drop it",
                                           "sql": "DROP TABLE ORDERS"}).json()
    assert client.post(f"/api/changes/{r2['id']}/reject", json={"text": ""}).status_code == 400
    assert client.post(f"/api/changes/{r2['id']}/reject", json={"text": "we still need it"}).json()["status"] \
        == "rejected"
    r3 = client.post("/api/changes", json={"server_id": sid, "database_id": dbid, "title": "Later",
                                           "sql": "CREATE TABLE LATER (X INTEGER)"}).json()
    assert client.post(f"/api/changes/{r3['id']}/comments", json={"text": "after the release"}).json()["history"][-1] \
        == {**client.get(f"/api/changes/{r3['id']}").json()["history"][-1], "detail": "after the release"}
    assert client.post(f"/api/changes/{r3['id']}/withdraw").json()["status"] == "withdrawn"
    active = client.get("/api/changes", params={"database_id": dbid, "status": "active"}).json()
    assert [x["id"] for x in active] == []
    every = client.get("/api/changes", params={"database_id": dbid}).json()
    assert [x["status"] for x in every] == ["withdrawn", "rejected", "applied"] and "sql" not in every[0]

    for r in client.get("/api/alerts/rules").json():
        client.delete(f"/api/alerts/rules/{r['id']}")
    client.delete(f"/api/alerts/channels/{hook['id']}")


@requires_server
def test_change_request_with_migrations(client, database):
    sid, dbid = database
    scripts = [{"name": "V1__customers.sql", "sql": "CREATE TABLE CUSTOMERS (ID INTEGER NOT NULL PRIMARY KEY);"},
               {"name": "V2__customer_name.sql", "sql": "ALTER TABLE CUSTOMERS ADD NAME VARCHAR(40);"}]
    assert client.post("/api/changes", json={"server_id": sid, "database_id": dbid, "title": "x", "kind": "migrations",
                                             "scripts": [{"name": "notes.txt", "sql": "x"}]}).status_code == 400
    cr = client.post("/api/changes", json={"server_id": sid, "database_id": dbid, "title": "Customers",
                                           "kind": "migrations", "scripts": scripts}).json()
    job = _job(client, client.post(f"/api/changes/{cr['id']}/dry-run"))
    assert job["status"] == "finished", job["log"]
    d = client.get(f"/api/changes/{cr['id']}").json()["dry_run"]
    assert d["pending"] == ["V1__customers.sql", "V2__customer_name.sql"]
    assert any(c["name"] == "CUSTOMERS" and c["change"] == "created" for c in d["changes"])
    client.post(f"/api/changes/{cr['id']}/approve")
    job = _job(client, client.post(f"/api/changes/{cr['id']}/apply"))
    assert job["status"] == "finished", job["log"]
    st = client.post(f"/api/servers/{sid}/databases/{dbid}/migrations/status",
                     json={"scripts": scripts}).json()
    assert st["current"] == "2" and st["pending"] == []


@requires_server
def test_change_request_permissions(srv, tmp_path):  # noqa: F811
    """dave (read) proposes; dave cannot review; carol (admin) reviews and applies; nobody approves their own."""
    import uuid

    c = srv
    login(c, "admin@example.org", ADMIN_PW)
    for name in ("dave", "carol", "erin"):
        assert c.post("/api/admin/users", json={"username": name, "password": f"{name}-password",
                                                "role": "user"}).status_code == 201
    s = c.post("/api/servers", json={"name": "fb", "host": HOST, "port": PORT, "user": USER, "shared": True}).json()
    path = f"{DB_DIR}/fbadmin_cr_{uuid.uuid4().hex[:8]}.fdb"
    c.post(f"/api/servers/{s['id']}/connect", json={"password": PASSWORD})
    db = c.post(f"/api/servers/{s['id']}/databases/create", json={"name": "app", "path": path})
    assert db.status_code == 201, db.text
    dbid = db.json()["id"]
    for name, lvl in (("dave", "read"), ("carol", "admin"), ("erin", "none")):
        assert c.post("/api/admin/access/grants", json={"subject_type": "user", "subject": name, "server_id": s["id"],
                                                        "db_id": dbid, "level": lvl}).status_code == 201
    try:
        _switch(c, "dave", "dave-password")
        c.post(f"/api/servers/{s['id']}/connect", json={"password": PASSWORD})
        assert c.post(f"/api/servers/{s['id']}/databases/{dbid}/execute",
                      json={"sql": "CREATE TABLE T (X INTEGER)"}).status_code == 403  # dave may not change it...
        cr = c.post("/api/changes", json={"server_id": s["id"], "database_id": dbid, "title": "Add T",
                                          "sql": "CREATE TABLE T (X INTEGER)"})
        assert cr.status_code == 201, cr.text  # ...but may propose it
        cid = cr.json()["id"]
        assert cr.json()["author"] == "dave" and cr.json()["mine"] is True and cr.json()["can"]["dry_run"] is False
        assert c.post(f"/api/changes/{cid}/dry-run").status_code == 403
        assert c.post(f"/api/changes/{cid}/reject", json={"text": "no"}).status_code == 403

        _switch(c, "erin", "erin-password")  # no access: the request does not exist for her
        assert c.get(f"/api/changes/{cid}").status_code == 404 and c.get("/api/changes").json() == []

        _switch(c, "carol", "carol-password")
        c.post(f"/api/servers/{s['id']}/connect", json={"password": PASSWORD})
        cr = c.get(f"/api/changes/{cid}").json()
        assert cr["can"]["dry_run"] and cr["can"]["edit"] is False and cr["mine"] is False
        assert c.put(f"/api/changes/{cid}", json={"title": "mine now"}).status_code == 403
        job = _job(c, c.post(f"/api/changes/{cid}/dry-run"))
        assert job["status"] == "finished", job["log"]
        assert c.post(f"/api/changes/{cid}/approve", json={"text": "ok"}).json()["status"] == "approved"

        # carol's own request needs someone else's approval
        own = c.post("/api/changes", json={"server_id": s["id"], "database_id": dbid, "title": "Mine",
                                           "sql": "CREATE TABLE U (X INTEGER)"}).json()
        _job(c, c.post(f"/api/changes/{own['id']}/dry-run"))
        r = c.post(f"/api/changes/{own['id']}/approve")
        assert r.status_code == 403 and "someone else" in r.json()["detail"]

        job = _job(c, c.post(f"/api/changes/{cid}/apply"))
        assert job["status"] == "finished", job["log"]
        done = c.get(f"/api/changes/{cid}").json()
        assert done["status"] == "applied" and done["applied"]["by"] == "carol"
        assert [(e["action"], e["by"]) for e in done["history"]][:3] == [("submitted", "dave"), ("dry_run", "carol"),
                                                                          ("approved", "carol")]
        _switch(c, "admin@example.org", ADMIN_PW)
        rows = c.get("/api/admin/audit", params={"limit": 200}).json()["rows"]
        assert any("change request" in (e.get("statement") or "") for e in rows)
    finally:
        _switch(c, "admin@example.org", ADMIN_PW)
        c.post(f"/api/servers/{s['id']}/databases/{dbid}/drop")


def test_store_reload_marks_an_interrupted_apply(tmp_path, monkeypatch):
    from fbadmin.config import settings

    monkeypatch.setattr(settings, "data_dir", tmp_path)
    st = C.get_store()
    cr = st.add(C.ChangeRequest(server_id="s", database_id="d", title="t", sql="CREATE TABLE T (X INTEGER)"))
    st.update(cr.id, lambda c: setattr(c, "status", "applying"))
    again = C.ChangeStore(st.path).get(cr.id)
    assert again.status == "failed" and "stopped while applying" in again.history[-1].detail
