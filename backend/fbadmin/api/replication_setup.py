"""Replication setup wizard (roadmap 2.2): plan, replication.conf, copy, replica mode, check.

See :mod:`fbadmin.monitoring.repl_setup` for the order of the steps and why.
"""

from __future__ import annotations

import time
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, field_validator

from ..connections import PasswordRequired, connect
from ..maintenance import ops
from ..monitoring import repl_setup as R
from ..monitoring import replication
from ..security.access import ADMIN, READ, require_level
from ..security.core import require_admin
from ..store import DatabaseEntry
from .deps import get_state

DB = "/api/servers/{server_id}/databases/{db_id}/replication"
router = APIRouter(tags=["replication"])

_RUNS: dict[str, dict[str, Any]] = {}       # job id -> result
_TOKENS: dict[str, dict[str, Any]] = {}     # check token -> sent facts
_KEEP = 200


def _remember(d: dict[str, Any], key: str, value: dict[str, Any]) -> None:
    d[key] = value
    while len(d) > _KEEP:  # oldest first (dicts keep insertion order)
        d.pop(next(iter(d)))


def _password(server_id: str) -> str:
    pw = get_state().store.password_for(server_id)
    if not pw:
        raise PasswordRequired(server_id)
    return pw


def _major(server_id: str) -> int:
    with get_state().manager.service(server_id) as svc:
        return ops.engine_major(svc)


def _audit(what: str, ok: bool = True, error: str | None = None, server_id: str | None = None,
           db_id: str | None = None) -> None:
    try:
        get_state().security.audit.record(server_id=server_id, db_id=db_id, statement=f"-- replication setup: {what}",
                                          success=ok, error=error, cat="ACTION")
    except Exception:  # noqa: BLE001 - auditing never breaks the request
        pass


def _plan(server_id: str, db_id: str, body: R.SetupIn) -> tuple[dict[str, Any], R.SetupIn]:
    st = get_state()
    require_level(body.replica_server_id, None, ADMIN, "creating the replica on that server")
    db = st.store.get_database(server_id, db_id)
    rsrv = st.store.get(body.replica_server_id)
    if rsrv.read_only:
        raise ValueError(f"The server {rsrv.name!r} is read-only in fbadmin; the replica must be created on it")
    b = R.defaults(db.path, body)
    with st.manager.meta(server_id, db_id) as conn:
        primary = replication.status(conn)
        others = R.other_attachments(conn) if primary.get("supported") else []
        conn.commit()
    primary["path"] = db.path
    try:
        rmajor: int | None = _major(body.replica_server_id)
    except PasswordRequired:
        raise
    except Exception as e:  # noqa: BLE001
        raise ValueError(f"Can't reach the replica server {rsrv.name!r}: {e}") from None
    rpw = _password(body.replica_server_id)
    exists = R.replica_exists(lambda: connect(rsrv, b.replica_path, rpw))
    conf = R.blocks(db.path, b, primary.get("guid"), rsrv.host, rsrv.port, rsrv.user,
                    b.sync_password or (rpw if b.mode == "sync" else None))
    same = server_id == body.replica_server_id or (
        st.store.get(server_id).host == rsrv.host and st.store.get(server_id).port == rsrv.port)
    return R.plan(primary=primary, b=b, same_server=same, replica_major=rmajor, replica_exists_=exists,
                  others=others, conf=conf), b


@router.post(DB + "/setup/plan")
def setup_plan(server_id: str, db_id: str, body: R.SetupIn):
    """What the setup will do, its checks (errors block it) and the replication.conf blocks."""
    p, _b = _plan(server_id, db_id, body)
    return p


@router.post(DB + "/setup/reopen")
def setup_reopen(server_id: str, db_id: str):
    """Close fbadmin's own connections to the database (metadata and Query Tool tabs), so that it is opened
    again - and its replication.conf block read - once no other application keeps it open."""
    st = get_state()
    st.manager.disconnect(server_id, db_id)
    with st.manager.meta(server_id, db_id) as conn:
        others = R.other_attachments(conn)
        conn.commit()
    return {"others": others, "reopened": not others}


