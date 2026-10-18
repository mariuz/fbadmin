"""Security settings, read from environment variables and an optional JSON file.

Everything is opt-in: without ``FBADMIN_SERVER_MODE`` fbadmin runs in desktop
mode (single user, no login) exactly as before.

The JSON file (``FBADMIN_AUTH_CONFIG``, default ``<data_dir>/auth.json``) can hold
the same settings as the environment with the keys in lower case, plus the
structured ``ldap``, ``oidc`` and ``proxy`` sections. Environment variables win.
See docs/SECURITY.md.
"""

from __future__ import annotations

import ipaddress
import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


def _bool(v: Any, default: bool = False) -> bool:
    if v is None or v == "":
        return default
    if isinstance(v, bool):
        return v
    return str(v).strip().lower() in ("1", "true", "yes", "on")


def _rate(v: Any, default: tuple[int, int]) -> tuple[int, int]:
    """'5/300' -> (5 hits, 300 seconds). '0' disables (0, 0)."""
    if v is None or v == "":
        return default
    s = str(v).strip()
    if s in ("0", "off", "none"):
        return (0, 0)
    n, _, w = s.partition("/")
    return (int(n), int(w or 60))


@dataclass
class LdapSettings:
    url: str = ""
    # Simple bind: "uid={username},ou=people,dc=example,dc=org"
    user_dn_template: str = ""
    # Search + bind (used when no template is given)
    base_dn: str = ""
    search_filter: str = "(uid={username})"
    bind_dn: str = ""
    bind_password: str = ""
    start_tls: bool = False
    email_attribute: str = "mail"
    # Members of this group (DN) become fbadmin administrators.
    admin_group: str = ""
    group_member_attribute: str = "member"
    timeout: int = 10


@dataclass
class OidcProvider:
    name: str
    title: str = ""
    # OpenID Connect: issuer + discovery. Plain OAuth2 (GitHub): give the endpoints.
    issuer: str = ""
    authorization_endpoint: str = ""
    token_endpoint: str = ""
    userinfo_endpoint: str = ""
    client_id: str = ""
    client_secret: str = ""
    scope: str = "openid email profile"
    username_claim: str = "email"
    email_claim: str = "email"
    groups_claim: str = "groups"
    admin_groups: list[str] = field(default_factory=list)
    admin_users: list[str] = field(default_factory=list)
    allowed_domains: list[str] = field(default_factory=list)
    pkce: bool = True


# Presets fill in the endpoints of well-known providers.
OIDC_PRESETS: dict[str, dict[str, Any]] = {
    "google": {"title": "Google", "issuer": "https://accounts.google.com"},
    "github": {
        "title": "GitHub",
        "authorization_endpoint": "https://github.com/login/oauth/authorize",
        "token_endpoint": "https://github.com/login/oauth/access_token",
        "userinfo_endpoint": "https://api.github.com/user",
        "scope": "read:user user:email",
        "username_claim": "login",
    },
    "keycloak": {"title": "Keycloak"},  # issuer: https://kc.example.org/realms/<realm>
    "entra": {"title": "Microsoft Entra ID", "username_claim": "preferred_username"},
    # issuer: https://login.microsoftonline.com/<tenant>/v2.0
}


@dataclass
class ProxySettings:
    header: str = "X-Forwarded-User"
    email_header: str = "X-Forwarded-Email"
    groups_header: str = "X-Forwarded-Groups"
    admin_groups: list[str] = field(default_factory=list)
    trusted: list[str] = field(default_factory=lambda: ["127.0.0.1/32", "::1/128"])

    def is_trusted(self, ip: str | None) -> bool:
        if not ip:
            return False
        try:
            addr = ipaddress.ip_address(ip)
        except ValueError:
            return False
        for net in self.trusted:
            try:
                if addr in ipaddress.ip_network(net, strict=False):
                    return True
            except ValueError:
                continue
        return False


@dataclass
class SecuritySettings:
    data_dir: Path
    server_mode: bool = False
    auth_sources: list[str] = field(default_factory=lambda: ["internal"])
    session_lifetime: int = 8 * 3600
    session_idle: int = 3600
    cookie_secure: str = "auto"  # auto | true | false
    admin_username: str = ""
    admin_email: str = ""
    admin_password: str = ""
    auto_create_users: bool = True
    login_rate: tuple[int, int] = (5, 300)
    api_rate: tuple[int, int] = (1200, 60)
    max_request_size: int = 16 * 1024 * 1024
    audit: bool = False
    public_url: str = ""
    ldap: LdapSettings = field(default_factory=LdapSettings)
    oidc: list[OidcProvider] = field(default_factory=list)
    proxy: ProxySettings = field(default_factory=ProxySettings)

    @property
    def db_file(self) -> Path:
        return self.data_dir / "fbadmin.db"

    @property
    def master_file(self) -> Path:
        return self.data_dir / "master_key.json"

    def oidc_provider(self, name: str) -> OidcProvider:
        for p in self.oidc:
            if p.name == name:
                return p
        raise KeyError(f"Unknown OAuth2/OIDC provider {name!r}")


