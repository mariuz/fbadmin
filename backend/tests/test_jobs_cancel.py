"""Cancelling long-running jobs (milestone 2.1): the job manager, and against a live server backups,
restores (streamed from fbadmin), data compare, applying a sync script and debugger runs."""

from __future__ import annotations

import threading
import time
import uuid

import pytest

from fbadmin import jobs
from fbadmin.driver_patches import _tag_service_attach

from .conftest import DB_DIR, HOST, PASSWORD, PORT, USER, requires_server


# ------------------------------------------------------------------ job manager
def _wait(mgr: jobs.JobManager, job: jobs.Job, timeout: float = 10) -> jobs.Job:
    end = time.time() + timeout
    while job.status == "running" and time.time() < end:
        time.sleep(0.01)
    return job


def test_cancel_stops_a_cancellable_job_at_its_next_check():
    mgr = jobs.JobManager()
    started = threading.Event()

    def fn(log):
        started.set()
        while True:
            jobs.check()
            time.sleep(0.01)

    job = mgr.start("t", "loop", "s", fn, cancellable=True)
    assert started.wait(5)
    assert job.summary()["cancellable"] is True
    mgr.cancel(job.id)
    _wait(mgr, job)
    assert job.status == "cancelled"
    assert job.error is None
    assert job.log[-1] == "Cancelled"
    with pytest.raises(ValueError, match="already ended"):
        mgr.cancel(job.id)


def test_jobs_that_do_not_check_cannot_be_cancelled():
    mgr = jobs.JobManager()
    gate = threading.Event()
    job = mgr.start("t", "plain", "s", lambda log: gate.wait(5))
    with pytest.raises(ValueError, match="cannot be cancelled"):
        mgr.cancel(job.id)
    gate.set()
    assert _wait(mgr, job).status == "finished"


def test_interrupt_hooks_run_on_cancel_and_their_message_is_logged():
    mgr = jobs.JobManager()
    unblock = threading.Event()
    calls = []

    def hook():
        calls.append(threading.current_thread().name)
        unblock.set()  # e.g. the blocked statement is cancelled on the server
        return "Cancelled 1 running statement(s)"

    def fn(log):
        with jobs.on_cancel(hook):
            unblock.wait(5)  # blocked in a server call that does not check
            jobs.check()

    job = mgr.start("t", "blocked", "s", fn, cancellable=True)
    time.sleep(0.05)
    mgr.cancel(job.id)
    _wait(mgr, job)
    assert job.status == "cancelled"
    assert calls and not calls[0].startswith("job-")  # ran in the cancelling thread
    assert "Cancelled 1 running statement(s)" in job.log


def test_hook_registered_after_cancel_runs_at_once_and_failures_are_logged():
    mgr = jobs.JobManager()
    go = threading.Event()

    def bad_hook():
        raise RuntimeError("no connection\nmore")

    def fn(log):
        go.wait(5)
        with jobs.on_cancel(bad_hook):
            jobs.check()

    job = mgr.start("t", "late", "s", fn, cancellable=True)
    mgr.cancel(job.id)
    go.set()
    _wait(mgr, job)
    assert job.status == "cancelled"
    assert "Could not interrupt the running operation: no connection" in job.log


def test_an_error_after_cancel_counts_as_cancelled():
    mgr = jobs.JobManager()
    go = threading.Event()

    def fn(log):
        go.wait(5)
        raise RuntimeError("connection shutdown")  # what a detached gbak reports

    job = mgr.start("t", "x", "s", fn, cancellable=True)
    mgr.cancel(job.id)
    go.set()
    assert _wait(mgr, job).status == "cancelled"


def test_service_tag_only_in_cancellable_jobs():
    assert jobs.service_tag() is None
    mgr = jobs.JobManager()
    seen = {}
    a = mgr.start("t", "a", "s", lambda log: seen.update(a=jobs.service_tag()), cancellable=True)
    b = mgr.start("t", "b", "s", lambda log: seen.update(b=jobs.service_tag()))
    _wait(mgr, a), _wait(mgr, b)
    assert seen == {"a": f"fbadmin-job-{a.id}", "b": None}


