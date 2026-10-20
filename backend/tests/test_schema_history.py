"""Schema history in git (milestone 2.0): snapshots, commits, diffs, migrations, push, scheduler, hooks."""

from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path

import pytest

from fbadmin import schemahistory as sh
from fbadmin.schemahistory import layout

from .conftest import requires_server
from .test_maintenance import execute, wait_job

HERE = Path(__file__).parent


def hbase(sid, dbid):
    return f"/api/servers/{sid}/databases/{dbid}/schema-history"


def git(repo, *args) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, check=True).stdout


# ------------------------------------------------------------------ units (no server)
def test_file_names_round_trip_and_never_escape():
    for name in ("CUSTOMER", "lower case", "A/B", "..", "X.Y", "Ünïcødé", 'Q"T', "$NAME_1"):
        f = layout.file_name(name)
        assert "/" not in f and "\\" not in f and f not in (".", "..") and not f.startswith(".")
        assert layout.object_name(f + ".sql") == name
    assert layout.file_name("CUSTOMER") == "CUSTOMER"
    assert layout.file_name("Customer") != layout.file_name("CUSTOMER")  # case-insensitive file systems


def test_describe_path_and_summary():
    assert layout.describe_path("tables/CUSTOMER.sql") == {"schema": None, "kind": "tables", "label": "table",
                                                           "name": "CUSTOMER"}
    assert layout.describe_path("PUBLIC/views/V.sql")["schema"] == "PUBLIC"
    assert layout.describe_path("SALES/grants.sql") == {"schema": "SALES", "kind": "grants", "label": "grants",
                                                        "name": None}
    assert layout.describe_path("full.sql") is None
    assert layout.describe_path(".fbadmin/snapshot.json") is None
    assert layout.describe_path("README.md") is None
    s = layout.change_summary([("A", "tables/T2.sql"), ("M", "tables/T1.sql"), ("D", "procedures/P.sql"),
                               ("M", "full.sql"), ("M", "grants.sql")])
    assert s == {"added": ["table T2"], "changed": ["grants", "table T1"], "dropped": ["procedure P"]}


def test_normalize():
    assert layout.normalize("a  \r\nb\t\n\n\n") == "a\nb\n"


def test_path_and_remote_validation(tmp_path, monkeypatch):
    monkeypatch.setenv("FBADMIN_SCHEMA_HISTORY_ROOT", str(tmp_path / "root"))
    assert sh.resolve_repo_path("db1") == tmp_path / "root" / "db1"
    assert sh.resolve_repo_path(str(tmp_path / "elsewhere")) == tmp_path / "elsewhere"  # desktop mode
    for bad in ("", "../x", "a/../../b", "a\\..\\b"):
        with pytest.raises(ValueError):
            sh.resolve_repo_path(bad)
    monkeypatch.setattr(sh, "_server_mode", lambda: True)
    with pytest.raises(ValueError, match="server mode"):
        sh.resolve_repo_path(str(tmp_path / "elsewhere"))
    assert sh.resolve_repo_path(str(tmp_path / "root" / "x")) == tmp_path / "root" / "x"
    # a symbolic link inside the root that points outside is refused, too
    (tmp_path / "root").mkdir()
    os.symlink(tmp_path, tmp_path / "root" / "link")
    with pytest.raises(ValueError):
        sh.resolve_repo_path("link/x")
    assert sh.check_subdir("/a/b/") == "a/b"
    for bad in ("../a", "a/../b", ".git", "-x", "a/.git/b"):
        with pytest.raises(ValueError):
            sh.check_subdir(bad)
    assert sh.check_remote("git@github.com:me/repo.git") == "git@github.com:me/repo.git"
    assert sh.check_remote("https://github.com/me/repo.git")
    assert sh.check_remote("ssh://git@host/repo.git")
    for bad in ("https://me:secret@github.com/me/repo.git", "ext::sh -c touch% /tmp/x", "--upload-pack=x"):
        with pytest.raises(ValueError):
            sh.check_remote(bad)
    assert sh.check_token("env:GIT_TOKEN") == "env:GIT_TOKEN"
    with pytest.raises(ValueError, match="env:NAME"):
        sh.check_token("ghp_plaintexttoken")


