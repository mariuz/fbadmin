"""API routers. Feature modules add themselves to ROUTERS (one line each, to merge cleanly)."""

from fastapi import APIRouter

from . import ai  # noqa: I001 - one import per line keeps parallel branches mergeable
from . import databases
from . import jobs
from . import maintenance
from . import query
from . import schedules
from . import servers
from . import trace

ROUTERS: list[APIRouter] = [
    servers.router,
    databases.router,
    query.router,
    jobs.router,
    ai.router,
    maintenance.router,
    trace.router,
    schedules.router,
]
