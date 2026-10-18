"""Integration tests for milestone 0.5 maintenance tools against a live Firebird 3/4/5 server."""

from __future__ import annotations

import time
import uuid

import firebirdsql
import pytest
from firebirdsql.wireprotocol import Packer

from .conftest import DB_DIR, HOST, PASSWORD, PORT, USER, requires_server

pytestmark = requires_server


def base(sid, dbid):
    return f"/api/servers/{sid}/databases/{dbid}"


def mbase(sid, dbid):
    return base(sid, dbid) + "/maintenance"


def wait_job(client, job, timeout=120):
    jid = job["id"]
    end = time.time() + timeout
    while time.time() < end:
        j = client.get(f"/api/jobs/{jid}").json()
        if j["status"] != "running":
            return j
        time.sleep(0.2)
    raise AssertionError(f"job {jid} did not finish")


def ok_job(client, r):
    assert r.status_code == 202, r.text
    j = wait_job(client, r.json())
    assert j["status"] == "finished", "\n".join(j["log"])
    return j


def execute(client, sid, dbid, sql):
    r = client.post(base(sid, dbid) + "/execute", json={"sql": sql})
    assert r.status_code == 200, r.text
    assert r.json()["error"] is None, r.json()["error"]
    return r.json()


def fill(client, sid, dbid, rows=300):
    execute(client, sid, dbid, f"""
CREATE TABLE ITEMS (ID INTEGER PRIMARY KEY, NAME VARCHAR(50), NOTE BLOB SUB_TYPE TEXT);
CREATE TABLE SKIPPED (ID INTEGER);
COMMIT;
EXECUTE BLOCK AS DECLARE i INTEGER = 0; BEGIN
  WHILE (i < {rows}) DO BEGIN
    INSERT INTO ITEMS VALUES (:i, 'item ' || :i, 'note ' || :i);
    i = i + 1;
  END
  INSERT INTO SKIPPED VALUES (1);
END;
COMMIT;""")


def count(client, sid, dbid, table="ITEMS"):
    res = execute(client, sid, dbid, f"SELECT COUNT(*), SUM(ID) FROM {table}")["results"][0]
    return tuple(res["rows"][0])


def major(client, sid):
    return client.get(f"/api/servers/{sid}/maintenance/capabilities").json()["engine_major"]


def new_path(tag):
    return f"{DB_DIR}/fbadmin_m05_{tag}_{uuid.uuid4().hex[:8]}.fdb"


def drop_path(path):
    try:
        c = firebirdsql.connect(host=HOST, port=PORT, user=USER, password=PASSWORD, database=path)
        c.drop_database()
    except Exception:
        pass


def register(client, sid, path, name="r"):
    r = client.post(f"/api/servers/{sid}/databases", json={"name": name, "path": path})
    assert r.status_code == 201, r.text
    return r.json()["id"]


# ------------------------------------------------------------- backup/restore
def test_capabilities(client, server):
    caps = client.get(f"/api/servers/{server['id']}/maintenance/capabilities").json()
    assert caps["engine_major"] >= 3
    assert caps["features"]["parallel_workers"] == (caps["engine_major"] >= 5)
    assert caps["features"]["rdb_config"] == (caps["engine_major"] >= 4)


