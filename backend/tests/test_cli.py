"""The command-line tool (``python -m fbadmin <subcommand>``), run as a subprocess against a live server."""

from __future__ import annotations

import gzip
import hashlib
import json
import os
import re
import socket
import subprocess
import sys
import time
import urllib.request
import uuid
from pathlib import Path

import firebirdsql
import pytest
from firebirdsql import services

from .conftest import DB_DIR, HOST, PASSWORD, PORT, USER, requires_server

BACKEND = Path(__file__).resolve().parents[1]

SCHEMA = """
CREATE DOMAIN D_NAME AS VARCHAR(40) NOT NULL;
CREATE SEQUENCE SEQ_ITEMS;
CREATE EXCEPTION E_BAD 'bad value';
CREATE TABLE PARENT (ID INTEGER PRIMARY KEY, NAME D_NAME);
CREATE TABLE ITEMS (ID INTEGER PRIMARY KEY, PARENT_ID INTEGER REFERENCES PARENT (ID), NAME VARCHAR(50),
                    NOTE BLOB SUB_TYPE TEXT, AMOUNT NUMERIC(12, 2) DEFAULT 0 CHECK (AMOUNT >= 0));
CREATE INDEX IX_ITEMS_NAME ON ITEMS (NAME);
CREATE VIEW V_ITEMS AS SELECT I.ID, I.NAME, P.NAME AS PARENT_NAME FROM ITEMS I JOIN PARENT P ON P.ID = I.PARENT_ID;
SET TERM ^ ;
CREATE PROCEDURE P_COUNT RETURNS (N INTEGER) AS BEGIN SELECT COUNT(*) FROM ITEMS INTO :N; SUSPEND; END^
CREATE TRIGGER ITEMS_BI FOR ITEMS BEFORE INSERT AS BEGIN IF (NEW.ID IS NULL) THEN NEW.ID = NEXT VALUE FOR SEQ_ITEMS; END^
SET TERM ; ^
COMMENT ON TABLE ITEMS IS 'the items';
COMMIT;
INSERT INTO PARENT VALUES (1, 'one');
INSERT INTO PARENT VALUES (2, 'two');
EXECUTE BLOCK AS DECLARE i INTEGER = 1; BEGIN
  WHILE (i <= 500) DO BEGIN
    INSERT INTO ITEMS (ID, PARENT_ID, NAME, NOTE, AMOUNT) VALUES (:i, 1 + MOD(:i, 2), 'item ' || :i, 'note ' || :i, :i);
    i = i + 1;
  END
END;
COMMIT;
"""


# ------------------------------------------------------------------ helpers
def fb(*args: str, env: dict | None = None, input: bytes | None = None, timeout: int = 300, cwd=None):
    e = {**os.environ, "PYTHONPATH": str(BACKEND), "ISC_PASSWORD": PASSWORD, "ISC_USER": USER}
    e.pop("FBADMIN_SERVERS_FILE", None)
    e.update(env or {})
    return subprocess.run([sys.executable, "-m", "fbadmin", *args], cwd=cwd or BACKEND, env=e, input=input,
                          capture_output=True, timeout=timeout)


def ok(r) -> str:
    assert r.returncode == 0, f"exit {r.returncode}\nstdout: {r.stdout[-3000:]!r}\nstderr: {r.stderr.decode()[-3000:]}"
    return r.stdout.decode()


def dsn(path: str) -> str:
    return f"{HOST}/{PORT}:{path}"


def new_path(tag: str) -> str:
    return f"{DB_DIR}/fbadmin_cli_{tag}_{uuid.uuid4().hex[:8]}.fdb"


def drop(path: str) -> None:
    try:
        firebirdsql.connect(host=HOST, port=PORT, user=USER, password=PASSWORD, database=path).drop_database()
    except Exception:  # noqa: BLE001
        pass


def query(path: str, sql: str):
    conn = firebirdsql.connect(host=HOST, port=PORT, user=USER, password=PASSWORD, database=path)
    try:
        cur = conn.cursor()
        cur.execute(sql)
        return cur.fetchall()
    finally:
        conn.close()


def engine_major() -> int:
    svc = services.connect(host=HOST, port=PORT, user=USER, password=PASSWORD)
    try:
        return int(re.search(r"-[VTB](\d+)\.", svc.getServerVersion()).group(1))
    finally:
        svc.close()


