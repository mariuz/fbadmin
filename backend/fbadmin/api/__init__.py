"""API routers. Feature modules add themselves to ROUTERS (one line each, to merge cleanly)."""

from fastapi import APIRouter

from . import ai, databases, jobs, query, servers

ROUTERS: list[APIRouter] = [
    servers.router,
    databases.router,
    query.router,
    jobs.router,
    ai.router,
]
