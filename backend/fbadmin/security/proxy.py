"""Reverse-proxy header authentication.

An authenticating proxy (oauth2-proxy, Authelia, Apache/nginx with SSO, ...) puts
the signed-in user into ``X-Forwarded-User``. The header is only trusted when the
request comes from an address in ``FBADMIN_TRUSTED_PROXIES``; fbadmin must not be
reachable directly by clients in this setup.
"""

from __future__ import annotations

from .sessions import Principal


def principal_from_headers(sec, headers: dict[str, str], username: str) -> Principal:
    cfg = sec.settings.proxy
    email = headers.get(cfg.email_header.lower()) or None
    groups = [g.strip() for g in (headers.get(cfg.groups_header.lower()) or "").replace(";", ",").split(",")
              if g.strip()]
    admin = any(g in cfg.admin_groups for g in groups)
    return sec.login_external({"username": username, "email": email, "admin": admin}, "proxy")
