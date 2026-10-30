"""Audit log forwarding (roadmap 2.2): destinations (syslog, webhook), their state, a test message. Admins only."""

from __future__ import annotations

import time
from typing import Literal

from fastapi import APIRouter
from pydantic import BaseModel, Field

from ..security import forward as F
from ..security.core import require_admin
from .deps import get_state

router = APIRouter(prefix="/api/admin/audit/forwarding", tags=["admin"])


def _sec():
    return get_state().security


def _audit(what: str) -> None:
    try:
        _sec().audit.record(server_id=None, db_id=None, statement=f"-- audit forwarding: {what}", success=True,
                            cat="SECURITY")
    except Exception:  # noqa: BLE001
        pass


@router.get("")
def list_destinations():
    require_admin()
    sec = _sec()
    newest = sec.db.max_audit_id()
    store = sec.forwarder.store
    return {"enabled": sec.audit.enabled, "newest": newest,
            "destinations": [F.view(d, store.state(d.id), newest) for d in store.destinations()]}


class DestinationIn(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    kind: Literal["syslog", "webhook"]
    enabled: bool = True
    host: str | None = None
    port: int | None = Field(default=None, ge=1, le=65535)
    protocol: Literal["udp", "tcp", "tls"] = "udp"
    facility: str = "local0"
    tls_verify: bool = True
    ca_file: str | None = None
    url: str | None = None
    secret: str | None = None
    token: str | None = None
    categories: list[str] = []
    failures_only: bool = False
    start: Literal["now", "all"] = "now"  # forward only new entries, or the whole log too


@router.post("", status_code=201)
def create(body: DestinationIn):
    require_admin()
    sec = _sec()
    d = F.Destination(**body.model_dump(exclude={"start"}))
    d.check()
    cursor = 0 if body.start == "all" else sec.db.max_audit_id()
    sec.forwarder.store.add(d, cursor)
    _audit(f"added {d.kind} destination {d.name!r}")
    sec.forwarder.notify()
    return F.view(d, sec.forwarder.store.state(d.id), sec.db.max_audit_id())


class DestinationUpdate(BaseModel):
    name: str | None = None
    enabled: bool | None = None
    host: str | None = None
    port: int | None = Field(default=None, ge=1, le=65535)
    protocol: Literal["udp", "tcp", "tls"] | None = None
    facility: str | None = None
    tls_verify: bool | None = None
    ca_file: str | None = None
    url: str | None = None
    secret: str | None = None
    token: str | None = None
    categories: list[str] | None = None
    failures_only: bool | None = None


@router.put("/{dest_id}")
def update(dest_id: str, body: DestinationUpdate):
    require_admin()
    sec = _sec()
    d = sec.forwarder.store.update(dest_id, body.model_dump(exclude_unset=True))
    _audit(f"changed destination {d.name!r}")
    sec.forwarder.retry_now(dest_id)
    return F.view(d, sec.forwarder.store.state(d.id), sec.db.max_audit_id())


@router.delete("/{dest_id}", status_code=204)
def delete(dest_id: str):
    require_admin()
    d = _sec().forwarder.store.get(dest_id)
    _sec().forwarder.store.remove(dest_id)
    _audit(f"removed destination {d.name!r}")


@router.post("/{dest_id}/test")
def test(dest_id: str):
    """Send one test entry now (not from the log), and report what the destination answered."""
    require_admin()
    d = _sec().forwarder.store.get(dest_id)
    e = F.entry({"id": 0, "ts": time.time(), "username": "fbadmin", "category": "OTHER", "success": 1,
                 "statement": "-- fbadmin audit forwarding test message"})
    try:
        return {"ok": True, "result": F.send(d, [e])}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc).strip() or exc.__class__.__name__}


class ResendIn(BaseModel):
    after_id: int = Field(ge=0)  # send the entries after this id again (0: the whole log)


@router.post("/{dest_id}/resend")
def resend(dest_id: str, body: ResendIn):
    require_admin()
    sec = _sec()
    d = sec.forwarder.store.get(dest_id)
    sec.forwarder.store.set_state(dest_id, cursor=min(body.after_id, sec.db.max_audit_id()))
    _audit(f"resend to {d.name!r} after entry {body.after_id}")
    sec.forwarder.retry_now(dest_id)
    return F.view(d, sec.forwarder.store.state(dest_id), sec.db.max_audit_id())
