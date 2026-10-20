"""Query library (milestone 2.0): CRUD, search, revisions, and private/shared items in server mode.

These tests do not need a Firebird server.
"""

from __future__ import annotations

import json

import pytest

from tests.test_security import ADMIN_PW, _client, login


@pytest.fixture
def desktop(tmp_path, monkeypatch):
    with _client(tmp_path, monkeypatch) as c:
        yield c
    from fbadmin.api import deps

    deps.reset_state()


@pytest.fixture
def srv(tmp_path, monkeypatch):
    with _client(tmp_path, monkeypatch, SERVER_MODE="1", ADMIN_EMAIL="admin@example.org",
                 ADMIN_PASSWORD=ADMIN_PW) as c:
        yield c
    from fbadmin.api import deps

    deps.reset_state()


def _switch(c, user: str, pw: str) -> None:
    c.post("/api/auth/logout")
    c.headers.pop("X-CSRF-Token", None)
    login(c, user, pw)


ITEM = {"name": "Active statements", "description": "What runs right now", "sql": "SELECT * FROM MON$STATEMENTS",
        "tags": ["monitoring", " MON$ ", "monitoring"], "folder": " Ops / Monitoring /"}


def test_crud_search_and_revisions(desktop):
    c = desktop
    r = c.post("/api/library/items", json=ITEM)
    assert r.status_code == 201, r.text
    it = r.json()
    assert it["tags"] == ["monitoring", "MON$"]  # trimmed, de-duplicated
    assert it["folder"] == "Ops/Monitoring"
    assert it["visibility"] == "private" and it["can_edit"] and it["rev"] == 1
    other = c.post("/api/library/items", json={"name": "Customers by city", "sql": "SELECT * FROM CUSTOMER "
                                                "WHERE CITY = :city", "tags": ["report"], "folder": "Reports"}).json()
    assert c.post("/api/library/items", json={"name": " ", "sql": "x"}).status_code == 400
    assert c.post("/api/library/items", json={"name": "x", "sql": "  "}).status_code == 400

    names = lambda **q: [i["name"] for i in c.get("/api/library/items", params=q).json()]  # noqa: E731
    assert names() == ["Active statements", "Customers by city"]  # sorted by folder, then name
    assert names(q="mon$statements") == ["Active statements"]  # SQL text
    assert names(q="city customer") == ["Customers by city"]  # every word must match
    assert names(q="right now") == ["Active statements"]  # description
    assert names(tag="REPORT") == ["Customers by city"]
    assert names(folder="Ops") == ["Active statements"]  # includes sub-folders
    assert names(folder="Ops/Monitoring") == ["Active statements"]
    assert names(q="nothing-like-this") == []
    info = c.get("/api/library").json()
    assert info["server_mode"] is False
    assert {f["path"] for f in info["folders"]} == {"Ops", "Ops/Monitoring", "Reports"}
    assert {t["tag"]: t["count"] for t in info["tags"]} == {"monitoring": 1, "MON$": 1, "report": 1}

    # updates keep revisions; an unchanged save does not add one
    r = c.put(f"/api/library/items/{it['id']}", json={"sql": "SELECT MON$SQL_TEXT FROM MON$STATEMENTS"})
    assert r.status_code == 200 and r.json()["rev"] == 2
    assert c.put(f"/api/library/items/{it['id']}", json={"sql": "SELECT MON$SQL_TEXT FROM MON$STATEMENTS"}).json()["rev"] == 2
    c.put(f"/api/library/items/{it['id']}", json={"name": "Running statements", "server_id": "s1", "database_id": "d1"})
    full = c.get(f"/api/library/items/{it['id']}").json()
    assert full["rev"] == 3 and full["server_id"] == "s1" and full["database_id"] == "d1"
    assert [r["rev"] for r in full["revisions"]] == [3, 2, 1]
    assert full["revisions"][2]["sql"] == ITEM["sql"]
    # clearing the default database
    assert c.put(f"/api/library/items/{it['id']}", json={"server_id": None}).json()["database_id"] is None
    r = c.post(f"/api/library/items/{it['id']}/revisions/1/restore")
    assert r.status_code == 200
    assert r.json()["sql"] == ITEM["sql"] and r.json()["name"] == "Active statements" and r.json()["rev"] == 5
    assert c.post(f"/api/library/items/{it['id']}/revisions/99/restore").status_code == 404
    revs = c.get(f"/api/library/items/{it['id']}/revisions").json()
    assert revs[0]["rev"] == 5 and revs[0]["author"] is None

    # copy, delete
    cp = c.post(f"/api/library/items/{other['id']}/copy", json={}).json()
    assert cp["name"] == "Customers by city (copy)" and cp["id"] != other["id"] and cp["rev"] == 1
    assert c.delete(f"/api/library/items/{cp['id']}").status_code == 204
    assert c.get(f"/api/library/items/{cp['id']}").status_code == 404

    # desktop mode has nobody to share with
    assert c.put(f"/api/library/items/{other['id']}", json={"visibility": "shared"}).json()["visibility"] == "private"

    # persisted to <data dir>/library.json (0600), and reloaded
    from fbadmin import config, library

    path = config.settings.data_dir / "library.json"
    assert oct(path.stat().st_mode & 0o777) == "0o600"
    assert len(json.loads(path.read_text())["items"]) == 2
    assert len(library.LibraryStore(path).items) == 2


