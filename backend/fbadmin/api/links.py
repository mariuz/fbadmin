"""Shareable links (roadmap 2.3): links to objects, saved queries, notebooks and result snapshots, opened with
the viewer's own permissions (see fbadmin/links.py)."""

from __future__ import annotations

import json
import time
from typing import Any

from fastapi import APIRouter, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from .. import links as L
from .library import actor
from .notebooks import writer

router = APIRouter(prefix="/api/links", tags=["links"])
MAX_SNAPSHOT_BYTES = 5 * 1024 * 1024


class SnapshotIn(BaseModel):
    sql: str = Field(default="", max_length=200_000)
    columns: list[dict[str, Any]] = Field(default_factory=list, max_length=2000)
    rows: list[list[Any]] = Field(default_factory=list)
    row_count: int | None = None
    truncated: bool = False


class LinkIn(BaseModel):
    kind: L.Kind
    title: str = Field(default="", max_length=300)
    server_id: str | None = None
    database_id: str | None = None
    otype: str | None = Field(default=None, max_length=64)
    name: str | None = Field(default=None, max_length=1000)
    schema_name: str | None = Field(default=None, alias="schema", max_length=252)
    item_id: str | None = Field(default=None, max_length=64)
    snapshot: SnapshotIn | None = None
    expires_days: float | None = Field(default=None, gt=0, le=3650)


def _view(lk: L.Link) -> dict[str, Any]:
    d = lk.model_dump(exclude={"snapshot", "owner_id"})
    d["path"] = f"/#/link/{lk.id}"
    if lk.snapshot:
        d["rows"] = len(lk.snapshot.rows)
    return d


@router.post("", status_code=201)
def create_link(body: LinkIn):
    who = writer()
    data = body.model_dump(exclude={"snapshot", "expires_days"}, by_alias=False)
    lk = L.Link(**data)
    if body.kind == "object" and not (body.otype and body.name):
        raise ValueError("An object link needs the object type and name")
    if body.kind in ("query", "notebook") and not body.item_id:
        raise ValueError("A link to a saved query or a notebook needs its id")
    if body.kind == "snapshot":
        if body.snapshot is None or not body.snapshot.columns:
            raise ValueError("A snapshot link needs the result's columns and rows")
        rows = body.snapshot.rows[: L.MAX_ROWS]
        snap = L.Snapshot(**{**body.snapshot.model_dump(), "rows": rows,
                             "truncated": body.snapshot.truncated or len(body.snapshot.rows) > L.MAX_ROWS})
        if len(json.dumps(snap.model_dump(), default=str)) > MAX_SNAPSHOT_BYTES:
            raise ValueError("The result is too large to share (5 MB): share fewer rows or columns")
        lk.snapshot = snap
    if body.expires_days:
        lk.expires_at = time.time() + body.expires_days * 86400
    try:
        L.check_create(lk, who)  # whoever makes a link must be able to open it
    except L.Denied as exc:
        raise ValueError(str(exc)) from None
    return _view(L.get_store().add(lk, who))


@router.get("")
def list_links(all: bool = False):  # noqa: A002 - query parameter name
    who = actor()
    items = L.get_store().all() if all and (who.desktop or who.is_admin) else L.get_store().mine(who)
    return {"links": [_view(lk) for lk in items], "server_mode": not who.desktop}


@router.get("/{link_id}")
def open_link(link_id: str):
    """What the link opens, checked with the permissions of the current user."""
    who = actor()
    lk = L.get_store().get(link_id)
    try:
        out = L.resolve(lk, who)
    except L.Denied as exc:
        return JSONResponse(status_code=403, content={"detail": str(exc), "code": "link_denied",
                                                      "kind": lk.kind, "title": lk.title,
                                                      "created_by": lk.created_by})
    L.get_store().opened(link_id)
    return out


@router.delete("/{link_id}", status_code=204)
def revoke_link(link_id: str):
    L.get_store().revoke(link_id, writer())
    return Response(status_code=204)
