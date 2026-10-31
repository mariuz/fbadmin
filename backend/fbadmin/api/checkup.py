"""Guided performance check-up (roadmap 2.3): one ranked "why is it slow?" report per database."""

from __future__ import annotations

from fastapi import APIRouter
from pydantic import BaseModel, Field

from ..monitoring import checkup
from ..serialize import to_json
from .deps import get_state

DB = "/api/servers/{server_id}/databases/{db_id}/monitoring/checkup"
router = APIRouter(tags=["monitoring"])


class CheckupIn(BaseModel):
    gstat: bool = True  # also read the database with gstat (record versions, actual index selectivity)


class IndexRef(BaseModel):
    name: str = Field(min_length=1, max_length=252)
    schema_name: str | None = Field(default=None, alias="schema", max_length=252)


class RecomputeIn(BaseModel):
    indices: list[IndexRef] = Field(min_length=1, max_length=10000)


@router.get(DB)
def last_report(server_id: str, db_id: str):
    """The last check-up of this database (kept in memory) and the running or last check-up job."""
    get_state().store.get_database(server_id, db_id)
    return to_json(checkup.last(server_id, db_id))


@router.post(DB, status_code=202)
def run_checkup(server_id: str, db_id: str, body: CheckupIn | None = None):
    """Start a check-up job; poll ``GET .../checkup`` (or the job) for the report."""
    return checkup.start(server_id, db_id, with_gstat=(body or CheckupIn()).gstat)


@router.post(DB + "/statistics", status_code=202)
def recompute_statistics(server_id: str, db_id: str, body: RecomputeIn):
    """Fix action: SET STATISTICS INDEX for the given indices, as a job."""
    st = get_state()
    db = st.store.get_database(server_id, db_id)
    items = [{"name": i.name, "schema": i.schema_name} for i in body.indices]

    def run(log):
        with st.manager.meta(server_id, db_id) as conn:
            done = checkup.recompute(conn, items, log=log)
        log(f"Done: {len(done)} index(es)")

    return st.jobs.start("selectivity", f"Recompute index statistics {db.name}", server_id, run).summary()