def _finish(server_id: str, db_id: str, b: R.SetupIn, copy_file: str, result: dict[str, Any], log) -> None:
    """Restore the copy on the replica server, make it a read-only replica, register it; synchronous: start
    publication."""
    st = get_state()
    mgr = st.manager
    db = st.store.get_database(server_id, db_id)
    rsrv = st.store.get(b.replica_server_id)
    rpw = _password(b.replica_server_id)
    with mgr.service(b.replica_server_id) as rsvc:
        R.restore_copy(rsvc, copy_file, b.replica_path, log)
    R.make_replica(lambda: connect(rsrv, b.replica_path, rpw), lambda: mgr.service(b.replica_server_id),
                   b.replica_path, log)
    existing = next((d for d in rsrv.databases if R._same_path(d.path, b.replica_path)), None)
    entry = existing or st.store.add_database(
        b.replica_server_id, DatabaseEntry(name=b.replica_name or f"{db.name} (replica)", path=b.replica_path))
    result["replica_db_id"] = entry.id
    log(f"Registered the replica in fbadmin as {entry.name!r}")
    if b.mode == "sync":
        mgr.disconnect(server_id, db_id)  # opened again: its replication.conf block is read
        with mgr.meta(server_id, db_id) as conn:
            R.publish(conn, b.tables, log)
        log("Publication enabled: commits are now sent to the replica")
    else:
        log("Next: add the replica's block to the replica server's replication.conf and restart that Firebird "
            "server (it reads the block at startup), then run the check")
    if result.get("copy_file"):
        log(f"The copy {result['copy_file']} can be deleted on the server")


def _start(kind: str, server_id: str, db_id: str, b: R.SetupIn, result: dict[str, Any], body) -> dict:
    st = get_state()
    db = st.store.get_database(server_id, db_id)
    rsrv = st.store.get(b.replica_server_id)
    what = f"{db.path} -> {rsrv.name}:{b.replica_path} ({b.mode}, {kind})"

    def run(log):
        try:
            body(log)
            _audit(what, server_id=server_id, db_id=db_id)
        except Exception as e:
            _audit(what, False, str(e), server_id=server_id, db_id=db_id)
            raise

    job = st.jobs.start("replication_setup", f"Replica of {db.name}", server_id, run)
    _remember(_RUNS, job.id, result)
    return job.summary()


@router.post(DB + "/setup/run", status_code=202)
def setup_run(server_id: str, db_id: str, body: R.SetupIn):
    """Publication and the journal check (asynchronous), the copy, and - when both servers see the copy
    directory - the restore, replica mode and registration. With ``transfer: manual`` the job ends after the
    copy (``waiting_for_transfer``); POST .../setup/restore continues once the file is on the replica server."""
    st = get_state()
    db = st.store.get_database(server_id, db_id)
    p, b = _plan(server_id, db_id, body)
    if not p["ok"]:
        raise ValueError(" ".join(c["message"] for c in p["checks"] if c["level"] == "error"))
    _password(b.replica_server_id)
    mgr = st.manager
    result: dict[str, Any] = {"replica_server_id": b.replica_server_id, "replica_path": b.replica_path,
                              "mode": b.mode, "transfer": b.transfer}

    def run(log):
        rsrv = st.store.get(b.replica_server_id)
        log(f"Replica of {db.name}: {b.replica_path} on {rsrv.name} ({b.mode})")
        mgr.disconnect(server_id, db_id)  # opened again below: its replication.conf block is read then
        with mgr.meta(server_id, db_id) as conn:
            if R.ensure_check_table(conn):
                log(f"Created the check table {R.CHECK_TABLE}")
            if b.mode == "async":
                R.publish(conn, b.tables, log)
                result["sequence_at_copy"] = R.journal_check(conn, log)
        with mgr.service(server_id) as psvc:
            result["copy_file"] = R.physical_copy(psvc, db.path, b, log)
        if b.transfer == "manual":
            result["waiting_for_transfer"] = True
            log(f"Copy {result['copy_file']} to the replica server, then continue with the restore")
            return
        _finish(server_id, db_id, b, R.replica_copy_path(result["copy_file"], b), result, log)

    return _start("copy", server_id, db_id, b, result, run)


