"""Scheduled notebooks (roadmap 2.4): a schedule runs a notebook's SQL cells, saves the results in the notebook and
sends a report to alert channels (webhook, e-mail with attachments), with the send policy, read-only runs,
errors, and the checks when a schedule is saved."""

from __future__ import annotations

import time

import pytest

from fbadmin import notebook_runs as NR

from .conftest import requires_server
from .test_access_fb import env  # noqa: F401 - fixture
from .test_alerts import add_webhook, receiver  # noqa: F401 - fixture
from .test_library import srv  # noqa: F401 - fixture


def _job(client, job_id, timeout=120):
    for _ in range(int(timeout / 0.2)):
        j = client.get(f"/api/jobs/{job_id}").json()
        if j["status"] != "running":
            return j
        time.sleep(0.2)
    raise AssertionError("job did not end")


def _cells(*sql, note="## Daily orders"):
    out = [{"id": "n1", "type": "markdown", "source": note}]
    out += [{"id": f"q{i}", "type": "sql", "source": s} for i, s in enumerate(sql)]
    return out


@pytest.fixture
def shop(client, database):
    sid, dbid = database
    base = f"/api/servers/{sid}/databases/{dbid}"
    for sql in ("CREATE TABLE ORDERS (ID INTEGER NOT NULL PRIMARY KEY, STATUS VARCHAR(10), AMOUNT INTEGER)",
                "INSERT INTO ORDERS VALUES (1, 'open', 10)", "INSERT INTO ORDERS VALUES (2, 'stuck', 20)",
                "INSERT INTO ORDERS VALUES (3, 'open', 30)"):
        r = client.post(base + "/execute", json={"sql": sql})
        assert r.json()["error"] is None, r.json()
    yield sid, dbid
    for s in client.get("/api/schedules").json():
        client.delete(f"/api/schedules/{s['id']}")
    for c in client.get("/api/alerts/channels").json():
        client.delete(f"/api/alerts/channels/{c['id']}")


def _schedule(client, sid, dbid, nb_id, **opts):
    r = client.post("/api/schedules", json={"name": "daily report", "kind": "notebook", "server_id": sid,
                                            "database_id": dbid, "cron": "0 7 * * *",
                                            "notebook": {"notebook_id": nb_id, **opts}})
    assert r.status_code == 201, r.text
    return r.json()


def _run(client, sched_id):
    j = _job(client, client.post(f"/api/schedules/{sched_id}/run").json()["id"])
    hist = client.get(f"/api/schedules/{sched_id}/history").json()
    return j, hist[0]


