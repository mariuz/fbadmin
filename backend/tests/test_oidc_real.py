"""OpenID Connect login against a real provider (Dex), not a mock.

Start it with ``scripts/oidc-test-server.sh`` and set ``FBADMIN_TEST_OIDC_ISSUER=http://127.0.0.1:5556/dex``;
without it the test is skipped. The test drives the whole authorization-code flow with PKCE:
fbadmin redirects to Dex, the test submits Dex's login form, Dex redirects back with a code,
fbadmin exchanges it, reads the userinfo endpoint and starts a session.
"""

from __future__ import annotations

import json
import os
import re
import urllib.parse

import httpx
import pytest

from .test_security import ADMIN_PW, _client

ISSUER = os.environ.get("FBADMIN_TEST_OIDC_ISSUER")
pytestmark = pytest.mark.skipif(not ISSUER, reason="FBADMIN_TEST_OIDC_ISSUER not set (real OIDC provider)")


def _provider(**extra):
    return json.dumps([{"name": "dex", "title": "Dex", "issuer": ISSUER, "client_id": "fbadmin",
                        "client_secret": "fbadmin-secret", "username_claim": "email", **extra}])


def _sign_in(c, email: str, password: str) -> httpx.Response:
    """Run the browser part of the flow; returns fbadmin's response to the callback."""
    r = c.get("/api/auth/oidc/dex/login", follow_redirects=False)
    assert r.status_code == 302
    auth_url = r.headers["location"]
    q = urllib.parse.parse_qs(urllib.parse.urlparse(auth_url).query)
    assert q["code_challenge_method"] == ["S256"] and q["redirect_uri"][0].endswith("/api/auth/oidc/dex/callback")
    with httpx.Client(trust_env=False, follow_redirects=False, timeout=15) as b:
        resp = b.get(auth_url)
        # Dex: /auth -> /auth/local (only one connector) -> login form
        while resp.status_code in (302, 303) and not resp.headers["location"].startswith("http://testserver/"):
            resp = b.get(urllib.parse.urljoin(str(resp.url), resp.headers["location"]))
        assert resp.status_code == 200 and 'name="password"' in resp.text, resp.text[:300]
        action = re.search(r'<form[^>]*action="([^"]+)"', resp.text)
        post_url = urllib.parse.urljoin(str(resp.url), action.group(1).replace("&amp;", "&")) if action else str(resp.url)
        resp = b.post(post_url, data={"login": email, "password": password})
        while resp.status_code in (302, 303) and not resp.headers["location"].startswith("http://testserver/"):
            resp = b.get(urllib.parse.urljoin(str(resp.url), resp.headers["location"]))
        if resp.status_code not in (302, 303):  # wrong password: Dex shows the form again (401)
            return resp
        cb = resp.headers["location"]
    assert cb.startswith("http://testserver/api/auth/oidc/dex/callback?")
    return c.get(cb.replace("http://testserver", ""), follow_redirects=False)


def test_oidc_login_flow(tmp_path, monkeypatch):
    with _client(tmp_path, monkeypatch, SERVER_MODE="1", AUTH_SOURCES="internal,oidc", ADMIN_USERNAME="root",
                 ADMIN_PASSWORD=ADMIN_PW, OIDC_PROVIDERS=_provider(admin_users=["carol@example.org"])) as c:
        assert any(s["type"] == "oidc" for s in c.get("/api/auth/status").json()["auth_sources"])
        r = _sign_in(c, "carol@example.org", "carol-pw")
        assert r.status_code == 302 and r.headers["location"] == "/", r.text
        me = c.get("/api/auth/status").json()["user"]
        assert (me["username"], me["auth_source"], me["role"]) == ("carol@example.org", "oidc:dex", "admin")
        # a second user without admin rights
        c.cookies.clear()
        assert _sign_in(c, "dan@example.org", "dan-pw").status_code == 302
        dan = c.get("/api/auth/status").json()["user"]
        assert (dan["username"], dan["role"]) == ("dan@example.org", "user")


def test_wrong_password_never_reaches_fbadmin(tmp_path, monkeypatch):
    with _client(tmp_path, monkeypatch, SERVER_MODE="1", AUTH_SOURCES="oidc", ADMIN_USERNAME="root",
                 ADMIN_PASSWORD=ADMIN_PW, OIDC_PROVIDERS=_provider()) as c:
        r = _sign_in(c, "carol@example.org", "wrong")
        assert r.status_code == 401 and 'name="password"' in r.text and "Invalid" in r.text  # Dex's form again
        assert c.get("/api/auth/status").json().get("user") is None


def test_domain_restriction_and_replayed_state(tmp_path, monkeypatch):
    with _client(tmp_path, monkeypatch, SERVER_MODE="1", AUTH_SOURCES="oidc", ADMIN_USERNAME="root",
                 ADMIN_PASSWORD=ADMIN_PW, OIDC_PROVIDERS=_provider(allowed_domains=["corp.example"])) as c:
        r = _sign_in(c, "carol@example.org", "carol-pw")
        assert r.status_code == 302 and "login_error" in r.headers["location"]
        assert "domain" in urllib.parse.unquote(r.headers["location"])
        assert c.get("/api/auth/status").json().get("user") is None
        # a callback with an unknown state is rejected
        r = c.get("/api/auth/oidc/dex/callback?code=x&state=forged", follow_redirects=False)
        assert r.status_code in (302, 400, 403) and "/api" not in r.headers.get("location", "/api")
