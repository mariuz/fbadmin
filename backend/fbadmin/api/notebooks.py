"""SQL notebooks API (roadmap 2.3): SQL cells, notes, results and charts in one saved document.

Cells are run by the client through the Query Tool's sessions (``/api/query/sessions``), so every
statement is checked against fbadmin's permissions there; this API stores the document.
"""

from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, Query
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, Field

from ..library import Visibility
from ..notebooks import Cell, Conflict, get_store, to_markdown
from .library import actor

router = APIRouter(prefix="/api/notebooks", tags=["notebooks"])


class NotebookIn(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    description: str = ""
    server_id: str | None = None
    database_id: str | None = None
    visibility: Visibility = "private"
    cells: list[Cell] | None = None


class NotebookUpdate(BaseModel):
    base_rev: int | None = None  # the revision the editor loaded: 409 if someone saved since
    name: str | None = None
    description: str | None = None
    server_id: str | None = None
    database_id: str | None = None
    visibility: Visibility | None = None
    cells: list[Cell] | None = None


class CopyIn(BaseModel):
    name: str | None = None


class ImportIn(BaseModel):
    notebook: dict[str, Any]
    server_id: str | None = None
    database_id: str | None = None


def writer():
    """The fbadmin user of a request that changes notebooks; read-only API tokens may only read them.
    (Notebooks are listed in access._GLOBAL_ALLOWED: they belong to users, not to a database.)"""
    who = actor()
    if not who.desktop:
        from ..security.access import WRITE
        from ..security.errors import AccessDenied
        from .deps import get_state

        acc = get_state().security.access.current()
        if not acc.free and acc.cap < WRITE:
            raise AccessDenied("This API token is read-only")
    return who


def _conflict(exc: Conflict):
    from ..notebooks import NotebookStore

    return JSONResponse(status_code=409, content={"detail": str(exc),
                                                  "notebook": NotebookStore.view(exc.notebook, actor())})


@router.get("")
def list_notebooks(q: str | None = None, scope: Literal["all", "mine", "shared"] = "all",
                   server_id: str | None = None, database_id: str | None = None):
    who = actor()
    st = get_store()
    return {"server_mode": not who.desktop, "user": who.username,
            "notebooks": [st.view(n, who, full=False) for n in st.search(who, q, scope, server_id, database_id)]}


@router.post("", status_code=201)
def create_notebook(body: NotebookIn):
    who = writer()
    data = body.model_dump(exclude_none=True)
    return get_store().view(get_store().create(data, who), who)


@router.post("/import", status_code=201)
def import_notebook(body: ImportIn):
    who = writer()
    return get_store().view(get_store().import_(body.notebook, who, body.server_id, body.database_id), who)


@router.get("/{nb_id}")
def get_notebook(nb_id: str):
    who = actor()
    return get_store().view(get_store().get(nb_id, who), who)


@router.put("/{nb_id}")
def update_notebook(nb_id: str, body: NotebookUpdate):
    who = writer()
    data = body.model_dump(exclude_unset=True)
    base = data.pop("base_rev", None)
    changes = {k: v for k, v in data.items() if v is not None or k in ("server_id", "database_id")}
    if "cells" in changes:
        changes["cells"] = body.cells
    try:
        return get_store().view(get_store().update(nb_id, changes, who, base_rev=base), who)
    except Conflict as exc:
        return _conflict(exc)


@router.delete("/{nb_id}", status_code=204)
def delete_notebook(nb_id: str):
    get_store().delete(nb_id, writer())
    return Response(status_code=204)


@router.post("/{nb_id}/copy", status_code=201)
def copy_notebook(nb_id: str, body: CopyIn | None = None):
    who = writer()
    return get_store().view(get_store().copy(nb_id, who, (body or CopyIn()).name), who)


@router.get("/{nb_id}/revisions")
def revisions(nb_id: str):
    nb = get_store().get(nb_id, actor())
    return {"revisions": [{"rev": r.rev, "at": r.at, "author": r.author, "name": r.name, "cells": len(r.cells)}
                          for r in reversed(nb.revisions)]}


@router.get("/{nb_id}/revisions/{rev}")
def revision(nb_id: str, rev: int):
    nb = get_store().get(nb_id, actor())
    r = next((x for x in nb.revisions if x.rev == rev), None)
    if r is None:
        raise KeyError(f"Revision {rev} is no longer kept")
    return r.model_dump()


@router.post("/{nb_id}/revisions/{rev}/restore")
def restore(nb_id: str, rev: int):
    who = writer()
    return get_store().view(get_store().restore(nb_id, rev, who), who)


@router.get("/{nb_id}/export")
def export(nb_id: str, format: Literal["json", "md"] = Query("json"), results: bool = True):
    nb = get_store().get(nb_id, actor())
    safe = "".join(ch if ch.isalnum() or ch in "-_ " else "_" for ch in nb.name).strip() or "notebook"
    if format == "md":
        return Response(to_markdown(nb), media_type="text/markdown; charset=utf-8",
                        headers={"Content-Disposition": f'attachment; filename="{safe}.md"'})
    return JSONResponse(get_store().export(nb, with_results=results),
                        headers={"Content-Disposition": f'attachment; filename="{safe}.fbnb.json"'})