@requires_server
def test_scheduled_notebook_report(client, shop, receiver):  # noqa: F811
    sid, dbid = shop
    hook = add_webhook(client, receiver)
    nb = client.post("/api/notebooks", json={"name": "Orders", "server_id": sid, "database_id": dbid, "cells": _cells(
        "SELECT ID, STATUS, AMOUNT FROM ORDERS ORDER BY ID", "SELECT COUNT(*) AS N FROM ORDERS WHERE STATUS = 'stuck'")})
    nb = nb.json()
    assert client.get("/api/notebooks/channels").json()["channels"] == [
        {"id": hook["id"], "name": "hook", "kind": "webhook", "enabled": True}]
    s = _schedule(client, sid, dbid, nb["id"], channels=[hook["id"]])
    assert s["kind"] == "notebook" and s["notebook"]["read_only"] is True and s["notebook"]["send"] == "always"

    j, h = _run(client, s["id"])
    assert j["status"] == "finished", j
    assert h["status"] == "finished" and h["cells"] == 2 and h["rows"] == 4 and h["saved"] is True
    assert h["sent"] == ["hook"]
    sent = receiver.json()
    assert receiver.requests[-1]["headers"]["X-Fbadmin-Event"] == "notebook.report"
    assert sent["title"] == "Notebook report: Orders" and sent["level"] == "info"
    assert "2 SQL cell(s), 2 result(s), 4 row(s)" in sent["message"] and "2 | stuck | 20" in sent["message"]
    md = sent["details"]["markdown"]
    assert md.startswith("# Orders") and "| ID | STATUS | AMOUNT |" in md and "| 2 | stuck | 20 |" in md
    # the results are in the notebook now
    got = client.get(f"/api/notebooks/{nb['id']}").json()
    out = got["cells"][1]["output"]
    assert out["results"][0]["rows"][1] == [2, "stuck", 20] and out["error"] is None
    assert got["cells"][2]["output"]["results"][0]["rows"] == [[1]]

    # send only when a result has rows
    client.put(f"/api/notebooks/{nb['id']}", json={"cells": _cells("SELECT ID FROM ORDERS WHERE STATUS = 'lost'")})
    client.put(f"/api/schedules/{s['id']}", json={"notebook": {"notebook_id": nb["id"], "channels": [hook["id"]], "send": "rows"}})
    n = len(receiver.requests)
    j, h = _run(client, s["id"])
    assert j["status"] == "finished" and h["rows"] == 0 and h["sent"] == [] and len(receiver.requests) == n

    # a cell that fails: the run fails, the report says so (send on error)
    client.put(f"/api/notebooks/{nb['id']}", json={"cells": _cells("SELECT * FROM NO_SUCH_TABLE", "SELECT 1 FROM RDB$DATABASE")})
    client.put(f"/api/schedules/{s['id']}", json={"notebook": {"notebook_id": nb["id"], "channels": [hook["id"]], "send": "error"}})
    j, h = _run(client, s["id"])
    assert j["status"] == "failed" and h["status"] == "failed" and "Cell 2" in h["error"] and "NO_SUCH_TABLE" in h["error"]  # numbered as in the notebook (the note is 1)
    assert h["cells"] == 1  # stopped at the first error
    sent = receiver.json()
    assert sent["level"] == "critical" and sent["title"].endswith("(failed)") and "Stopped at cell 2" in sent["message"]

    # read only by default: a cell that changes data is refused, nothing changes
    client.put(f"/api/notebooks/{nb['id']}", json={"cells": _cells("UPDATE ORDERS SET STATUS = 'closed' WHERE ID = 2")})
    client.put(f"/api/schedules/{s['id']}", json={"notebook": {"notebook_id": nb["id"]}})
    j, h = _run(client, s["id"])
    assert j["status"] == "failed" and "read only" in h["error"]
    sess = client.post("/api/query/sessions", json={"server_id": sid, "database_id": dbid}).json()["session_id"]
    q = client.post(f"/api/query/sessions/{sess}/execute",
                    json={"sql": "SELECT STATUS FROM ORDERS WHERE ID = 2", "autocommit": True}).json()
    assert q["results"][0]["rows"] == [["stuck"]]
    # ... unless the schedule allows it (a month-end close)
    client.put(f"/api/schedules/{s['id']}", json={"notebook": {"notebook_id": nb["id"], "read_only": False}})
    j, h = _run(client, s["id"])
    assert j["status"] == "finished", h
    q = client.post(f"/api/query/sessions/{sess}/execute",
                    json={"sql": "SELECT STATUS FROM ORDERS WHERE ID = 2", "autocommit": True}).json()
    assert q["results"][0]["rows"] == [["closed"]]
    client.delete(f"/api/query/sessions/{sess}")

    # on the database's timeline
    tl = client.get(f"/api/servers/{sid}/databases/{dbid}/timeline", params={"category": "data"}).json()["events"]
    assert any(e["title"] == "Scheduled notebook: daily report" for e in tl)

    # checks when the schedule is saved
    bad = client.post("/api/schedules", json={"name": "x", "kind": "notebook", "server_id": sid, "database_id": dbid,
                                              "cron": "0 7 * * *", "notebook": {"notebook_id": "nope"}})
    assert bad.status_code == 400
    bad = client.post("/api/schedules", json={"name": "x", "kind": "notebook", "server_id": sid, "database_id": dbid,
                                              "cron": "0 7 * * *",
                                              "notebook": {"notebook_id": nb["id"], "channels": ["nope"]}})
    assert bad.status_code == 400 and "Unknown alert channel" in bad.text
    other = client.post("/api/notebooks", json={"name": "elsewhere"}).json()
    bad = client.post("/api/schedules", json={"name": "x", "kind": "notebook", "server_id": sid, "database_id": dbid,
                                              "cron": "0 7 * * *", "notebook": {"notebook_id": other["id"]}})
    assert bad.status_code == 400 and "another database" in bad.text
    # the notebook is deleted: the run fails with a clear message
    client.delete(f"/api/notebooks/{nb['id']}")
    j, h = _run(client, s["id"])
    assert j["status"] == "failed" and "deleted" in h["error"]


@requires_server
def test_email_report_has_attachments(client, shop, monkeypatch):
    sid, dbid = shop
    sent = []

    class FakeSMTP:
        def __init__(self, *a, **kw):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def starttls(self, **kw):
            pass

        def login(self, *a):
            pass

        def send_message(self, msg, to_addrs=None):
            sent.append(msg)

    from fbadmin.alerts import channels

    monkeypatch.setattr(channels.smtplib, "SMTP", FakeSMTP)
    ch = client.post("/api/alerts/channels", json={"name": "mail", "kind": "email", "settings": {
        "host": "smtp.example", "from_addr": "fbadmin@example.org", "to": "dba@example.org", "security": "none"}})
    assert ch.status_code == 201, ch.text
    nb = client.post("/api/notebooks", json={"name": "Month end", "server_id": sid, "database_id": dbid,
                                             "cells": _cells("SELECT STATUS, SUM(AMOUNT) AS TOTAL FROM ORDERS GROUP BY 1 ORDER BY 1")}).json()
    s = _schedule(client, sid, dbid, nb["id"], channels=[ch.json()["id"]])
    j, h = _run(client, s["id"])
    assert j["status"] == "finished", h
    msg = sent[-1]
    assert msg["Subject"] == "[fbadmin] INFO: Notebook report: Month end" and msg["To"] == "dba@example.org"
    files = {p.get_filename(): p.get_content() for p in msg.iter_attachments()}
    assert set(files) == {"Month end.md", "cell2-result1.csv"}
    assert "| open | 40 |" in files["Month end.md"]
    assert files["cell2-result1.csv"].replace("\r\n", "\n") == "STATUS,TOTAL\nopen,40\nstuck,20\n"