def test_service_attach_packet_gets_the_process_name():
    from firebirdsql.wireprotocol import Packer

    def packet(spb: bytes) -> bytes:
        p = Packer()
        p.pack_int(82)
        p.pack_int(0)
        p.pack_bytes(b"service_mgr")
        p.pack_bytes(spb)
        return p.get_buffer()

    spb = bytes([2, 2, 28, 6]) + b"SYSDBA" + bytes([58, 4, 120, 10, 0, 0])
    out = _tag_service_attach(packet(spb), "fbadmin-job-abc")
    assert out == packet(bytes([2, 2, 112, 15]) + b"fbadmin-job-abc" + spb[2:])
    assert _tag_service_attach(b"\x00\x00\x00\x13rest", "x") == b"\x00\x00\x00\x13rest"  # another op


# ------------------------------------------------------------------ live server
ROWS = 400_000  # about 80 MB: a backup takes long enough to be cancelled while it runs


def _make_db(rows: int):
    import firebirdsql

    path = f"{DB_DIR}/fbadmin_cancel_{uuid.uuid4().hex[:8]}.fdb"
    conn = firebirdsql.create_database(host=HOST, port=PORT, database=path, user=USER, password=PASSWORD,
                                       charset="UTF8", page_size=8192)
    cur = conn.cursor()
    cur.execute("CREATE TABLE BIG (ID INTEGER NOT NULL PRIMARY KEY, S VARCHAR(200))")
    conn.commit()
    cur.execute(f"EXECUTE BLOCK AS DECLARE I INT = 0; BEGIN WHILE (I < {rows}) DO BEGIN "
                "INSERT INTO BIG VALUES (:I, UUID_TO_CHAR(GEN_UUID()) || UUID_TO_CHAR(GEN_UUID()) || "
                "UUID_TO_CHAR(GEN_UUID()) || UUID_TO_CHAR(GEN_UUID()) || UUID_TO_CHAR(GEN_UUID())); "
                "I = I + 1; END END")
    conn.commit()
    conn.close()
    return path


def _drop_db(path: str) -> None:
    import firebirdsql

    try:
        firebirdsql.connect(host=HOST, port=PORT, database=path, user=USER, password=PASSWORD).drop_database()
    except Exception:  # noqa: BLE001
        pass


@pytest.fixture(scope="module")
def big_db():
    """A database with one large table (created once for this module, dropped afterwards)."""
    path = _make_db(ROWS)
    yield path
    _drop_db(path)


@pytest.fixture(scope="module")
def medium_db():
    """About 16 MB: its backup streams through the pure-Python driver (about 0.3 MB/s with wire
    encryption) in under a minute, yet restoring it takes long enough to be cancelled."""
    path = _make_db(80_000)
    yield path
    _drop_db(path)


def _register(client, server, path, name):
    r = client.post(f"/api/servers/{server['id']}/databases", json={"name": name, "path": path})
    assert r.status_code == 201, r.text
    return server["id"], r.json()["id"], path


@pytest.fixture
def big(client, server, big_db):
    return _register(client, server, big_db, "big")


@pytest.fixture
def medium(client, server, medium_db):
    return _register(client, server, medium_db, "medium")


def _job(client, job_id):
    return client.get(f"/api/jobs/{job_id}").json()


def _wait_job(client, job_id, timeout=180):
    end = time.time() + timeout
    while time.time() < end:
        j = _job(client, job_id)
        if j["status"] != "running":
            return j
        time.sleep(0.05)
    raise AssertionError("job did not end")


def _cancel_when(client, job_id, pred, timeout=60):
    """Cancel the job as soon as its log satisfies ``pred`` (returns False if it ended first)."""
    end = time.time() + timeout
    while time.time() < end:
        j = _job(client, job_id)
        if j["status"] != "running":
            return False
        if pred(j["log"]):
            r = client.post(f"/api/jobs/{job_id}/cancel")
            assert r.status_code == 200, r.text
            return True
        time.sleep(0.01)
    raise AssertionError("condition not reached")