def service_major() -> int:
    """The Firebird version whose service options the CLI may use (ops.engine_major): the server's,
    capped at the client library's when the native driver is in use."""
    from fbadmin import drivers

    major = engine_major()
    if drivers.effective()[0] == "native":
        from .drivers_util import client_major

        major = min(major, client_major() or major)
    return major


def counts(path: str):
    return query(path, "SELECT COUNT(*), SUM(ID), (SELECT COUNT(*) FROM PARENT) FROM ITEMS")[0]


@pytest.fixture
def paths():
    made: list[str] = []

    def make(tag: str) -> str:
        p = new_path(tag)
        made.append(p)
        return p

    yield make
    for p in made:
        drop(p)


@pytest.fixture
def srcdb(paths, tmp_path):
    path = paths("src")
    script = tmp_path / "schema.sql"
    script.write_text(SCHEMA)
    ok(fb("sql", "--create", dsn(path), "-f", str(script), "-q"))
    return path


pytestmark = requires_server


# ------------------------------------------------------------------ backup / restore
@pytest.mark.parametrize("compress", ["none", "gzip"])
def test_backup_restore_local_roundtrip(srcdb, paths, tmp_path, compress):
    out = tmp_path / ("b.fbk.gz" if compress == "gzip" else "b.fbk")
    res = json.loads(ok(fb("backup", dsn(srcdb), "-o", str(out), "--checksum", "--json")))
    assert res["ok"] and res["compress"] == compress and res["file"] == str(out)
    data = out.read_bytes()
    assert (data[:2] == b"\x1f\x8b") == (compress == "gzip")
    if compress == "gzip":
        assert len(gzip.decompress(data)) == res["bytes"]
    side = Path(str(out) + ".sha256").read_text().split()
    assert side == [hashlib.sha256(data).hexdigest(), out.name]
    target = paths("restored")
    ok(fb("restore", str(out), dsn(target), "--page-size", "8192", "--buffers", "500"))
    assert counts(target) == counts(srcdb)
    assert query(target, "SELECT MON$PAGE_SIZE FROM MON$DATABASE")[0][0] == 8192


def test_restore_needs_replace(srcdb, tmp_path):
    out = tmp_path / "b.fbk"
    ok(fb("backup", dsn(srcdb), "-o", str(out), "-q"))
    r = fb("restore", str(out), dsn(srcdb))
    assert r.returncode == 4
    assert b"--replace" in r.stderr and b"Traceback" not in r.stderr
    ok(fb("restore", str(out), dsn(srcdb), "--replace", "-q"))
    assert counts(srcdb)[0] == 500


def test_backup_pipe_to_restore(srcdb, paths):
    target = paths("piped")
    b = fb("backup", dsn(srcdb), "-o", "-", "--compress", "gzip", "-q")
    assert b.returncode == 0 and b.stdout[:2] == b"\x1f\x8b"
    ok(fb("restore", "-", dsn(target), input=b.stdout))
    assert counts(target) == counts(srcdb)


def test_backup_server_file_verify_and_restore(srcdb, paths):
    bfile = new_path("srvfile").replace(".fdb", ".fbk")
    res = json.loads(ok(fb("backup", dsn(srcdb), "--server-file", bfile, "--verify", "--json")))
    assert res["server_file"] == bfile and res["verify"]["ok"] and res["verify"]["tables"] == 3  # 2 tables + 1 view
    assert not query(srcdb, "SELECT 1 FROM MON$ATTACHMENTS WHERE MON$ATTACHMENT_NAME CONTAINING 'fbadmin_verify'")
    target = paths("fromsrv")
    ok(fb("restore", "--server-file", bfile, dsn(target), "--deactivate-indexes"))
    assert counts(target) == counts(srcdb)
    # a database restored with --deactivate-indexes has its user indices inactive
    inactive = query(target, "SELECT COUNT(*) FROM RDB$INDICES WHERE RDB$INDEX_NAME = 'IX_ITEMS_NAME' "
                             "AND RDB$INDEX_INACTIVE = 1")[0][0]
    assert inactive == 1


def test_backup_dir_retention_and_verify(srcdb, tmp_path):
    d = tmp_path / "backups"
    d.mkdir()
    (d / "other_20200101-000000.fbk").write_bytes(b"not ours")  # another database: never touched
    for _ in range(3):
        res = json.loads(ok(fb("backup", dsn(srcdb), "--dir", str(d), "--name", "nightly", "--compress", "gzip",
                               "--keep", "2", "--verify", "--checksum", "--json")))
        assert res["verify"]["ok"]
        time.sleep(1.1)  # distinct timestamps
    names = sorted(p.name for p in d.iterdir())
    ours = [n for n in names if n.startswith("nightly_") and n.endswith(".fbk.gz")]
    assert len(ours) == 2
    assert len([n for n in names if n.endswith(".sha256")]) == 2
    assert "other_20200101-000000.fbk" in names
    assert res["removed"] and not Path(res["removed"][0]).exists()


