"""Cloud storage targets for backups (S3, Azure Blob Storage, Google Cloud Storage).

Targets hold cloud credentials, so in server mode only administrators may manage them.
Secret settings are never returned; send ``********`` (or omit it) to keep a stored secret.
"""

from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from ..security.core import require_admin
from ..storage import StorageError, Target, get_store, object_prefix, open_client

router = APIRouter(prefix="/api/storage", tags=["storage"])


class TargetIn(BaseModel):
    name: str
    kind: Literal["s3", "azure", "gcs"]
    settings: dict[str, Any] = {}


class TargetUpdate(BaseModel):
    name: str | None = None
    kind: Literal["s3", "azure", "gcs"] | None = None
    settings: dict[str, Any] | None = None


def _provider_call(fn):
    """Run a call to the provider; its errors become 502 with the provider's message."""
    try:
        return fn()
    except StorageError as e:
        raise HTTPException(status_code=502, detail=str(e)) from e


@router.get("/targets")
def list_targets():
    require_admin()
    return get_store().list()


@router.post("/targets", status_code=201)
def create_target(body: TargetIn):
    require_admin()
    store = get_store()
    return store.view(store.add(Target(name=body.name, kind=body.kind, settings=body.settings)))


@router.get("/targets/{target_id}")
def get_target(target_id: str):
    require_admin()
    store = get_store()
    return store.view(store.get(target_id))


@router.put("/targets/{target_id}")
def update_target(target_id: str, body: TargetUpdate):
    require_admin()
    store = get_store()
    return store.view(store.update(target_id, body.model_dump(exclude_unset=True)))


@router.delete("/targets/{target_id}", status_code=204)
def delete_target(target_id: str):
    require_admin()
    get_store().remove(target_id)


@router.post("/targets/{target_id}/test")
def test_target(target_id: str):
    require_admin()
    client = open_client(get_store().get(target_id))
    return _provider_call(client.test)


@router.get("/targets/{target_id}/objects")
def list_objects(target_id: str, prefix: str = ""):
    """Objects under the target's prefix (plus ``prefix``), newest first."""
    require_admin()
    t = get_store().get(target_id)
    client = open_client(t)
    p = object_prefix(t, prefix)
    return _provider_call(lambda: client.list(p + "/" if p else ""))
