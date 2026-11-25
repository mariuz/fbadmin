"""Issue #9: Disconnect ends access from the UI until Connect; closing the last tab disconnects everything."""

from __future__ import annotations

import time

from fbadmin import connstate
from fbadmin.connstate import Disconnected

from .conftest import requires_server

UI = {"X-Fbadmin-Client": "ui"}


def test_state_and_tabs(monkeypatch):
    connstate.reset()
    connstate.ui_request.set(True)
    try:
        try:
            connstate.check("s1")
            raise AssertionError("a server starts disconnected")
        except Disconnected as exc:
            assert exc.server_id == "s1" and exc.db_id is None
        connstate.connect("s1")
        connstate.check("s1", "d1")
        connstate.disconnect("s1", "d1")
        connstate.check("s1")
        try:
            connstate.check("s1", "d1")
            raise AssertionError("the database was disconnected")
        except Disconnected as exc:
            assert exc.db_id == "d1"
        connstate.connect("s1", "d1")
        connstate.check("s1", "d1")
        connstate.ui_request.set(False)
        connstate.check("s2")  # not a UI request (scheduler, CLI): not checked
        connstate.ui_request.set(True)

        closed: list[str] = []
        monkeypatch.setattr(connstate, "_on_all_closed", closed.append)  # restored afterwards
        monkeypatch.setenv("FBADMIN_DISCONNECT_ON_CLOSE", "0.2")
        connstate.hello("tab-aaaaaaaa")
        connstate.hello("tab-bbbbbbbb")
        connstate.bye("tab-aaaaaaaa")
        time.sleep(0.4)
        assert closed == []  # another tab is open
        connstate.bye("tab-bbbbbbbb")
        connstate.hello("tab-bbbbbbbb")  # a reload comes back in time
        time.sleep(0.4)
        assert closed == []
        connstate.bye("tab-bbbbbbbb")
        time.sleep(0.4)
        assert closed == [""]
    finally:
        connstate.ui_request.set(False)
        connstate.reset()


@requires_server
def test_disconnect_ends_ui_access(client, database):
    sid, dbid = database
    base = f"/api/servers/{sid}/databases/{dbid}"
    assert client.get(base + "/info", headers=UI).status_code == 200  # registered: connected

    assert client.post(base + "/disconnect", headers=UI).status_code == 200
    r = client.get(base + "/info", headers=UI)
    assert r.status_code == 409 and r.json()["code"] == "disconnected" and r.json()["database_id"] == dbid
    r = client.post("/api/query/sessions", json={"server_id": sid, "database_id": dbid}, headers=UI)
    assert r.status_code == 409
    view = next(s for s in client.get("/api/servers", headers=UI).json() if s["id"] == sid)
    assert not view["disconnected"] and next(d for d in view["databases"] if d["id"] == dbid)["disconnected"]
    assert client.get(base + "/info").status_code == 200  # the CLI / API: not affected
    assert client.post(base + "/connect", headers=UI).status_code == 200
    assert client.get(base + "/info", headers=UI).status_code == 200

    # the server: forgets the password typed this session, and everything on it is refused
    assert client.post(f"/api/servers/{sid}/disconnect", headers=UI).status_code == 200
    assert client.get(base + "/info", headers=UI).json()["code"] == "disconnected"
    assert client.get(f"/api/servers/{sid}/info", headers=UI).status_code == 409
    view = next(s for s in client.get("/api/servers", headers=UI).json() if s["id"] == sid)
    assert view["disconnected"]
    # connecting a database connects its server again
    assert client.post(base + "/connect", headers=UI).status_code == 200
    assert client.get(f"/api/servers/{sid}/info", headers=UI).status_code == 200
    assert client.post(f"/api/servers/{sid}/disconnect", headers=UI).status_code == 200
    assert client.post(f"/api/servers/{sid}/connect", json={}, headers=UI).status_code == 200
    assert client.get(base + "/info", headers=UI).status_code == 200


@requires_server
def test_closing_the_last_tab_disconnects(client, database, monkeypatch):
    sid, dbid = database
    base = f"/api/servers/{sid}/databases/{dbid}"
    monkeypatch.setenv("FBADMIN_DISCONNECT_ON_CLOSE", "0.2")
    assert client.post("/api/client/hello", json={"tab": "t-1234567890"}, headers=UI).status_code == 200
    assert client.get(base + "/info", headers=UI).status_code == 200
    assert client.post("/api/client/bye", json={"tab": "t-1234567890"}, headers=UI).status_code == 200
    time.sleep(0.6)
    assert client.get(base + "/info", headers=UI).json()["code"] == "disconnected"
    assert client.get(base + "/info").status_code == 200  # background / CLI still work