def test_git_missing_is_reported(client, monkeypatch):
    monkeypatch.setenv("FBADMIN_GIT", "/nonexistent/git")
    with pytest.raises(sh.GitMissing, match="git is not installed"):
        sh.gitrepo.git_binary()
    st = sh.status()
    assert st["git_available"] is False and "not installed" in st["error"]


# ------------------------------------------------------------------ integration
def configure(client, sid, dbid, tmp_path, **extra):
    body = {"repo_path": str(tmp_path / "history"), "committer_name": "Schema Bot",
            "committer_email": "bot@example.org", **extra}
    r = client.put(hbase(sid, dbid), json=body)
    assert r.status_code == 200, r.text
    return r.json()["config"]


def snap(client, sid, dbid):
    r = client.post(hbase(sid, dbid) + "/snapshot")
    assert r.status_code == 200, r.text
    return r.json()


def is_fb6(client, sid, dbid) -> bool:
    res = execute(client, sid, dbid, "SELECT rdb$get_context('SYSTEM', 'ENGINE_VERSION') FROM rdb$database")
    return int(res["results"][0]["rows"][0][0].split(".")[0]) >= 6


@requires_server
def test_snapshot_commit_diff_and_migration(client, database, tmp_path):
    sid, dbid = database
    execute(client, sid, dbid, (HERE / "sample_schema.sql").read_text())
    assert client.get(hbase(sid, dbid)).json()["config"] is None
    assert client.get(hbase(sid, dbid) + "/log").status_code == 404
    cfg = configure(client, sid, dbid, tmp_path)
    repo = Path(cfg["resolved_path"])
    pre = "PUBLIC/" if is_fb6(client, sid, dbid) else ""

    first = snap(client, sid, dbid)
    assert first["committed"] and len(first["commit"]) == 40
    assert f"table CUSTOMER{' (schema PUBLIC)' if pre else ''}" in first["changes"]["added"]
    files = set(git(repo, "ls-files").split())
    for f in ("full.sql", ".fbadmin/snapshot.json", f"{pre}tables/CUSTOMER.sql", f"{pre}tables/ORDERS.sql",
              f"{pre}views/V_CUSTOMER_TOTALS.sql", f"{pre}procedures/SP_ORDERS_FOR.sql",
              f"{pre}domains/D_MONEY.sql", f"{pre}sequences/SEQ_ORDERS.sql", f"{pre}triggers/ORDERS_BI.sql",
              f"{pre}indices/IX_ORDERS_AMOUNT.sql", f"{pre}exceptions/E_INVALID.sql"):
        assert f in files, (f, sorted(files))
    customer = (repo / f"{pre}tables/CUSTOMER.sql").read_text()
    assert "CREATE TABLE CUSTOMER" in customer and "COMMENT ON TABLE CUSTOMER IS 'Customers';" in customer
    assert "RESTART WITH" not in (repo / "full.sql").read_text()  # sequence values change with the data

    # determinism: an unchanged database gives no commit, even after data and sequence changes
    execute(client, sid, dbid, "SELECT NEXT VALUE FOR SEQ_ORDERS FROM RDB$DATABASE")
    again = snap(client, sid, dbid)
    assert again["committed"] is False and again["commit"] == first["commit"]
    assert git(repo, "rev-list", "--count", "HEAD").strip() == "1"
    assert git(repo, "status", "--porcelain") == ""

    # a change produces exactly the expected file changes
    execute(client, sid, dbid, "ALTER TABLE CUSTOMER ADD PHONE VARCHAR(30);\n"
                               "CREATE TABLE AUDIT_LOG (ID INTEGER NOT NULL PRIMARY KEY, MSG VARCHAR(100));\n"
                               "DROP INDEX IX_ORDERS_AMOUNT;")
    second = snap(client, sid, dbid)
    assert second["committed"]
    sfx = " (schema PUBLIC)" if pre else ""
    assert second["changes"] == {"added": [f"table AUDIT_LOG{sfx}"], "changed": [f"table CUSTOMER{sfx}"],
                                 "dropped": [f"index IX_ORDERS_AMOUNT{sfx}"]}
    names = git(repo, "show", "--name-status", "--format=", "HEAD").split("\n")
    assert sorted(x for x in names if x) == sorted([
        "M\t.fbadmin/snapshot.json", f"A\t{pre}tables/AUDIT_LOG.sql", f"M\t{pre}tables/CUSTOMER.sql",
        f"D\t{pre}indices/IX_ORDERS_AMOUNT.sql", "M\tfull.sql"])

    # commit metadata: desktop mode -> the configured identity is author and committer
    meta = git(repo, "show", "-s", "--format=%an|%ae|%cn|%ce|%s", "HEAD").strip()
    assert meta == f"Schema Bot|bot@example.org|Schema Bot|bot@example.org|Schema snapshot of {'t'}: " \
                   "1 added, 1 changed, 1 dropped"
    body = git(repo, "show", "-s", "--format=%b", "HEAD")
    assert f"Added:\n  table AUDIT_LOG{sfx}" in body and "manual" in body

    # history API
    log = client.get(hbase(sid, dbid) + "/log").json()
    assert [c["sha"] for c in log["commits"]] == [second["commit"], first["commit"]]
    assert log["commits"][0]["author"] == "Schema Bot" and log["has_more"] is False
    assert log["commits"][0]["changes"]["dropped"] == [f"index IX_ORDERS_AMOUNT{sfx}"]
    assert client.get(hbase(sid, dbid) + "/log", params={"limit": 1}).json()["has_more"] is True

    d = client.get(hbase(sid, dbid) + f"/commits/{second['commit']}").json()
    by_path = {f["path"]: f for f in d["files"]}
    assert ".fbadmin/snapshot.json" not in by_path
    assert by_path[f"{pre}tables/CUSTOMER.sql"]["status"] == "changed"
    assert "+    PHONE VARCHAR(30)" in by_path[f"{pre}tables/CUSTOMER.sql"]["diff"]
    assert by_path[f"{pre}tables/AUDIT_LOG.sql"]["status"] == "added"
    assert by_path[f"{pre}indices/IX_ORDERS_AMOUNT.sql"]["status"] == "dropped"
    assert d["parent"] == first["commit"]
    first_diff = client.get(hbase(sid, dbid) + f"/commits/{first['commit']}").json()
    assert all(f["status"] == "added" for f in first_diff["files"])

    r = client.get(hbase(sid, dbid) + f"/commits/{first['commit']}/file",
                   params={"path": f"{pre}indices/IX_ORDERS_AMOUNT.sql"})
    assert r.status_code == 200 and "CREATE DESCENDING INDEX IX_ORDERS_AMOUNT" in r.json()["content"]
    assert client.get(hbase(sid, dbid) + f"/commits/{second['commit']}/file",
                      params={"path": f"{pre}indices/IX_ORDERS_AMOUNT.sql"}).status_code == 404
    assert client.get(hbase(sid, dbid) + f"/commits/{second['commit']}/file",
                      params={"path": "../../etc/passwd"}).status_code == 400
    assert client.get(hbase(sid, dbid) + "/commits/HEAD;rm/file", params={"path": "full.sql"}).status_code == 400
    listing = client.get(hbase(sid, dbid) + f"/commits/{second['commit']}/files").json()
    assert {"path": f"{pre}tables/AUDIT_LOG.sql",
            "object": {"schema": "PUBLIC" if pre else None, "kind": "tables", "label": "table",
                       "name": "AUDIT_LOG"}} in listing

    # migration script between the two commits, from the stored snapshots
    m = client.get(hbase(sid, dbid) + "/migration",
                   params={"from_sha": first["commit"], "to_sha": second["commit"]}).json()
    assert m["summary"] == {"create": 1, "alter": 1, "drop": 1}, m["items"]
    assert "ADD PHONE VARCHAR(30)" in m["script"] and "CREATE TABLE AUDIT_LOG" in m["script"]
    assert "DROP INDEX IX_ORDERS_AMOUNT" in m["script"]
    back = client.get(hbase(sid, dbid) + "/migration",
                      params={"from_sha": second["commit"], "to_sha": first["commit"]}).json()
    assert "DROP TABLE AUDIT_LOG" in back["script"] and "CREATE DESCENDING INDEX IX_ORDERS_AMOUNT" in back["script"]
    same = client.get(hbase(sid, dbid) + "/migration",
                      params={"from_sha": second["commit"], "to_sha": second["commit"]}).json()
    assert same["items"] == [] and "identical" in same["script"]

    # the migration script really turns the old schema into the new one
    execute(client, sid, dbid, back["script"])
    assert snap(client, sid, dbid)["changes"] == {"added": [f"index IX_ORDERS_AMOUNT{sfx}"],
                                                  "changed": [f"table CUSTOMER{sfx}"],
                                                  "dropped": [f"table AUDIT_LOG{sfx}"]}
    assert git(repo, "diff", first["commit"], "HEAD", "--", ".", ":(exclude).fbadmin") == ""