def _env(name: str, file_cfg: dict[str, Any]) -> Any:
    v = os.environ.get("FBADMIN_" + name)
    if v is not None:
        return v
    return file_cfg.get(name.lower())


def _list(v: Any) -> list[str]:
    if v is None or v == "":
        return []
    if isinstance(v, list):
        return [str(x).strip() for x in v if str(x).strip()]
    return [x.strip() for x in str(v).split(",") if x.strip()]


def _oidc(items: list[dict[str, Any]]) -> list[OidcProvider]:
    out = []
    for raw in items:
        item = dict(raw)
        preset = OIDC_PRESETS.get(item.get("preset") or item.get("name", ""), {})
        merged = {**preset, **{k: v for k, v in item.items() if k != "preset"}}
        known = {f for f in OidcProvider.__dataclass_fields__}
        p = OidcProvider(**{k: v for k, v in merged.items() if k in known})
        p.title = p.title or p.name
        out.append(p)
    return out


def load(data_dir: Path) -> SecuritySettings:
    cfg_path = os.environ.get("FBADMIN_AUTH_CONFIG") or str(data_dir / "auth.json")
    file_cfg: dict[str, Any] = {}
    if cfg_path and Path(cfg_path).is_file():
        file_cfg = json.loads(Path(cfg_path).read_text("utf-8") or "{}")

    server_mode = _bool(_env("SERVER_MODE", file_cfg))
    s = SecuritySettings(data_dir=data_dir, server_mode=server_mode)
    s.auth_sources = _list(_env("AUTH_SOURCES", file_cfg)) or ["internal"]
    s.session_lifetime = int(_env("SESSION_LIFETIME", file_cfg) or s.session_lifetime)
    s.session_idle = int(_env("SESSION_IDLE", file_cfg) or s.session_idle)
    s.cookie_secure = str(_env("COOKIE_SECURE", file_cfg) or "auto").lower()
    s.admin_email = str(_env("ADMIN_EMAIL", file_cfg) or "")
    s.admin_username = str(_env("ADMIN_USERNAME", file_cfg) or s.admin_email or "")
    s.admin_password = str(_env("ADMIN_PASSWORD", file_cfg) or "")
    s.auto_create_users = _bool(_env("AUTO_CREATE_USERS", file_cfg), True)
    s.login_rate = _rate(_env("LOGIN_RATE", file_cfg), s.login_rate)
    s.api_rate = _rate(_env("RATE_LIMIT", file_cfg), s.api_rate)
    s.max_request_size = int(_env("MAX_REQUEST_SIZE", file_cfg) or s.max_request_size)
    s.audit = _bool(_env("AUDIT", file_cfg), server_mode)
    s.public_url = str(_env("PUBLIC_URL", file_cfg) or "").rstrip("/")

    ldap_cfg = dict(file_cfg.get("ldap") or {})
    for key in LdapSettings.__dataclass_fields__:
        v = os.environ.get("FBADMIN_LDAP_" + key.upper())
        if v is not None:
            ldap_cfg[key] = v
    if "start_tls" in ldap_cfg:
        ldap_cfg["start_tls"] = _bool(ldap_cfg["start_tls"])
    if "timeout" in ldap_cfg:
        ldap_cfg["timeout"] = int(ldap_cfg["timeout"])
    s.ldap = LdapSettings(**{k: v for k, v in ldap_cfg.items() if k in LdapSettings.__dataclass_fields__})

    oidc_items = file_cfg.get("oidc") or []
    env_oidc = os.environ.get("FBADMIN_OIDC_PROVIDERS")
    if env_oidc:
        oidc_items = json.loads(env_oidc)
    s.oidc = _oidc(oidc_items)

    proxy_cfg = dict(file_cfg.get("proxy") or {})
    if os.environ.get("FBADMIN_PROXY_HEADER"):
        proxy_cfg["header"] = os.environ["FBADMIN_PROXY_HEADER"]
    if os.environ.get("FBADMIN_TRUSTED_PROXIES"):
        proxy_cfg["trusted"] = _list(os.environ["FBADMIN_TRUSTED_PROXIES"])
    if os.environ.get("FBADMIN_PROXY_ADMIN_GROUPS"):
        proxy_cfg["admin_groups"] = _list(os.environ["FBADMIN_PROXY_ADMIN_GROUPS"])
    for k in ("trusted", "admin_groups"):
        if k in proxy_cfg:
            proxy_cfg[k] = _list(proxy_cfg[k])
    s.proxy = ProxySettings(**{k: v for k, v in proxy_cfg.items() if k in ProxySettings.__dataclass_fields__})
    return s