def _tagged_attachments(path, job_id):
    """Attachments of the job's service action (tagged on Firebird 5+; without a process name on 3/4)."""
    import firebirdsql

    c = firebirdsql.connect(host=HOST, port=PORT, database=path, user=USER, password=PASSWORD)
    try:
        cur = c.cursor()
        cur.execute("SELECT COUNT(*) FROM MON$ATTACHMENTS WHERE (MON$REMOTE_PROCESS = ? OR MON$REMOTE_PROCESS IS NULL) "
                    "AND MON$SYSTEM_FLAG = 0 AND MON$ATTACHMENT_ID <> CURRENT_CONNECTION", [f"fbadmin-job-{job_id}"])
        return cur.fetchone()[0]
    finally:
        c.close()


def _restore_fails(client, sid, server_file):
    """The backup file is gone (gbak 4+ deletes its partial output when it is stopped)."""
    target = f"{DB_DIR}/fbadmin_cancel_chk_{uuid.uuid4().hex[:6]}.fdb"
    r = client.post(f"/api/servers/{sid}/maintenance/restore",
                    json={"source": "server", "backup_file": server_file, "database_path": target})
    j = _wait_job(client, r.json()["id"])
    if j["status"] != "failed":
        _drop_db(target)
    return j["status"] == "failed"


def _engine_major(client, sid, dbid):
    r = client.post(f"/api/servers/{sid}/databases/{dbid}/execute",
                    json={"sql": "SELECT RDB$GET_CONTEXT('SYSTEM', 'ENGINE_VERSION') FROM RDB$DATABASE"}).json()
    return int(r["results"][0]["rows"][0][0].split(".")[0])


@requires_server
@pytest.mark.parametrize("when", ["running", "at once"])
def test_cancel_backup_to_server_file(client, big, when):
    sid, dbid, path = big
    fbk = f"{DB_DIR}/fbadmin_cancel_{uuid.uuid4().hex[:8]}.fbk"
    r = client.post(f"/api/servers/{sid}/databases/{dbid}/maintenance/backup",
                    json={"target": "server", "backup_file": fbk})
    assert r.status_code == 202, r.text
    job = r.json()
    assert job["cancellable"] is True
    if when == "running":
        assert _cancel_when(client, job["id"], lambda log: any(ln.startswith("gbak:") for ln in log))
    else:
        assert client.post(f"/api/jobs/{job['id']}/cancel").status_code == 200
    j = _wait_job(client, job["id"])
    assert j["status"] == "cancelled", "\n".join(j["log"])
    assert "Backup completed" not in j["log"]
    time.sleep(1)
    assert _tagged_attachments(path, job["id"]) == 0  # gbak is not still running on the server
    if _engine_major(client, sid, dbid) >= 4:  # Firebird 3's gbak leaves the partial file behind
        assert _restore_fails(client, sid, fbk)
    if when == "running":  # cancelled at once, the backup is never started
        assert any(ln.startswith("Stopped the server operation") for ln in j["log"]), "\n".join(j["log"])


@requires_server
def test_cancel_backup_into_the_archive(client, big):
    """Cancelled while the data streams in: nothing is kept, and the server can back up again."""
    sid, dbid, path = big
    r = client.post(f"/api/servers/{sid}/databases/{dbid}/maintenance/backup", json={"target": "archive"})
    job = r.json()
    assert _cancel_when(client, job["id"], lambda log: any("MB received" in ln for ln in log), timeout=120)
    j = _wait_job(client, job["id"])
    assert j["status"] == "cancelled", "\n".join(j["log"])
    assert _archive_files(client, sid) == []  # the partial download (.part) is removed
    time.sleep(1)
    assert _tagged_attachments(path, job["id"]) == 0