@requires_server
def test_push_to_bare_remote_subdir_and_errors(client, database, tmp_path, monkeypatch):
    sid, dbid = database
    execute(client, sid, dbid, "CREATE TABLE T1 (ID INTEGER);")
    pre = "PUBLIC/" if is_fb6(client, sid, dbid) else ""
    bare = tmp_path / "remote.git"
    subprocess.run(["git", "init", "-q", "--bare", str(bare)], check=True)
    # an existing repository with other content: fbadmin only touches its sub-folder
    work = tmp_path / "existing"
    work.mkdir()
    git(work, "init", "-q")
    (work / "README.md").write_text("docs\n")
    (work / "staged.txt").write_text("not ours\n")
    git(work, "add", "staged.txt")
    configure(client, sid, dbid, tmp_path, repo_path=str(work), subdir="dbs/t", remote_url=str(bare),
              branch="schema")
    res = snap(client, sid, dbid)
    assert res["committed"] and res["push"]["ok"], res
    assert git(bare, "rev-parse", "refs/heads/schema").strip() == res["commit"]
    shown = git(work, "show", "--name-only", "--format=", "HEAD").split()
    assert f"dbs/t/{pre}tables/T1.sql" in shown and "staged.txt" not in shown and "README.md" not in shown
    assert git(work, "diff", "--cached", "--name-only").split() == ["staged.txt"]  # user's staged file kept
    assert (work / "README.md").exists()
    log = client.get(hbase(sid, dbid) + "/log").json()["commits"]
    assert len(log) == 1 and log[0]["changes"]["added"] == ["table T1" + (" (schema PUBLIC)" if pre else "")]
    d = client.get(hbase(sid, dbid) + f"/commits/{res['commit']}").json()
    assert f"{pre}tables/T1.sql" in {f["path"] for f in d["files"]}
    assert client.post(hbase(sid, dbid) + "/push").json()["ok"] is True

    # push errors are reported, not raised; the commit is kept
    configure(client, sid, dbid, tmp_path, repo_path=str(work), subdir="dbs/t",
              remote_url=str(tmp_path / "missing.git"), branch="schema")
    execute(client, sid, dbid, "CREATE TABLE T2 (ID INTEGER);")
    res = snap(client, sid, dbid)
    assert res["committed"] and res["push"]["ok"] is False and "missing.git" in res["push"]["error"]
    cfg = client.get(hbase(sid, dbid)).json()["config"]
    assert cfg["last_push_error"] and cfg["last_commit"] == res["commit"]
    r = client.post(hbase(sid, dbid) + "/push")
    assert r.status_code == 502

    # an HTTPS token is read from the environment and never shown in errors
    monkeypatch.setenv("FBADMIN_TEST_GIT_TOKEN", "s3cr3t-t0ken")
    r = client.put(hbase(sid, dbid), json={"repo_path": str(work), "subdir": "dbs/t", "branch": "schema",
                                           "remote_url": "http://127.0.0.1:9/repo.git",
                                           "token": "env:FBADMIN_TEST_GIT_TOKEN"})
    assert r.status_code == 200 and r.json()["config"]["token"] == "env:FBADMIN_TEST_GIT_TOKEN"
    r = client.post(hbase(sid, dbid) + "/push")
    assert r.status_code == 502 and "s3cr3t" not in r.text
    assert "s3cr3t" not in (tmp_path / "schema_history.json").read_text()
    assert "s3cr3t" not in (work / ".git" / "config").read_text()
    assert client.put(hbase(sid, dbid), json={"repo_path": str(work), "token": "plain"}).status_code == 400
    assert client.put(hbase(sid, dbid), json={"repo_path": "../x"}).status_code == 400
    assert client.put(hbase(sid, dbid), json={"repo_path": "x", "branch": "-bad"}).status_code == 400

    # removing the configuration keeps the repository
    assert client.delete(hbase(sid, dbid)).status_code == 204
    assert client.get(hbase(sid, dbid)).json()["config"] is None
    assert (work / "dbs" / "t" / "full.sql").exists()