def test_backup_options(srcdb, paths, tmp_path):
    out = tmp_path / "meta.fbk"
    ok(fb("backup", dsn(srcdb), "-o", str(out), "--metadata-only", "--no-gc", "-q"))
    target = paths("meta")
    ok(fb("restore", str(out), dsn(target), "-q"))
    assert counts(target)[0] == 0
    out2 = tmp_path / "skip.fbk"
    ok(fb("backup", dsn(srcdb), "-o", str(out2), "--skip-table", "ITEMS", "-q"))
    target2 = paths("skip")
    ok(fb("restore", str(out2), dsn(target2), "-q"))
    assert counts(target2)[0] == 0 and counts(target2)[2] == 2
    r = fb("backup", dsn(srcdb), "-o", str(tmp_path / "p.fbk"), "--parallel", "2")
    # parallel workers need Firebird 5 on the server and, with the native driver, in the client
    # library too (an older libfbclient refuses the option): fbadmin says so instead of failing
    if service_major() >= 5:
        ok(r)
    else:
        assert r.returncode == 4 and b"Firebird 5" in r.stderr


def test_backup_usage_errors(srcdb, tmp_path):
    r = fb("backup", dsn(srcdb))
    assert r.returncode == 2 and b"destination" in r.stderr
    r = fb("backup", dsn(srcdb), "-o", str(tmp_path / "x.fbk"), "--keep", "3")
    assert r.returncode == 2 and b"--dir" in r.stderr
    r = fb("restore", str(tmp_path / "missing.fbk"), dsn(srcdb))
    assert r.returncode == 2 and b"No such backup file" in r.stderr


# ------------------------------------------------------------------ errors
def test_bad_password_and_unreachable(srcdb, tmp_path):
    r = fb("backup", dsn(srcdb), "-o", str(tmp_path / "x.fbk"), "--password-env", "MY_PW",
           env={"MY_PW": "wrong-secret-pw"})
    assert r.returncode == 3
    err = r.stderr.decode()
    assert "password" in err.lower() and "Traceback" not in err and "wrong-secret-pw" not in err
    assert not (tmp_path / "x.fbk").exists() and not (tmp_path / "x.fbk.part").exists()
    r = fb("extract", dsn(srcdb), "-p", "wrong-secret-pw", "--json")
    assert r.returncode == 3
    doc = json.loads(r.stdout)
    assert doc["ok"] is False and doc["exit_code"] == 3 and "wrong-secret-pw" not in r.stdout.decode()
    r = fb("check", dsn(srcdb), "-p", "wrong")
    assert r.returncode == 1
    r = fb("sql", f"{HOST}/1:{srcdb}", "-c", "SELECT 1 FROM RDB$DATABASE")
    assert r.returncode == 3 and b"Traceback" not in r.stderr
    r = fb("sql", dsn(srcdb), "-c", "SELECT 1 FROM RDB$DATABASE", env={"ISC_PASSWORD": ""})
    assert r.returncode == 2 and b"No password" in r.stderr
    r = fb("backup", dsn(srcdb + ".missing"), "-o", str(tmp_path / "y.fbk"))
    assert r.returncode == 4 and b"Traceback" not in r.stderr
    r = fb("backup", dsn(srcdb + ".missing"), "-o", str(tmp_path / "y.fbk"), "--debug")
    assert r.returncode == 4 and b"Traceback" in r.stderr