@requires_server
def test_cancel_a_restore_streamed_from_fbadmin(client, medium):
    sid, dbid, path = medium
    j = _wait_job(client, client.post(f"/api/servers/{sid}/databases/{dbid}/maintenance/backup",
                                      json={"target": "archive"}).json()["id"])
    assert j["status"] == "finished", "\n".join(j["log"])
    rel = _archive_files(client, sid)[0]["path"]
    target = f"{DB_DIR}/fbadmin_cancel_rst_{uuid.uuid4().hex[:8]}.fdb"
    r = client.post(f"/api/servers/{sid}/maintenance/restore",
                    json={"source": "archive", "backup_file": rel, "database_path": target})
    job = r.json()
    assert job["cancellable"] is True
    assert _cancel_when(client, job["id"], lambda log: any(ln.startswith("Sent 10%") for ln in log))
    j = _wait_job(client, job["id"])
    assert j["status"] == "cancelled", "\n".join(j["log"])
    assert f"Dropped the incomplete database {target}" in j["log"], "\n".join(j["log"])
    import firebirdsql

    with pytest.raises(Exception):  # noqa: B017 - the half-restored database is gone
        firebirdsql.connect(host=HOST, port=PORT, database=target, user=USER, password=PASSWORD)


def _archive_files(client, sid):
    return client.get(f"/api/servers/{sid}/maintenance/archive").json()


@requires_server
def test_restore_from_a_server_file_is_not_cancellable(client, big):
    sid, _dbid, _path = big
    r = client.post(f"/api/servers/{sid}/maintenance/restore",
                    json={"source": "server", "backup_file": f"{DB_DIR}/missing_{uuid.uuid4().hex}.fbk",
                          "database_path": f"{DB_DIR}/never_{uuid.uuid4().hex}.fdb"})
    job = r.json()
    assert job["cancellable"] is False
    _wait_job(client, job["id"])


@requires_server
def test_cancel_data_compare_and_apply(client, server, big):
    sid, src, _path = big
    path = f"{DB_DIR}/fbadmin_cancel_tgt_{uuid.uuid4().hex[:8]}.fdb"
    r = client.post(f"/api/servers/{sid}/databases/create", json={"name": "tgt", "path": path})
    tgt = r.json()["id"]
    try:
        r = client.post(f"/api/servers/{sid}/databases/{tgt}/execute",
                        json={"sql": "CREATE TABLE BIG (ID INTEGER NOT NULL PRIMARY KEY, S VARCHAR(200))"})
        assert r.json()["error"] is None
        r = client.post("/api/datacompare/compare", json={"source": {"server_id": sid, "db_id": src},
                                                          "target": {"server_id": sid, "db_id": tgt},
                                                          "tables": [{"name": "BIG"}]})
        job = r.json()
        assert job["cancellable"] is True
        assert _cancel_when(client, job["id"], lambda log: any("comparing by" in ln for ln in log))
        j = _wait_job(client, job["id"])
        assert j["status"] == "cancelled", "\n".join(j["log"])
        res = client.get(f"/api/datacompare/jobs/{job['id']}").json()
        assert res["status"] == "cancelled"
        assert res["tables"][0]["status"] == "cancelled"
        assert client.get(f"/api/datacompare/jobs/{job['id']}/script").status_code == 400
    finally:
        client.post(f"/api/servers/{sid}/databases/{tgt}/drop")


@requires_server
def test_cancel_debugger_run(client, database):
    sid, dbid = database
    base = f"/api/servers/{sid}/databases/{dbid}"
    r = client.post(base + "/execute", json={"sql": (
        "CREATE PROCEDURE SPIN RETURNS (N BIGINT) AS DECLARE I BIGINT = 0; BEGIN "
        "WHILE (I < 300000000) DO I = I + 1; N = I; SUSPEND; END")})  # about a minute if not cancelled
    assert r.json()["error"] is None, r.json()["error"]
    run_id = uuid.uuid4().hex
    out = {}

    def call():
        out["r"] = client.post(base + "/debug/run", json={"otype": "procedures", "name": "SPIN", "run_id": run_id})

    t = threading.Thread(target=call)
    t.start()
    end = time.time() + 30
    cancelled = False
    while time.time() < end and not cancelled:  # until the run has registered
        time.sleep(0.1)
        cancelled = client.post(base + "/debug/cancel", json={"run_id": run_id}).json()["cancelled"]
    assert cancelled
    t.join(120)
    assert out["r"].status_code == 409, out["r"].text
    assert out["r"].json()["code"] == "cancelled"
    # an unknown or finished run: nothing to cancel
    assert client.post(base + "/debug/cancel", json={"run_id": run_id}).json() == {"cancelled": False}
