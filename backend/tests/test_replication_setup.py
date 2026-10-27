"""Roadmap 2.2: the replication setup wizard.

The first tests run on the usual test server (plan checks, replication.conf merging, a setup that stops
because the primary writes no journal). The end-to-end tests need the two servers of
``scripts/replication-test-servers.sh`` (FBADMIN_TEST_REPL=1): fbadmin writes both replication.conf files,
copies the primary (nbackup -SEQ, through a shared directory or moved by hand), makes the replica and checks that changes arrive,
asynchronously (journal archive, replica server restarted) and synchronously.
"""

from __future__ import annotations

import os
import shlex
import socket
import subprocess
import time
import uuid
from pathlib import Path

import pytest

from fbadmin.monitoring import repl_setup as R

from .conftest import DB_DIR, HOST, PASSWORD, USER, requires_server


# ------------------------------------------------------------------ units
def test_merge_conf_replaces_or_appends():
    text = ("# header\n\ndatabase\n{\n    log_errors = true\n}\n\n"
            "database = /data/a.fdb\n{\n    journal_directory = /old\n    # inner { braces } in a comment\n}\n"
            "database = /data/b.fdb {\n    sync_replica = x\n}\n")
    block = "database = /data/a.fdb\n{\n    journal_directory = /new\n}\n"
    out, replaced = R.merge_conf(text, "/data/a.fdb", block)
    assert replaced and "/new" in out and "/old" not in out
    assert out.count("database") == 3 and "log_errors = true" in out and "sync_replica = x" in out
    # a block opened on the database line
    out2, replaced = R.merge_conf(out, "/data/b.fdb", "database = /data/b.fdb\n{\n    sync_replica = y\n}\n")
    assert replaced and "sync_replica = y" in out2 and "sync_replica = x" not in out2
    out3, replaced = R.merge_conf(out2, "/data/c.fdb", "database = /data/c.fdb\n{\n}\n")
    assert not replaced and out3.startswith(out2) and out3.endswith("database = /data/c.fdb\n{\n}\n")
    # Windows paths compare case-insensitively
    win = "database = C:\\Data\\A.FDB\n{\n}\n"
    out4, replaced = R.merge_conf(win, "c:\\data\\a.fdb", "database = c:\\data\\a.fdb\n{\n    x = 1\n}\n")
    assert replaced and "x = 1" in out4
    with pytest.raises(ValueError, match="closing brace"):
        R.merge_conf("database = /a.fdb\n{\n  x = 1\n", "/a.fdb", "database = /a.fdb\n{\n}\n")


def test_write_conf_in_place_and_server_mode(tmp_path, monkeypatch):
    conf = tmp_path / "replication.conf"
    conf.write_text("database\n{\n}\n")
    inode = conf.stat().st_ino
    block = "database = /data/a.fdb\n{\n    journal_directory = /j\n}\n"
    out = R.write_conf(str(conf), "/data/a.fdb", block, server_mode=False)
    assert not out["replaced"] and Path(out["backup"]).read_text() == "database\n{\n}\n"
    assert conf.stat().st_ino == inode and "journal_directory = /j" in conf.read_text()
    with pytest.raises(ValueError, match="named replication.conf"):
        R.write_conf(str(tmp_path / "firebird.conf"), "/data/a.fdb", block, server_mode=False)
    with pytest.raises(ValueError, match="doesn't exist"):
        R.write_conf(str(tmp_path / "x" / "replication.conf"), "/data/a.fdb", block, server_mode=False)
    with pytest.raises(ValueError, match="must start with"):
        R.write_conf(str(conf), "/data/a.fdb", "journal_directory = /x", server_mode=False)
    monkeypatch.delenv("FBADMIN_REPLICATION_CONF", raising=False)
    with pytest.raises(PermissionError):
        R.write_conf(str(conf), "/data/a.fdb", block, server_mode=True)
    monkeypatch.setenv("FBADMIN_REPLICATION_CONF", str(conf))
    assert R.write_conf(str(conf), "/data/a.fdb", block, server_mode=True)["replaced"]