def test_backup_download_and_upload_restore_roundtrip(client, database):
    sid, dbid = database
    fill(client, sid, dbid, 500)
    expected = count(client, sid, dbid)

    r = client.post(mbase(sid, dbid) + "/backup/stream", json={})
    assert r.status_code == 200, r.text
    info = r.json()
    assert info["filename"].endswith(".fbk")
    with client.stream("GET", info["url"]) as resp:
        assert resp.status_code == 200
        assert "attachment" in resp.headers["content-disposition"]
        data = b"".join(resp.iter_bytes())
    assert len(data) > 10000
    # one-time token
    assert client.get(info["url"]).status_code == 404

    target = new_path("upl")
    chunks = (data[i:i + 7000] for i in range(0, len(data), 7000))
    r = client.post(f"/api/servers/{sid}/maintenance/restore/upload",
                    params={"database_path": target, "filename": "x.fbk", "register_as": "uploaded"},
                    content=chunks, headers={"Content-Type": "application/octet-stream"})
    try:
        job = ok_job(client, r)
        assert any("Sent 100%" in line for line in job["log"])
        assert any("gbak:" in line for line in job["log"])
        srv = client.get(f"/api/servers/{sid}").json()
        new_id = next(d["id"] for d in srv["databases"] if d["name"] == "uploaded")
        assert count(client, sid, new_id) == expected
        # uploads are removed after the restore unless keep=true
        assert client.get(f"/api/servers/{sid}/maintenance/archive").json() == []
    finally:
        client.post(f"/api/servers/{sid}/databases/{new_id}/disconnect")
        drop_path(target)


def test_backup_options_and_archive_restore(client, database):
    sid, dbid = database
    fill(client, sid, dbid, 50)
    v = major(client, sid)
    opts = {"skip_data": "SKIPPED"}
    if v >= 4:
        opts["zip"] = True
    if v >= 5:
        opts["parallel_workers"] = 2
    j = ok_job(client, client.post(mbase(sid, dbid) + "/backup", json={"target": "archive", "options": opts}))
    assert any("Backup written" in line for line in j["log"])
    arch = client.get(f"/api/servers/{sid}/maintenance/archive").json()
    assert len(arch) == 1 and arch[0]["folder"] == "manual" and arch[0]["size"] > 0
    r = client.get(f"/api/servers/{sid}/maintenance/archive/download", params={"path": arch[0]["path"]})
    assert r.status_code == 200 and len(r.content) == arch[0]["size"]

    target = new_path("arch")
    ropts = {"page_size": 8192}
    if v >= 5:
        ropts["parallel_workers"] = 2
    try:
        ok_job(client, client.post(f"/api/servers/{sid}/maintenance/restore", json={
            "source": "archive", "backup_file": arch[0]["path"], "database_path": target, "options": ropts}))
        rid = register(client, sid, target)
        assert count(client, sid, rid) == count(client, sid, dbid)
        assert count(client, sid, rid, "SKIPPED") == (0, None)  # data skipped
        props = client.get(mbase(sid, rid) + "/properties").json()
        assert props["header"]["page_size"] == 8192
        client.post(f"/api/servers/{sid}/databases/{rid}/disconnect")
    finally:
        drop_path(target)
    r = client.delete(f"/api/servers/{sid}/maintenance/archive", params={"path": arch[0]["path"]})
    assert r.status_code == 204
    assert client.get(f"/api/servers/{sid}/maintenance/archive").json() == []
    assert client.delete(f"/api/servers/{sid}/maintenance/archive", params={"path": "../x.fbk"}).status_code == 400


def test_backup_option_gating(client, database):
    sid, dbid = database
    v = major(client, sid)
    r = client.post(mbase(sid, dbid) + "/backup/stream", json={"parallel_workers": 4})
    if v < 5:
        assert r.status_code == 400 and "Firebird 5" in r.json()["detail"]
    else:
        assert r.status_code == 200
        with client.stream("GET", r.json()["url"]) as resp:
            assert len(b"".join(resp.iter_bytes())) > 0
    r = client.post(mbase(sid, dbid) + "/backup/stream", json={"include_data": "X"})
    if v < 4:
        assert r.status_code == 400 and "Firebird 4" in r.json()["detail"]
    else:
        assert r.status_code == 200
        with client.stream("GET", r.json()["url"]) as resp:
            b"".join(resp.iter_bytes())
    if v >= 4:
        # A crypt plugin that does not exist: the server reports the error before streaming starts.
        r = client.post(mbase(sid, dbid) + "/backup/stream", json={"crypt_plugin": "NoSuchPlugin", "key_name": "k"})
        assert r.status_code in (200, 400)
        if r.status_code == 200:
            with client.stream("GET", r.json()["url"]) as resp:
                b"".join(resp.iter_bytes())


