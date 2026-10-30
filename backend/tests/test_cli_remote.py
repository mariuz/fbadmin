"""Remote mode of the command-line tool: ``fbadmin --url … --token …`` against a running fbadmin server
(started here in server mode as a subprocess), with a live Firebird server behind it."""

from __future__ import annotations

import gzip
import http.cookiejar
import json
import os
import signal
import socket
import subprocess
import sys
import time
import urllib.request
import uuid

import pytest

from fbadmin.cli import remote

from .conftest import DB_DIR, HOST, PASSWORD, PORT, USER, requires_server
from .test_cli import BACKEND, drop, fb

ADMIN_PW = "remote-admin-password"


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Web:
    """A signed-in browser-like session on the fbadmin server (cookies + CSRF token)."""

    def __init__(self, base: str):
        self.base = base
        self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
        self.csrf = None

    def call(self, method: str, path: str, body=None):
        headers = {"Content-Type": "application/json"}
        if self.csrf:
            headers["X-CSRF-Token"] = self.csrf
        req = urllib.request.Request(self.base + path, data=json.dumps(body).encode() if body is not None else None,
                                     method=method, headers=headers)
        with self.opener.open(req, timeout=60) as r:
            raw = r.read()
        return json.loads(raw) if raw else None


@pytest.fixture(scope="module")
def web(tmp_path_factory):
    """fbadmin in server mode with one Firebird server (password saved) and one database."""
    ddir = tmp_path_factory.mktemp("remote")
    port = _free_port()
    env = {k: v for k, v in os.environ.items() if not k.startswith("FBADMIN_") or k.startswith("FBADMIN_TEST")}
    env.update(PYTHONPATH=str(BACKEND), FBADMIN_DATA_DIR=str(ddir), FBADMIN_AUTH_CONFIG=str(ddir / "auth.json"),
               FBADMIN_SERVER_MODE="1", FBADMIN_ADMIN_USERNAME="root", FBADMIN_ADMIN_PASSWORD=ADMIN_PW)
    proc = subprocess.Popen([sys.executable, "-m", "fbadmin", "--no-browser", "--host", "127.0.0.1", "--port",
                             str(port), "--log-level", "warning"], cwd=BACKEND, env=env,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    base = f"http://127.0.0.1:{port}"
    for _ in range(100):
        try:
            urllib.request.urlopen(base + "/api/health", timeout=1).read()
            break
        except OSError:
            time.sleep(0.2)
    else:
        proc.kill()
        raise RuntimeError("fbadmin did not start: " + proc.stdout.read().decode()[-2000:])
    w = Web(base)
    w.csrf = w.call("POST", "/api/auth/login", {"username": "root", "password": ADMIN_PW})["csrf_token"]
    srv = w.call("POST", "/api/servers", {"name": "Firebird", "host": HOST, "port": PORT, "user": USER,
                                          "password": PASSWORD, "save_password": True})
    path = f"{DB_DIR}/fbadmin_remote_{uuid.uuid4().hex[:8]}.fdb"
    db = w.call("POST", f"/api/servers/{srv['id']}/databases/create", {"name": "app", "path": path})
    w.call("POST", f"/api/servers/{srv['id']}/databases/{db['id']}/execute", {"sql": (
        "CREATE TABLE ITEMS (ID INTEGER NOT NULL PRIMARY KEY, NAME VARCHAR(40));"
        "INSERT INTO ITEMS VALUES (1, 'one'); INSERT INTO ITEMS VALUES (2, 'two'); COMMIT;")})
    rw = w.call("POST", "/api/access/me/tokens", {"name": "cli", "scope": "read-write", "expires_days": 1})["token"]
    ro = w.call("POST", "/api/access/me/tokens", {"name": "ro", "scope": "read-only", "expires_days": 1})["token"]
    yield {"url": base, "token": rw, "ro_token": ro, "server": srv, "db": db, "path": path, "web": w}
    proc.send_signal(signal.SIGTERM)
    try:
        proc.wait(10)
    except subprocess.TimeoutExpired:
        proc.kill()
    drop(path)


def rfb(web, *args, token: str | None = None, env: dict | None = None, **kw):
    """Run the CLI in remote mode (the Firebird password is only used by the fbadmin server)."""
    e = {"FBADMIN_URL": web["url"], "FBADMIN_TOKEN": token or web["token"], **(env or {})}
    r = fb(*args, env=e, **kw)
    return r


def ok(r) -> str:
    assert r.returncode == 0, f"exit {r.returncode}\nstdout: {r.stdout[-3000:]!r}\nstderr: {r.stderr.decode()[-3000:]}"
    return r.stdout.decode()


# ------------------------------------------------------------------ unit
def test_remote_options_work_before_and_after_the_command():
    from fbadmin.cli import build_parser

    p = build_parser()
    a = p.parse_args(["--url", "http://x", "check", "@S/D"])
    assert remote.url_from(a) == "http://x"
    a = p.parse_args(["check", "@S/D", "--url", "http://y", "--token-env", "T"])
    assert remote.url_from(a) == "http://y" and a.token_env == "T"
    a = p.parse_args(["--url", "http://x", "check", "@S/D", "--token-env", "T"])
    assert remote.url_from(a) == "http://x"  # the subcommand does not reset the top-level value


def test_unreachable_server_is_a_connect_error(tmp_path):
    r = fb("--url", f"http://127.0.0.1:{_free_port()}", "servers", env={"FBADMIN_TOKEN": "x"})
    assert r.returncode == 3
    assert b"Cannot reach fbadmin" in r.stderr


# ------------------------------------------------------------------ live
@requires_server
def test_servers_and_check(web):
    out = ok(rfb(web, "servers", "--json"))
    data = json.loads(out)
    assert data["url"] == web["url"]
    assert data["servers"][0]["name"] == "Firebird" and data["servers"][0]["password"] == "saved"
    assert data["servers"][0]["databases"][0]["name"] == "app"
    out = ok(rfb(web, "check", "@Firebird/app"))
    assert out.startswith("OK Firebird / app: Firebird ") and "ODS" in out
    out = ok(rfb(web, "check", "--server", "firebird", "--server-only"))
    assert out.startswith("OK Firebird: ") and ("-V" in out or "-T" in out)
    # a DSN is refused in remote mode: the fbadmin server's registry decides where to connect
    r = rfb(web, "check", f"{HOST}:{web['path']}")
    assert r.returncode == 2 and b"@SERVER/DATABASE" in r.stderr
    r = rfb(web, "check", "@Nope/app")
    assert r.returncode == 2 and b"No server 'Nope'" in r.stderr
    # local connection options make no sense remotely
    r = rfb(web, "check", "@Firebird/app", "--password-env", "X")
    assert r.returncode == 2 and b"--password-env is not available in remote mode" in r.stderr


@requires_server
def test_tokens_are_required_and_scoped(web):
    r = fb("--url", web["url"], "servers")
    assert r.returncode == 3 and b"Not signed in" in r.stderr and b"--token-env" in r.stderr
    r = rfb(web, "servers", token="fbat_wrong")
    assert r.returncode == 3 and b"Invalid, expired or revoked API token" in r.stderr
    # a read-only token can read but not write
    ok(rfb(web, "check", "@Firebird/app", token=web["ro_token"]))
    r = rfb(web, "sql", "@Firebird/app", "-c", "INSERT INTO ITEMS VALUES (9, 'x')", token=web["ro_token"])
    assert r.returncode == 3 and b"Not allowed" in r.stderr
    # --token-env names the variable
    ok(fb("--url", web["url"], "--token-env", "MY_TOKEN", "servers", env={"MY_TOKEN": web["token"]}))


@requires_server
def test_sql_and_extract(web):
    out = ok(rfb(web, "sql", "@Firebird/app", "-c", "SELECT ID, NAME FROM ITEMS ORDER BY ID"))
    assert out.splitlines() == ["ID\tNAME", "1\tone", "2\ttwo"]
    r = rfb(web, "sql", "@Firebird/app", "-c", "SELECT * FROM NO_SUCH_TABLE")
    assert r.returncode == 4 and b"line 1" in r.stderr
    r = rfb(web, "sql", "@Firebird/app", "-c", "SELECT 1 FROM RDB$DATABASE", "--single-transaction")
    assert r.returncode == 2
    ddl = ok(rfb(web, "extract", "@Firebird/app", "--no-header"))
    assert "CREATE TABLE ITEMS" in ddl
    r = rfb(web, "extract", "@Firebird/app", "--split", "x")
    assert r.returncode == 2 and b"--split is not available in remote mode" in r.stderr


@requires_server
def test_diff_against_a_script_with_data(web, tmp_path):
    schema = tmp_path / "schema.sql"
    schema.write_text("CREATE TABLE ITEMS (ID INTEGER NOT NULL PRIMARY KEY, NAME VARCHAR(40), PRICE INTEGER);\n")
    r = rfb(web, "diff", str(schema), "@Firebird/app", "--exit-code")
    assert r.returncode == 1, r.stderr
    assert "ALTER TABLE ITEMS ADD PRICE INTEGER" in r.stdout.decode()
    data = json.loads(ok(rfb(web, "diff", "@Firebird/app", "@Firebird/app", "--json")))
    assert data["differences"] == 0


@requires_server
def test_backup_download_and_restore_upload(web, tmp_path):
    fbk = tmp_path / "app.fbk.gz"
    data = json.loads(ok(rfb(web, "backup", "@Firebird/app", "-o", str(fbk), "--checksum", "--json")))
    assert data["file"] == str(fbk) and data["compress"] == "gzip" and fbk.is_file()
    assert gzip.open(fbk).read(4)  # a gzip file
    assert (tmp_path / "app.fbk.gz.sha256").read_text().startswith(data["sha256"])
    # restore it (decompressed on the fly) into a new database file, registered on the fbadmin server
    target = f"{DB_DIR}/fbadmin_remote_rst_{uuid.uuid4().hex[:8]}.fdb"
    try:
        out = ok(rfb(web, "restore", str(fbk), f"@Firebird:{target}", "--register-as", "restored"))
        assert "Restored" in out
        rows = ok(rfb(web, "sql", "@Firebird/restored", "-c", "SELECT COUNT(*) FROM ITEMS"))
        assert rows.splitlines()[1] == "2"
        # a registered database is only overwritten with --replace
        r = rfb(web, "restore", str(fbk), "@Firebird/restored")
        assert r.returncode == 2 and b"--replace" in r.stderr
        ok(rfb(web, "restore", str(fbk), "@Firebird/restored", "--replace", "-q"))
    finally:
        drop(target)
    # into the server's archive, and to a file on the Firebird server
    ok(rfb(web, "backup", "@Firebird/app", "--archive"))
    server_fbk = f"{DB_DIR}/fbadmin_remote_{uuid.uuid4().hex[:8]}.fbk"
    out = ok(rfb(web, "backup", "@Firebird/app", "--server-file", server_fbk))
    assert server_fbk in out
    r = rfb(web, "backup", "@Firebird/app", "-o", str(tmp_path / "x.fbk"), "--verify")
    assert r.returncode == 2 and b"--verify is not available in remote mode" in r.stderr


@requires_server
def test_ctrl_c_cancels_the_job_on_the_server(web):
    """Interrupting a remote backup to a server file cancels its job, and gbak stops on the server.

    The backup must still run when Ctrl+C arrives. On a fast runner it can finish first ("The job has
    already ended", which the CLI reports correctly); then the table grows and the test tries again."""
    ok(rfb(web, "sql", "@Firebird/app", "-c", "CREATE TABLE FILLER (ID INTEGER NOT NULL PRIMARY KEY, S VARCHAR(200));"))
    rows = 0
    for _attempt in range(3):
        more = 300000 if rows == 0 else rows  # double the table each time
        ok(rfb(web, "sql", "@Firebird/app", "-c", (
            f"EXECUTE BLOCK AS DECLARE I INT = {rows}; BEGIN WHILE (I < {rows + more}) DO BEGIN "
            "INSERT INTO FILLER VALUES (:I, UUID_TO_CHAR(GEN_UUID()) || UUID_TO_CHAR(GEN_UUID()) || "
            "UUID_TO_CHAR(GEN_UUID()) || UUID_TO_CHAR(GEN_UUID()) || UUID_TO_CHAR(GEN_UUID())); I = I + 1; END END")))
        rows += more
        fbk = f"{DB_DIR}/fbadmin_remote_cxl_{uuid.uuid4().hex[:8]}.fbk"
        e = {**os.environ, "PYTHONPATH": str(BACKEND), "FBADMIN_URL": web["url"], "FBADMIN_TOKEN": web["token"]}
        p = subprocess.Popen([sys.executable, "-m", "fbadmin", "backup", "@Firebird/app", "--server-file", fbk],
                             cwd=BACKEND, env=e, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        seen = b""
        end = time.time() + 120
        while b"(on the server)" not in seen and time.time() < end:  # the server's job log: the job has started
            chunk = p.stderr.read1(4096)
            if not chunk:
                break
            seen += chunk
        p.send_signal(signal.SIGINT)
        _, err = p.communicate(timeout=120)
        err = (seen + err).decode()
        assert "Cancelling the job on the server" in err, err[-2000:]
        if "already ended" not in err:
            break
    assert p.returncode == 130, err[-2000:]
    assert "was cancelled" in err


def test_migrate_remote(web, tmp_path):
    """fbadmin migrate --url: the scripts of a local directory, run by the fbadmin server (dry run first)."""
    d = tmp_path / "migrations"
    d.mkdir()
    (d / "V1__remote_t.sql").write_text("CREATE TABLE REMOTE_MIG (ID INTEGER NOT NULL PRIMARY KEY);\n")
    (d / "V2__remote_rows.sql").write_text("INSERT INTO REMOTE_MIG VALUES (1);\n")
    r = rfb(web, "migrate", "status", "@Firebird/app", "--dir", str(d), "--check")
    assert r.returncode == 1 and b"pending" in r.stdout
    r = rfb(web, "migrate", "apply", "@Firebird/app", "--dir", str(d), "--token-env", "X", env={"X": web["ro_token"]})
    assert r.returncode != 0  # a read-only token can't
    out = json.loads(ok(rfb(web, "migrate", "apply", "@Firebird/app", "--dir", str(d), "--json")))
    assert out["applied"] == ["V1__remote_t.sql", "V2__remote_rows.sql"] and out["current"] == "2"
    assert out["dry_run"]["ok"] and out["dry_run"]["source_ref"]
    ok(rfb(web, "migrate", "status", "@Firebird/app", "--dir", str(d), "--check"))
    (d / "V3__bad.sql").write_text("INSERT INTO NOPE VALUES (1);\n")
    r = rfb(web, "migrate", "apply", "@Firebird/app", "--dir", str(d))
    assert r.returncode == 4 and b"dry run failed" in r.stderr
    audit = web["web"].call("GET", "/api/admin/audit?q=migrations")["rows"]
    assert any("applied V1__remote_t.sql, V2__remote_rows.sql" in a["statement"] for a in audit), audit[:3]
