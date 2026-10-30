"""Roadmap 2.2: versioned migrations - scripts in git (or a directory) applied in order, the record in the
database, the dry run on a copy, apply, baseline, repair, the lock, and the command-line tool."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import uuid
from pathlib import Path

import pytest

from fbadmin import migrations as M

from .conftest import DB_DIR, HOST, PASSWORD, PORT, USER, requires_server
from .test_maintenance import execute, wait_job

BACKEND = Path(__file__).resolve().parent.parent


# ------------------------------------------------------------------ units
def test_names_order_and_checksums():
    files = [("V2__orders.sql", "B"), ("V1.10__ten.sql", "C"), ("V1_2__two.sql", "D"), ("V1__create_customers.sql", "A"),
             ("sub/V3.0__three.sql", "E"), ("README.md", "x"), ("R__view.sql", "x"), ("v4__lower_case.sql", "F")]
    scripts, warnings = M.parse(files)
    assert [s.version for s in scripts] == ["1", "1.2", "1.10", "2", "3", "4"]
    assert scripts[0].description == "create customers" and scripts[4].name == "sub/V3.0__three.sql"
    assert warnings == ["R__view.sql is ignored: migration scripts are named V<version>__<description>.sql"]
    with pytest.raises(ValueError, match="Two scripts have version 1"):
        M.parse([("V1__a.sql", ""), ("V1.0__b.sql", "")])
    assert M.checksum("﻿a\r\nb\r\n") == M.checksum("a\nb\n") != M.checksum("a\nb")
    with pytest.raises(ValueError):
        M.version_key("1.x")


def _row(v, *, kind="SQL", ok=True, checksum=None):
    return {"version": v, "key": M.version_key(v), "description": "", "script": f"V{v}__x.sql", "checksum": checksum,
            "kind": kind, "success": ok, "installed_by": "t", "installed_at": 1.0, "execution_ms": 1,
            "source_ref": None, "error": None if ok else "boom"}


def test_status_rules():
    scripts, _ = M.parse([(f"V{v}__s{v}.sql", f"-- {v}") for v in ("1", "2", "3", "4")])
    c = {s.version: s.checksum for s in scripts}
    st = M.status(scripts, [_row("1", checksum=c["1"]), _row("3", checksum=c["3"])])
    states = {i["version"]: i["state"] for i in st["items"]}
    assert states == {"1": "applied", "2": "late", "3": "applied", "4": "pending"}
    assert st["errors"] and "lower version" in st["errors"][0] and st["current"] == "3"
    st = M.status(scripts, [_row("1", checksum=c["1"]), _row("3", checksum=c["3"])], out_of_order=True)
    assert st["pending"] == ["2", "4"] and not st["errors"]
    st = M.status(scripts, [_row("1", checksum="0" * 64)])
    assert st["items"][0]["state"] == "changed" and "changed after it was applied" in st["errors"][0]
    st = M.status(scripts, [_row("1", checksum=c["1"]), _row("2", ok=False, checksum=c["2"])])
    assert {i["version"]: i["state"] for i in st["items"]}["2"] == "failed" and "repair" in st["errors"][0]
    st = M.status(scripts, [_row("2", kind="BASELINE")])
    assert [i["state"] for i in st["items"]] == ["baseline", "baseline", "pending", "pending"]
    assert st["pending"] == ["3", "4"] and st["current"] == "2"
    st = M.status(scripts, [_row("2.5", kind="BASELINE")])
    assert [i["state"] for i in st["items"]] == ["baseline", "baseline", "baseline_record", "pending", "pending"]
    st = M.status(scripts, [_row("1", checksum=c["1"]), _row("1.5", checksum="x")], target="3")  # 1.5: gone
    assert st["pending"] == ["2", "3"] and st["warnings"] and {i["version"]: i["state"] for i in st["items"]}["4"] == "above_target"
    assert M.status(scripts, [])["fingerprint"] != M.status(scripts[:3], [])["fingerprint"]


# ------------------------------------------------------------------ git repository of scripts
def git(repo: Path, *args: str) -> str:
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t",
           "GIT_COMMITTER_EMAIL": "t@t"}
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True, env=env).stdout


def commit(repo: Path, files: dict[str, str | None], msg: str = "change") -> None:
    for name, text in files.items():
        p = repo / "db" / name
        if text is None:
            p.unlink()
        else:
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(text)
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", msg)


@pytest.fixture
def repo(tmp_path):
    r = tmp_path / "scripts-repo"
    r.mkdir()
    git(r, "init", "-q", "-b", "main")
    commit(r, {
        "V1__customers.sql": "CREATE TABLE CUSTOMERS (ID INTEGER NOT NULL PRIMARY KEY, NAME VARCHAR(40));\n",
        "V2__seed.sql": "INSERT INTO CUSTOMERS VALUES (1, 'Ann');\nINSERT INTO CUSTOMERS VALUES (2, 'Bob');\n",
        "notes.txt": "not a script",
    }, "first")
    return r


def _base(sid, dbid):
    return f"/api/servers/{sid}/databases/{dbid}/migrations"


def _job(client, r, ok=True):
    assert r.status_code == 202, r.text
    j = wait_job(client, r.json(), timeout=300)
    if ok:
        assert j["status"] == "finished", (j.get("error"), j["log"])
    return j


def _rows(client, sid, dbid, table):
    r = client.get(f"/api/servers/{sid}/databases/{dbid}/tables/{table}/data")
    assert r.status_code == 200, r.text
    return r.json()["rows"]


@requires_server
def test_git_source_dry_run_apply_repair(client, database, repo):
    sid, dbid = database
    b = _base(sid, dbid)
    assert client.get(b).status_code == 400  # no source yet
    assert client.put(b + "/source", json={"kind": "git", "url": "https://u:p@example.com/x.git"}).status_code == 400
    r = client.put(b + "/source", json={"kind": "git", "url": str(repo), "ref": "main", "path": "db"})
    assert r.status_code == 200, r.text
    st = client.get(b).json()
    assert st["pending"] == ["1", "2"] and st["current"] is None and st["source_ref"].startswith("main@")
    assert st["dry_run_needed"] == "Run a dry run first"

    # apply needs a dry run of the same scripts
    assert client.post(b + "/apply", json={}).status_code == 409
    dj = _job(client, client.post(b + "/dry-run", json={}))
    rep = client.get(b + f"/runs/{dj['id']}").json()
    assert rep["ok"] and [s["script"] for s in rep["scripts"]] == ["V1__customers.sql", "V2__seed.sql"]
    assert rep["scripts"][1]["statements"] == 2 and rep["copy"] == "metadata"
    assert {(c["type"], c["name"], c["change"]) for c in rep["changes"]} >= {("tables", "CUSTOMERS", "created")}
    assert rep["summary"]["created"] >= 1 and not any("FBADMIN_MIGRATIONS" in c["name"] for c in rep["changes"])
    assert client.get(b).json()["dry_run_needed"] is None
    # the copy is gone
    assert rep["scratch"].endswith(".fdb") and "fbadmin_migrate_" in rep["scratch"]
    assert "Dropped the copy" in "\n".join(client.get(f"/api/jobs/{dj['id']}").json()["log"])

    aj = _job(client, client.post(b + "/apply", json={}))
    res = client.get(b + f"/runs/{aj['id']}").json()
    assert [a["script"] for a in res["applied"]] == ["V1__customers.sql", "V2__seed.sql"] and res["current"] == "2"
    assert _rows(client, sid, dbid, "CUSTOMERS") == [[1, "Ann"], [2, "Bob"]]
    st = client.get(b).json()
    assert [i["state"] for i in st["items"]] == ["applied", "applied"] and st["pending"] == []
    assert st["items"][0]["installed_by"] and st["items"][0]["source_ref"].startswith("main@")
    rec = _rows(client, sid, dbid, "FBADMIN_MIGRATIONS")
    assert len(rec) == 2

    # a new script that fails half way: the dry run catches it
    commit(repo, {"V3__email.sql": "ALTER TABLE CUSTOMERS ADD EMAIL VARCHAR(80);\nALTER TABLE NOPE ADD X INTEGER;\n"})
    assert client.post(b + "/apply", json={}).json()["detail"].startswith("The scripts or the database changed")
    dj = _job(client, client.post(b + "/dry-run", json={}))
    rep = client.get(b + f"/runs/{dj['id']}").json()
    assert not rep["ok"] and rep["scripts"][0]["error"]["line"] == 2 and "NOPE" in rep["scripts"][0]["error"]["sql"]
    assert client.post(b + "/apply", json={}).json()["detail"].startswith("The last dry run failed")
    # applied anyway: the record says it failed, and the next run waits for a repair
    aj = _job(client, client.post(b + "/apply", json={"skip_dry_run": True}), ok=False)
    assert "V3__email.sql failed at line 2" in aj["error"]
    st = client.get(b).json()
    assert st["items"][2]["state"] == "failed" and "repair" in st["errors"][0]
    assert client.post(b + "/dry-run", json={}).status_code == 400
    execute(client, sid, dbid, "ALTER TABLE CUSTOMERS DROP EMAIL")  # undo the half-applied script by hand
    commit(repo, {"V3__email.sql": "ALTER TABLE CUSTOMERS ADD EMAIL VARCHAR(80);\n"})
    fix = client.post(b + "/repair", json={}).json()
    assert fix == {"removed_failed": ["3"], "accepted_checksums": []}
    _job(client, client.post(b + "/dry-run", json={"copy_mode": "full"}))
    assert client.get(b + "/dry-run").json()["copy"] == "full"
    _job(client, client.post(b + "/apply", json={}))
    assert client.get(b).json()["current"] == "3"

    # an applied script was edited: refused, until repair accepts the new checksum
    commit(repo, {"V1__customers.sql": "-- reviewed\nCREATE TABLE CUSTOMERS (ID INTEGER NOT NULL PRIMARY KEY, NAME VARCHAR(40));\n"})
    st = client.get(b).json()
    assert st["items"][0]["state"] == "changed" and st["errors"]
    assert client.post(b + "/repair", json={}).json()["accepted_checksums"] == ["1"]
    assert client.get(b).json()["errors"] == []

    # a script merged with an older version: late, applied with out-of-order
    commit(repo, {"V2.5__late.sql": "CREATE TABLE LATE_T (ID INTEGER);\n"})
    st = client.get(b).json()
    assert {i["version"]: i["state"] for i in st["items"]}["2.5"] == "late" and st["errors"]
    st = client.post(b + "/status", json={"out_of_order": True}).json()  # the configured source, with the flag
    assert st["pending"] == ["2.5"] and not st["errors"]
    client.put(b + "/source", json={"kind": "git", "url": str(repo), "ref": "main", "path": "db", "out_of_order": True})
    assert client.get(b).json()["pending"] == ["2.5"]
    _job(client, client.post(b + "/dry-run", json={}))
    _job(client, client.post(b + "/apply", json={}))
    assert client.get(b).json()["current"] == "3"

    # a tag: the scripts as they were
    git(repo, "tag", "v-old", git(repo, "rev-list", "--max-parents=0", "HEAD").strip())
    client.put(b + "/source", json={"kind": "git", "url": str(repo), "ref": "v-old", "path": "db"})
    st = client.get(b).json()
    assert st["source_ref"].startswith("v-old@") and {i["state"] for i in st["items"]} >= {"missing"}
    assert client.put(b + "/source", json={"kind": "git", "url": str(repo), "ref": "nope", "path": "db"}).status_code == 200
    assert "No branch, tag or commit" in client.get(b).json()["detail"]


@requires_server
def test_inline_scripts_baseline_and_lock(client, database):
    sid, dbid = database
    b = _base(sid, dbid)
    execute(client, sid, dbid, "CREATE TABLE LEGACY (ID INTEGER)")
    scripts = [{"name": "V1__legacy.sql", "sql": "CREATE TABLE LEGACY (ID INTEGER);"},
               {"name": "V2__more.sql", "sql": "ALTER TABLE LEGACY ADD NOTE VARCHAR(10);"},
               {"name": "V3__new.sql", "sql": "CREATE TABLE NEW_T (ID INTEGER);"}]
    assert client.post(b + "/baseline", json={"version": "2.0"}).json() == {"ok": True, "version": "2"}
    execute(client, sid, dbid, "ALTER TABLE LEGACY ADD NOTE VARCHAR(10)")
    st = client.post(b + "/status", json={"scripts": scripts, "source_ref": "ci@abc"}).json()
    assert [i["state"] for i in st["items"]] == ["baseline", "baseline", "pending"]
    assert st["pending"] == ["3"] and st["source_ref"] == "ci@abc"
    _job(client, client.post(b + "/dry-run", json={"scripts": scripts}))
    _job(client, client.post(b + "/apply", json={"scripts": scripts}))
    assert client.post(b + "/status", json={"scripts": scripts}).json()["current"] == "3"
    r = client.post(b + "/baseline", json={"version": "5"})
    assert r.status_code == 400 and "already" in r.json()["detail"]

    # one run per database: a second one waits for nobody
    from fbadmin.api.deps import get_state
    from fbadmin.connections import connect

    st_ = get_state()
    srv = st_.store.get(sid)
    path = st_.store.get_database(sid, dbid).path
    conn = lambda: connect(srv, path, PASSWORD)  # noqa: E731
    with M.lock(conn, "a", "first"):
        with pytest.raises(RuntimeError, match="in progress"):
            with M.lock(conn, "a", "second"):
                pass
        with pytest.raises(RuntimeError, match="elsewhere"):  # another process: the row lock
            with M.lock(conn, "b", "second"):
                pass
    with M.lock(conn, "b", "third"):
        pass


# ------------------------------------------------------------------ command-line tool
def _cli(*args, cwd=None):
    env = {**os.environ, "PYTHONPATH": str(BACKEND)}
    env.pop("FBADMIN_URL", None)
    return subprocess.run([sys.executable, "-m", "fbadmin", *args], capture_output=True, text=True, env=env,
                          cwd=cwd or BACKEND, timeout=300)


@requires_server
def test_cli_local(tmp_path):
    path = f"{DB_DIR}/fbadmin_test_mig_{uuid.uuid4().hex[:8]}.fdb"
    conn = ["--host", HOST, "--port", str(PORT), "-u", USER, "-p", PASSWORD]
    d = tmp_path / "migrations"
    d.mkdir()
    (d / "V1__t.sql").write_text("CREATE TABLE T (ID INTEGER NOT NULL PRIMARY KEY);\n")
    (d / "V2__rows.sql").write_text("INSERT INTO T VALUES (1);\n")
    try:
        r = _cli("sql", path, *conn, "--create", "-c", "SELECT 1 FROM RDB$DATABASE")
        assert r.returncode == 0, r.stderr
        r = _cli("migrate", "status", path, *conn, "--dir", str(d), "--check")
        assert r.returncode == 1 and "pending" in r.stdout, r.stderr
        r = _cli("migrate", "dry-run", path, *conn, "--dir", str(d), "--json")
        assert r.returncode == 0, r.stderr
        assert json.loads(r.stdout)["dry_run"]["ok"]
        r = _cli("migrate", "apply", path, *conn, "--dir", str(d), "--json")
        assert r.returncode == 0, r.stderr
        out = json.loads(r.stdout)
        assert out["applied"] == ["V1__t.sql", "V2__rows.sql"] and out["current"] == "2" and out["dry_run"]["ok"]
        assert _cli("migrate", "status", path, *conn, "--dir", str(d), "--check").returncode == 0
        (d / "V3__bad.sql").write_text("INSERT INTO T VALUES (2);\nINSERT INTO NOPE VALUES (3);\n")
        r = _cli("migrate", "apply", path, *conn, "--dir", str(d))
        assert r.returncode == 4 and "dry run failed" in r.stderr and "line 2" in r.stderr, r.stderr
        r = _cli("migrate", "apply", path, *conn, "--dir", str(d), "--no-dry-run")
        assert r.returncode == 4 and "V3__bad.sql failed at line 2" in r.stderr
        r = _cli("migrate", "status", path, *conn, "--dir", str(d))
        assert r.returncode == 4 and "failed" in r.stdout
        (d / "V3__bad.sql").write_text("INSERT INTO T VALUES (3);\n")
        _cli("sql", path, *conn, "-c", "DELETE FROM T WHERE ID = 2")
        assert _cli("migrate", "repair", path, *conn, "--dir", str(d)).returncode == 0
        r = _cli("migrate", "apply", path, *conn, "--dir", str(d), "--json")
        assert r.returncode == 0 and json.loads(r.stdout)["current"] == "3", r.stderr
        time.sleep(0.1)
    finally:
        _cli("sql", path, *conn, "-c", "SELECT 1 FROM RDB$DATABASE")  # (exists)
        import firebirdsql

        try:
            firebirdsql.connect(host=HOST, port=PORT, database=path, user=USER, password=PASSWORD).drop_database()
        except Exception:  # noqa: BLE001
            pass