@requires_server
def test_scheduler_kind_and_after_ddl_hook(client, database, tmp_path, monkeypatch):
    sid, dbid = database
    monkeypatch.setenv("FBADMIN_SCHEMA_HISTORY_DELAY", "0.2")
    cfg = configure(client, sid, dbid, tmp_path, after_ddl=True)
    repo = Path(cfg["resolved_path"])
    r = client.post("/api/schedules", json={"name": "schema", "kind": "schema_snapshot", "server_id": sid,
                                            "database_id": dbid, "cron": "0 4 * * *"})
    assert r.status_code == 201, r.text
    sched = r.json()
    job = wait_job(client, client.post(f"/api/schedules/{sched['id']}/run").json())
    assert job["status"] == "finished", job["log"]
    h = client.get(f"/api/schedules/{sched['id']}/history").json()[0]
    assert h["status"] == "finished" and h["kind"] == "schema_snapshot" and h["commit"]
    assert "schedule schema" in git(repo, "log", "-1", "--format=%b")

    # DDL through the dialogs' execute endpoint -> a background snapshot a moment later
    execute(client, sid, dbid, "CREATE TABLE HOOKED (ID INTEGER)")
    deadline = time.time() + 30
    while time.time() < deadline and git(repo, "rev-list", "--count", "HEAD").strip() != "2":
        time.sleep(0.2)
    assert git(repo, "rev-list", "--count", "HEAD").strip() == "2"
    assert "after DDL" in git(repo, "log", "-1", "--format=%b")
    assert "table HOOKED" in git(repo, "log", "-1", "--format=%b")

    # in the Query Tool the snapshot waits for the commit
    sess = client.post("/api/query/sessions", json={"server_id": sid, "database_id": dbid}).json()["session_id"]
    r = client.post(f"/api/query/sessions/{sess}/execute", json={"sql": "CREATE TABLE HOOKED2 (ID INTEGER)",
                                                                 "autocommit": False})
    assert r.json()["transaction_open"] is True
    time.sleep(0.6)
    sh.wait_idle()
    assert git(repo, "rev-list", "--count", "HEAD").strip() == "2"
    client.post(f"/api/query/sessions/{sess}/commit")
    deadline = time.time() + 30
    while time.time() < deadline and git(repo, "rev-list", "--count", "HEAD").strip() != "3":
        time.sleep(0.2)
    assert "table HOOKED2" in git(repo, "log", "-1", "--format=%b")
    client.delete(f"/api/query/sessions/{sess}")
    # plain queries never trigger a snapshot
    sh.after_sql(sid, dbid, "SELECT 1 FROM RDB$DATABASE")
    assert not sh._timers