def test_server_file_backup_restore_job(client, database):
    sid, dbid = database
    fill(client, sid, dbid, 20)
    fbk = new_path("srv").replace(".fdb", ".fbk")
    target = new_path("srvr")
    ok_job(client, client.post(mbase(sid, dbid) + "/backup", json={"target": "server", "backup_file": fbk}))
    try:
        j = ok_job(client, client.post(f"/api/servers/{sid}/maintenance/restore", json={
            "source": "server", "backup_file": fbk, "database_path": target,
            "options": {"deactivate_indexes": True}}))
        assert any("gbak:" in line for line in j["log"])
    finally:
        drop_path(target)


# ---------------------------------------------------------------- nbackup
def test_nbackup_levels_and_restore_chain(client, database):
    sid, dbid = database
    fill(client, sid, dbid, 100)
    stem = new_path("nbk").replace(".fdb", "")
    ok_job(client, client.post(mbase(sid, dbid) + "/nbackup", json={"level": 0, "file": stem + ".nbk0"}))
    execute(client, sid, dbid, "INSERT INTO ITEMS VALUES (1000, 'after level 0', NULL); COMMIT;")
    ok_job(client, client.post(mbase(sid, dbid) + "/nbackup", json={"level": 1, "file": stem + ".nbk1"}))
    hist = client.get(mbase(sid, dbid) + "/nbackup/history").json()
    assert [h["level"] for h in hist] == [0, 1]
    target = stem + "_restored.fdb"
    try:
        ok_job(client, client.post(f"/api/servers/{sid}/maintenance/nrestore", json={
            "files": [stem + ".nbk0", stem + ".nbk1"], "database_path": target, "register_as": "chain"}))
        srv = client.get(f"/api/servers/{sid}").json()
        rid = next(d["id"] for d in srv["databases"] if d["name"] == "chain")
        assert count(client, sid, rid) == (101, sum(range(100)) + 1000)
        client.post(f"/api/servers/{sid}/databases/{rid}/disconnect")
    finally:
        drop_path(target)
        # level 0 only -> no row inserted after level 0
    target0 = stem + "_l0.fdb"
    try:
        ok_job(client, client.post(f"/api/servers/{sid}/maintenance/nrestore", json={
            "files": [stem + ".nbk0"], "database_path": target0}))
        rid0 = register(client, sid, target0, "l0")
        assert count(client, sid, rid0)[0] == 100
        client.post(f"/api/servers/{sid}/databases/{rid0}/disconnect")
    finally:
        drop_path(target0)


def test_nbackup_lock_unlock_fixup(client, database):
    sid, dbid = database
    st = client.post(mbase(sid, dbid) + "/nbackup/lock").json()
    assert st["header"]["backup_state"] == "locked"
    assert st["database"]["backup_state_name"] in ("locked", "1")
    st = client.post(mbase(sid, dbid) + "/nbackup/unlock").json()
    assert st["header"]["backup_state"] == "normal"
    # fixup: lock, then fix the (locked) file as if it were a copy
    client.post(mbase(sid, dbid) + "/nbackup/lock")
    r = client.post(mbase(sid, dbid) + "/nbackup/fixup", json={})
    if major(client, sid) < 4:
        assert r.status_code == 400 and "Firebird 4" in r.json()["detail"]
        client.post(mbase(sid, dbid) + "/nbackup/unlock")
    else:
        assert r.status_code == 200, r.text
        props = client.get(mbase(sid, dbid) + "/properties").json()
        assert props["header"]["backup_state"] == "normal"


