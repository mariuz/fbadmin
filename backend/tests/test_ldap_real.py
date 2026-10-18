"""LDAP login against a real OpenLDAP server (not a mock).

Start one with ``scripts/ldap-test-server.sh`` and set ``FBADMIN_TEST_LDAP_URL=ldap://127.0.0.1:3389``;
without it the test is skipped. Directory: uid=alice (member of cn=fbadmins) and uid=bob.
"""

from __future__ import annotations

import os

import pytest

from .test_security import ADMIN_PW, _client, login

URL = os.environ.get("FBADMIN_TEST_LDAP_URL")
pytestmark = pytest.mark.skipif(not URL, reason="FBADMIN_TEST_LDAP_URL not set (real LDAP server)")
pytest.importorskip("ldap3")
BASE = "dc=example,dc=org"


def test_search_bind_with_admin_group(tmp_path, monkeypatch):
    with _client(tmp_path, monkeypatch, SERVER_MODE="1", AUTH_SOURCES="internal,ldap", ADMIN_USERNAME="root",
                 ADMIN_PASSWORD=ADMIN_PW, LDAP_URL=URL, LDAP_BASE_DN=BASE,
                 LDAP_BIND_DN=f"cn=admin,{BASE}", LDAP_BIND_PASSWORD="adminpw",
                 LDAP_ADMIN_GROUP=f"cn=fbadmins,ou=groups,{BASE}") as c:
        assert c.post("/api/auth/login", json={"username": "alice", "password": "wrong"}).status_code == 401
        assert c.post("/api/auth/login", json={"username": "alice", "password": ""}).status_code == 401
        assert c.post("/api/auth/login", json={"username": "nobody", "password": "x"}).status_code == 401
        # LDAP filter injection does not match other entries
        assert c.post("/api/auth/login", json={"username": "*", "password": "alice-pw"}).status_code == 401
        a = login(c, "alice", "alice-pw")["user"]
        assert (a["auth_source"], a["role"], a["email"]) == ("ldap", "admin", "alice@example.org")
    with _client(tmp_path / "b", monkeypatch, SERVER_MODE="1", AUTH_SOURCES="ldap", ADMIN_USERNAME="root",
                 ADMIN_PASSWORD=ADMIN_PW, LDAP_URL=URL, LDAP_BASE_DN=BASE,
                 LDAP_BIND_DN=f"cn=admin,{BASE}", LDAP_BIND_PASSWORD="adminpw",
                 LDAP_ADMIN_GROUP=f"cn=fbadmins,ou=groups,{BASE}") as c:
        b = login(c, "bob", "bob-pw")["user"]
        assert (b["role"], b["email"]) == ("user", "bob@example.org")


def test_simple_bind_with_dn_template(tmp_path, monkeypatch):
    with _client(tmp_path, monkeypatch, SERVER_MODE="1", AUTH_SOURCES="ldap", ADMIN_USERNAME="root",
                 ADMIN_PASSWORD=ADMIN_PW, LDAP_URL=URL,
                 LDAP_USER_DN_TEMPLATE=f"uid={{username}},ou=people,{BASE}") as c:
        assert login(c, "bob", "bob-pw")["user"]["auth_source"] == "ldap"
        assert c.post("/api/auth/login", json={"username": "bob", "password": "alice-pw"}).status_code == 401
        assert c.post("/api/auth/login", json={"username": "root", "password": ADMIN_PW}).status_code == 401


def test_bad_service_account_is_reported(tmp_path, monkeypatch):
    with _client(tmp_path, monkeypatch, SERVER_MODE="1", AUTH_SOURCES="ldap", ADMIN_USERNAME="root",
                 ADMIN_PASSWORD=ADMIN_PW, LDAP_URL=URL, LDAP_BASE_DN=BASE,
                 LDAP_BIND_DN=f"cn=admin,{BASE}", LDAP_BIND_PASSWORD="wrong") as c:
        r = c.post("/api/auth/login", json={"username": "alice", "password": "alice-pw"})
        # a misconfigured directory is an upstream error (502), not "wrong password"
        assert r.status_code == 502 and r.json()["code"] == "ldap_error" and "bind_dn" in r.json()["detail"]
