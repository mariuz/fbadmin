"""Two-factor sign-in, API tokens and finer permissions (server mode, milestone 2.0).

See docs/PERMISSIONS.md and the modules ``security/mfa.py``, ``security/tokens.py`` and
``security/access.py``.
"""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Request, Response
from pydantic import BaseModel

from ..security.access import LEVEL_LABELS, LEVELS
from ..security.core import require_admin
from ..security.errors import LoginRequired, SecurityError
from ..security.sessions import Principal, current_principal
from .auth import _login_response
from .deps import get_state

router = APIRouter(tags=["access"])


# ------------------------------------------------------------------ helpers
def _server_mode() -> None:
    if not get_state().security.server_mode:
        raise SecurityError(400, "Two-factor sign-in, API tokens and permissions exist only in server mode",
                            "desktop_mode")


def _me() -> tuple[Principal, dict]:
    _server_mode()
    p = current_principal.get()
    if p is None:
        raise LoginRequired()
    user = get_state().security.db.get_user(p.user_id)
    if user is None:
        raise LoginRequired()
    return p, user


def _admin() -> Principal:
    _server_mode()
    p = require_admin()
    assert p is not None
    return p


def _ip(request: Request) -> str | None:
    return request.client.host if request.client else None


class CodeIn(BaseModel):
    code: str


class MfaLoginIn(BaseModel):
    mfa_token: str
    code: str


# ------------------------------------------------------------------ sign-in with a second factor
@router.post("/api/auth/login/mfa")
def login_mfa(body: MfaLoginIn, request: Request, response: Response):
    """Second step of a sign-in: a code from the authenticator app or a recovery code."""
    sec = get_state().security
    _server_mode()
    ch = sec.mfa.take(body.mfa_token, "verify")
    user = sec.db.get_user(ch.user_id)
    if user is None or not user["active"]:
        raise SecurityError(403, "This account is disabled", "account_disabled")
    how = sec.mfa.check_code(user, body.code, _ip(request))
    sec.mfa.done(body.mfa_token)
    out = _login_response(request, response, sec.finish_login(user, ch.password))
    return {**out, "mfa_method": how}


@router.post("/api/auth/login/mfa-setup")
def login_mfa_setup(body: MfaLoginIn, request: Request, response: Response):
    """Sign-in of a user who must use 2FA but has not enrolled yet: confirm the new secret."""
    sec = get_state().security
    _server_mode()
    ch = sec.mfa.take(body.mfa_token, "enrol")
    user = sec.db.get_user(ch.user_id)
    if user is None or not user["active"]:
        raise SecurityError(403, "This account is disabled", "account_disabled")
    codes = sec.mfa.enrol_at_login(ch, user, body.code)
    sec.mfa.done(body.mfa_token)
    out = _login_response(request, response, sec.finish_login(user, ch.password))
    return {**out, "recovery_codes": codes}


# ------------------------------------------------------------------ my two-factor settings
@router.get("/api/auth/2fa")
def mfa_status():
    _, user = _me()
    return get_state().security.mfa.status(user)


@router.post("/api/auth/2fa/setup")
def mfa_setup():
    """Start enrolment: a new secret (shown as a QR code and as text) that is active only after /enable."""
    _, user = _me()
    return get_state().security.mfa.begin(user)


@router.post("/api/auth/2fa/enable")
def mfa_enable(body: CodeIn):
    _, user = _me()
    mfa = get_state().security.mfa
    codes = mfa.confirm(user, body.code)
    return {**mfa.status(user), "recovery_codes": codes}


@router.post("/api/auth/2fa/disable")
def mfa_disable(body: CodeIn, request: Request):
    _, user = _me()
    mfa = get_state().security.mfa
    mfa.disable(user, body.code, _ip(request))
    return mfa.status(user)


@router.post("/api/auth/2fa/recovery-codes")
def mfa_recovery_codes(body: CodeIn, request: Request):
    """New recovery codes (the old ones stop working); needs a current code."""
    _, user = _me()
    mfa = get_state().security.mfa
    if not mfa.status(user)["enabled"]:
        raise SecurityError(400, "Two-factor authentication is not enabled", "mfa_disabled")
    mfa.check_code(user, body.code, _ip(request))
    return {**mfa.status(user), "recovery_codes": mfa.new_recovery_codes(user["id"])}


# ------------------------------------------------------------------ my access and API tokens
@router.get("/api/access/me")
def my_access():
    """Effective rights of the signed-in user (or of the API token used for this request)."""
    st = get_state()
    sec = st.security
    if not sec.server_mode:
        return {"server_mode": False, "restricted": False, "cap": "admin", "token_servers": None, "servers": [],
                "levels": LEVELS, "level_labels": LEVEL_LABELS}
    p = current_principal.get()
    if p is None:
        raise LoginRequired()
    from ..security.servers import UserScopedStore

    acc = sec.access.for_principal(p)
    servers = UserScopedStore.list(st.store)  # unfiltered: effective() applies the rights
    out = sec.access.effective(acc, servers)
    return {**out, "server_mode": True, "admin": p.is_admin, "token": p.token_name, "levels": LEVELS,
            "level_labels": LEVEL_LABELS}


class TokenIn(BaseModel):
    name: str
    scope: Literal["read-only", "read-write"] = "read-only"
    expires_days: int | None = 90
    server_ids: list[str] | None = None


@router.get("/api/access/me/tokens")
def my_tokens():
    p, _ = _me()
    return get_state().security.tokens.list_for(p.user_id)