@requires_server
def test_server_mode_permissions_and_author(tmp_path, monkeypatch):
    from .conftest import DB_DIR, HOST, PASSWORD, PORT, USER
    from .test_security import _client, login

    admin_pw = "admin-password-1"
    with _client(tmp_path, monkeypatch, SERVER_MODE="1", ADMIN_EMAIL="admin@example.org",
                 ADMIN_PASSWORD=admin_pw, SCHEMA_HISTORY_ROOT=str(tmp_path / "root")) as c:
        login(c, "admin@example.org", admin_pw)
        assert c.post("/api/admin/users", json={"username": "alice", "password": "alice-password",
                                                 "role": "user"}).status_code == 201
        srv = c.post("/api/servers", json={"name": "s", "host": HOST, "port": PORT, "user": USER,
                                           "password": PASSWORD, "save_password": True})
        assert srv.status_code == 201, srv.text
        sid = srv.json()["id"]
        import uuid

        path = f"{DB_DIR}/fbadmin_hist_{uuid.uuid4().hex[:8]}.fdb"
        r = c.post(f"/api/servers/{sid}/databases/create", json={"name": "h", "path": path})
        assert r.status_code == 201, r.text
        dbid = r.json()["id"]
        try:
            # outside the root: refused in server mode
            r = c.put(hbase(sid, dbid), json={"repo_path": str(tmp_path / "outside")})
            assert r.status_code == 400 and "server mode" in r.text
            r = c.put(hbase(sid, dbid), json={"repo_path": "h"})
            assert r.status_code == 200
            assert r.json()["config"]["resolved_path"] == str(tmp_path / "root" / "h")
            execute(c, sid, dbid, "CREATE TABLE T (ID INTEGER)")
            res = c.post(hbase(sid, dbid) + "/snapshot").json()
            assert res["committed"]
            # the fbadmin user is the author, the configured identity the committer
            meta = git(tmp_path / "root" / "h", "show", "-s", "--format=%an|%ae|%cn", "HEAD").strip()
            assert meta == "admin@example.org|admin@example.org|fbadmin", meta

            c.post("/api/auth/logout")
            c.headers.pop("X-CSRF-Token", None)
            login(c, "alice", "alice-password")
            assert c.put(hbase(sid, dbid), json={"repo_path": "evil"}).status_code == 403
            assert c.delete(hbase(sid, dbid)).status_code == 403
            # alice cannot see the admin's database at all
            assert c.get(hbase(sid, dbid)).status_code in (403, 404)
            assert c.post(hbase(sid, dbid) + "/snapshot").status_code in (403, 404)
            assert c.get(hbase(sid, dbid) + "/log").status_code in (403, 404)
            c.post("/api/auth/logout")
            c.headers.pop("X-CSRF-Token", None)
            login(c, "admin@example.org", admin_pw)
        finally:
            c.post(f"/api/servers/{sid}/databases/{dbid}/drop")
    from fbadmin.api import deps

    deps.reset_state()