def test_report_text_helpers():
    from fbadmin.notebooks import Cell, Notebook, Output, ResultOut

    res = ResultOut(kind="select", columns=[{"name": "A"}, {"name": "B"}], rows=[[i, None] for i in range(8)])
    cells = [Cell(id="q", source="SELECT", output=Output(results=[res]))]
    text = NR._first_rows(cells)
    assert text.splitlines()[:3] == ["Cell 1 - 8 row(s)", "A | B", "0 | NULL"] and "... 3 more" in text
    assert NR._csv(res).decode().splitlines()[:2] == ["A,B", "0,"]
    nb = Notebook(name="N")
    assert NR._summary_text(nb, {"cells": 1, "results": 1, "rows": 8, "error": None, "error_cell": None}, "db (srv)") \
        == 'Notebook "N" on db (srv): 1 SQL cell(s), 1 result(s), 8 row(s).'


@requires_server
def test_runs_with_the_owners_permissions(env, receiver):  # noqa: F811
    """Server mode: the schedule runs as the user who saved it; hidden tables stay hidden from that user."""
    from .test_access_fb import as_user

    c, sid, dbs = env
    main = dbs["main"]
    as_user(c, sid, "root")  # an fbadmin administrator sets up the channel and the grants
    hook = add_webhook(c, receiver)
    assert c.post("/api/admin/users", json={"username": "ops", "password": "ops-password"}).status_code == 201
    for db, pattern, level in ((main, None, "admin"), (main, "SECRET*", "none")):
        r = c.post("/api/admin/access/grants", json={"subject_type": "user", "subject": "ops", "server_id": sid,
                                                      "db_id": db, "object_pattern": pattern, "level": level})
        assert r.status_code == 201, r.text
    as_user(c, sid, "ops")
    assert c.get("/api/notebooks/channels").json()["channels"][0]["id"] == hook["id"]
    nb = c.post("/api/notebooks", json={"name": "pay", "server_id": sid, "database_id": main, "visibility": "shared",
                                        "cells": _cells("SELECT NAME FROM CUSTOMER", "SELECT AMOUNT FROM SECRET_PAY")}).json()
    s = _schedule(c, sid, main, nb["id"], channels=[hook["id"]], send="rows")
    n = len(receiver.requests)
    j, h = _run(c, s["id"])
    assert j["status"] == "failed" and "SECRET_PAY is hidden from you" in h["error"], h
    assert h["cells"] == 2 and h["rows"] == 1  # the first cell ran, the second was refused
    sent = receiver.json() if len(receiver.requests) > n else None
    assert sent is not None and "1000" not in receiver.requests[-1]["body"].decode()
    # ops has per-table restrictions on the database, so saved results are hidden from ops (as for any notebook)
    assert c.get(f"/api/notebooks/{nb['id']}").json()["results_hidden"] is True
    as_user(c, sid, "root")
    out = c.get(f"/api/notebooks/{nb['id']}").json()["cells"]
    assert out[1]["output"]["results"][0]["rows"] == [["Ann"]] and out[1]["output"]["ran_by"] == "ops"
    assert "hidden from you" in out[2]["output"]["error"] and out[2]["output"]["results"] == []


def test_server_mode_manager_forwards_per_user_helpers(srv):  # noqa: F811
    """``manager._password`` / ``manager._open`` (used by the event monitor, trace, database properties, the debugger,
    anonymised copies...) reach the signed-in user's own manager in server mode."""
    from fbadmin.api.deps import get_state
    from fbadmin.security.sessions import current_principal

    from .test_security import ADMIN_PW, login

    c = srv
    login(c, "admin@example.org", ADMIN_PW)
    s = c.post("/api/servers", json={"name": "x", "host": "127.0.0.1", "port": 9, "password": "pw-x"}).json()
    from fbadmin.security.sessions import Principal

    st = get_state()
    me = c.get("/api/auth/status").json()["user"]
    tok = current_principal.set(Principal(user_id=me["id"], username=me["username"], role=me["role"]))
    try:
        assert st.manager._password(s["id"]) == "pw-x"
        assert callable(st.manager._open)
    finally:
        current_principal.reset(tok)