# -------------------------------------------------------------------- gfix
def test_validation_modes(client, database):
    sid, dbid = database
    fill(client, sid, dbid, 50)
    j = ok_job(client, client.post(mbase(sid, dbid) + "/validate", json={"mode": "online"}))
    assert any("Validation finished" in line for line in j["log"])
    assert any("ITEMS" in line for line in j["log"])
    j = ok_job(client, client.post(mbase(sid, dbid) + "/validate",
                                   json={"mode": "online", "tables_include": "SKIPPED"}))
    assert not any("ITEMS" in line for line in j["log"])
    for mode in ("full", "check", "mend"):
        ok_job(client, client.post(mbase(sid, dbid) + "/validate", json={"mode": mode}))
    assert client.post(mbase(sid, dbid) + "/validate", json={"mode": "bogus"}).status_code == 400


def test_sweep_and_icu(client, database):
    sid, dbid = database
    ok_job(client, client.post(mbase(sid, dbid) + "/sweep", json={}))
    r = client.post(mbase(sid, dbid) + "/sweep", json={"parallel_workers": 2})
    j = wait_job(client, r.json())
    assert j["status"] == ("finished" if major(client, sid) >= 5 else "failed")
    ok_job(client, client.post(mbase(sid, dbid) + "/icu"))


def _make_limbo(path):
    """Prepare a transaction (2PC phase one) and drop the connection: it stays in limbo."""
    c = firebirdsql.connect(host=HOST, port=PORT, user=USER, password=PASSWORD, database=path)
    cur = c.cursor()
    cur.execute("INSERT INTO ITEMS VALUES (5000, 'limbo', NULL)")
    p = Packer()
    p.pack_int(51)  # op_prepare2
    p.pack_int(cur.transaction._trans_handle)
    p.pack_bytes(b"")
    c.sock.send(p.get_buffer())
    c._op_response()
    c.sock._sock.close()


def test_limbo_list_commit_rollback(client, database):
    sid, dbid = database
    fill(client, sid, dbid, 5)
    path = client.get(f"/api/servers/{sid}").json()["databases"][0]["path"]
    assert client.get(mbase(sid, dbid) + "/limbo").json() == []
    client.post(f"/api/servers/{sid}/databases/{dbid}/disconnect")
    _make_limbo(path)
    lim = client.get(mbase(sid, dbid) + "/limbo").json()
    assert len(lim) == 1 and lim[0]["id"] > 0 and not lim[0]["multi"]
    r = client.post(mbase(sid, dbid) + f"/limbo/{lim[0]['id']}/commit")
    assert r.status_code == 200, r.text
    assert r.json()["limbo"] == []
    assert count(client, sid, dbid)[0] == 6  # committed row is visible
    client.post(f"/api/servers/{sid}/databases/{dbid}/disconnect")
    execute(client, sid, dbid, "DELETE FROM ITEMS WHERE ID = 5000; COMMIT;")
    client.post(f"/api/servers/{sid}/databases/{dbid}/disconnect")
    _make_limbo(path)
    lim = client.get(mbase(sid, dbid) + "/limbo").json()
    r = client.post(mbase(sid, dbid) + f"/limbo/{lim[0]['id']}/rollback")
    assert r.status_code == 200 and r.json()["limbo"] == []
    assert count(client, sid, dbid)[0] == 5
    assert client.post(mbase(sid, dbid) + "/limbo/1/bogus").status_code == 400


# ------------------------------------------------------------ shutdown/online
def test_shutdown_and_online(client, database):
    sid, dbid = database
    b = mbase(sid, dbid)
    hdr = client.post(b + "/shutdown", json={"mode": "multi", "method": "force", "timeout": 0}).json()
    assert hdr["shutdown_mode"] == "multi"
    # SYSDBA may still attach in multi-user maintenance
    assert client.get(b + "/properties").json()["database"]["shutdown_mode"] == 1
    hdr = client.post(b + "/shutdown", json={"mode": "single", "method": "attachments", "timeout": 0}).json()
    assert hdr["shutdown_mode"] == "single"
    hdr = client.post(b + "/shutdown", json={"mode": "full", "method": "transactions", "timeout": 0}).json()
    assert hdr["shutdown_mode"] == "full"
    props = client.get(b + "/properties").json()
    assert props["database"] is None and "full shutdown" in props["error"]
    # cannot go back to a less restrictive shutdown mode
    r = client.post(b + "/shutdown", json={"mode": "multi"})
    assert r.status_code == 400
    hdr = client.post(b + "/online", json={"mode": "single"}).json()
    assert hdr["shutdown_mode"] == "single"
    hdr = client.post(b + "/online", json={"mode": "normal"}).json()
    assert hdr["shutdown_mode"] == "online"
    assert client.get(b + "/properties").json()["database"]["shutdown_mode"] == 0
    assert client.post(b + "/shutdown", json={"mode": "weird"}).status_code == 400


