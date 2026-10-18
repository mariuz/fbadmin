"""OAuth2 / OpenID Connect login (authorization code flow with PKCE).

* OIDC providers (Google, Keycloak, Microsoft Entra ID, ...) are configured with
  their ``issuer``; endpoints come from ``/.well-known/openid-configuration``.
* Plain OAuth2 providers (GitHub) list their endpoints explicitly.

The user's identity is read from the provider's *userinfo* endpoint over TLS
with the access token, so no ID-token signature handling is needed.
"""

from __future__ import annotations

import base64
import hashlib
import json
import secrets
import threading
import time
import urllib.parse
import urllib.request
from typing import Any

from .errors import AccessDenied
from .settings import OidcProvider

PENDING_TTL = 600


def http_json(method: str, url: str, *, data: dict[str, str] | None = None,
              headers: dict[str, str] | None = None) -> dict[str, Any]:
    """Small JSON-over-HTTPS helper (replaced in tests)."""
    body = urllib.parse.urlencode(data).encode() if data is not None else None
    req = urllib.request.Request(url, data=body, method=method,
                                 headers={"Accept": "application/json", "User-Agent": "fbadmin", **(headers or {})})
    with urllib.request.urlopen(req, timeout=15) as resp:  # noqa: S310 - provider URLs come from the admin's config
        return json.loads(resp.read().decode("utf-8") or "{}")


class OidcClient:
    def __init__(self):
        self._discovery: dict[str, dict[str, Any]] = {}
        self._pending: dict[str, dict[str, Any]] = {}
        self._lock = threading.Lock()

    def endpoints(self, p: OidcProvider) -> dict[str, str]:
        ep = {"authorization_endpoint": p.authorization_endpoint, "token_endpoint": p.token_endpoint,
              "userinfo_endpoint": p.userinfo_endpoint}
        if all(ep.values()):
            return ep
        if not p.issuer:
            raise ValueError(f"Provider {p.name!r}: set 'issuer' or all three endpoints")
        if p.issuer not in self._discovery:
            url = p.issuer.rstrip("/") + "/.well-known/openid-configuration"
            self._discovery[p.issuer] = http_json("GET", url)
        disc = self._discovery[p.issuer]
        return {k: v or disc.get(k, "") for k, v in ep.items()}

    def start(self, p: OidcProvider, redirect_uri: str) -> str:
        """URL to send the browser to."""
        state = secrets.token_urlsafe(24)
        nonce = secrets.token_urlsafe(24)
        verifier = secrets.token_urlsafe(48)
        now = time.time()
        with self._lock:
            for k in [k for k, v in self._pending.items() if now - v["created"] > PENDING_TTL]:
                self._pending.pop(k, None)
            self._pending[state] = {"provider": p.name, "verifier": verifier, "nonce": nonce,
                                    "redirect_uri": redirect_uri, "created": now}
        params = {"response_type": "code", "client_id": p.client_id, "redirect_uri": redirect_uri,
                  "scope": p.scope, "state": state, "nonce": nonce}
        if p.pkce:
            challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
            params.update(code_challenge=challenge, code_challenge_method="S256")
        ep = self.endpoints(p)["authorization_endpoint"]
        return ep + ("&" if "?" in ep else "?") + urllib.parse.urlencode(params)

    def finish(self, p: OidcProvider, code: str, state: str) -> dict[str, Any]:
        """Exchange the code; return the userinfo claims."""
        with self._lock:
            pending = self._pending.pop(state, None)
        if not pending or pending["provider"] != p.name or time.time() - pending["created"] > PENDING_TTL:
            raise AccessDenied("Invalid or expired login state - try again")
        ep = self.endpoints(p)
        data = {"grant_type": "authorization_code", "code": code, "redirect_uri": pending["redirect_uri"],
                "client_id": p.client_id}
        if p.client_secret:
            data["client_secret"] = p.client_secret
        if p.pkce:
            data["code_verifier"] = pending["verifier"]
        tok = http_json("POST", ep["token_endpoint"], data=data)
        access = tok.get("access_token")
        if not access:
            raise AccessDenied(f"Token request failed: {tok.get('error_description') or tok.get('error') or tok}")
        claims = http_json("GET", ep["userinfo_endpoint"], headers={"Authorization": f"Bearer {access}"})
        return claims


def identity(p: OidcProvider, claims: dict[str, Any]) -> dict[str, Any]:
    """Map provider claims to ``{"username", "email", "admin"}``; raises AccessDenied if not allowed."""
    email = claims.get(p.email_claim) or claims.get("email")
    username = claims.get(p.username_claim) or claims.get("preferred_username") or email or claims.get("sub")
    if not username:
        raise AccessDenied("The provider returned no usable user name")
    username = str(username)
    if p.allowed_domains:
        domain = str(email or "").rpartition("@")[2].lower()
        if domain not in [d.lower() for d in p.allowed_domains]:
            raise AccessDenied("Your e-mail domain is not allowed to sign in")
        if claims.get("email_verified") is False:
            raise AccessDenied("Your e-mail address is not verified")
    groups = claims.get(p.groups_claim) or []
    if isinstance(groups, str):
        groups = [groups]
    admin = username in p.admin_users or (email in p.admin_users if email else False) or any(
        g in p.admin_groups for g in groups)
    return {"username": username, "email": email, "admin": admin}