class RestoreIn(R.SetupIn):
    copy_file: str  # the copy as the replica server sees it

    @field_validator("copy_file")
    @classmethod
    def _one(cls, v: str) -> str:
        v = v.strip()
        if not v or any(c in v for c in "\r\n\0") or len(v) > 1000:
            raise ValueError("A file path on the replica server")
        return v


@router.post(DB + "/setup/restore", status_code=202)
def setup_restore(server_id: str, db_id: str, body: RestoreIn):
    """After a manual transfer: restore the copy on the replica server and finish the setup."""
    p, b = _plan(server_id, db_id, R.SetupIn(**body.model_dump(exclude={"copy_file"})))
    errors = [c for c in p["checks"] if c["level"] == "error"]
    if errors:
        raise ValueError(" ".join(c["message"] for c in errors))
    result: dict[str, Any] = {"replica_server_id": b.replica_server_id, "replica_path": b.replica_path,
                              "mode": b.mode, "transfer": b.transfer}
    return _start("restore", server_id, db_id, b, result,
                  lambda log: _finish(server_id, db_id, b, body.copy_file, result, log))


@router.get(DB + "/setup/runs/{job_id}")
def setup_result(server_id: str, db_id: str, job_id: str):
    job = get_state().jobs.get(job_id)
    if job_id not in _RUNS:
        raise HTTPException(404, "Unknown setup run")
    return {"status": job.status, "error": job.error, **_RUNS[job_id]}


class CheckIn(BaseModel):
    peer_server: str
    peer_db: str


@router.post(DB + "/check")
def check_send(server_id: str, db_id: str, body: CheckIn):
    """Insert a check row on the primary; GET .../check/{token} tells when it is on the replica."""
    st = get_state()
    require_level(body.peer_server, body.peer_db, READ, "the replica")
    st.store.get_database(body.peer_server, body.peer_db)
    with st.manager.meta(server_id, db_id) as conn:
        info = replication.status(conn)
        conn.commit()
        if not info.get("supported"):
            raise ValueError("Replication needs Firebird 4 or newer")
        if info.get("replica_mode") not in (None, "none"):
            raise ValueError("This database is a replica: send the check from its primary")
        if not info["publication_enabled"]:
            raise ValueError("Publication is disabled: nothing reaches a replica")
        R.ensure_check_table(conn)
        published = {t["name"] for t in info["tables"] if t["published"]}
        if not info["auto_enable"] and R.CHECK_TABLE not in published:
            replication.change_tables(conn, "include", [R.CHECK_TABLE])
            conn.commit()
        t = R.send_token(conn)
    _remember(_TOKENS, t["token"], {**t, "peer": (body.peer_server, body.peer_db), "primary": (server_id, db_id)})
    return t


@router.get(DB + "/check/{token}")
def check_poll(server_id: str, db_id: str, token: str):
    st = get_state()
    sent = _TOKENS.get(token)
    if not sent or sent["primary"] != (server_id, db_id):
        raise HTTPException(404, "Unknown check")
    ps, pd = sent["peer"]
    require_level(ps, pd, READ, "the replica")
    with st.manager.meta(ps, pd) as conn:
        r = R.token_arrived(conn, token)
        seq = replication.status(conn).get("replication_sequence")
        conn.commit()
    if r["arrived"] and "arrived_after" not in sent:
        sent["arrived_after"] = round(time.time() - sent["sent_at"], 1)
    return {**r, "token": token, "seconds": round(time.time() - sent["sent_at"], 1),
            "arrived_after": sent.get("arrived_after"), "primary_sequence": sent["primary_sequence"],
            "replica_sequence": seq}


class ConfWriteIn(BaseModel):
    path: str
    database: str
    block: str


@router.post("/api/replication/conf/write")
def conf_write(body: ConfWriteIn):
    """Put a block into a replication.conf that fbadmin can reach (a local Firebird, a mounted volume)."""
    require_admin()
    try:
        out = R.write_conf(body.path, body.database, body.block, server_mode=get_state().security.server_mode)
    except PermissionError as e:
        raise HTTPException(403, str(e)) from None
    _audit(f"wrote the block of {body.database} into {out['path']}")
    return out