def test_defaults_and_blocks():
    b = R.defaults("/var/lib/firebird/data/main.fdb", R.SetupIn(replica_server_id="x", replica_path="/r/main.fdb"))
    assert b.journal_directory == "/var/lib/firebird/data/replication/journal"
    assert b.source_directory == b.archive_directory == "/var/lib/firebird/data/replication/archive"
    assert b.copy_directory == b.copy_directory_replica == "/var/lib/firebird/data/replication"
    w = R.defaults("C:\\Data\\main.fdb", R.SetupIn(replica_server_id="x", replica_path="D:\\r.fdb"))
    assert w.journal_directory == "C:\\Data\\replication\\journal"
    conf = R.blocks("/data/main.fdb", b, "{GUID}", "replica-host", 3050, "SYSDBA", None)
    assert conf["primary"].startswith("database = /data/main.fdb\n{")
    assert "journal_archive_directory = /var/lib/firebird/data/replication/archive" in conf["primary"]
    assert 'source_guid = "{GUID}"' in conf["replica"] and conf["replica"].startswith("database = /r/main.fdb")
    s = R.defaults("/data/main.fdb", R.SetupIn(replica_server_id="x", replica_path="/r.fdb", mode="sync"))
    conf = R.blocks("/data/main.fdb", s, None, "replica-host", 3051, "SYSDBA", "pw")
    assert "sync_replica = SYSDBA:pw@replica-host/3051:/r.fdb" in conf["primary"] and conf["replica"] is None
    with pytest.raises(ValueError):
        R.SetupIn(replica_server_id="x", replica_path="/r.fdb\n}\ndatabase = /etc")


# ------------------------------------------------------------------ on the usual test server
def _base(sid, dbid):
    return f"/api/servers/{sid}/databases/{dbid}/replication"


def _exec(client, sid, dbid, sql):
    r = client.post(f"/api/servers/{sid}/databases/{dbid}/execute", json={"sql": sql})
    assert r.status_code == 200, r.text
    assert r.json()["error"] is None, r.json()["error"]


def _wait_job(client, job, timeout=300):
    end = time.time() + timeout
    while time.time() < end:
        j = client.get(f"/api/jobs/{job['id']}").json()
        if j["status"] != "running":
            return j
        time.sleep(0.3)
    raise AssertionError("job did not finish")


def _checks(plan) -> dict[str, str]:
    return {c["code"]: c["level"] for c in plan["checks"]}


@requires_server
def test_plan_checks(client, server, database):
    sid, dbid = database
    path = client.get(f"/api/servers/{sid}").json()["databases"][0]["path"]
    replica = f"{DB_DIR}/fbadmin_test_replica_{uuid.uuid4().hex[:8]}.fdb"
    plan = client.post(_base(sid, dbid) + "/setup/plan", json={"replica_server_id": sid, "replica_path": replica})
    assert plan.status_code == 200, plan.text
    plan = plan.json()
    if not client.get(_base(sid, dbid)).json()["supported"]:  # Firebird 3
        assert _checks(plan)["version"] == "error" and plan["ok"] is False
        r = client.post(_base(sid, dbid) + "/setup/run", json={"replica_server_id": sid, "replica_path": replica})
        assert r.status_code == 400 and "Firebird 4" in r.json()["detail"]
        return
    assert plan["ok"], plan["checks"]
    assert plan["steps"][:3] == ["conf_primary", "publication", "journal_check"]
    assert plan["conf"]["primary"].startswith(f"database = {path}")
    guid = plan["primary"]["guid"]
    assert f'source_guid = "{guid}"' in plan["conf"]["replica"]
    assert _checks(plan)["copy_dir"] == "info"

    _exec(client, sid, dbid, "CREATE TABLE WITH_KEY (ID INTEGER NOT NULL PRIMARY KEY); CREATE TABLE NO_KEY (ID INTEGER);")
    same = client.post(_base(sid, dbid) + "/setup/plan", json={"replica_server_id": sid, "replica_path": path}).json()
    c = _checks(same)
    assert c["same_file"] == "error" and c["replica_exists"] == "error" and c["no_key"] == "warning"
    only = client.post(_base(sid, dbid) + "/setup/plan", json={
        "replica_server_id": sid, "replica_path": replica, "tables": ["WITH_KEY"]}).json()
    assert "no_key" not in _checks(only) and only["ok"]
    bad = client.post(_base(sid, dbid) + "/setup/plan", json={
        "replica_server_id": sid, "replica_path": replica, "tables": ["NOPE"]}).json()
    assert _checks(bad)["unknown_tables"] == "error"
    sync = client.post(_base(sid, dbid) + "/setup/plan", json={
        "replica_server_id": sid, "replica_path": replica, "mode": "sync", "transfer": "manual"}).json()
    assert sync["ok"] and sync["steps"] == ["conf_primary", "copy", "transfer", "restore", "replica_mode", "register",
                                            "publication", "verify"]
    assert _checks(sync)["copy_manual"] == "info"
    assert f"sync_replica = {USER}:{PASSWORD}@{HOST}" in sync["conf"]["primary"]

    # with publication enabled: no synchronous setup (its copy is made with publication off)
    assert client.post(_base(sid, dbid) + "/publication", json={"enabled": True}).status_code == 200
    s = client.post(_base(sid, dbid) + "/setup/plan", json={
        "replica_server_id": sid, "replica_path": replica, "mode": "sync"}).json()
    assert _checks(s)["sync_publication"] == "error"
    r = client.post(_base(sid, dbid) + "/setup/run", json={
        "replica_server_id": sid, "replica_path": replica, "mode": "sync"})
    assert r.status_code == 400 and "Disable publication" in r.json()["detail"]


