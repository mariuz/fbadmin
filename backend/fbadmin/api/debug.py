"""PSQL debugging help (milestone 1.2): trace runs, profiled runs and saved tests of procedures/functions.

Every run uses a dedicated attachment and one transaction that is rolled back.
"""

from __future__ import annotations

from contextlib import contextmanager
from typing import Any, Literal

from fastapi import APIRouter
from pydantic import BaseModel

from ..debug import runner
from ..debug.psql import PsqlError
from ..debug.store import Expect, TestCase, get_store
from ..metadata import Catalog
from .deps import get_state

router = APIRouter(prefix="/api/servers/{server_id}/databases/{db_id}/debug", tags=["debug"])

OType = Literal["procedures", "functions", "block"]


@contextmanager
def _dedicated(server_id: str, db_id: str):
    fbc = get_state().manager._open(server_id, db_id)
    try:
        yield fbc.conn
    finally:
        try:
            fbc.conn.rollback()
        except Exception:  # noqa: BLE001
            pass
        fbc.close()


def _info(conn, otype: str, name: str | None, sql: str | None) -> dict[str, Any]:
    if otype == "block":
        if not sql or not sql.strip():
            raise ValueError("Type an EXECUTE BLOCK statement to debug")
        try:
            return runner.block_info(sql)
        except PsqlError as exc:
            return {"otype": "block", "name": None, "kind": "executable", "inputs": [], "outputs": [], "returns": None,
                    "source": sql, "external": False, "traceable": False, "reason": str(exc),
                    "error_line": exc.line, "locals": [], "warnings": [], "sql_security": None}
    if not name:
        raise ValueError("name is required")
    return runner.routine(Catalog(conn), otype, name)


@router.get("/routine")
def routine_info(server_id: str, db_id: str, otype: OType, name: str):
    with get_state().manager.meta(server_id, db_id) as conn:
        info = _info(conn, otype, name, None)
        cat = Catalog(conn)
        info["warnings"] = info["warnings"] + runner.dependency_warnings(cat, otype, name)
        from ..monitoring import profiler

        info["profiler"] = profiler.available(conn)
        info["engine_version"] = cat.engine_version
    return info


class ParseIn(BaseModel):
    sql: str


@router.post("/parse")
def parse_block(server_id: str, db_id: str, body: ParseIn):
    """Inputs, outputs and variables of an anonymous EXECUTE BLOCK."""
    get_state().store.get_database(server_id, db_id)
    info = _info(None, "block", None, body.sql)
    with get_state().manager.meta(server_id, db_id) as conn:
        from ..monitoring import profiler

        info["profiler"] = profiler.available(conn)
        info["engine_version"] = Catalog(conn).engine_version
    return info


class RunIn(BaseModel):
    otype: OType
    name: str | None = None
    sql: str | None = None  # EXECUTE BLOCK text (otype "block")
    inputs: dict[str, str | None] = {}
    watch: list[str] | None = None  # variables to trace (default: all)
    max_steps: int = runner.MAX_STEPS


@router.post("/trace")
def trace_run(server_id: str, db_id: str, body: RunIn):
    with _dedicated(server_id, db_id) as conn:
        info = _info(conn, body.otype, body.name, body.sql)
        return runner.trace(conn, info, body.inputs, watch=body.watch, max_steps=body.max_steps,
                            block_sql=body.sql if body.otype == "block" else None)


@router.post("/run")
def plain_run(server_id: str, db_id: str, body: RunIn):
    """Call the routine itself (not instrumented), rolled back."""
    with _dedicated(server_id, db_id) as conn:
        info = _info(conn, body.otype, body.name, body.sql)
        r = runner.run_call(conn, info, body.inputs, block_sql=body.sql if body.otype == "block" else None)
        conn.rollback()
        return r


@router.post("/profile")
def profile_run(server_id: str, db_id: str, body: RunIn):
    with _dedicated(server_id, db_id) as conn:
        info = _info(conn, body.otype, body.name, body.sql)
        return runner.profile(conn, info, body.inputs, block_sql=body.sql if body.otype == "block" else None)


# ------------------------------------------------------------------ tests
class TestIn(BaseModel):
    otype: Literal["procedures", "functions"]
    routine: str
    name: str
    inputs: dict[str, str | None] = {}
    expect: Expect = Expect()


class TestUpdate(BaseModel):
    name: str | None = None
    inputs: dict[str, str | None] | None = None
    expect: Expect | None = None


class RunTestsIn(BaseModel):
    ids: list[str] | None = None  # default: all tests of the routine / database
    otype: Literal["procedures", "functions"] | None = None
    routine: str | None = None


@router.get("/tests")
def list_tests(server_id: str, db_id: str, otype: str | None = None, routine: str | None = None):
    get_state().store.get_database(server_id, db_id)
    return [t.model_dump() for t in get_store().list(server_id, db_id, otype, routine)]


@router.post("/tests", status_code=201)
def add_test(server_id: str, db_id: str, body: TestIn):
    get_state().store.get_database(server_id, db_id)
    t = TestCase(server_id=server_id, database_id=db_id, **body.model_dump())
    return get_store().add(t).model_dump()


@router.put("/tests/{test_id}")
def update_test(server_id: str, db_id: str, test_id: str, body: TestUpdate):
    changes = body.model_dump(exclude_none=True)
    if body.expect is not None:
        changes["expect"] = body.expect.model_dump()
    return get_store().update(server_id, db_id, test_id, changes).model_dump()


@router.delete("/tests/{test_id}", status_code=204)
def delete_test(server_id: str, db_id: str, test_id: str):
    get_store().delete(server_id, db_id, test_id)


@router.post("/tests/run")
def run_tests(server_id: str, db_id: str, body: RunTestsIn):
    store = get_store()
    tests = store.list(server_id, db_id, body.otype, body.routine)
    if body.ids is not None:
        tests = [t for t in tests if t.id in set(body.ids)]
    results = []
    infos: dict[tuple[str, str], dict[str, Any] | Exception] = {}
    with _dedicated(server_id, db_id) as conn:
        for t in tests:
            key = (t.otype, t.routine)
            if key not in infos:
                try:
                    infos[key] = runner.routine(Catalog(conn), t.otype, t.routine)
                except (LookupError, ValueError) as exc:
                    infos[key] = exc
                conn.rollback()
            info = infos[key]
            if isinstance(info, Exception):
                r = {"id": t.id, "status": "error", "diffs": [str(info)], "run": None, "elapsed_ms": 0}
            else:
                r = runner.run_test(conn, info, t.model_dump())
            r.update(name=t.name, otype=t.otype, routine=t.routine)
            store.record(t.id, r["status"])
            results.append(r)
    summary = {s: sum(1 for r in results if r["status"] == s) for s in ("pass", "fail", "error")}
    return {"results": results, "summary": summary, "rolled_back": True}
