"""Weekly digest per database (roadmap 2.5): what the period's records say, rendered as Markdown and sent to
channels by a ``digest`` schedule. No Firebird server needed: the digest connects to no database."""

from __future__ import annotations

import time

from fbadmin import changes as CR
from fbadmin import comments as CM
from fbadmin import digest as D
from fbadmin.maintenance.scheduler import Schedule, get_scheduler
from fbadmin.monitoring import forecast, trace_errors

from .test_alerts import add_webhook, flush, receiver  # noqa: F401 - fixture
from .test_maintenance import wait_job


def test_digest(client, receiver):  # noqa: F811
    s = client.post("/api/servers", json={"name": "prod", "host": "h"}).json()
    db = client.post(f"/api/servers/{s['id']}/databases", json={"name": "shop", "path": "/db/shop.fdb"}).json()
    sid, dbid = s["id"], db["id"]
    now = time.time()
    sch = get_scheduler()
    backup = sch.add(Schedule(name="nightly", kind="backup", server_id=sid, database_id=dbid, cron="0 2 * * *"))
    for i, status in enumerate(("finished", "finished", "failed")):
        sch._record(backup, None, now - (3 - i) * 86400, status, "disk full" if status == "failed" else None,
                    file=f"b{i}.fbk")
        sch.history[-1]["finished_at"] = now - (3 - i) * 86400
    forecast._remember(sid, dbid, {"findings": [{"key": "seq:S", "severity": "warning",
                                                 "title": "Sequence S feeding T.ID (INTEGER) is at 85 %"}],
                                   "enough": True, "size": {"bytes": 1}}, now)
    for i, pages in enumerate((1000, 1500)):
        forecast.add_observation(sid, dbid, {"pages": pages, "page_size": 8192, "next_tx": 10, "seqs": {}},
                                 ts=now - (2 - i) * 86400)
    CR.get_store().add(CR.ChangeRequest(server_id=sid, database_id=dbid, title="Add index", sql="CREATE INDEX X ON T (A)"))
    CM.get_store().add(CM.Comment(target=CM.Target(server_id=sid, database_id=dbid, otype="tables", name="T"), text="hi"))
    err = trace_errors.parse({"type": "ERROR AT JStatement::execute", "text": "h\n335544336 : deadlock"})
    for i in range(4):
        trace_errors.record(sid, dbid, err, sql="UPDATE T SET A = 1", user="APP", attachment=1, ts=now - 3600 * (i + 1))

    d = D.collect(sid, dbid, 7, now)
    assert d["backups"]["ok"] == 2 and d["backups"]["failed"] == 1
    assert d["size"] == {"start": 1000 * 8192, "end": 1500 * 8192}
    assert d["changes"] == {"proposed": 1, "applied": 0, "open": 1, "failed": 0}
    assert d["errors"]["total"] == 4 and d["errors"]["lock"] == 4 and d["comments"] == 1
    assert d["status"] == "critical"  # the last backup failed
    text = D.render(d)
    assert text.startswith("# shop (prod): the last 7 day(s)")
    for line in ("- Backups: 2 made, 1 failed", "- Capacity: Sequence S feeding T.ID (INTEGER) is at 85 %; size 11.7 MB (+3.9 MB)",
                 "- Change requests: 1 proposed, 0 applied, 1 open", "- Errors seen by the trace: 4 (4 lock conflict(s))",
                 "  - 4 x deadlock (new)", "- Check-up: never run", "- Comments: 1 new"):
        assert line in text, (line, text)

    # a digest schedule sends it to its channels
    hook = add_webhook(client, receiver)
    assert client.post("/api/schedules", json={"name": "weekly", "kind": "digest", "server_id": sid, "database_id": dbid,
                                               "cron": "0 7 * * 1", "digest": {"channels": ["nope"]}}).status_code == 400
    r = client.post("/api/schedules", json={"name": "weekly", "kind": "digest", "server_id": sid, "database_id": dbid,
                                            "cron": "0 7 * * 1", "digest": {"channels": [hook["id"]], "days": 7}})
    assert r.status_code == 201, r.text
    job = wait_job(client, client.post(f"/api/schedules/{r.json()['id']}/run").json())
    assert job["status"] == "finished", job["log"]
    flush()
    msg = receiver.json(len(receiver.requests) - 1)
    assert msg["title"] == "Digest: shop (prod), the last 7 day(s)" and "- Backups: 2 made, 1 failed" in msg["message"]
    h = client.get(f"/api/schedules/{r.json()['id']}/history").json()[0]
    assert h["status"] == "finished" and h["sent"] and h["status_of_db"] == "critical" and h["digest"].startswith("# shop")
    client.delete(f"/api/schedules/{r.json()['id']}")
    client.delete(f"/api/schedules/{backup.id}")
    client.delete(f"/api/alerts/channels/{hook['id']}")
