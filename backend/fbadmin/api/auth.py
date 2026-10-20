"""Login, logout, first-run setup, OAuth2/OIDC and the master password (vault)."""

from __future__ import annotations

import urllib.parse

from fastapi import APIRouter, Request, Response
from fastapi.responses import RedirectResponse
from pydantic import BaseModel

from ..security.errors import LoginRequired, SecurityError
from ..security.middleware import SESSION_COOKIE, clear_cookies, is_secure, session_cookies
from ..security.oidc import identity
from ..security.sessions import current_principal
from .deps import get_state

router = APIRouter(prefix="/api/auth", tags=["auth"])


class LoginIn(BaseModel):
    username: str
    password: str


class SetupIn(BaseModel):
    username: str
    email: str | None = None
    password: str


class PasswordChangeIn(BaseModel):
    old_password: str
    new_password: str


class VaultIn(BaseModel):
    password: str


class VaultChangeIn(BaseModel):
    old_password: str
    new_password: str


def _set_cookies(response: Response, headers: list[tuple[bytes, bytes]]) -> None:
    for k, v in headers:
        response.headers.append(k.decode(), v.decode())


def _status(request: Request) -> dict:
    sec = get_state().security
    p = current_principal.get()
    out: dict = {
        "server_mode": sec.server_mode,
        "authenticated": (p is not None) or not sec.server_mode,
        "needs_setup": sec.needs_setup(),
        "auth_sources": [],
        "user": None,
        "audit": sec.audit.enabled,
    }
    if sec.server_mode:
        srcs = sec.settings.auth_sources
        out["auth_sources"] = [{"type": "password", "name": s} for s in srcs if s in ("internal", "ldap")]
        if "oidc" in srcs:
            out["auth_sources"] += [{"type": "oidc", "name": o.name, "title": o.title} for o in sec.settings.oidc]
        if "proxy" in srcs:
            out["auth_sources"].append({"type": "proxy", "name": "proxy"})
    if p is not None:
        user = sec.db.get_user(p.user_id) or {}
        out["user"] = {"id": p.user_id, "username": p.username, "role": p.role, "email": user.get("email"),
                       "auth_source": p.auth_source}
    elif not sec.server_mode:
        out["user"] = {"id": 0, "username": "desktop", "role": "admin", "email": None, "auth_source": "desktop"}
    if out["authenticated"]:
        try:
            out["vault"] = sec.vault_status()
        except SecurityError:
            out["vault"] = None
    return out


@router.get("/status")
def status(request: Request):
    return _status(request)


def _login_response(request: Request, response: Response, principal) -> dict:
    sec = get_state().security
    old = request.cookies.get(SESSION_COOKIE)
    if old:
        sec.end_session(old)
    session = sec.start_session(principal)
    _set_cookies(response, session_cookies(sec, session, is_secure(sec, request.url.scheme)))
    token = current_principal.set(principal)
    try:
        return {**_status(request), "csrf_token": session.csrf}
    finally:
        current_principal.reset(token)


@router.post("/login")
def login(body: LoginIn, request: Request, response: Response):
    sec = get_state().security
    if not sec.server_mode:
        raise SecurityError(400, "fbadmin runs in desktop mode (no login)", "desktop_mode")
    if sec.needs_setup():
        raise SecurityError(409, "Create the first administrator first", "needs_setup")
    srcs = sec.settings.auth_sources
    if "internal" not in srcs and "ldap" not in srcs:
        raise SecurityError(400, "Password login is disabled", "password_login_disabled")
    ip = request.client.host if request.client else None
    user = sec.authenticate_password(body.username, body.password, ip)
    challenge = sec.mfa.challenge(user, body.password, ip)  # two-factor sign-in (security/mfa.py)
    if challenge is not None:
        return {**_status(request), **challenge}
    return _login_response(request, response, sec.finish_login(user, body.password))


@router.post("/setup")
def setup(body: SetupIn, request: Request, response: Response):
    """First run in server mode: create the initial administrator."""
    sec = get_state().security
    if not sec.needs_setup():
        raise SecurityError(409, "Setup has already been completed", "setup_done")
    sec.create_user(body.username, email=body.email, password=body.password, role="admin")
    p = sec.login_password(body.username, body.password, request.client.host if request.client else None)
    return _login_response(request, response, p)


@router.post("/logout")
def logout(request: Request, response: Response):
    sec = get_state().security
    sec.end_session(request.cookies.get(SESSION_COOKIE))
    _set_cookies(response, clear_cookies(is_secure(sec, request.url.scheme)))
    return {"ok": True}


@router.post("/password")
def change_password(body: PasswordChangeIn):
    sec = get_state().security
    p = current_principal.get()
    if p is None:
        raise LoginRequired()
    sec.change_password(p, body.old_password, body.new_password)
    return {"ok": True}


# ------------------------------------------------------------- OAuth2 / OIDC
def _redirect_uri(request: Request, provider: str) -> str:
    sec = get_state().security
    base = sec.settings.public_url or str(request.base_url).rstrip("/")
    return f"{base}/api/auth/oidc/{urllib.parse.quote(provider)}/callback"


@router.get("/oidc/{provider}/login")
def oidc_login(provider: str, request: Request):
    sec = get_state().security
    if not sec.server_mode or "oidc" not in sec.settings.auth_sources:
        raise SecurityError(400, "OAuth2/OIDC login is not enabled", "oidc_disabled")
    p = sec.settings.oidc_provider(provider)
    return RedirectResponse(sec.oidc.start(p, _redirect_uri(request, provider)), status_code=302)


@router.get("/oidc/{provider}/callback")
def oidc_callback(provider: str, request: Request, code: str = "", state: str = "", error: str = ""):
    sec = get_state().security
    if not sec.server_mode or "oidc" not in sec.settings.auth_sources:
        raise SecurityError(400, "OAuth2/OIDC login is not enabled", "oidc_disabled")
    if error or not code:
        return RedirectResponse("/?login_error=" + urllib.parse.quote(error or "no code"), status_code=302)
    p = sec.settings.oidc_provider(provider)
    try:
        info = identity(p, sec.oidc.finish(p, code, state))
        principal = sec.login_external(info, f"oidc:{provider}")
    except SecurityError as exc:
        return RedirectResponse("/?login_error=" + urllib.parse.quote(exc.detail), status_code=302)
    resp = RedirectResponse("/", status_code=302)
    old = request.cookies.get(SESSION_COOKIE)
    if old:
        sec.end_session(old)
    session = sec.start_session(principal)
    _set_cookies(resp, session_cookies(sec, session, is_secure(sec, request.url.scheme)))
    return resp


# ------------------------------------------------------------- master password
@router.get("/vault")
def vault_status():
    return get_state().security.vault_status()


@router.post("/vault/setup")
def vault_setup(body: VaultIn):
    return get_state().security.vault_setup(body.password)


@router.post("/vault/unlock")
def vault_unlock(body: VaultIn):
    return get_state().security.vault_unlock(body.password)


@router.post("/vault/lock")
def vault_lock():
    return get_state().security.vault_lock()


@router.post("/vault/change")
def vault_change(body: VaultChangeIn):
    return get_state().security.vault_change(body.old_password, body.new_password)


@router.post("/vault/disable")
def vault_disable(body: VaultIn):
    return get_state().security.vault_disable(body.password)


@router.post("/vault/reset")
def vault_reset():
    return get_state().security.vault_reset()
