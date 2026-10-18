"""Security and multi-user server mode (milestone 0.4).

* ``attach(state)`` - called when the shared application state is created: loads
  the settings, wraps the server registry (encryption, per-user lists) and
  creates the :class:`~fbadmin.security.core.Security` service.
* ``install(app)`` - called from ``create_app``: middleware and error handlers.
"""

from __future__ import annotations

from typing import Any


def attach(state: Any) -> None:
    from ..config import settings as app_settings
    from ..connections import ConnectionManager
    from . import wirecompat
    from .core import Security
    from .servers import EncryptedServerStore, UserScopedJobs, UserScopedManager, UserScopedStore
    from .settings import load

    wirecompat.apply()
    s = load(app_settings.data_dir)
    if s.server_mode:
        state.store = UserScopedStore(app_settings.data_dir)
        state.manager = UserScopedManager(state.store)
        state.jobs = UserScopedJobs()
    else:
        state.store = EncryptedServerStore(app_settings.servers_file, encrypted=s.master_file.exists())
        state.manager = ConnectionManager(state.store)
    state.security = Security(s, state)
    state.security.bootstrap()
    if s.server_mode:
        state.manager.on_reap = state.security.purge_sessions


def install(app: Any) -> None:
    from fastapi import Request
    from fastapi.responses import JSONResponse

    from ..api.deps import get_state
    from ..connections import PasswordRequired
    from .errors import SecurityError
    from .middleware import SecurityMiddleware

    app.add_middleware(SecurityMiddleware)

    @app.exception_handler(SecurityError)
    async def _security_error(request: Request, exc: SecurityError):
        return JSONResponse(status_code=exc.status, content={"detail": exc.detail, "code": exc.code},
                            headers=exc.headers or None)

    @app.exception_handler(PasswordRequired)
    async def _password_required(request: Request, exc: PasswordRequired):
        sid = exc.args[0] if exc.args else ""
        if sid and get_state().security.needs_unlock(sid):
            return JSONResponse(status_code=401, content={
                "detail": "Enter your master password to unlock saved passwords",
                "code": "master_password_required"})
        return JSONResponse(status_code=401, content={"detail": "Password required", "code": "password_required"})
