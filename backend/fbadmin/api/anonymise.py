"""Anonymised copies of production (roadmap 2.3): masking profiles of a source database, a preview of the
masked rows, and the copy job (backup, scratch copy on the source server, masking, restore on the target)."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Response
from pydantic import BaseModel, Field

from .. import anonymise as A
from ..metadata.types import quote_ident
from ..query import end_transaction
from ..serialize import to_json
from .deps import get_state
from .library import actor

DB = "/api/servers/{server_id}/databases/{db_id}/anonymise"
router = APIRouter(tags=["anonymise"])


class ProfileIn(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    rules: dict[str, dict[str, A.Rule]] = Field(default_factory=dict)
    target: A.Target | None = None
    scratch_path: str | None = Field(default=None, max_length=1000)


class PreviewIn(BaseModel):
    table: str
    rules: dict[str, dict[str, A.Rule]] = Field(default_factory=dict)
    profile_id: str | None = None
    limit: int = Field(default=10, ge=1, le=100)


def _profile(server_id: str, db_id: str, pid: str) -> A.Profile:
    p = A.get_store().get(pid)
    if (p.server_id, p.database_id) != (server_id, db_id):
        raise KeyError(f"Unknown anonymisation profile {pid!r}")
    return p


def _schema(server_id: str, db_id: str) -> dict[str, Any]:
    with get_state().manager.meta(server_id, db_id) as conn:
        try:
            return A.analyze(conn)
        finally:
            end_transaction(conn, commit=False)


def _check_target(t: A.Target | None) -> None:
    """Writing a database on the target server needs the admin level there."""
    if t is None:
        return
    from ..security.access import ADMIN, require_level

    srv = get_state().store.get(t.server_id)  # 404 for unknown servers
    reg = next((d for d in srv.databases if A._same(d.path, t.path)), None)
    require_level(t.server_id, reg.id if reg else None, ADMIN, "writing the anonymised copy on the target server")


@router.get(DB)
def overview(server_id: str, db_id: str):
    """The profiles of this database (without their secret)."""
    get_state().store.get_database(server_id, db_id)
    return {"profiles": [A.public(p) for p in A.get_store().list(server_id, db_id)]}


@router.get(DB + "/columns")
def columns(server_id: str, db_id: str):
    """Tables and columns with their kind, keys, the strategies that fit, suggestions and foreign-key links."""
    return to_json(_schema(server_id, db_id))


@router.post(DB + "/profiles", status_code=201)
def create_profile(server_id: str, db_id: str, body: ProfileIn):
    get_state().store.get_database(server_id, db_id)
    _check_target(body.target)
    p = A.Profile(name=body.name.strip(), server_id=server_id, database_id=db_id, rules=body.rules,
                  target=body.target, scratch_path=body.scratch_path, owner=actor().username)
    return A.public(A.get_store().put(p))


@router.put(DB + "/profiles/{pid}")
def update_profile(server_id: str, db_id: str, pid: str, body: ProfileIn):
    p = _profile(server_id, db_id, pid)
    _check_target(body.target)
    new = p.model_copy(update={"name": body.name.strip(), "rules": body.rules, "target": body.target,
                               "scratch_path": body.scratch_path})
    return A.public(A.get_store().put(new))


@router.delete(DB + "/profiles/{pid}", status_code=204)
def delete_profile(server_id: str, db_id: str, pid: str):
    _profile(server_id, db_id, pid)
    A.get_store().delete(pid)
    return Response(status_code=204)


@router.post(DB + "/profiles/{pid}/check")
def check_profile(server_id: str, db_id: str, pid: str):
    """The columns that will be masked, rules copied along foreign keys included; 400 for invalid rules."""
    p = _profile(server_id, db_id, pid)
    schema = _schema(server_id, db_id)
    eff = A.effective_rules({t: {c: r.model_dump() for c, r in cr.items()} for t, cr in p.rules.items()}, schema)
    own = {(t, c) for t, cr in p.rules.items() for c, r in cr.items() if r.strategy != "keep"}
    return {"columns": [{"table": t, "column": c, "strategy": r.strategy, "linked": (t, c) not in own}
                        for t, cr in sorted(eff.items()) for c, r in sorted(cr.items())],
            "tables": len(eff)}


@router.post(DB + "/preview")
def preview(server_id: str, db_id: str, body: PreviewIn):
    """The first rows of a table, original and masked (nothing is written)."""
    secret = bytes.fromhex(_profile(server_id, db_id, body.profile_id).secret) if body.profile_id else b"preview"
    schema = _schema(server_id, db_id)
    table = next((t for t in schema["tables"] if t["name"] == body.table), None)
    if table is None:
        raise KeyError(f"Unknown table {body.table!r}")
    eff = A.effective_rules({t: {c: r.model_dump() for c, r in cr.items()} for t, cr in body.rules.items()}, schema)
    rules = eff.get(body.table, {})
    cols = [c for c in table["columns"] if c["kind"] not in ("blob",)]
    fns = {c["name"]: A.masker(rules[c["name"]], c["kind"], c["length"], secret, c["unique"])
           for c in cols if c["name"] in rules and rules[c["name"]].strategy != "shuffle"}
    with get_state().manager.meta(server_id, db_id) as conn:
        cur = conn.cursor()
        try:
            cur.execute(f"SELECT FIRST {body.limit} " + ", ".join(quote_ident(c["name"]) for c in cols)
                        + f" FROM {quote_ident(body.table)}")
            rows = cur.fetchall()
        finally:
            cur.close()
            end_transaction(conn, commit=False)
    masked = [[fns[c["name"]](v) if c["name"] in fns else v for c, v in zip(cols, r, strict=True)] for r in rows]
    return to_json({"columns": [{"name": c["name"], "type": c["data_type"],
                                 "strategy": rules[c["name"]].strategy if c["name"] in rules else "keep"} for c in cols],
                    "original": [list(r) for r in rows], "masked": masked})


@router.post(DB + "/profiles/{pid}/run", status_code=202)
def run_profile(server_id: str, db_id: str, pid: str):
    """Make the anonymised copy (a job)."""
    p = _profile(server_id, db_id, pid)
    _check_target(p.target)
    if p.target is None:
        raise ValueError("Choose the target server and file of the copy first")
    st = get_state()
    db = st.store.get_database(server_id, db_id)
    if p.target.server_id == server_id and A._same(p.target.path, db.path):
        raise ValueError("The target is the source database")

    def run(log):
        A.run_copy(pid, log)

    return st.jobs.start("anonymise", f"Anonymised copy of {db.name}", p.target.server_id, run,
                         cancellable=True).summary()
