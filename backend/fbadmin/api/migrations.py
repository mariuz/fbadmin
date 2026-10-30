"""Versioned migrations (roadmap 2.2): source, status, dry run, apply, baseline, repair.

Scripts come from the database's configured source (git or a directory), or from the request (``scripts``: the
command-line tool in remote mode sends the files of a local directory). Every change is audited.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any, Literal

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from .. import migrations as M
from ..connections import PasswordRequired, connect
from ..security.access import ADMIN, require_level
from ..security.core import require_admin
from ..security.sessions import current_principal
from .deps import get_state

DB = "/api/servers/{server_id}/databases/{db_id}/migrations"
router = APIRouter(tags=["migrations"])
_RUNS: dict[str, dict[str, Any]] = {}


class ScriptIn(BaseModel):
    name: str = Field(max_length=500)
    sql: str = Field(max_length=10_000_000)


class FilesIn(BaseModel):
    scripts: list[ScriptIn] | None = None   # instead of the configured source
    source_ref: str | None = None           # what the inline scripts are (a commit, a directory)
    out_of_order: bool | None = None
    target: str | None = None               # apply up to this version


def _key(server_id: str, db_id: str) -> str:
    return f"{server_id}/{db_id}"


def _user() -> str:
    p = current_principal.get()
    if p is not None:
        return getattr(p, "username", None) or "fbadmin"
    import getpass

    try:
        return getpass.getuser()
    except Exception:  # noqa: BLE001
        return "fbadmin"


def _audit(what: str, server_id: str, db_id: str, ok: bool = True, error: str | None = None) -> None:
    try:
        get_state().security.audit.record(server_id=server_id, db_id=db_id, statement=f"-- migrations: {what}",
                                          success=ok, error=error, cat="DDL")
    except Exception:  # noqa: BLE001
        pass


def _password(server_id: str) -> str:
    srv = get_state().store.get(server_id)
    if srv.embedded:
        return ""
    pw = get_state().store.password_for(server_id)
    if not pw:
        raise PasswordRequired(server_id)
    return pw


def _connector(server_id: str, path: str):
    srv = get_state().store.get(server_id)
    pw = _password(server_id)
    return lambda p=None: connect(srv, p or path, pw)


def _files(server_id: str, db_id: str, body: FilesIn | None, log=lambda _m: None):
    """(scripts, warnings, source ref, source settings)."""
    src = M.store().get(_key(server_id, db_id)) or M.Source()
    if body and body.scripts is not None:
        scripts, warnings = M.parse([(s.name, s.sql) for s in body.scripts])
        return scripts, warnings, (body.source_ref or "inline")[:120], src
    if not M.store().get(_key(server_id, db_id)):
        raise ValueError("Set where the migration scripts are (a git repository or a directory) first")
    files, ref = M.read_source(src, _key(server_id, db_id), log)
    scripts, warnings = M.parse(files)
    return scripts, warnings, ref, src


def _status(server_id: str, db_id: str, body: FilesIn | None, log=lambda _m: None):
    db = get_state().store.get_database(server_id, db_id)
    scripts, warnings, ref, src = _files(server_id, db_id, body, log)
    conn = _connector(server_id, db.path)()
    try:
        rows = M.applied(conn)
    finally:
        conn.close()
    ooo = body.out_of_order if body and body.out_of_order is not None else src.out_of_order
    st = M.status(scripts, rows, out_of_order=ooo, target=body.target if body else None)
    st["warnings"] = warnings + st["warnings"]
    return st, scripts, ref, src


def _view(st: dict[str, Any], ref: str | None, key: str) -> dict[str, Any]:
    last = M.store().dry_run(key)
    return {**M.public(st), "source_ref": ref, "scripts": len([i for i in st["items"] if i.get("script")]),
            "dry_run": last and {k: last.get(k) for k in ("ok", "finished_at", "fingerprint", "pending", "copy",
                                                         "summary", "source_ref")},
            "dry_run_needed": M.dry_run_valid(last, st["fingerprint"]) if st["pending"] else None}


# ------------------------------------------------------------------ settings
@router.get(DB + "/source")
def get_source(server_id: str, db_id: str):
    get_state().store.get_database(server_id, db_id)
    src = M.store().get(_key(server_id, db_id))
    return src.model_dump() if src else None


@router.put(DB + "/source")
def put_source(server_id: str, db_id: str, body: M.Source):
    from ..schemahistory import check_remote, check_subdir, check_token
    from ..schemahistory.gitrepo import check_ref

    get_state().store.get_database(server_id, db_id)
    if body.kind == "directory":
        require_admin()  # reads files on the fbadmin host
        if not body.path:
            raise ValueError("The directory is required")
        M._check_directory(Path(body.path).expanduser())
    else:
        body.url = check_remote(body.url)
        if not body.url:
            raise ValueError("The git repository URL is required")
        remote = body.url.startswith(("https://", "http://", "ssh://", "git@"))
        if get_state().security.server_mode and not remote:
            require_admin()  # a repository on the fbadmin host
        body.path = check_subdir(body.path)
        body.ref = check_ref(body.ref or "main")
        body.token = check_token(body.token)
    M.store().put(_key(server_id, db_id), body)
    _audit(f"source set to {body.kind} {body.url or body.path} {body.ref if body.kind == 'git' else ''}".strip(),
           server_id, db_id)
    return body.model_dump()


@router.delete(DB + "/source", status_code=204)
def delete_source(server_id: str, db_id: str):
    get_state().store.get_database(server_id, db_id)
    M.store().put(_key(server_id, db_id), None)


# ------------------------------------------------------------------ status
@router.get(DB)
def status(server_id: str, db_id: str, target: str | None = None):
    """The scripts of the configured source and the database's record, with the state of each."""
    st, _s, ref, _src = _status(server_id, db_id, FilesIn(target=target))
    return _view(st, ref, _key(server_id, db_id))


