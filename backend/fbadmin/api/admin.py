"""fbadmin user administration and the audit log viewer (administrators only)."""

from __future__ import annotations

from fastapi import APIRouter, Query
from pydantic import BaseModel

from ..security.core import require_admin
from ..security.errors import SecurityError
from .deps import get_state

router = APIRouter(prefix="/api/admin", tags=["admin"])


class UserIn(BaseModel):
    username: str
    email: str | None = None
    password: str | None = None
    role: str = "user"
    active: bool = True
    auth_source: str = "internal"


class UserUpdate(BaseModel):
    email: str | None = None
    password: str | None = None
    role: str | None = None
    active: bool | None = None


def _server_mode_admin():
    p = require_admin()
    if p is None:
        raise SecurityError(400, "User accounts exist only in server mode", "desktop_mode")
    return p


@router.get("/users")
def list_users():
    _server_mode_admin()
    return get_state().security.db.list_users()


@router.post("/users", status_code=201)
def create_user(body: UserIn):
    _server_mode_admin()
    sec = get_state().security
    if body.auth_source not in ("internal", "ldap", "proxy") and not body.auth_source.startswith("oidc:"):
        raise SecurityError(400, f"Unknown authentication source {body.auth_source!r}", "bad_source")
    user = sec.create_user(body.username, email=body.email, password=body.password, role=body.role,
                           auth_source=body.auth_source, active=body.active)
    return {k: v for k, v in user.items() if k not in ("password_hash", "vault")}


@router.put("/users/{user_id}")
def update_user(user_id: int, body: UserUpdate):
    p = _server_mode_admin()
    user = get_state().security.update_user(user_id, body.model_dump(exclude_unset=True), p)
    return {k: v for k, v in user.items() if k not in ("password_hash", "vault")}


@router.delete("/users/{user_id}", status_code=204)
def delete_user(user_id: int):
    p = _server_mode_admin()
    get_state().security.delete_user(user_id, p)


@router.get("/audit")
def audit(
    limit: int = Query(100, ge=1, le=1000),
    offset: int = Query(0, ge=0),
    username: str | None = None,
    server: str | None = None,
    q: str | None = None,
    success: bool | None = None,
    since: float | None = None,
    until: float | None = None,
    category: str | None = None,
):
    require_admin()
    sec = get_state().security
    out = sec.db.query_audit(limit=limit, offset=offset, username=username, server=server, text=q,
                             success=success, since=since, until=until, category=category) if sec.db.exists else {"total": 0, "rows": []}
    return {**out, "enabled": sec.audit.enabled}
