"""API routers. Feature modules add themselves to ROUTERS (one line each, to merge cleanly)."""

from fastapi import APIRouter

from . import databases, jobs, query, servers
from . import admin, auth, fbsecurity  # security / server mode (0.4)

ROUTERS: list[APIRouter] = [
    servers.router,
    databases.router,
    query.router,
    jobs.router,
    auth.router,  # 0.4
    admin.router,  # 0.4
    fbsecurity.router,  # 0.4
]