@requires_server
def test_firebird6_schemas_are_folders(client, database, tmp_path):
    sid, dbid = database
    if not is_fb6(client, sid, dbid):
        pytest.skip("Firebird 6 only")
    execute(client, sid, dbid, "CREATE TABLE T1 (ID INTEGER);")
    cfg = configure(client, sid, dbid, tmp_path)
    repo = Path(cfg["resolved_path"])
    first = snap(client, sid, dbid)
    execute(client, sid, dbid, "CREATE SCHEMA SALES;\nCREATE TABLE SALES.ORDERS (ID INTEGER NOT NULL PRIMARY KEY);")
    second = snap(client, sid, dbid)
    assert second["changes"]["added"] == ["table ORDERS (schema SALES)"]
    assert (repo / "SALES" / "tables" / "ORDERS.sql").exists() and (repo / "PUBLIC" / "tables" / "T1.sql").exists()
    full = (repo / "full.sql").read_text()
    assert "CREATE SCHEMA SALES;" in full and "SET SEARCH_PATH TO SALES, SYSTEM;" in full
    m = client.get(hbase(sid, dbid) + "/migration",
                   params={"from_sha": first["commit"], "to_sha": second["commit"]}).json()
    assert "CREATE SCHEMA SALES;" in m["script"] and "CREATE TABLE ORDERS" in m["script"]
    assert [i["schema"] for i in m["items"]] == ["SALES"]
    assert snap(client, sid, dbid)["committed"] is False


@requires_server
def test_complex_schema_is_deterministic(client, database, tmp_path):
    sid, dbid = database
    execute(client, sid, dbid, (HERE / "complex_schema.sql").read_text())
    configure(client, sid, dbid, tmp_path)
    first = snap(client, sid, dbid)
    assert first["committed"] and first["files"] > 20
    # a fresh extraction (new connection, new snapshot) gives byte-identical files
    second = snap(client, sid, dbid)
    assert second["committed"] is False and second["commit"] == first["commit"]