@router.post(DB + "/status")
def status_inline(server_id: str, db_id: str, body: FilesIn):
    st, _s, ref, _src = _status(server_id, db_id, body)
    return _view(st, ref, _key(server_id, db_id))


@router.get(DB + "/dry-run")
def last_dry_run(server_id: str, db_id: str):
    r = M.store().dry_run(_key(server_id, db_id))
    if r is None:
        raise HTTPException(404, "No dry run yet")
    return r


@router.get(DB + "/runs/{job_id}")
def run_result(server_id: str, db_id: str, job_id: str):
    job = get_state().jobs.get(job_id)
    if job_id not in _RUNS:
        raise HTTPException(404, "Unknown migration run")
    return {"status": job.status, "error": job.error, **_RUNS[job_id]}


# ------------------------------------------------------------------ dry run, apply
class DryRunIn(FilesIn):
    copy_mode: Literal["metadata", "full"] | None = None
    scratch: str | None = None


def _remember(job_id: str, value: dict[str, Any]) -> None:
    _RUNS[job_id] = value
    while len(_RUNS) > 200:
        _RUNS.pop(next(iter(_RUNS)))


@router.post(DB + "/dry-run", status_code=202)
def dry_run(server_id: str, db_id: str, body: DryRunIn):
    """Restore a copy of the database, run the pending scripts on it, report the result and the schema changes."""
    st_ = get_state()
    require_level(server_id, db_id, ADMIN, "a migration dry run (backup and restore)")
    db = st_.store.get_database(server_id, db_id)
    srv = st_.store.get(server_id)
    key = _key(server_id, db_id)
    st, _scripts, ref, src = _status(server_id, db_id, body)
    if st["errors"]:
        raise ValueError(" ".join(st["errors"]))
    pending = st["_pending"]
    scratch = M.scratch_path(db.path, key, body.scratch or src.scratch)
    copy = body.copy_mode or src.copy_mode
    result: dict[str, Any] = {}
    user = _user()

    def run(log):
        if not pending:
            log("Nothing pending: the database is up to date")
            report = {"ok": True, "scripts": [], "changes": [], "summary": {"created": 0, "altered": 0, "dropped": 0},
                      "copy": copy, "seconds": 0, "finished_at": time.time()}
        else:
            log(f"Dry run of {len(pending)} script(s): {', '.join(p.name for p in pending)}")
            report = M.dry_run(pending=pending, db_path=db.path, scratch=scratch, copy=copy,
                               svc_factory=lambda: st_.manager.service(server_id),
                               connect=_connector(server_id, db.path), registered=[d.path for d in srv.databases],
                               user=user, ref=ref, log=log)
        report.update(fingerprint=st["fingerprint"], pending=st["pending"], source_ref=ref, by=user)
        M.store().save_dry_run(key, report)
        result.update(report)
        _audit(f"dry run of {', '.join(st['pending']) or 'nothing'}: {'passed' if report['ok'] else 'failed'}",
               server_id, db_id, report["ok"])

    job = st_.jobs.start("migrations-dry-run", f"Migration dry run: {db.name}", server_id, run)
    _remember(job.id, result)
    return job.summary()