def test_revisions_are_capped(desktop, monkeypatch):
    from fbadmin import library

    monkeypatch.setattr(library, "REVISIONS_KEEP", 3)
    c = desktop
    it = c.post("/api/library/items", json={"name": "n", "sql": "SELECT 0 FROM RDB$DATABASE"}).json()
    for i in range(1, 6):
        c.put(f"/api/library/items/{it['id']}", json={"sql": f"SELECT {i} FROM RDB$DATABASE"})
    revs = c.get(f"/api/library/items/{it['id']}/revisions").json()
    assert [r["rev"] for r in revs] == [6, 5, 4]
    assert c.post(f"/api/library/items/{it['id']}/revisions/1/restore").status_code == 404


def test_import_browser_snippets(desktop):
    c = desktop
    snippets = [{"name": "Execute block", "sql": "EXECUTE BLOCK AS BEGIN END"}, {"name": "x", "sql": ""},
                {"name": "Context", "sql": "SELECT CURRENT_USER FROM RDB$DATABASE"}]
    r = c.post("/api/library/import", json={"items": snippets})
    assert r.json() == {"imported": 2}
    # importing again (second browser, page reload) does not duplicate
    assert c.post("/api/library/import", json={"items": snippets}).json() == {"imported": 0}
    items = c.get("/api/library/items").json()
    assert {i["folder"] for i in items} == {"Snippets"}
    assert {i["name"] for i in items} == {"Execute block", "Context"}


def test_server_mode_visibility_and_permissions(srv):
    c = srv
    login(c, "admin@example.org", ADMIN_PW)
    for name in ("alice", "bob"):
        assert c.post("/api/admin/users", json={"username": name, "password": f"{name}-password",
                                                "role": "user"}).status_code == 201
    _switch(c, "alice", "alice-password")
    info = c.get("/api/library").json()
    assert info["server_mode"] is True and info["user"] == "alice" and info["is_admin"] is False
    priv = c.post("/api/library/items", json={"name": "alice private", "sql": "SELECT 1 FROM RDB$DATABASE"}).json()
    shared = c.post("/api/library/items", json={"name": "team report", "sql": "SELECT 2 FROM RDB$DATABASE",
                                                "visibility": "shared", "tags": ["team"]}).json()
    assert shared["owner"] == "alice" and shared["mine"] and shared["can_edit"]

    # bob sees the shared item only, cannot change or delete it, but can save a copy
    _switch(c, "bob", "bob-password")
    items = c.get("/api/library/items").json()
    assert [i["name"] for i in items] == ["team report"]
    assert items[0]["can_edit"] is False and items[0]["mine"] is False
    assert c.get(f"/api/library/items/{priv['id']}").status_code == 404
    assert c.put(f"/api/library/items/{priv['id']}", json={"name": "x"}).status_code == 404
    r = c.put(f"/api/library/items/{shared['id']}", json={"sql": "DELETE FROM X"})
    assert r.status_code == 403 and r.json()["code"] == "forbidden"
    assert c.delete(f"/api/library/items/{shared['id']}").status_code == 403
    assert c.post(f"/api/library/items/{shared['id']}/revisions/1/restore").status_code == 403
    cp = c.post(f"/api/library/items/{shared['id']}/copy", json={"name": "bob's version"}).json()
    assert cp["owner"] == "bob" and cp["visibility"] == "private" and cp["can_edit"]
    assert c.put(f"/api/library/items/{cp['id']}", json={"sql": "SELECT 3 FROM RDB$DATABASE"}).status_code == 200
    assert {i["name"] for i in c.get("/api/library/items", params={"scope": "mine"}).json()} == {"bob's version"}
    assert {i["name"] for i in c.get("/api/library/items", params={"scope": "shared"}).json()} == {"team report"}
    assert {t["tag"] for t in c.get("/api/library").json()["tags"]} == {"team"}

    # an fbadmin administrator may change and delete shared items, but never sees private ones
    _switch(c, "admin@example.org", ADMIN_PW)
    assert {i["name"] for i in c.get("/api/library/items").json()} == {"team report"}
    assert c.get(f"/api/library/items/{cp['id']}").status_code == 404
    r = c.put(f"/api/library/items/{shared['id']}", json={"sql": "SELECT 22 FROM RDB$DATABASE"})
    assert r.status_code == 200 and r.json()["updated_by"] == "admin@example.org" and r.json()["owner"] == "alice"
    assert c.put(f"/api/library/items/{shared['id']}", json={"visibility": "private"}).status_code == 400
    revs = c.get(f"/api/library/items/{shared['id']}/revisions").json()
    assert [(r["rev"], r["author"]) for r in revs] == [(2, "admin@example.org"), (1, "alice")]

    # the owner can make it private again: then bob no longer sees it
    _switch(c, "alice", "alice-password")
    assert c.put(f"/api/library/items/{shared['id']}", json={"visibility": "private"}).json()["visibility"] == "private"
    _switch(c, "bob", "bob-password")
    assert c.get(f"/api/library/items/{shared['id']}").status_code == 404
    _switch(c, "admin@example.org", ADMIN_PW)
    assert c.delete(f"/api/library/items/{shared['id']}").status_code == 404  # private now: invisible to admin
    _switch(c, "alice", "alice-password")
    assert c.put(f"/api/library/items/{shared['id']}", json={"visibility": "shared"}).status_code == 200
    _switch(c, "admin@example.org", ADMIN_PW)
    assert c.delete(f"/api/library/items/{shared['id']}").status_code == 204

    # logged out: nothing
    c.post("/api/auth/logout")
    c.headers.pop("X-CSRF-Token", None)
    assert c.get("/api/library/items").status_code == 401
