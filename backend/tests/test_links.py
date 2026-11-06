"""Shareable links (roadmap 2.3): links to objects, saved queries, notebooks and result snapshots, opened with
the viewer's own permissions; expiry, revocation and listing. These tests do not need a Firebird server."""

from __future__ import annotations

import time

from fbadmin import links as L

from .test_library import _switch, desktop, srv  # noqa: F401 - fixtures
from .test_security import ADMIN_PW, login

SNAP = {"sql": "SELECT REGION, SUM(AMOUNT) FROM SALES GROUP BY 1",
        "columns": [{"name": "REGION", "type": "VARCHAR"}, {"name": "SUM", "type": "BIGINT"}],
        "rows": [["north", 10], ["south", 7]], "row_count": 2}


def test_desktop_links(desktop):  # noqa: F811
    c = desktop
    s = c.post("/api/servers", json={"name": "local", "host": "127.0.0.1", "port": 9}).json()
    d = c.post(f"/api/servers/{s['id']}/databases", json={"name": "app", "path": "/db/app.fdb"}).json()
    r = c.post("/api/links", json={"kind": "object", "title": "Orders table", "server_id": s["id"],
                                   "database_id": d["id"], "otype": "tables", "name": "ORDERS"})
    assert r.status_code == 201, r.text
    lk = r.json()
    assert lk["path"] == f"/#/link/{lk['id']}" and len(lk["id"]) >= 10 and lk["kind"] == "object"
    o = c.get(f"/api/links/{lk['id']}").json()
    assert (o["kind"], o["otype"], o["name"], o["database"]) == ("object", "tables", "ORDERS", "app")
    assert c.get("/api/links").json()["links"][0]["opened"] == 1
    # a snapshot keeps the rows (capped)
    big = {**SNAP, "rows": [[str(i), i] for i in range(L.MAX_ROWS + 10)]}
    sn = c.post("/api/links", json={"kind": "snapshot", "server_id": s["id"], "database_id": d["id"], "snapshot": big}).json()
    assert sn["rows"] == L.MAX_ROWS
    got = c.get(f"/api/links/{sn['id']}").json()["snapshot"]
    assert got["truncated"] is True and len(got["rows"]) == L.MAX_ROWS and got["sql"].startswith("SELECT")
    # invalid links
    assert c.post("/api/links", json={"kind": "object", "server_id": s["id"], "database_id": d["id"]}).status_code == 400
    assert c.post("/api/links", json={"kind": "snapshot", "server_id": s["id"], "database_id": d["id"]}).status_code == 400
    assert c.post("/api/links", json={"kind": "query", "item_id": "nope"}).status_code == 400
    assert c.post("/api/links", json={"kind": "object", "server_id": "nope", "database_id": "x", "otype": "tables",
                                      "name": "T"}).status_code == 400
    assert c.get("/api/links/doesnotexist").status_code == 404
    # expiry and revocation
    ex = c.post("/api/links", json={"kind": "object", "server_id": s["id"], "database_id": d["id"], "otype": "views",
                                    "name": "V", "expires_days": 0.00001}).json()
    assert ex["expires_at"] > time.time() - 5
    time.sleep(1.2)
    assert c.get(f"/api/links/{ex['id']}").status_code == 404
    assert c.delete(f"/api/links/{lk['id']}").status_code == 204
    assert c.get(f"/api/links/{lk['id']}").status_code == 404
    L._store = None  # survives a restart
    assert c.get(f"/api/links/{sn['id']}").status_code == 200