@requires_server
def test_setup_stops_when_the_primary_writes_no_journal(client, server, database):
    """The usual test server has no replication.conf block: the setup enables publication, sees that the
    replication sequence doesn't move, and stops before copying anything."""
    sid, dbid = database
    if not client.get(_base(sid, dbid)).json()["supported"]:
        pytest.skip("Firebird 4+")
    replica = f"{DB_DIR}/fbadmin_test_replica_{uuid.uuid4().hex[:8]}.fdb"
    r = client.post(_base(sid, dbid) + "/setup/run", json={"replica_server_id": sid, "replica_path": replica})
    assert r.status_code == 202, r.text
    job = _wait_job(client, r.json())
    assert job["status"] == "failed" and "writes no journal" in job["error"], job
    res = client.get(_base(sid, dbid) + f"/setup/runs/{job['id']}").json()
    assert res["status"] == "failed" and "replica_db_id" not in res
    st = client.get(_base(sid, dbid)).json()
    assert st["publication_enabled"] and R.CHECK_TABLE in {t["name"] for t in st["tables"]}
    plan = client.post(_base(sid, dbid) + "/setup/plan", json={"replica_server_id": sid, "replica_path": replica}).json()
    assert "replica_exists" not in _checks(plan)  # nothing was copied


@requires_server
def test_check_needs_publication(client, server, database):
    sid, dbid = database
    if not client.get(_base(sid, dbid)).json()["supported"]:
        pytest.skip("Firebird 4+")
    other = client.post(f"/api/servers/{sid}/databases/create", json={
        "name": "peer", "path": f"{DB_DIR}/fbadmin_test_peer_{uuid.uuid4().hex[:8]}.fdb"}).json()
    try:
        r = client.post(_base(sid, dbid) + "/check", json={"peer_server": sid, "peer_db": other["id"]})
        assert r.status_code == 400 and "Publication is disabled" in r.json()["detail"]
        assert client.get(_base(sid, dbid) + "/check/nope").status_code == 404
    finally:
        client.post(f"/api/servers/{sid}/databases/{other['id']}/drop")


def test_conf_write_endpoint(client, tmp_path):
    conf = tmp_path / "replication.conf"
    conf.write_text("database\n{\n}\n")
    r = client.post("/api/replication/conf/write", json={
        "path": str(conf), "database": "/d/a.fdb", "block": "database = /d/a.fdb\n{\n    x = 1\n}\n"})
    assert r.status_code == 200, r.text
    assert r.json()["replaced"] is False and "x = 1" in conf.read_text()
    r = client.post("/api/replication/conf/write", json={
        "path": str(tmp_path / "other.conf"), "database": "/d/a.fdb", "block": "database = /d/a.fdb\n{\n}\n"})
    assert r.status_code == 400


# ------------------------------------------------------------------ end to end (two servers)
REPL = os.environ.get("FBADMIN_TEST_REPL") == "1"
P_PORT = int(os.environ.get("FBADMIN_TEST_REPL_PRIMARY_PORT", "3060"))
R_PORT = int(os.environ.get("FBADMIN_TEST_REPL_REPLICA_PORT", "3061"))
REPL_DIR = Path(os.environ.get("FBADMIN_REPL_DIR", "/tmp/fbadmin-repl"))
EXEC = shlex.split(os.environ.get("FBADMIN_TEST_REPL_EXEC", "docker exec -u firebird fbrepl-p"))
EXEC_ROOT = shlex.split(os.environ.get("FBADMIN_TEST_REPL_EXEC_ROOT", "docker exec fbrepl-p"))  # the copy is root's
RESTART = shlex.split(os.environ.get("FBADMIN_TEST_REPL_RESTART", "docker restart fbrepl-r"))
REPLICA_ALIAS = os.environ.get("FBADMIN_TEST_REPL_REPLICA_HOST", "fbrepl-r")  # as the primary server sees it
DATA = "/var/lib/firebird/data"
requires_repl = pytest.mark.skipif(not REPL, reason="needs scripts/replication-test-servers.sh (FBADMIN_TEST_REPL=1)")