@router.post("/api/access/me/tokens", status_code=201)
def create_token(body: TokenIn):
    p, _ = _me()
    st = get_state()
    if body.server_ids:
        for sid in body.server_ids:
            st.store.get(sid)  # KeyError (404) for servers the user cannot see
    store = st.store.user_store(p.user_id)
    info, token = st.security.tokens.create(p, body.name, scope=body.scope, expires_days=body.expires_days,
                                            server_ids=body.server_ids, vault_key=store.key)
    return {**info, "token": token, "vault": store.key is not None}


@router.delete("/api/access/me/tokens/{token_id}")
def revoke_my_token(token_id: int):
    p, _ = _me()
    tokens = get_state().security.tokens
    if tokens.get(token_id)["user_id"] != p.user_id:
        raise KeyError(f"Unknown token {token_id}")
    return tokens.revoke(token_id)


# ------------------------------------------------------------------ administrators: tokens
@router.get("/api/admin/tokens")
def all_tokens():
    _admin()
    return get_state().security.tokens.list_all()


@router.delete("/api/admin/tokens/{token_id}")
def admin_revoke_token(token_id: int):
    _admin()
    return get_state().security.tokens.revoke(token_id)


# ------------------------------------------------------------------ administrators: 2FA
class MfaPolicyIn(BaseModel):
    require_all: bool


class MfaUserIn(BaseModel):
    required: bool


@router.get("/api/admin/2fa")
def mfa_overview():
    _admin()
    sec = get_state().security
    users = sec.mfa.users_overview()
    return {"require_all": sec.mfa.require_all,
            "users": [{"id": u["id"], "username": u["username"], "auth_source": u["auth_source"],
                       "available": u["auth_source"] in ("internal", "ldap"),
                       "enabled": users.get(u["id"], {}).get("enabled", False),
                       "required": users.get(u["id"], {}).get("required", False)}
                      for u in sec.db.list_users()]}


@router.put("/api/admin/2fa")
def mfa_policy(body: MfaPolicyIn):
    _admin()
    get_state().security.mfa.set_require_all(body.require_all)
    return mfa_overview()


@router.put("/api/admin/users/{user_id}/2fa")
def mfa_user(user_id: int, body: MfaUserIn):
    _admin()
    sec = get_state().security
    if sec.db.get_user(user_id) is None:
        raise KeyError(f"Unknown user {user_id}")
    sec.mfa.set_required(user_id, body.required)
    return mfa_overview()


@router.delete("/api/admin/users/{user_id}/2fa")
def mfa_reset(user_id: int):
    """Lost phone: remove the user's authenticator secret and recovery codes."""
    _admin()
    sec = get_state().security
    if sec.db.get_user(user_id) is None:
        raise KeyError(f"Unknown user {user_id}")
    sec.mfa.reset(user_id)
    sec.audit.record(server_id=None, db_id=None, statement=f"-- reset two-factor authentication of user #{user_id}",
                     success=True, cat="ACTION")
    return mfa_overview()


# ------------------------------------------------------------------ administrators: permissions
class GrantIn(BaseModel):
    subject_type: Literal["user", "group"]
    subject: str
    server_id: str = "*"
    db_id: str | None = None
    object_pattern: str | None = None
    level: Literal["none", "read", "write", "ddl", "admin"]
    note: str | None = None


class GrantUpdate(BaseModel):
    server_id: str | None = None
    db_id: str | None = None
    object_pattern: str | None = None
    level: Literal["none", "read", "write", "ddl", "admin"] | None = None
    note: str | None = None


class GroupIn(BaseModel):
    description: str | None = None
    members: list[int] = []


class AccessSettingsIn(BaseModel):
    default: Literal["full", "none"]


@router.get("/api/admin/access")
def access_overview():
    _admin()
    ac = get_state().security.access
    return {"default": ac.default, "grants": ac.list_grants(), "groups": ac.list_groups(), "levels": LEVELS,
            "level_labels": LEVEL_LABELS}


@router.put("/api/admin/access/settings")
def access_settings(body: AccessSettingsIn):
    _admin()
    get_state().security.access.set_default(body.default)
    return access_overview()


@router.post("/api/admin/access/grants", status_code=201)
def add_grant(body: GrantIn):
    _admin()
    return get_state().security.access.add_grant(body.model_dump())


@router.put("/api/admin/access/grants/{grant_id}")
def update_grant(grant_id: int, body: GrantUpdate):
    _admin()
    return get_state().security.access.update_grant(grant_id, body.model_dump(exclude_unset=True))


@router.delete("/api/admin/access/grants/{grant_id}", status_code=204)
def delete_grant(grant_id: int):
    _admin()
    get_state().security.access.delete_grant(grant_id)


@router.put("/api/admin/access/groups/{name}")
def save_group(name: str, body: GroupIn):
    _admin()
    return get_state().security.access.save_group(name, body.description, body.members)


@router.delete("/api/admin/access/groups/{name}", status_code=204)
def delete_group(name: str):
    _admin()
    get_state().security.access.delete_group(name)


@router.get("/api/admin/access/effective/{user_id}")
def effective_for_user(user_id: int):
    """What a user may do on the shared servers (an administrator cannot see users' own servers)."""
    _admin()
    st = get_state()
    sec = st.security
    user = sec.db.get_user(user_id)
    if user is None:
        raise KeyError(f"Unknown user {user_id}")
    acc = sec.access.for_user(user_id, user["role"])
    return {**sec.access.effective(acc, st.store.shared.list()), "username": user["username"],
            "admin": user["role"] == "admin", "levels": LEVELS, "level_labels": LEVEL_LABELS}
