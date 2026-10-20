#!/usr/bin/env python3
"""Smoke test of local (embedded) databases against a running fbadmin, e.g. the frozen desktop app:

    python packaging/embedded/smoke-test.py http://127.0.0.1:5106 /tmp/smoke

Creates a local database in DIR, runs DDL, inserts and queries through a Query Tool
session, extracts DDL, backs it up with the embedded service manager and restores the
backup as a second local database. Standard library only; exits non-zero on failure.
"""

from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.request


def main() -> int:
    base, work = sys.argv[1].rstrip("/"), sys.argv[2]
    os.makedirs(work, exist_ok=True)

    def call(method: str, path: str, body: dict | None = None, ok=(200, 201, 202, 204)):
        req = urllib.request.Request(base + path, method=method, data=None if body is None else json.dumps(body).encode(),
                                     headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=120) as r:
                status, raw = r.status, r.read()
        except urllib.error.HTTPError as e:
            status, raw = e.code, e.read()
        data = json.loads(raw) if raw else None
        if status not in ok:
            raise SystemExit(f"{method} {path} -> {status}: {data}")
        return data

    def job(r: dict) -> dict:
        for _ in range(600):
            j = call("GET", f"/api/jobs/{r['id']}")
            if j["status"] != "running":
                if j["status"] != "finished":
                    raise SystemExit("job failed:\n" + "\n".join(j["log"]))
                return j
            time.sleep(0.2)
        raise SystemExit("job did not finish")

    st = call("GET", "/api/embedded/status")
    print("status:", st)
    if not st["available"]:
        raise SystemExit(f"embedded engine not available: {st['reason']}")

    path = os.path.join(work, f"smoke-{int(time.time())}.fdb")
    r = call("POST", "/api/embedded/databases", {"path": path, "create": True, "name": "smoke"})
    sid, dbid = r["server_id"], r["database"]["id"]
    db = f"/api/servers/{sid}/databases/{dbid}"
    res = call("POST", db + "/execute", {"sql": "CREATE TABLE T (ID INTEGER PRIMARY KEY, S VARCHAR(20) "
                                                "COLLATE UNICODE_CI, TS TIMESTAMP WITH TIME ZONE "
                                                "DEFAULT CURRENT_TIMESTAMP); COMMIT; "
                                                "INSERT INTO T (ID, S) VALUES (1, 'Ünïcode'); "
                                                "INSERT INTO T (ID, S) VALUES (2, 'two');"})
    assert res["error"] is None, res["error"]
    sess = call("POST", "/api/query/sessions", {"server_id": sid, "database_id": dbid})["session_id"]
    res = call("POST", f"/api/query/sessions/{sess}/execute",
               {"sql": "SELECT ID, S, RDB$GET_CONTEXT('SYSTEM', 'ENGINE_VERSION') FROM T WHERE S = 'üNÏCODE'",
                "with_plan": True})
    assert res["error"] is None, res["error"]
    rows = res["results"][0]["rows"]
    assert rows and rows[0][0] == 1, rows
    print("query:", rows, res["results"][0].get("plan"))
    call("DELETE", f"/api/query/sessions/{sess}")
    ddl = call("GET", db + "/objects/tables/T/script")["sql"]
    assert "CREATE TABLE" in ddl, ddl
    fbk = path[:-4] + ".fbk"
    job(call("POST", db + "/maintenance/backup", {"target": "server", "backup_file": fbk}))
    target = path[:-4] + "-restored.fdb"
    job(call("POST", f"/api/servers/{sid}/maintenance/restore",
             {"source": "server", "backup_file": fbk, "database_path": target, "register_as": "restored"}))
    srv = call("GET", f"/api/servers/{sid}")
    rid = next(d["id"] for d in srv["databases"] if d["name"] == "restored")
    n = call("GET", f"/api/servers/{sid}/databases/{rid}/tables/T/count")["count"]
    assert n == 2, n
    print(f"backup {os.path.getsize(fbk)} bytes, restored {n} rows: OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