def _wait_port(port, timeout=60):
    end = time.time() + timeout
    while time.time() < end:
        try:
            with socket.create_connection((HOST, port), timeout=1):
                return
        except OSError:
            time.sleep(0.5)
    raise AssertionError(f"port {port} not up")


@pytest.fixture
def pair(client):
    p = client.post("/api/servers", json={"name": "primary", "host": HOST, "port": P_PORT, "user": USER,
                                          "password": PASSWORD}).json()
    r = client.post("/api/servers", json={"name": "replica", "host": HOST, "port": R_PORT, "user": USER,
                                          "password": PASSWORD}).json()
    run = uuid.uuid4().hex[:8]
    path = f"{DATA}/repl_{run}.fdb"
    db = client.post(f"/api/servers/{p['id']}/databases/create", json={"name": "main", "path": path})
    assert db.status_code == 201, db.text
    created = [(p["id"], db.json()["id"])]
    # per run: its own journal and archive (Firebird doesn't create them)
    jdir, adir = f"/repl/{run}/journal", f"/repl/{run}/archive"
    subprocess.run([*EXEC, "mkdir", "-p", jdir, adir], check=True)
    ctx = {"p": p["id"], "r": r["id"], "db": db.json()["id"], "path": path, "run": run,
           "replica_path": f"{DATA}/repl_{run}_replica.fdb", "journal": jdir, "archive": adir, "created": created}
    yield ctx
    for sid, dbid in created:
        client.post(f"/api/servers/{sid}/databases/{dbid}/drop")


def _write(client, side: str, database: str, block: str):
    r = client.post("/api/replication/conf/write", json={
        "path": str(REPL_DIR / side / "replication.conf"), "database": database, "block": block})
    assert r.status_code == 200, r.text


def _setup(client, ctx, **opts) -> tuple[dict, dict]:
    body = {"replica_server_id": ctx["r"], "replica_path": ctx["replica_path"], "journal_directory": ctx["journal"],
            "archive_directory": ctx["archive"], "copy_directory": f"/repl/{ctx['run']}", "archive_timeout": 2, **opts}
    base = _base(ctx["p"], ctx["db"])
    plan = client.post(base + "/setup/plan", json=body).json()
    assert plan["ok"], plan["checks"]
    _write(client, "primary", ctx["path"], plan["conf"]["primary"])
    assert client.post(base + "/setup/reopen").json()["reopened"]
    r = client.post(base + "/setup/run", json=body)
    assert r.status_code == 202, r.text
    job = _wait_job(client, r.json())
    assert job["status"] == "finished", (job["error"], job["log"])
    res = client.get(base + f"/setup/runs/{job['id']}").json()
    if res.get("waiting_for_transfer"):
        return plan, res
    ctx["created"].append((ctx["r"], res["replica_db_id"]))
    _restart_replica(client, ctx, plan, res)
    return plan, res


def _restart_replica(client, ctx, plan, res) -> None:
    if not plan["conf"]["replica"]:
        return
    _write(client, "replica", ctx["replica_path"], plan["conf"]["replica"])
    subprocess.run(RESTART, check=True, capture_output=True)  # the replica server reads its block at startup
    _wait_port(R_PORT)
    time.sleep(2)
    client.post(f"/api/servers/{ctx['r']}/databases/{res['replica_db_id']}/disconnect")


def _arrives(client, ctx, res, timeout=120) -> dict:
    base = _base(ctx["p"], ctx["db"])
    sent = client.post(base + "/check", json={"peer_server": ctx["r"], "peer_db": res["replica_db_id"]})
    assert sent.status_code == 200, sent.text
    token = sent.json()["token"]
    end = time.time() + timeout
    last: dict = {}
    while time.time() < end:
        r = client.get(base + f"/check/{token}")
        if r.status_code == 200:
            last = r.json()
            if last["arrived"]:
                return last
        time.sleep(1)
    raise AssertionError(f"the check row did not arrive: {last}")


def _rows(client, sid, dbid, table):
    r = client.get(f"/api/servers/{sid}/databases/{dbid}/tables/{table}/data")
    assert r.status_code == 200, r.text
    return sorted(row[0] for row in r.json()["rows"])


