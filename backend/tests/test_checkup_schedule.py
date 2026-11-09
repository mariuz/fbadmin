"""Scheduled performance check-up (roadmap 2.4): alerts only on new or worse findings, once resolved, the
ignore list, the automatic selectivity fix, the check-up page's last report and the timeline."""

from __future__ import annotations

from fbadmin.monitoring import checkup_schedule as CS

from .conftest import requires_server
from .test_alerts import add_webhook, flush, receiver  # noqa: F401 - fixture
from .test_maintenance import execute, wait_job


def _f(code, sev, labels=()):
    return {"code": code, "severity": sev, "evidence": [{"label": x, "value": ""} for x in labels],
            "text": {"title": code}}


def test_state_and_news():
    opts = CS.CheckupOptions()
    cur = CS.state([_f("oat_stuck", "critical"), _f("selectivity_outdated", "warning", ["IX_A"]),
                    _f("sweep_off", "info")], opts)
    assert set(cur) == {"oat_stuck", "selectivity_outdated"}
    assert CS.news({}, cur) == ["oat_stuck", "selectivity_outdated"]
    assert CS.news(cur, cur) == []
    later = CS.state([_f("oat_stuck", "critical"), _f("selectivity_outdated", "warning", ["IX_A", "IX_B"])], opts)
    assert CS.news(cur, later) == ["selectivity_outdated"]  # names an index it did not name before
    assert CS.news(CS.state([_f("oat_stuck", "warning")], opts), cur)[0] == "oat_stuck"  # more severe
    assert set(CS.state([_f("oat_stuck", "critical"), _f("regressions", "warning")],
                        CS.CheckupOptions(min_severity="critical"))) == {"oat_stuck"}
    assert set(CS.state([_f("oat_stuck", "critical")], CS.CheckupOptions(ignore=["oat_stuck"]))) == set()


@requires_server
def test_scheduled_checkup(client, database, receiver):  # noqa: F811
    sid, dbid = database
    # an index created on an empty table has no selectivity once the table has rows
    execute(client, sid, dbid, "CREATE TABLE LOADED (ID INTEGER NOT NULL, CODE VARCHAR(10))")
    execute(client, sid, dbid, "CREATE INDEX IX_LOADED_CODE ON LOADED (CODE)")
    execute(client, sid, dbid, "EXECUTE BLOCK AS DECLARE I INTEGER = 0; BEGIN WHILE (I < 500) DO BEGIN "
                               "INSERT INTO LOADED VALUES (:I, MOD(:I, 7)); I = I + 1; END END")
    hook = add_webhook(client, receiver)
    client.post("/api/alerts/rules", json={"name": "checkup", "event": "checkup.findings", "server_id": sid,
                                           "database_id": dbid, "channels": [hook["id"]]})
    r = client.post("/api/schedules", json={"name": "nightly check-up", "kind": "checkup", "server_id": sid,
                                            "database_id": dbid, "cron": "0 3 * * *",
                                            "checkup": {"gstat": False}})
    assert r.status_code == 201, r.text
    sched = r.json()
    assert sched["checkup"] == {"gstat": False, "min_severity": "warning", "fix_selectivity": False, "ignore": []}

    def run():
        job = wait_job(client, client.post(f"/api/schedules/{sched['id']}/run").json())
        assert job["status"] == "finished", job["log"]
        flush()
        return client.get(f"/api/schedules/{sched['id']}/history").json()[0]

    def titles():
        return [receiver.json(i)["title"] for i in range(len(receiver.requests))]

    h = run()
    assert h["status"] == "findings" and "selectivity_never" in h["new"], h
    assert "IX_LOADED_CODE" in h["alerted"]["selectivity_never"]["labels"]
    assert any(t.startswith("Check-up of ") for t in titles())
    sent = len(receiver.requests)
    # the check-up page shows the scheduled run's report
    rep = client.get(f"/api/servers/{sid}/databases/{dbid}/monitoring/checkup").json()["report"]
    assert rep["schedule"]["id"] == sched["id"] and any(f["code"] == "selectivity_never" for f in rep["findings"])

    # the same findings again: nothing new, no message
    h = run()
    assert h["status"] == "findings" and h["new"] == []
    assert len(receiver.requests) == sent

    # fix it automatically: the findings are gone, one resolved message
    others = [c for c in h["alerted"] if c != "selectivity_never"]
    r = client.put(f"/api/schedules/{sched['id']}", json={"checkup": {"gstat": False, "fix_selectivity": True,
                                                                      "ignore": others}})
    assert r.status_code == 200, r.text
    h = run()
    assert any("IX_LOADED_CODE" in x for x in h["fixed"]) and h["status"] == "finished" and h["alerted"] == {}
    assert len(receiver.requests) == sent + 1 and titles()[-1].startswith("Resolved: check-up of ")
    h = run()  # nothing again: no second resolved message
    assert h["new"] == [] and h["fixed"] == [] and len(receiver.requests) == sent + 1

    # the timeline lists the runs
    tl = client.get(f"/api/servers/{sid}/databases/{dbid}/timeline").json()
    assert any(e.get("title", "").startswith("Scheduled check-up: nightly check-up") for e in tl["events"])

    client.delete(f"/api/schedules/{sched['id']}")
    for r in client.get("/api/alerts/rules").json():
        client.delete(f"/api/alerts/rules/{r['id']}")
    client.delete(f"/api/alerts/channels/{hook['id']}")
