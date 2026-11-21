"""Comments on objects and notebooks (roadmap 2.4): threads per object, who sees them (the object's read level,
the notebook's visibility), edit and delete rights, counts per database, the comment.added alert, and the
notebook's comments going with it. These tests do not need a Firebird server."""

from __future__ import annotations

from .test_library import _switch, desktop, srv  # noqa: F401 - fixtures
from .test_security import ADMIN_PW, login


def test_desktop_threads(desktop):  # noqa: F811
    c = desktop
    s = c.post("/api/servers", json={"name": "local", "host": "h"}).json()
    db = c.post(f"/api/servers/{s['id']}/databases", json={"name": "app", "path": "/db/app.fdb"}).json()
    obj = {"server_id": s["id"], "database_id": db["id"], "otype": "indices", "name": "IX_ORDERS_DAY"}
    assert c.post("/api/comments", json={**obj, "text": "   "}).status_code == 400
    a = c.post("/api/comments", json={**obj, "text": "Needed by the nightly report"})
    assert a.status_code == 201, a.text
    b = c.post("/api/comments", json={**obj, "text": "Rebuilt after the March load"}).json()
    other = c.post("/api/comments", json={**obj, "name": "IX_OTHER", "text": "x"}).json()
    thread = c.get("/api/comments", params={k: v for k, v in obj.items()}).json()
    assert [x["text"] for x in thread] == ["Needed by the nightly report", "Rebuilt after the March load"]
    assert thread[0]["mine"] and thread[0]["can_delete"]
    r = c.put(f"/api/comments/{b['id']}", json={"text": "Rebuilt after the April load"})
    assert r.json()["text"] == "Rebuilt after the April load" and r.json()["edited_at"]
    counts = c.get("/api/comments/counts", params={"server_id": s["id"], "database_id": db["id"]}).json()
    assert {(x["name"], x["count"]) for x in counts} == {("IX_ORDERS_DAY", 2), ("IX_OTHER", 1)}
    assert c.delete(f"/api/comments/{other['id']}").status_code == 204
    assert c.get("/api/comments", params={**obj, "name": "IX_OTHER"}).json() == []
    # an unknown database: as if the object did not exist
    assert c.get("/api/comments", params={**obj, "database_id": "nope"}).status_code == 404

    # a notebook's thread, deleted with the notebook
    nb = c.post("/api/notebooks", json={"name": "close", "server_id": s["id"], "database_id": db["id"]}).json()
    t = {"kind": "notebook", "notebook_id": nb["id"]}
    assert c.post("/api/comments", json={**t, "text": "Run it after the month-end close"}).status_code == 201
    assert len(c.get("/api/comments", params=t).json()) == 1
    assert c.delete(f"/api/notebooks/{nb['id']}").status_code == 204
    assert c.get("/api/comments", params=t).status_code == 404
    from fbadmin import comments as C

    assert not [x for x in C.get_store().items.values() if x.target.notebook_id == nb["id"]]


def test_comments_follow_permissions(srv):  # noqa: F811
    c = srv
    login(c, "admin@example.org", ADMIN_PW)
    for name in ("alice", "dave"):
        assert c.post("/api/admin/users", json={"username": name, "password": f"{name}-password",
                                                "role": "user"}).status_code == 201
    s = c.post("/api/servers", json={"name": "prod", "host": "h1", "shared": True}).json()
    sales = c.post(f"/api/servers/{s['id']}/databases", json={"name": "sales", "path": "/db/sales.fdb"}).json()
    hr = c.post(f"/api/servers/{s['id']}/databases", json={"name": "hr", "path": "/db/hr.fdb"}).json()
    # dave reads sales, except the SECRET* objects; no access to hr
    for g in ({"db_id": sales["id"], "level": "read"},
              {"db_id": sales["id"], "object_pattern": "SECRET*", "level": "none"}):
        r = c.post("/api/admin/access/grants", json={"subject_type": "user", "subject": "dave", "server_id": s["id"], **g})
        assert r.status_code == 201, r.text
    on = lambda db, name: {"server_id": s["id"], "database_id": db["id"], "otype": "tables", "name": name}  # noqa: E731

    _switch(c, "alice", "alice-password")
    mine = c.post("/api/comments", json={**on(sales, "ORDERS"), "text": "Partitioned by month"}).json()
    c.post("/api/comments", json={**on(sales, "SECRET_PAY"), "text": "Payroll, keep private"})
    c.post("/api/comments", json={**on(hr, "STAFF"), "text": "HR only"})
    nb_private = c.post("/api/notebooks", json={"name": "mine", "server_id": s["id"], "database_id": sales["id"]}).json()
    nb_shared = c.post("/api/notebooks", json={"name": "team", "visibility": "shared", "server_id": s["id"],
                                               "database_id": sales["id"]}).json()
    for nb in (nb_private, nb_shared):
        assert c.post("/api/comments", json={"kind": "notebook", "notebook_id": nb["id"], "text": "note"}).status_code == 201

    _switch(c, "dave", "dave-password")
    t = c.get("/api/comments", params=on(sales, "ORDERS")).json()
    assert [x["text"] for x in t] == ["Partitioned by month"] and t[0]["author"] == "alice" and not t[0]["mine"]
    assert c.get("/api/comments", params=on(sales, "SECRET_PAY")).status_code == 404
    assert c.post("/api/comments", json={**on(sales, "SECRET_PAY"), "text": "?"}).status_code == 404
    assert c.get("/api/comments", params=on(hr, "STAFF")).status_code == 404
    counts = c.get("/api/comments/counts", params={"server_id": s["id"], "database_id": sales["id"]}).json()
    assert [x["name"] for x in counts] == ["ORDERS"]  # the hidden table's comment is not counted
    assert c.get("/api/comments", params={"kind": "notebook", "notebook_id": nb_private["id"]}).status_code == 404
    assert len(c.get("/api/comments", params={"kind": "notebook", "notebook_id": nb_shared["id"]}).json()) == 1
    # dave comments, and may not change alice's comment
    own = c.post("/api/comments", json={**on(sales, "ORDERS"), "text": "Also used by the BI export"}).json()
    assert own["author"] == "dave" and own["mine"]
    assert c.put(f"/api/comments/{mine['id']}", json={"text": "changed"}).status_code == 403
    assert c.delete(f"/api/comments/{mine['id']}").status_code == 403

    # an fbadmin administrator may delete it, not edit it
    _switch(c, "admin@example.org", ADMIN_PW)
    assert c.put(f"/api/comments/{own['id']}", json={"text": "x"}).status_code == 403
    assert c.delete(f"/api/comments/{own['id']}").status_code == 204