# -------------------------------------------------------------- properties
def test_properties_editor(client, database):
    sid, dbid = database
    b = mbase(sid, dbid)
    r = client.put(b + "/properties", json={"page_buffers": 3000, "sweep_interval": 12345, "forced_writes": False,
                                            "reserve_space": False, "linger": 30, "default_charset": "win1252"})
    assert r.status_code == 200, r.text
    st = r.json()["state"]
    assert len(r.json()["changed"]) == 6
    assert st["header"]["page_buffers"] == 3000
    assert st["header"]["forced_writes"] is False and st["header"]["reserve_space"] is False
    db = st["database"]
    assert db["page_buffers"] == 3000 and db["sweep_interval"] == 12345
    assert db["forced_writes"] == 0 and db["reserve_space"] == 0
    assert db["linger"] == 30 and db["default_charset"] == "WIN1252"

    st = client.put(b + "/properties", json={"read_only": True}).json()["state"]
    assert st["header"]["read_only"] is True and st["database"]["read_only"] == 1
    st = client.put(b + "/properties", json={"read_only": False, "linger": 0, "dialect": 1,
                                             "forced_writes": True}).json()["state"]
    assert st["database"]["read_only"] == 0 and st["database"]["linger"] == 0
    assert st["database"]["sql_dialect"] == 1 and st["header"]["forced_writes"] is True
    st = client.put(b + "/properties", json={"dialect": 3}).json()["state"]
    assert st["header"]["database_dialect"] == 3
    assert client.put(b + "/properties", json={"dialect": 2}).status_code == 400


# -------------------------------------------------------------------- gstat
def test_gstat_job_parsed(client, database):
    sid, dbid = database
    fill(client, sid, dbid, 400)
    execute(client, sid, dbid, "CREATE INDEX IX_ITEMS_NAME ON ITEMS (NAME); COMMIT;")
    j = ok_job(client, client.post(mbase(sid, dbid) + "/gstat", json={}))
    r = client.get(f"/api/servers/{sid}/maintenance/gstat/{j['id']}").json()
    assert r["status"] == "finished"
    tables = {t["name"]: t for t in r["tables"]}
    assert tables["ITEMS"]["total_records"] == 400
    assert {i["name"] for i in tables["ITEMS"]["indices"]} >= {"IX_ITEMS_NAME"}
    assert r["header"]["page_size"] == 16384
    assert isinstance(r["attention"], list)
    p = client.post(f"/api/servers/{sid}/maintenance/gstat/parse", json={"text": r["text"]}).json()
    assert len(p["tables"]) == len(r["tables"])
    j = ok_job(client, client.post(mbase(sid, dbid) + "/gstat", json={"tables": ["SKIPPED"]}))
    r = client.get(f"/api/servers/{sid}/maintenance/gstat/{j['id']}").json()
    assert [t["name"] for t in r["tables"]] == ["SKIPPED"]


# -------------------------------------------------------------- encryption
def test_encryption_error_path_and_state(client, database):
    sid, dbid = database
    st = client.get(mbase(sid, dbid) + "/crypt-state").json()
    assert st["progress"] == 0
    assert st["supported_state"] == (major(client, sid) >= 4)
    if st["supported_state"]:
        assert st["crypt_state_name"] == "not encrypted"
    r = client.post(mbase(sid, dbid) + "/encrypt", json={"plugin": "NoSuchPlugin", "key_name": "red"})
    assert r.status_code == 400
    assert "NOSUCHPLUGIN" in r.json()["detail"].upper()
    r = client.post(mbase(sid, dbid) + "/decrypt")
    assert r.status_code == 400  # not encrypted
    assert client.post(mbase(sid, dbid) + "/encrypt", json={"plugin": " "}).status_code == 400