class ApplyIn(FilesIn):
    skip_dry_run: bool = False


@router.post(DB + "/apply", status_code=202)
def apply(server_id: str, db_id: str, body: ApplyIn):
    """Apply the pending scripts in order. Needs a passed dry run of the same scripts (unless skipped)."""
    st_ = get_state()
    db = st_.store.get_database(server_id, db_id)
    if st_.store.get(server_id).read_only:
        raise ValueError("The server is read-only in fbadmin")
    key = _key(server_id, db_id)
    st, scripts, ref, src = _status(server_id, db_id, body)
    if st["errors"]:
        raise ValueError(" ".join(st["errors"]))
    if st["pending"] and not body.skip_dry_run:
        why = M.dry_run_valid(M.store().dry_run(key), st["fingerprint"])
        if why:
            raise HTTPException(409, why)
    ooo = body.out_of_order if body.out_of_order is not None else src.out_of_order
    result: dict[str, Any] = {}
    user = _user()

    def run(log):
        try:
            rep = M.apply(_connector(server_id, db.path), scripts, key=key, user=user, ref=ref, log=log,
                          out_of_order=ooo, target=body.target)
        except Exception as e:
            _audit("apply failed", server_id, db_id, False, str(e))
            raise
        result.update(rep)
        names = ", ".join(r["script"] for r in rep["applied"]) or "nothing"
        _audit(f"applied {names}" + (f"; FAILED {rep['failed']['script']}" if rep["failed"] else "")
               + (" (dry run skipped)" if body.skip_dry_run and st["pending"] else ""), server_id, db_id, rep["ok"],
               rep["failed"]["error"]["message"] if rep["failed"] else None)
        if rep["failed"]:
            f = rep["failed"]
            raise RuntimeError(f"{f['script']} failed at line {f['error']['line']}: {f['error']['message']}")

    job = st_.jobs.start("migrations-apply", f"Migrations: {db.name}", server_id, run)
    _remember(job.id, result)
    return job.summary()


class BaselineIn(BaseModel):
    version: str
    description: str = "Baseline"


@router.post(DB + "/baseline")
def baseline(server_id: str, db_id: str, body: BaselineIn):
    """Mark an existing database as being at ``version``: older scripts are never run on it."""
    db = get_state().store.get_database(server_id, db_id)
    conn = _connector(server_id, db.path)()
    try:
        M.baseline(conn, body.version, body.description, user=_user())
    finally:
        conn.close()
    _audit(f"baseline at {body.version}", server_id, db_id)
    return {"ok": True, "version": ".".join(map(str, M.version_key(body.version)))}


@router.post(DB + "/repair")
def repair(server_id: str, db_id: str, body: FilesIn):
    """Remove failed records and accept the checksums of changed scripts."""
    db = get_state().store.get_database(server_id, db_id)
    scripts, _w, _ref, _src = _files(server_id, db_id, body)
    conn = _connector(server_id, db.path)()
    try:
        res = M.repair(conn, scripts)
    finally:
        conn.close()
    _audit(f"repair: removed failed {res['removed_failed'] or 'none'}, accepted {res['accepted_checksums'] or 'none'}",
           server_id, db_id)
    return res
