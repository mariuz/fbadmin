from __future__ import annotations

from fastapi import APIRouter

from .deps import get_state

router = APIRouter(prefix="/api/jobs", tags=["jobs"])


@router.get("")
def list_jobs():
    return [j.summary() for j in get_state().jobs.list()]


@router.get("/{job_id}")
def get_job(job_id: str):
    return get_state().jobs.get(job_id).summary(with_log=True)
