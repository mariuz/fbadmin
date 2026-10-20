"""Schema history in git: configuration, snapshots, commit log, diffs and migrations between commits.

In server mode only administrators create, change or remove a configuration (it names a folder on the
fbadmin host and a remote); users who can open the database can take snapshots and read the history.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from .. import schemahistory as sh
from ..security.core import require_admin
from .deps import get_state

router = APIRouter(tags=["schema-history"])
DB = "/api/servers/{server_id}/databases/{db_id}/schema-history"


class ConfigIn(BaseModel):
    repo_path: str
    subdir: str = ""
    branch: str = "main"
    remote_url: str | None = None
    push: bool = True
    token: str | None = None
    token_user: str = "x-access-token"
    committer_name: str = "fbadmin"
    committer_email: str = "fbadmin@localhost"
    after_ddl: bool = False


def _db(server_id: str, db_id: str):
    return get_state().store.get_database(server_id, db_id)  # access check (404 when not visible)


@router.get(DB)
def get_config(server_id: str, db_id: str):
    _db(server_id, db_id)
    c = sh.get_store().find(server_id, db_id)
    return {"status": sh.status(), "config": sh.view(c) if c else None}


@router.put(DB)
def put_config(server_id: str, db_id: str, body: ConfigIn):
    require_admin()
    _db(server_id, db_id)
    c = sh.get_store().put(server_id, db_id, body.model_dump())
    return {"status": sh.status(), "config": sh.view(c)}


@router.delete(DB, status_code=204)
def delete_config(server_id: str, db_id: str):
    """Forget the configuration; the repository folder is left as it is."""
    require_admin()
    _db(server_id, db_id)
    sh.get_store().remove(server_id, db_id)


@router.post(DB + "/snapshot")
def take_snapshot(server_id: str, db_id: str):
    _db(server_id, db_id)
    return sh.snapshot(server_id, db_id, trigger="manual")


@router.post(DB + "/push")
def push(server_id: str, db_id: str):
    _db(server_id, db_id)
    res = sh.push_now(server_id, db_id)
    if not res["ok"]:
        raise HTTPException(status_code=502, detail=res["error"])
    return res


@router.get(DB + "/log")
def log(server_id: str, db_id: str, limit: int = 50, skip: int = 0):
    _db(server_id, db_id)
    return sh.log_entries(server_id, db_id, max(1, min(limit, 500)), skip)


@router.get(DB + "/commits/{sha}")
def commit(server_id: str, db_id: str, sha: str, context: int = 3):
    _db(server_id, db_id)
    return sh.commit_diff(server_id, db_id, sha, context)


@router.get(DB + "/commits/{sha}/files")
def files(server_id: str, db_id: str, sha: str):
    _db(server_id, db_id)
    return sh.files_at(server_id, db_id, sha)


@router.get(DB + "/commits/{sha}/file")
def file(server_id: str, db_id: str, sha: str, path: str):
    _db(server_id, db_id)
    return {"path": path, "content": sh.file_at(server_id, db_id, sha, path)}


@router.get(DB + "/migration")
def migration(server_id: str, db_id: str, from_sha: str, to_sha: str):
    _db(server_id, db_id)
    return sh.migration(server_id, db_id, from_sha, to_sha)
