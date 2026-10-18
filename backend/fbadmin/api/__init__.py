"""API routers. Feature modules add themselves to ROUTERS (one line each, to merge cleanly)."""

from fastapi import APIRouter

from . import databases, jobs, query, servers
from . import monitoring  # milestone 0.7
from . import metrics  # milestone 0.7
from . import replication  # milestone 0.8

ROUTERS: list[APIRouter] = [
    servers.router,
    databases.router,
    query.router,
    jobs.router,
    monitoring.router,
    monitoring.settings_router,
    metrics.router,
    replication.router,
    replication.conf_router,
]