# ------------------------------------------------------------------ sql / check
def test_sql_runner(srcdb, tmp_path):
    out = ok(fb("sql", dsn(srcdb), "-c", "SELECT COUNT(*) AS N FROM ITEMS; SELECT N FROM P_COUNT;"))
    assert out.splitlines() == ["N", "500", "N", "500"]
    script = tmp_path / "bad.sql"
    script.write_text("INSERT INTO PARENT VALUES (3, 'three');\nINSERT INTO NOSUCH VALUES (1);\n"
                      "INSERT INTO PARENT VALUES (4, 'four');\n")
    r = fb("sql", dsn(srcdb), "-f", str(script))
    assert r.returncode == 4
    assert f"{script}, line 2".encode() in r.stderr and b"NOSUCH" in r.stderr and b"Traceback" not in r.stderr
    # autocommit (default): the first statement stays, the third never ran
    assert [x[0] for x in query(srcdb, "SELECT ID FROM PARENT ORDER BY ID")] == [1, 2, 3]
    script.write_text("INSERT INTO PARENT VALUES (5, 'five');\nINSERT INTO NOSUCH VALUES (1);\n")
    r = fb("sql", dsn(srcdb), "-f", str(script), "--single-transaction", "--json")
    assert r.returncode == 4
    doc = json.loads(r.stdout)
    assert doc["error_at"]["line"] == 2 and doc["exit_code"] == 4
    assert [x[0] for x in query(srcdb, "SELECT ID FROM PARENT ORDER BY ID")] == [1, 2, 3]
    r = fb("sql", dsn(srcdb), input=b"SELECT 42 FROM RDB$DATABASE;")
    assert ok(r).strip().splitlines()[-1] == "42"


def test_check(srcdb):
    doc = json.loads(ok(fb("check", dsn(srcdb), "--json")))
    assert doc["ok"] and doc["page_size"] == 16384 and doc["dialect"] == 3
    assert int(doc["engine_version"].split(".")[0]) == engine_major()
    assert re.match(r"^\d+\.\d+$", doc["ods"])
    doc = json.loads(ok(fb("check", "--server-only", "--host", HOST, "--port", str(PORT), "--json")))
    assert doc["engine_major"] == engine_major()
    assert fb("check", f"{HOST}/1:{srcdb}").returncode == 1


# ------------------------------------------------------------------ extract
def test_extract_matches_web_api(srcdb, client, server, tmp_path):
    r = client.post(f"/api/servers/{server['id']}/databases", json={"name": "src", "path": srcdb})
    assert r.status_code == 201, r.text
    api = client.get(f"/api/servers/{server['id']}/databases/{r.json()['id']}/schema/extract").json()["sql"]
    cli = ok(fb("extract", dsn(srcdb)))
    assert cli == api
    assert "CREATE TABLE ITEMS" in cli and "TRIGGER ITEMS_BI" in cli
    out = tmp_path / "ddl.sql"
    ok(fb("extract", dsn(srcdb), "-o", str(out), "--no-grants", "--no-comments"))
    text = out.read_text()
    assert "COMMENT ON" not in text and "GRANT " not in text
    only = ok(fb("extract", dsn(srcdb), "--types", "tables,indices", "--no-header"))
    assert "CREATE TABLE ITEMS" in only and "IX_ITEMS_NAME" in only
    assert "CREATE VIEW" not in only and "P_COUNT" not in only and "SEQ_ITEMS" not in only
    assert fb("extract", dsn(srcdb), "--types", "tables,bogus").returncode == 2


def test_extract_split(srcdb, tmp_path):
    d = tmp_path / "split"
    ok(fb("extract", dsn(srcdb), "--split", str(d)))
    files = {str(p.relative_to(d)).replace("\\", "/") for p in d.rglob("*.sql")}
    base = "PUBLIC/" if any(f.startswith("PUBLIC/") for f in files) else ""
    for f in ("tables/ITEMS.sql", "tables/PARENT.sql", "views/V_ITEMS.sql", "procedures/P_COUNT.sql",
              "triggers/ITEMS_BI.sql", "sequences/SEQ_ITEMS.sql", "domains/D_NAME.sql", "indices/IX_ITEMS_NAME.sql"):
        assert base + f in files, files
    assert "CREATE TABLE ITEMS" in (d / base / "tables" / "ITEMS.sql").read_text()


# ------------------------------------------------------------------ diff
def test_diff_databases(srcdb, paths, tmp_path):
    bk = tmp_path / "b.fbk"
    ok(fb("backup", dsn(srcdb), "-o", str(bk), "-q"))
    clone = paths("clone")
    ok(fb("restore", str(bk), dsn(clone), "-q"))
    r = fb("diff", dsn(srcdb), dsn(clone), "--exit-code")
    assert r.returncode == 0, r.stderr
    assert b"No differences" in r.stderr
    ok(fb("sql", dsn(clone), "-q", "-c",
          "ALTER TABLE ITEMS ADD EXTRA INTEGER; DROP VIEW V_ITEMS; CREATE TABLE JUNK (A INTEGER);"))
    script = tmp_path / "migrate.sql"
    r = fb("diff", dsn(srcdb), dsn(clone), "--exit-code", "-o", str(script))
    assert r.returncode == 1, r.stderr
    text = script.read_text()
    assert "DROP TABLE JUNK" in text and "CREATE VIEW V_ITEMS" in text and "DROP EXTRA" in text
    ok(fb("sql", dsn(clone), "-f", str(script), "-q"))
    doc = json.loads(ok(fb("diff", dsn(srcdb), dsn(clone), "--exit-code", "--json")))
    assert doc["differences"] == 0 and doc["schema"]["items"] == []


