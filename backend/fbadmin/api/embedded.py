"""Local databases opened with the Firebird embedded engine (desktop app, no server needed).

Registered databases end up under one server entry with ``embedded=True`` ("Local
databases"); after that every existing endpoint (tree, DDL, data, Query Tool, backup and
restore, gstat...) works on them through that entry. See fbadmin/embedded.py.
"""

from __future__ import annotations

import os

from fastapi import APIRouter
from pydantic import BaseModel

from .. import embedded
from ..store import DatabaseEntry, ServerEntry
from .deps import get_state

router = APIRouter(prefix="/api/embedded", tags=["embedded"])


class LocalDatabaseIn(BaseModel):
    path: str
    name: str | None = None
    create: bool = False  # create a new database file (otherwise open an existing one)
    page_size: int = 16384
    charset: str = "UTF8"  # default character set of a new database


def ensure_server() -> ServerEntry:
    """The "Local databases" registration (created on first use)."""
    store = get_state().store
    for srv in store.list():
        if srv.embedded:
            return srv
    return store.add(ServerEntry(name=embedded.SERVER_NAME, host="", port=0, user=embedded.EMBEDDED_USER,
                                 save_password=False, wire_crypt=False, group=embedded.SERVER_GROUP,
                                 embedded=True))


@router.get("/status")
def status():
    st = embedded.status()
    srv = next((s for s in get_state().store.list() if s.embedded), None)
    st["server_id"] = srv.id if srv else None
    return st


@router.post("/databases", status_code=201)
def add_local_database(body: LocalDatabaseIn):
    """Create a new local database file or open an existing one, and register it."""
    embedded.require_available()
    path = embedded.check_local_path(body.path)
    if body.page_size not in (4096, 8192, 16384, 32768):
        raise ValueError("Page size must be 4096, 8192, 16384 or 32768")
    srv = ensure_server()
    for db in srv.databases:
        if os.path.normcase(db.path) == os.path.normcase(path):
            if body.create:
                raise ValueError(f"{path} is already registered as {db.name!r}")
            return {"server_id": srv.id, "database": db.model_dump(), "created": False}
    conn = embedded.connect_local(path, charset=body.charset, create=body.create, page_size=body.page_size)
    conn.close()  # opening it checks that it is a Firebird database no other process holds
    name = (body.name or "").strip() or os.path.splitext(os.path.basename(path))[0] or path
    db = get_state().store.add_database(srv.id, DatabaseEntry(name=name, path=path))
    return {"server_id": srv.id, "database": db.model_dump(), "created": body.create}