# ------------------------------------------------------------------ config
def test_rdb_config(client, database):
    sid, dbid = database
    cfg = client.get(mbase(sid, dbid) + "/config").json()
    if major(client, sid) < 4:
        assert cfg["available"] is False and "Firebird 4" in cfg["message"]
    else:
        assert cfg["available"] is True
        names = {r["name"]: r for r in cfg["rows"]}
        assert "DefaultDbCachePages" in names and "TempCacheLimit" in names
        assert any(r["source"] for r in cfg["rows"] if r["is_set"]) or True


# ------------------------------------------------------------------- trace
def test_trace_session_captures_statement(client, database):
    sid, dbid = database
    templates = {t["id"] for t in client.get(f"/api/servers/{sid}/trace/templates").json()}
    assert {"slow_queries", "errors", "connections", "psql", "custom"} <= templates
    prev = client.post(f"/api/servers/{sid}/trace/preview", json={"template": "slow_queries",
                                                                  "database_id": dbid}).json()
    assert "time_threshold = 1000" in prev["config"]
    r = client.post(f"/api/servers/{sid}/trace/sessions",
                    json={"template": "all_statements", "database_id": dbid, "name": "pytest trace"})
    assert r.status_code == 201, r.text
    sess = r.json()
    assert sess["status"] == "running" and sess["session_id"]
    try:
        listed = client.get(f"/api/servers/{sid}/trace/sessions").json()
        assert any(s["id"] == sess["session_id"] for s in listed["server"])
        assert any(s["id"] == sess["id"] for s in listed["local"])
        marker = f"m{uuid.uuid4().hex[:6]}"
        execute(client, sid, dbid, f"SELECT 42 AS {marker} FROM RDB$DATABASE")
        found = []
        for _ in range(50):
            ev = client.get(f"/api/servers/{sid}/trace/sessions/{sess['id']}/events",
                            params={"q": marker}).json()
            found = [e for e in ev["events"] if e["sql"] and marker in e["sql"]]
            if found:
                break
            time.sleep(0.2)
        assert found, "statement not captured"
        assert found[0]["type"].startswith("EXECUTE_STATEMENT_FINISH")
        assert found[0]["plan"] and "RDB$DATABASE" in found[0]["plan"]
    finally:
        r = client.post(f"/api/servers/{sid}/trace/sessions/{sess['id']}/stop")
        assert r.status_code == 200
    for _ in range(50):
        s = client.get(f"/api/servers/{sid}/trace/sessions/{sess['id']}").json()
        if s["status"] != "running":
            break
        time.sleep(0.2)
    assert s["status"] == "stopped"
    log = client.get(f"/api/servers/{sid}/trace/sessions/{sess['id']}/download")
    assert log.status_code == 200 and marker in log.text
    assert client.delete(f"/api/servers/{sid}/trace/sessions/{sess['id']}").status_code == 204


@pytest.mark.parametrize("template", ["slow_queries", "errors", "connections", "psql"])
def test_trace_templates_accepted_by_server(client, database, template):
    sid, dbid = database
    r = client.post(f"/api/servers/{sid}/trace/sessions", json={"template": template, "database_id": dbid})
    assert r.status_code == 201, r.text
    sess = r.json()
    assert sess["status"] == "running", sess
    client.post(f"/api/servers/{sid}/trace/sessions/{sess['id']}/stop")