def test_diff_ddl_file_and_data(srcdb, paths, tmp_path):
    ddl = tmp_path / "schema.sql"
    ok(fb("extract", dsn(srcdb), "-o", str(ddl)))
    r = fb("diff", str(ddl), dsn(srcdb), "--exit-code")
    assert r.returncode == 0, r.stderr.decode()
    empty = paths("empty")
    ok(fb("sql", "--create", dsn(empty), "-c", "CREATE TABLE PARENT (ID INTEGER PRIMARY KEY)", "-q"))
    r = fb("diff", str(ddl), dsn(empty), "--exit-code", "--json")
    assert r.returncode == 1
    doc = json.loads(r.stdout)
    assert doc["schema"]["summary"]["create"] > 0 and doc["schema"]["summary"]["alter"] == 1
    script = tmp_path / "up.sql"
    script.write_text(doc["script"])
    ok(fb("sql", dsn(empty), "-f", str(script), "-q"))
    assert fb("diff", str(ddl), dsn(empty), "--exit-code", "-q").returncode == 0
    # data: the schemas now match, the rows do not
    r = fb("diff", dsn(srcdb), dsn(empty), "--data", "parent", "items", "--no-schema", "--exit-code",
           "-o", str(tmp_path / "data.sql"))
    assert r.returncode == 1, r.stderr.decode()
    assert b"502 row(s) to insert" in r.stderr
    ok(fb("sql", dsn(empty), "-f", str(tmp_path / "data.sql"), "-q"))
    assert counts(empty) == counts(srcdb)
    assert fb("diff", dsn(srcdb), dsn(empty), "--data", "--exit-code", "-q").returncode == 0


# ------------------------------------------------------------------ registry
def _write_servers(ddir: Path, password, dbpath: str) -> None:
    ddir.mkdir(parents=True, exist_ok=True)
    (ddir / "servers.json").write_text(json.dumps({"servers": [{
        "id": "abc123", "name": "Test Server", "host": HOST, "port": PORT, "user": USER, "password": password,
        "save_password": True, "databases": [{"id": "db1", "name": "main", "path": dbpath},
                                             {"id": "db2", "name": "other", "path": dbpath + ".x"}]}]}))


def test_registry_plain_and_encrypted(srcdb, tmp_path):
    ddir = tmp_path / "data"
    _write_servers(ddir, PASSWORD, srcdb)
    env = {"ISC_PASSWORD": "", "FBADMIN_DATA_DIR": str(ddir)}
    doc = json.loads(ok(fb("servers", "--json", env=env)))
    assert doc["servers"][0]["password"] == "saved" and "masterkey" not in json.dumps(doc)
    assert ok(fb("check", "@Test Server/main", env=env)).startswith("OK")
    assert ok(fb("check", "--server", "test server", "--database", "db1", env=env)).startswith("OK")
    r = fb("check", "--server", "Test Server", env=env)  # no database: the server only
    assert r.returncode == 0 and b"Firebird" in r.stdout
    r = fb("extract", "--server", "Test Server", env=env)  # two databases: must choose
    assert r.returncode == 2 and b"--database" in r.stderr
    r = fb("extract", "@nope/main", env=env)
    assert r.returncode == 2 and b"Test Server" in r.stderr
    # saved password encrypted with the master password (desktop vault)
    from fbadmin.security.crypto import encrypt, vault_create

    record, key = vault_create("my master")
    (ddir / "master_key.json").write_text(json.dumps(record))
    _write_servers(ddir, encrypt(key, PASSWORD, "abc123"), srcdb)
    assert json.loads(ok(fb("servers", "--json", env=env)))["servers"][0]["password"] == "encrypted"
    r = fb("check", "@Test Server/main", env=env)
    assert r.returncode == 2 and b"FBADMIN_MASTER_PASSWORD" in r.stderr
    r = fb("check", "@Test Server/main", env={**env, "FBADMIN_MASTER_PASSWORD": "wrong"})
    assert r.returncode == 2 and b"Wrong fbadmin master password" in r.stderr
    ok(fb("check", "@Test Server/main", env={**env, "FBADMIN_MASTER_PASSWORD": "my master"}))
    ok(fb("check", "@Test Server/main", "--master-password-env", "MP", env={**env, "MP": "my master"}))
    # an explicit password wins over the saved one
    ok(fb("check", "@Test Server/main", "--password-env", "PW", env={**env, "PW": PASSWORD}))