def test_links_follow_the_viewers_permissions(srv):  # noqa: F811
    c = srv
    login(c, "admin@example.org", ADMIN_PW)
    for name in ("alice", "dave"):
        assert c.post("/api/admin/users", json={"username": name, "password": f"{name}-password",
                                                "role": "user"}).status_code == 201
    s1 = c.post("/api/servers", json={"name": "prod", "host": "h1", "shared": True}).json()
    sales = c.post(f"/api/servers/{s1['id']}/databases", json={"name": "sales", "path": "/db/sales.fdb"}).json()
    hr = c.post(f"/api/servers/{s1['id']}/databases", json={"name": "hr", "path": "/db/hr.fdb"}).json()
    # dave may read sales only; alice has no grants (everything)
    assert c.post("/api/admin/access/grants", json={"subject_type": "user", "subject": "dave", "server_id": s1["id"],
                                                    "db_id": sales["id"], "level": "read"}).status_code == 201

    _switch(c, "alice", "alice-password")

    def mk(**body):
        r = c.post("/api/links", json=body)
        assert r.status_code == 201, r.text
        return r.json()["id"]

    on_sales = mk(kind="object", server_id=s1["id"], database_id=sales["id"], otype="tables", name="ORDERS")
    on_hr = mk(kind="object", server_id=s1["id"], database_id=hr["id"], otype="tables", name="SALARIES")
    snap_sales = mk(kind="snapshot", server_id=s1["id"], database_id=sales["id"], snapshot=SNAP)
    snap_hr = mk(kind="snapshot", server_id=s1["id"], database_id=hr["id"], snapshot=SNAP)
    private = c.post("/api/library/items", json={"name": "mine", "sql": "SELECT 1 FROM RDB$DATABASE"}).json()
    shared = c.post("/api/library/items", json={"name": "team", "sql": "SELECT 2 FROM RDB$DATABASE",
                                                "visibility": "shared"}).json()
    q_private, q_shared = mk(kind="query", item_id=private["id"]), mk(kind="query", item_id=shared["id"])
    nb = c.post("/api/notebooks", json={"name": "close", "visibility": "shared", "server_id": s1["id"],
                                        "database_id": sales["id"]}).json()
    nb_link = mk(kind="notebook", item_id=nb["id"], title="Month-end close")

    _switch(c, "dave", "dave-password")
    ok = c.get(f"/api/links/{on_sales}")
    assert ok.status_code == 200 and ok.json()["name"] == "ORDERS" and ok.json()["created_by"] == "alice"
    no = c.get(f"/api/links/{on_hr}")
    assert no.status_code == 403 and no.json()["code"] == "link_denied" and no.json()["created_by"] == "alice"
    assert c.get(f"/api/links/{snap_sales}").json()["snapshot"]["rows"] == [["north", 10], ["south", 7]]
    deny = c.get(f"/api/links/{snap_hr}")
    assert deny.status_code == 403 and "snapshot" not in deny.json() and "north" not in deny.text
    assert c.get(f"/api/links/{q_private}").status_code == 403
    assert c.get(f"/api/links/{q_shared}").json()["sql"] == "SELECT 2 FROM RDB$DATABASE"
    assert c.get(f"/api/links/{nb_link}").json()["name"] == "close"
    # dave cannot make a link he could not open himself, nor revoke alice's
    r = c.post("/api/links", json={"kind": "object", "server_id": s1["id"], "database_id": hr["id"],
                                   "otype": "tables", "name": "SALARIES"})
    assert r.status_code == 400
    assert c.delete(f"/api/links/{on_sales}").status_code == 403
    assert c.get("/api/links").json()["links"] == []
    mine = c.post("/api/links", json={"kind": "object", "server_id": s1["id"], "database_id": sales["id"],
                                      "otype": "views", "name": "V_ORDERS"}).json()
    assert [x["id"] for x in c.get("/api/links").json()["links"]] == [mine["id"]]

    # the owner makes the notebook private: the link stops working for dave
    _switch(c, "alice", "alice-password")
    assert len(c.get("/api/links").json()["links"]) == 7
    c.put(f"/api/notebooks/{nb['id']}", json={"visibility": "private"})
    _switch(c, "dave", "dave-password")
    assert c.get(f"/api/links/{nb_link}").status_code == 403

    # an administrator sees every link and may revoke any
    _switch(c, "admin@example.org", ADMIN_PW)
    assert len(c.get("/api/links", params={"all": "true"}).json()["links"]) == 8
    assert c.delete(f"/api/links/{on_sales}").status_code == 204
    assert c.get(f"/api/links/{on_sales}").status_code == 404
