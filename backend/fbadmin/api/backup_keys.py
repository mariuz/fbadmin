"""Backup encryption keys (roadmap 2.2): list, create, add, choose, remove. Administrators only.

The key text is returned only when a key is created and on an explicit export: it is the recovery key that
decrypts the backups without fbadmin (``fbadmin backup-decrypt``), so it belongs in a password manager.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from ..maintenance import archive, backupcrypt
from ..security.core import require_admin
from .deps import get_state

router = APIRouter(prefix="/api/backup-keys", tags=["backup-keys"])


def _audit(what: str) -> None:
    try:
        get_state().security.audit.record(server_id=None, db_id=None, statement=f"-- backup keys: {what}",
                                          success=True, error=None, cat="ACTION")
    except Exception:  # noqa: BLE001
        pass


def _usage() -> dict[str, int]:
    used: dict[str, int] = {}
    for b in archive.list_backups():
        if b.get("key_id"):
            used[b["key_id"]] = used.get(b["key_id"], 0) + 1
    return used


@router.get("")
def list_keys():
    require_admin()
    ring = backupcrypt.keyring()
    used = _usage()
    keys = [{**k, "archived_backups": used.get(k["id"], 0)} for k in ring.list()]
    missing = sorted(set(used) - {k["id"] for k in keys})
    return {"keys": keys, "active": ring.active_id(), "missing": [{"id": m, "archived_backups": used[m]} for m in missing]}


@router.post("", status_code=201)
def create_key():
    require_admin()
    text = backupcrypt.keyring().generate()
    kid = backupcrypt.key_id(backupcrypt.parse_key(text))
    _audit(f"created key {kid}")
    return {"id": kid, "key": text, "active": backupcrypt.keyring().active_id() == kid}


class KeyIn(BaseModel):
    key: str
    activate: bool = False


@router.post("/import", status_code=201)
def import_key(body: KeyIn):
    require_admin()
    kid = backupcrypt.keyring().add(body.key, activate=body.activate)
    _audit(f"added key {kid}")
    return {"id": kid}


@router.post("/{key_id}/activate")
def activate(key_id: str):
    require_admin()
    backupcrypt.keyring().activate(key_id)
    _audit(f"key {key_id} encrypts new backups")
    return {"active": backupcrypt.keyring().active_id()}


@router.post("/{key_id}/export")
def export(key_id: str):
    require_admin()
    text = backupcrypt.keyring().export(key_id)
    _audit(f"exported key {key_id}")
    return {"id": key_id, "key": text}


@router.delete("/{key_id}", status_code=204)
def remove(key_id: str, force: bool = False):
    """A key that archived backups still need is only removed with ``force``: they can't be read without it."""
    require_admin()
    n = _usage().get(key_id, 0)
    if n and not force:
        raise HTTPException(409, f"{n} archived backup(s) are encrypted with this key: without it they can't be "
                                 "restored. Export it first, or remove it anyway.")
    backupcrypt.keyring().remove(key_id)
    _audit(f"removed key {key_id}")
