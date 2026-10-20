"""Query library API: saved queries and snippets, private or shared with every fbadmin user."""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Query
from pydantic import BaseModel, Field

from ..library import Actor, Visibility, get_store
from .deps import get_state

router = APIRouter(prefix="/api/library", tags=["library"])


class LibraryItemIn(BaseModel):
    name: str
    sql: str
    description: str = ""
    tags: list[str] = Field(default_factory=list)
    folder: str = ""
    server_id: str | None = None
    database_id: str | None = None
    visibility: Visibility = "private"


class LibraryItemUpdate(BaseModel):
    name: str | None = None
    sql: str | None = None
    description: str | None = None
    tags: list[str] | None = None
    folder: str | None = None
    server_id: str | None = None
    database_id: str | None = None
    visibility: Visibility | None = None


class LibraryCopyIn(BaseModel):
    name: str | None = None


class LibrarySnippet(BaseModel):
    name: str
    sql: str
    description: str | None = None
    tags: list[str] | None = None
    folder: str | None = None


class LibraryImportIn(BaseModel):
    items: list[LibrarySnippet]
    folder: str = "Snippets"


def actor() -> Actor:
    """The fbadmin user of this request (desktop mode: the only user)."""
    sec = getattr(get_state(), "security", None)
    if sec is None or not sec.server_mode:
        return Actor()
    from ..security.errors import LoginRequired
    from ..security.sessions import current_principal

    p = current_principal.get()
    if p is None:
        raise LoginRequired()
    return Actor(user_id=p.user_id, username=p.username, is_admin=p.is_admin)


def _set(body: BaseModel) -> dict:
    data = body.model_dump(exclude_unset=True)
    # explicit nulls clear the default database; other nulls mean "unchanged"
    return {k: v for k, v in data.items() if v is not None or k in ("server_id", "database_id")}


@router.get("")
def library_info():
    who = actor()
    return {"server_mode": not who.desktop, "user": who.username, "is_admin": who.is_admin,
            **get_store().summary(who)}


@router.get("/items")
def list_items(q: str | None = None, tag: str | None = None, folder: str | None = None,
               scope: Literal["all", "mine", "shared"] = "all", server_id: str | None = None,
               database_id: str | None = None, limit: int = Query(500, ge=1, le=5000)):
    who = actor()
    st = get_store()
    items = st.search(who, q=q, tag=tag, folder=folder, scope=scope, server_id=server_id, database_id=database_id)
    return [st.view(i, who) for i in items[:limit]]


@router.post("/items", status_code=201)
def create_item(body: LibraryItemIn):
    who = actor()
    st = get_store()
    return st.view(st.create(body.model_dump(), who), who)


@router.post("/import")
def import_snippets(body: LibraryImportIn):
    """Import snippets kept in the browser by older fbadmin versions (skips ones already imported)."""
    who = actor()
    n = get_store().import_items([i.model_dump() for i in body.items], who, folder=body.folder)
    return {"imported": n}


@router.get("/items/{item_id}")
def get_item(item_id: str):
    who = actor()
    st = get_store()
    return st.view(st.get(item_id, who), who, with_revisions=True)


@router.put("/items/{item_id}")
def update_item(item_id: str, body: LibraryItemUpdate):
    who = actor()
    st = get_store()
    return st.view(st.update(item_id, _set(body), who), who)


@router.delete("/items/{item_id}", status_code=204)
def delete_item(item_id: str):
    get_store().delete(item_id, actor())


@router.post("/items/{item_id}/copy", status_code=201)
def copy_item(item_id: str, body: LibraryCopyIn | None = None):
    who = actor()
    st = get_store()
    return st.view(st.copy(item_id, who, body.name if body else None), who)


@router.get("/items/{item_id}/revisions")
def revisions(item_id: str):
    who = actor()
    it = get_store().get(item_id, who)
    return [r.model_dump() for r in reversed(it.revisions)]


@router.post("/items/{item_id}/revisions/{rev}/restore")
def restore_revision(item_id: str, rev: int):
    who = actor()
    st = get_store()
    return st.view(st.restore(item_id, rev, who), who)
