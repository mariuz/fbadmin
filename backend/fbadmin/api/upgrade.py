"""Upgrade assistant API (milestone 1.2): check a database before moving it to a newer Firebird."""

from __future__ import annotations

import re
import threading
import time
from contextlib import contextmanager
from typing import Any

from fastapi import APIRouter
from pydantic import BaseModel

from ..config import settings
from ..connections import PasswordRequired, connect
from ..metadata import Catalog
from ..serialize import to_json
from ..store import DatabaseEntry
from ..upgrade import checks, plan, trial
from ..upgrade import keywords as K
from .deps import get_state

router = APIRouter(tags=["upgrade"])
DB = "/api/servers/{server_id}/databases/{db_id}/upgrade"

_results: dict[str, dict] = {}
_lock = threading.Lock()


def _password(server_id: str) -> str:
    pw = get_state().store.password_for(server_id)
    if not pw:
        raise PasswordRequired(server_id)
    return pw


def build_report(server_id: str, db_id: str, target: str, *, target_server_id: str | None = None,
                 trial_result: dict | None = None) -> dict[str, Any]:
    st = get_state()
    srv = st.store.get(server_id)
    db = st.store.get_database(server_id, db_id)
    conn = connect(srv, db.path, _password(server_id))
    try:
        cat = Catalog(conn, schema="")
        report = checks.analyze(cat, target)
        report["source"]["tables"] = [r["name"] for r in cat.list_objects("tables")]
        conn.commit()
    finally:
        try:
            conn.close()
        except Exception:  # noqa: BLE001
            pass
    report["database"] = {"name": db.name, "path": db.path, "host": srv.host, "port": srv.port,
                          "server": srv.name, "server_id": server_id, "db_id": db_id}
    if target_server_id:
        tsrv = st.store.get(target_server_id)
        report["target_server"] = {"id": tsrv.id, "name": tsrv.name, "host": tsrv.host, "port": tsrv.port}
    if trial_result:
        report["trial"] = {k: v for k, v in trial_result.items() if k != "findings"}
        report["findings"] = trial_result["findings"] + report["findings"]
        order = {s: i for i, s in enumerate(checks.SEVERITIES)}
        report["findings"].sort(key=lambda f: order[f["severity"]])
        report["counts"] = {s: sum(1 for f in report["findings"] if f["severity"] == s) for s in checks.SEVERITIES}
    report["plan"] = plan.build_plan(report)
    report["generated_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    return report


@router.get("/api/upgrade/versions")
def versions():
    return {"versions": list(K.VERSIONS), "ods": {v: "{}.{}".format(*o) for v, o in K.ODS.items()},
            "keyword_sources": {v: d["source"] for v, d in K.KEYWORDS.items()}}


class CheckIn(BaseModel):
    target: str = "5.0"


@router.post(DB + "/check")
def run_check(server_id: str, db_id: str, body: CheckIn):
    return to_json(build_report(server_id, db_id, body.target))


class TrialIn(BaseModel):
    target_server_id: str
    target: str | None = None          # must match the target server's version when given
    scratch_path: str | None = None    # path on the target server; default: next to its databases
    metadata_only: bool = False
    keep: bool = False                 # keep (and register) the restored database for testing


def _engine_major(server_id: str) -> int:
    from ..maintenance import ops

    with get_state().manager.service(server_id) as svc:
        return ops.engine_major(svc)


@router.post(DB + "/trial", status_code=202)
def run_trial(server_id: str, db_id: str, body: TrialIn):
    st = get_state()
    db = st.store.get_database(server_id, db_id)
    tsrv = st.store.get(body.target_server_id)
    if tsrv.read_only:
        raise ValueError(f"The server {tsrv.name!r} is read-only in fbadmin; the trial restore must write to it")
    tmajor = _engine_major(body.target_server_id)
    target = K.version_key(str(tmajor))
    if body.target and K.version_key(body.target) != target:
        raise ValueError(f"The target server runs Firebird {tmajor}, not {body.target}")
    smajor = _engine_major(server_id)
    if smajor >= tmajor:
        raise ValueError(f"The target server runs Firebird {tmajor}; it must be newer than the source "
                         f"(Firebird {smajor})")
    registered = [d.path for d in tsrv.databases]
    # default: next to the target server's first database, else in the source database's directory
    scratch = trial.scratch_path(registered[0] if registered else db.path, body.scratch_path)
    tpw = _password(body.target_server_id)
    _password(server_id)

    result: dict[str, Any] = {}

    def run(log):
        src_report = build_report(server_id, db_id, target)
        @contextmanager
        def source_service():
            with st.manager.service(server_id) as svc, st.manager.detach_on_cancel(server_id, db.path):
                yield svc

        res = trial.run_trial(
            source_service=source_service, source_path=db.path,
            target_service=lambda: st.manager.service(body.target_server_id),
            target_connect=lambda p: connect(tsrv, p, tpw), scratch=scratch, registered=registered,
            work_dir=settings.data_dir / "upgrade", metadata_only=body.metadata_only, keep=body.keep,
            source_facts=src_report["source"], log=log)
        if res.get("kept"):
            name = f"{db.name} (Firebird {target} trial)"
            st.store.add_database(body.target_server_id, DatabaseEntry(name=name, path=scratch))
            res["registered_as"] = name
            log(f"Registered the trial database as {name!r}")
        result["report"] = build_report(server_id, db_id, target, target_server_id=body.target_server_id,
                                        trial_result=res)
        log("Upgrade report ready")

    job = st.jobs.start("upgrade", f"Upgrade trial {db.name} → Firebird {target} ({tsrv.name})", server_id, run,
                        cancellable=True)
    with _lock:
        _results[job.id] = result
        for k in list(_results)[:-100]:
            _results.pop(k, None)
    return job.summary()


@router.get("/api/upgrade/jobs/{job_id}/result")
def trial_result(job_id: str):
    job = get_state().jobs.get(job_id)
    with _lock:
        res = _results.get(job_id, {})
    return to_json({"status": job.status, "error": job.error, **res})


class RenderIn(BaseModel):
    report: dict[str, Any]
    format: str = "md"  # md | html


@router.post("/api/upgrade/render")
def render(body: RenderIn):
    """The report as a Markdown or HTML document: {filename, media_type, content}."""
    rep = body.report
    if not isinstance(rep.get("source"), dict) or not isinstance(rep.get("target"), dict):
        raise ValueError("Not an upgrade report")
    name = re.sub(r"[^A-Za-z0-9_.-]", "_", (rep.get("database") or {}).get("name") or "database")
    fname = f"{name}_upgrade_fb{str(rep['target'].get('version', ''))[:1]}"
    if body.format == "html":
        return {"filename": f"{fname}.html", "media_type": "text/html", "content": plan.to_html(rep)}
    if body.format == "md":
        return {"filename": f"{fname}.md", "media_type": "text/markdown", "content": plan.to_markdown(rep)}
    raise ValueError(f"Unknown format {body.format!r}")
