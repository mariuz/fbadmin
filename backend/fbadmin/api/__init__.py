"""API routers. Feature modules add themselves to ROUTERS (one line each, to merge cleanly)."""

from fastapi import APIRouter

from . import databases, jobs, query, servers
from . import maintenance, schedules, trace

ROUTERS: list[APIRouter] = [
    servers.router,
    databases.router,
    query.router,
    jobs.router,
    maintenance.router,
    trace.router,
    schedules.router,
]
