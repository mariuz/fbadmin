"""Comments on objects and notebooks (roadmap 2.4; fbadmin/comments.py)."""

from __future__ import annotations

from fastapi import APIRouter, Query
from pydantic import BaseModel, Field

from .. import comments as C
from ..notebook_runs import actor
from ..security.errors import AccessDenied

router = APIRouter(prefix="/api/comments", tags=["comments"])


class CommentIn(C.Target):
    text: str = Field(min_length=1, max_length=C.MAX_TEXT)


class TextIn(BaseModel):
    text: str = Field(min_length=1, max_length=C.MAX_TEXT)


@router.get("")
def thread(kind: str = "object", server_id: str | None = None, database_id: str | None = None,
           otype: str | None = None, name: str | None = None, schema: str | None = None,
           notebook_id: str | None = None):
    """The comments of one object or notebook, oldest first."""
    who = actor()
    t = C.Target(kind=kind, server_id=server_id, database_id=database_id, otype=otype, name=name, schema=schema,
                 notebook_id=notebook_id)
    C.check(t, who)
    return [C.view(c, who) for c in C.get_store().thread(t.key())]


@router.get("/counts")
def counts(server_id: str, database_id: str, limit: int = Query(500, ge=1, le=5000)):
    """Objects of a database that have comments (the ones this user may see), most recent first."""
    from .deps import get_state
    from ..links import may_read

    get_state().store.get_database(server_id, database_id)
    out: dict[str, dict] = {}
    for c in C.get_store().in_database(server_id, database_id):
        t = c.target
        if not may_read(server_id, database_id, t.name):
            continue
        k = t.key()
        e = out.setdefault(k, {"otype": t.otype, "name": t.name, "schema": t.schema_name, "count": 0, "last_at": 0,
                               "last_text": ""})
        e["count"] += 1
        if c.created_at > e["last_at"]:
            e["last_at"], e["last_text"], e["last_by"] = c.created_at, c.text[:200], c.author
    return sorted(out.values(), key=lambda e: -e["last_at"])[:limit]


@router.post("", status_code=201)
def add(body: CommentIn):
    who = actor()
    t = C.Target(**body.model_dump(by_alias=True, exclude={"text"}))
    C.check(t, who)
    text = body.text.strip()
    if not text:
        raise ValueError("Write the comment")
    c = C.get_store().add(C.Comment(target=t, text=text, author=who.username, author_id=who.user_id))
    C.notify(c)
    return C.view(c, who)


def _mine(cid: str):
    who = actor()
    c = C.get_store().get(cid)
    C.check(c.target, who)
    return c, who


@router.put("/{cid}")
def edit(cid: str, body: TextIn):
    c, who = _mine(cid)
    if not C.may_change(c, who):
        raise AccessDenied("Only its author can edit a comment")
    return C.view(C.get_store().update(cid, body.text.strip()), who)


@router.delete("/{cid}", status_code=204)
def delete(cid: str):
    c, who = _mine(cid)
    if not C.may_delete(c, who):
        raise AccessDenied("Only its author or an fbadmin administrator can delete a comment")
    C.get_store().delete(cid)