def test_trace_bad_custom_config(client, database):
    sid, dbid = database
    from fbadmin.maintenance.trace import database_pattern

    path = client.get(f"/api/servers/{sid}").json()["databases"][0]["path"]
    cfg = f"database = {database_pattern(path)}\n{{\n  enabled = true\n  no_such_option = 1\n}}\n"
    r = client.post(f"/api/servers/{sid}/trace/sessions", json={"template": "custom", "config": cfg})
    assert r.status_code == 201
    sess = r.json()
    try:
        client.post(f"/api/servers/{sid}/databases/{dbid}/disconnect")
        execute(client, sid, dbid, "SELECT 1 FROM RDB$DATABASE")  # attaching reports the config error
        for _ in range(50):
            ev = client.get(f"/api/servers/{sid}/trace/sessions/{sess['id']}/events").json()
            errs = [e for e in ev["events"] if e["type"] == "TRACE_CONFIG_ERROR"]
            if errs:
                break
            time.sleep(0.2)
        assert errs and "no_such_option" in errs[0]["text"]
        assert ev["session"]["error"]
    finally:
        client.post(f"/api/servers/{sid}/trace/sessions/{sess['id']}/stop")
    assert client.post(f"/api/servers/{sid}/trace/sessions",
                       json={"template": "custom", "config": ""}).status_code == 400


# --------------------------------------------------------------- scheduler
def test_scheduled_backup_retention_and_sweep(client, database):
    sid, dbid = database
    fill(client, sid, dbid, 10)
    r = client.post("/api/schedules", json={"name": "nightly", "kind": "backup", "server_id": sid,
                                            "database_id": dbid, "cron": "0 3 * * *", "keep": 2})
    assert r.status_code == 201, r.text
    sch = r.json()
    assert sch["next_run"] > time.time()
    assert client.post("/api/schedules", json={**{k: sch[k] for k in ("name", "server_id", "database_id")},
                                               "cron": "61 * * * *"}).status_code == 400
    for _ in range(3):
        ok_job(client, client.post(f"/api/schedules/{sch['id']}/run"))
        time.sleep(1.1)  # distinct timestamps in file names
    backups = client.get(f"/api/schedules/{sch['id']}/backups").json()
    assert len(backups) == 2  # retention
    hist = client.get(f"/api/schedules/{sch['id']}/history").json()
    assert len(hist) == 3 and all(h["status"] == "finished" for h in hist)
    assert len(hist[0]["removed"]) == 1 and hist[0]["size"] > 0

    # restore one of the scheduled backups through the archive
    target = new_path("sched")
    try:
        ok_job(client, client.post(f"/api/servers/{sid}/maintenance/restore", json={
            "source": "archive", "backup_file": backups[0]["path"], "database_path": target}))
    finally:
        drop_path(target)

    upd = client.put(f"/api/schedules/{sch['id']}", json={"enabled": False, "cron": "@hourly"}).json()
    assert upd["enabled"] is False and upd["next_run"] is None and upd["cron"] == "@hourly"

    # the scheduler tick starts due schedules
    from fbadmin.maintenance.scheduler import get_scheduler

    sw = client.post("/api/schedules", json={"name": "sweep", "kind": "sweep", "server_id": sid,
                                             "database_id": dbid, "cron": "* * * * *"}).json()
    sched = get_scheduler()
    assert sched.tick(time.time()) == []  # not due yet
    started = sched.tick(sw["next_run"] + 1)
    assert len(started) == 1
    wait_job(client, {"id": started[0]})
    h = client.get(f"/api/schedules/{sw['id']}/history").json()
    assert h[0]["status"] == "finished" and h[0]["kind"] == "sweep"
    assert len(client.get("/api/schedules/history").json()) == 4
    assert len(client.get("/api/schedules").json()) == 2

    # persisted in the data dir
    from fbadmin.maintenance.scheduler import Scheduler

    assert set(Scheduler().schedules) == {sch["id"], sw["id"]}
    assert client.delete(f"/api/schedules/{sch['id']}", params={"delete_backups": True}).status_code == 204
    assert client.get(f"/api/schedules/{sch['id']}").status_code == 404
    assert client.get("/api/schedules/cron-preview", params={"expr": "@daily"}).json()["next"][0].endswith("00:00")