def test_registry_servers_file(srcdb, tmp_path):
    cfg = tmp_path / "servers.yaml"
    cfg.write_text(f"servers:\n  - name: Prod DB\n    host: {HOST}\n    port: {PORT}\n    user: {USER}\n"
                   f"    password_env: PROD_PW\n    databases:\n      - {{name: app, path: {srcdb}}}\n")
    env = {"ISC_PASSWORD": "", "FBADMIN_DATA_DIR": str(tmp_path / "empty"), "FBADMIN_SERVERS_FILE": str(cfg)}
    r = fb("check", "@Prod DB/app", env=env)
    assert r.returncode == 2 and b"PROD_PW" in r.stderr
    out = ok(fb("check", "@Prod DB", env={**env, "PROD_PW": PASSWORD}))  # the only database
    assert out.startswith("OK Prod DB / app")


# ------------------------------------------------------------------ the web server still starts
def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.mark.parametrize("args", [[], ["serve"]])
def test_server_starts_with_old_flags(tmp_path, args):
    port = _free_port()
    env = {**os.environ, "PYTHONPATH": str(BACKEND), "FBADMIN_DATA_DIR": str(tmp_path)}
    env.pop("FBADMIN_SERVERS_FILE", None)
    proc = subprocess.Popen([sys.executable, "-m", "fbadmin", *args, "--no-browser", "--host", "127.0.0.1",
                             "--port", str(port), "--log-level", "warning", "--log-format", "json"],
                            cwd=BACKEND, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    try:
        deadline = time.time() + 30
        while True:
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/health", timeout=2) as resp:
                    assert resp.status == 200
                    break
            except OSError:
                if time.time() > deadline or proc.poll() is not None:
                    raise AssertionError(proc.stdout.read().decode() if proc.poll() is not None else "timeout") from None
                time.sleep(0.3)
    finally:
        proc.terminate()
        proc.wait(timeout=15)


def test_help_and_version():
    r = fb("--help")
    assert r.returncode == 0 and b"--no-browser" in r.stdout and b"backup" in r.stdout
    r = fb("--version")
    assert r.returncode == 0 and r.stdout.startswith(b"fbadmin ")
    for cmd in ("backup", "restore", "extract", "diff", "sql", "check", "servers"):
        r = fb(cmd, "--help")
        assert r.returncode == 0 and b"exit codes" in r.stdout, cmd


def test_zstd_needs_python_314(srcdb, paths, tmp_path):
    out = tmp_path / "b.fbk.zst"
    r = fb("backup", dsn(srcdb), "-o", str(out))
    if sys.version_info < (3, 14):
        assert r.returncode == 2 and b"Python 3.14" in r.stderr
        return
    ok(r)
    assert out.read_bytes()[:4] == b"\x28\xb5\x2f\xfd"
    target = paths("zstd")
    ok(fb("restore", str(out), dsn(target), "-q"))
    assert counts(target) == counts(srcdb)


def test_extract_schemas_fb6(srcdb, client, server, tmp_path):
    if engine_major() < 6:
        pytest.skip("schemas need Firebird 6")
    ok(fb("sql", dsn(srcdb), "-q", "-c",
          "CREATE SCHEMA SALES; COMMIT; CREATE TABLE SALES.ORDERS (ID INTEGER PRIMARY KEY, NOTE VARCHAR(10));"))
    r = client.post(f"/api/servers/{server['id']}/databases", json={"name": "src", "path": srcdb})
    api = client.get(f"/api/servers/{server['id']}/databases/{r.json()['id']}/schema/extract").json()["sql"]
    assert ok(fb("extract", dsn(srcdb))) == api
    sales = ok(fb("extract", dsn(srcdb), "--schema", "SALES", "--no-header"))
    assert "ORDERS" in sales and "ITEMS" not in sales
    d = tmp_path / "split"
    ok(fb("extract", dsn(srcdb), "--split", str(d)))
    assert (d / "SALES" / "tables" / "ORDERS.sql").is_file() and (d / "PUBLIC" / "tables" / "ITEMS.sql").is_file()