@requires_repl
def test_async_with_a_physical_copy(client, pair):
    """Publication and DDL history before the copy: nbackup -SEQ keeps the sequence, so nothing is applied
    twice (a replayed CREATE TABLE would stop the replica)."""
    ctx = pair
    p, db = ctx["p"], ctx["db"]
    _exec(client, p, db, "CREATE TABLE T (ID INTEGER NOT NULL PRIMARY KEY, V VARCHAR(20));"
                         " INSERT INTO T VALUES (1, 'before');")
    plan, res = _setup(client, ctx)
    assert res["copy_file"].startswith(f"/repl/{ctx['run']}/") and res["sequence_at_copy"] >= 1
    st = client.get(_base(ctx["r"], res["replica_db_id"])).json()
    assert st["replica_mode"] == "read_only" and st["publication_enabled"] is False
    assert st["guid"] == plan["primary"]["guid"]  # a physical copy
    _exec(client, p, db, "INSERT INTO T VALUES (2, 'after'); CREATE TABLE T2 (ID INTEGER NOT NULL PRIMARY KEY);"
                         " INSERT INTO T2 VALUES (7);")
    got = _arrives(client, ctx, res)
    assert got["replica_mode"] == "read_only" and got["arrived_after"] is not None
    assert _rows(client, ctx["r"], res["replica_db_id"], "T") == [1, 2]
    assert _rows(client, ctx["r"], res["replica_db_id"], "T2") == [7]


@requires_repl
def test_async_with_a_manual_transfer(client, pair):
    """Servers without a shared directory: the setup stops after the copy, the administrator moves the file,
    and the restore continues the setup."""
    ctx = pair
    p, db = ctx["p"], ctx["db"]
    _exec(client, p, db, "CREATE TABLE T (ID INTEGER NOT NULL PRIMARY KEY); INSERT INTO T VALUES (1);")
    moved = f"/repl/{ctx['run']}/moved"
    plan, res = _setup(client, ctx, transfer="manual", copy_directory_replica=moved)
    assert res["waiting_for_transfer"] and "replica_db_id" not in res
    _exec(client, p, db, "INSERT INTO T VALUES (2);")  # journaled: reaches the replica later
    name = res["copy_file"].rsplit("/", 1)[1]
    subprocess.run([*EXEC_ROOT, "sh", "-c", f"mkdir -p {moved} && cp -p {res['copy_file']} {moved}/"], check=True)
    body = {"replica_server_id": ctx["r"], "replica_path": ctx["replica_path"], "journal_directory": ctx["journal"],
            "archive_directory": ctx["archive"], "copy_directory": f"/repl/{ctx['run']}", "transfer": "manual",
            "copy_file": f"{moved}/{name}"}
    r = client.post(_base(p, db) + "/setup/restore", json=body)
    assert r.status_code == 202, r.text
    job = _wait_job(client, r.json())
    assert job["status"] == "finished", (job["error"], job["log"])
    res = client.get(_base(p, db) + f"/setup/runs/{job['id']}").json()
    ctx["created"].append((ctx["r"], res["replica_db_id"]))
    _restart_replica(client, ctx, plan, res)
    _exec(client, p, db, "INSERT INTO T VALUES (3); CREATE TABLE T3 (ID INTEGER NOT NULL PRIMARY KEY);")
    _arrives(client, ctx, res)
    assert _rows(client, ctx["r"], res["replica_db_id"], "T") == [1, 2, 3]
    assert _rows(client, ctx["r"], res["replica_db_id"], "T3") == []
    # a second restore of the same replica is refused (the file exists now)
    again = client.post(_base(p, db) + "/setup/restore", json=body)
    assert again.status_code == 400 and "exists" in again.json()["detail"]


@requires_repl
def test_sync(client, pair):
    ctx = pair
    p, db = ctx["p"], ctx["db"]
    _exec(client, p, db, "CREATE TABLE T (ID INTEGER NOT NULL PRIMARY KEY); INSERT INTO T VALUES (1);")
    plan, res = _setup(client, ctx, mode="sync", sync_address=REPLICA_ALIAS)
    assert plan["conf"]["replica"] is None and f"@{REPLICA_ALIAS}:" in plan["conf"]["primary"]
    _exec(client, p, db, "INSERT INTO T VALUES (2);")
    got = _arrives(client, ctx, res, timeout=20)  # synchronous: on the replica at commit
    assert got["arrived_after"] < 10
    assert _rows(client, ctx["r"], res["replica_db_id"], "T") == [1, 2]
