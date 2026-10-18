"""Security / server mode tests that do not need a Firebird server."""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from fbadmin.security import crypto
from fbadmin.security.readonly import blocked_request, category, write_statements

ADMIN_PW = "admin-password-1"


def _client(tmp_path, monkeypatch, **env):
    monkeypatch.setenv("FBADMIN_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("FBADMIN_AUTH_CONFIG", str(tmp_path / "auth.json"))
    for k in list(__import__("os").environ):
        if k.startswith("FBADMIN_") and k not in ("FBADMIN_DATA_DIR", "FBADMIN_AUTH_CONFIG") \
                and not k.startswith("FBADMIN_TEST"):
            monkeypatch.delenv(k, raising=False)
    for k, v in env.items():
        monkeypatch.setenv("FBADMIN_" + k, str(v))
    from fbadmin import config
    from fbadmin.api import deps

    monkeypatch.setattr(config.settings, "data_dir", tmp_path)
    deps.reset_state()
    from fbadmin.app import create_app

    return TestClient(create_app())


@pytest.fixture
def desktop(tmp_path, monkeypatch):
    with _client(tmp_path, monkeypatch) as c:
        yield c
    from fbadmin.api import deps

    deps.reset_state()


@pytest.fixture
def srv_client(tmp_path, monkeypatch):
    with _client(tmp_path, monkeypatch, SERVER_MODE="1", ADMIN_EMAIL="admin@example.org",
                 ADMIN_PASSWORD=ADMIN_PW, LOGIN_RATE="3/60") as c:
        yield c
    from fbadmin.api import deps

    deps.reset_state()


def login(c: TestClient, user: str, pw: str) -> dict:
    r = c.post("/api/auth/login", json={"username": user, "password": pw})
    assert r.status_code == 200, r.text
    c.headers["X-CSRF-Token"] = r.json()["csrf_token"]
    return r.json()


# ---------------------------------------------------------------- crypto
def test_password_hash_and_vault():
    h = crypto.hash_password("secret-pw")
    assert h.startswith("$argon2id$")
    assert crypto.verify_password(h, "secret-pw")
    assert not crypto.verify_password(h, "wrong")
    record, dek = crypto.vault_create("master-pw")
    assert crypto.vault_unlock(record, "master-pw") == dek
    with pytest.raises(crypto.WrongPassword):
        crypto.vault_unlock(record, "nope")
    tok = crypto.encrypt(dek, "masterkey", "srv1")
    assert crypto.is_token(tok) and "masterkey" not in tok
    assert crypto.decrypt(dek, tok, "srv1") == "masterkey"
    with pytest.raises(ValueError):
        crypto.decrypt(dek, tok, "other-server")  # bound to the server id
    rewrapped = crypto.vault_wrap(dek, "new-master")
    assert crypto.vault_unlock(rewrapped, "new-master") == dek


def test_statement_classification():
    assert category("select 1 from rdb$database") == "QUERY"
    assert category("/* x */ insert into t values (1)") == "DML"
    assert category("create table t (a int)") == "DDL"
    assert category("grant select on t to public") == "DDL"
    assert category("commit") == "TX"
    script = "select 1 from rdb$database;\nupdate t set a = 1;\nwith x as (select 1 a from rdb$database) select * from x;"
    assert write_statements(script) == ["update t set a = 1"]
    assert write_statements("SET TRANSACTION READ WRITE") == ["SET TRANSACTION READ WRITE"]
    assert write_statements("execute procedure p; select 1 from rdb$database") == []
    assert blocked_request("POST", "/api/servers/s1/databases/d1/tables/T/data")[0] == "s1"
    assert blocked_request("POST", "/api/servers/s1/databases/d1/execute") == (None, "")
    assert blocked_request("PUT", "/api/servers/s1") == (None, "")
    assert blocked_request("POST", "/api/servers/s1/connect") == (None, "")
    assert blocked_request("POST", "/api/servers/s1/databases/create")[0] == "s1"
    assert blocked_request("DELETE", "/api/servers/s1/databases/d1/objects/tables/T")[0] == "s1"


# ---------------------------------------------------------------- desktop mode
def test_desktop_mode_unchanged(desktop):
    st = desktop.get("/api/auth/status").json()
    assert st["server_mode"] is False and st["authenticated"] is True
    assert desktop.get("/api/servers").status_code == 200
    r = desktop.post("/api/servers", json={"name": "x", "host": "127.0.0.1", "password": "pw"})
    assert r.status_code == 201
    assert "x-frame-options" in r.headers
    # no login, no CSRF token needed, no fbadmin.db written
    assert desktop.delete(f"/api/servers/{r.json()['id']}").status_code == 204
    from fbadmin import config

    assert not (config.settings.data_dir / "fbadmin.db").exists()


def test_cross_site_requests_refused(desktop):
    r = desktop.post("/api/servers", json={"name": "x"}, headers={"Sec-Fetch-Site": "cross-site"})
    assert r.status_code == 403 and r.json()["code"] == "csrf_failed"
    r = desktop.post("/api/servers", json={"name": "x"}, headers={"Origin": "http://evil.example"})
    assert r.status_code == 403
    r = desktop.post("/api/servers", json={"name": "x"}, headers={"Sec-Fetch-Site": "same-origin"})
    assert r.status_code == 201


def test_master_password_desktop(desktop):
    from fbadmin import config

    r = desktop.post("/api/servers", json={"name": "a", "host": "127.0.0.1", "password": "plain-secret"})
    sid = r.json()["id"]
    path = config.settings.data_dir / "servers.json"
    assert "plain-secret" in path.read_text()  # legacy behaviour: plaintext

    r = desktop.post("/api/auth/vault/setup", json={"password": "short"})
    assert r.status_code == 400
    r = desktop.post("/api/auth/vault/setup", json={"password": "my-master-pw"})
    assert r.status_code == 200 and r.json()["migrated"] == 1 and r.json()["unlocked"]
    text = path.read_text()
    assert "plain-secret" not in text and "fbenc:v1:" in text
    assert (config.settings.data_dir / "master_key.json").exists()

    from fbadmin.api.deps import get_state

    assert get_state().store.password_for(sid) == "plain-secret"
    desktop.post("/api/auth/vault/lock")
    assert get_state().store.password_for(sid) is None
    # using the saved password while locked asks for the master password
    r = desktop.get(f"/api/servers/{sid}/info")
    assert r.status_code == 401 and r.json()["code"] == "master_password_required"
    # saving a new password while locked too
    r = desktop.post("/api/servers", json={"name": "b", "host": "h", "password": "pw2"})
    assert r.status_code == 401 and r.json()["code"] == "master_password_required"
    assert desktop.post("/api/auth/vault/unlock", json={"password": "bad-password"}).status_code == 400
    assert desktop.post("/api/auth/vault/unlock", json={"password": "my-master-pw"}).json()["unlocked"]
    assert get_state().store.password_for(sid) == "plain-secret"

    # restart: the vault starts locked and the file stays encrypted
    from fbadmin.api import deps

    deps.reset_state()
    st = deps.get_state()
    assert st.store.encrypted and st.store.password_for(sid) is None
    desktop.post("/api/auth/vault/unlock", json={"password": "my-master-pw"})
    assert deps.get_state().store.password_for(sid) == "plain-secret"
    # change, then disable (decrypts back)
    r = desktop.post("/api/auth/vault/change", json={"old_password": "my-master-pw", "new_password": "other-master"})
    assert r.status_code == 200
    r = desktop.post("/api/auth/vault/disable", json={"password": "other-master"})
    assert r.status_code == 200 and not r.json()["enabled"]
    assert "plain-secret" in path.read_text()


def test_master_password_reset(desktop):
    from fbadmin.api.deps import get_state

    sid = desktop.post("/api/servers", json={"name": "a", "host": "h", "password": "pw"}).json()["id"]
    desktop.post("/api/auth/vault/setup", json={"password": "my-master-pw"})
    desktop.post("/api/auth/vault/lock")
    r = desktop.post("/api/auth/vault/reset")
    assert r.json()["dropped"] == 1 and not r.json()["enabled"]
    assert get_state().store.password_for(sid) is None


# ---------------------------------------------------------------- server mode
def test_login_required_and_logout(srv_client):
    c = srv_client
    st = c.get("/api/auth/status").json()
    assert st["server_mode"] and not st["authenticated"] and not st["needs_setup"]
    r = c.get("/api/servers")
    assert r.status_code == 401 and r.json()["code"] == "auth_required"
    assert c.get("/api/health").status_code == 200
    r = c.post("/api/auth/login", json={"username": "admin@example.org", "password": "wrong-pw"})
    assert r.status_code == 401
    data = login(c, "admin@example.org", ADMIN_PW)
    assert data["user"]["role"] == "admin"
    cookies = r.headers.get_list("set-cookie") if hasattr(r.headers, "get_list") else []
    del cookies
    assert c.get("/api/servers").status_code == 200
    r = c.post("/api/auth/logout")
    assert r.status_code == 200
    assert c.get("/api/servers").status_code == 401


def test_session_cookie_flags(srv_client):
    r = srv_client.post("/api/auth/login", json={"username": "admin@example.org", "password": ADMIN_PW})
    cookies = [v for k, v in r.headers.multi_items() if k == "set-cookie"]
    sess = next(c for c in cookies if c.startswith("fbadmin_session="))
    csrf = next(c for c in cookies if c.startswith("fbadmin_csrf="))
    assert "HttpOnly" in sess and "SameSite=Lax" in sess and "Secure" not in sess
    assert "HttpOnly" not in csrf


def test_secure_cookie_over_https(tmp_path, monkeypatch):
    with _client(tmp_path, monkeypatch, SERVER_MODE="1", ADMIN_USERNAME="root",
                 ADMIN_PASSWORD=ADMIN_PW) as c:
        c.base_url = c.base_url.copy_with(scheme="https")
        r = c.post("https://testserver/api/auth/login", json={"username": "root", "password": ADMIN_PW})
        sess = next(v for k, v in r.headers.multi_items() if k == "set-cookie" and v.startswith("fbadmin_session"))
        assert "Secure" in sess


def test_csrf_protection(srv_client):
    c = srv_client
    login(c, "admin@example.org", ADMIN_PW)
    token = c.headers.pop("X-CSRF-Token")
    r = c.post("/api/servers", json={"name": "x", "host": "h"})
    assert r.status_code == 403 and r.json()["code"] == "csrf_failed"
    r = c.post("/api/servers", json={"name": "x", "host": "h"}, headers={"X-CSRF-Token": "forged"})
    assert r.status_code == 403
    r = c.post("/api/servers", json={"name": "x", "host": "h"}, headers={"X-CSRF-Token": token})
    assert r.status_code == 201
    assert c.get("/api/servers").status_code == 200  # safe methods need no token


def test_login_rate_limit(srv_client):
    c = srv_client
    for _ in range(3):
        assert c.post("/api/auth/login", json={"username": "admin@example.org", "password": "x"}).status_code == 401
    r = c.post("/api/auth/login", json={"username": "admin@example.org", "password": ADMIN_PW})
    assert r.status_code == 429 and "retry-after" in r.headers


def test_api_rate_and_size_limits(tmp_path, monkeypatch):
    with _client(tmp_path, monkeypatch, SERVER_MODE="1", ADMIN_USERNAME="root", ADMIN_PASSWORD=ADMIN_PW,
                 RATE_LIMIT="5/60", MAX_REQUEST_SIZE="2000") as c:
        login(c, "root", ADMIN_PW)
        r = c.post("/api/servers", json={"name": "x" * 5000, "host": "h"})
        assert r.status_code == 413
        codes = [c.get("/api/servers").status_code for _ in range(6)]
        assert 429 in codes


def test_first_run_setup(tmp_path, monkeypatch):
    with _client(tmp_path, monkeypatch, SERVER_MODE="1") as c:
        st = c.get("/api/auth/status").json()
        assert st["needs_setup"]
        assert c.post("/api/auth/login", json={"username": "a", "password": "b"}).status_code == 409
        r = c.post("/api/auth/setup", json={"username": "boss", "email": "b@x.org", "password": "boss-password"})
        assert r.status_code == 200 and r.json()["user"]["role"] == "admin"
        c.headers["X-CSRF-Token"] = r.json()["csrf_token"]
        assert c.post("/api/auth/setup", json={"username": "x", "password": "yyyyyyyyyy"}).status_code == 409


def test_user_admin_and_per_user_servers(srv_client):
    c = srv_client
    login(c, "admin@example.org", ADMIN_PW)
    r = c.post("/api/admin/users", json={"username": "alice", "password": "alice-password", "role": "user"})
    assert r.status_code == 201, r.text
    alice_id = r.json()["id"]
    assert "password_hash" not in r.json()
    own = c.post("/api/servers", json={"name": "admin-private", "host": "h1", "password": "pw-a"}).json()
    shared = c.post("/api/servers", json={"name": "shared-one", "host": "h2", "shared": True,
                                          "password": "admin-pw"}).json()
    assert shared["shared"] is True
    names = {s["name"] for s in c.get("/api/servers").json()}
    assert names == {"admin-private", "shared-one"}

    from fbadmin.api.deps import get_state

    data_dir = get_state().security.settings.data_dir
    text = (data_dir / "users" / "1" / "servers.json").read_text()
    assert "pw-a" not in text and "admin-pw" not in text  # encrypted with the admin's login-derived key
    assert "admin-pw" not in (data_dir / "shared_servers.json").read_text()

    c.post("/api/auth/logout")
    c.headers.pop("X-CSRF-Token", None)
    login(c, "alice", "alice-password")
    servers = c.get("/api/servers").json()
    assert [s["name"] for s in servers] == ["shared-one"]  # not the admin's private server
    assert servers[0]["password_known"] is False  # the admin's password is not shared
    assert c.get(f"/api/servers/{own['id']}").status_code == 404
    # users cannot change shared servers, but can store their own password for them
    assert c.put(f"/api/servers/{shared['id']}", json={"name": "hacked"}).status_code == 403
    assert c.put(f"/api/servers/{shared['id']}", json={"password": "alice-fb-pw"}).status_code == 200
    assert c.delete(f"/api/servers/{shared['id']}").status_code == 403
    assert c.post("/api/servers", json={"name": "s", "host": "h", "shared": True}).status_code == 403
    assert c.get("/api/admin/users").status_code == 403
    assert c.get("/api/admin/audit").status_code == 403
    alice_srv = c.post("/api/servers", json={"name": "alice-own", "host": "h3", "password": "alice-saved"}).json()
    assert {s["name"] for s in c.get("/api/servers").json()} == {"alice-own", "shared-one"}
    st = get_state()
    from fbadmin.security.sessions import Principal, current_principal

    tok = current_principal.set(Principal(user_id=alice_id, username="alice", role="user"))
    try:
        assert st.store.password_for(alice_srv["id"]) == "alice-saved"
        assert st.store.password_for(shared["id"]) == "alice-fb-pw"
    finally:
        current_principal.reset(tok)
    # change own password: the vault key is re-wrapped, saved passwords survive
    r = c.post("/api/auth/password", json={"old_password": "alice-password", "new_password": "alice-password-2"})
    assert r.status_code == 200
    c.post("/api/auth/logout")
    c.headers.pop("X-CSRF-Token", None)
    login(c, "alice", "alice-password-2")
    tok = current_principal.set(Principal(user_id=alice_id, username="alice", role="user"))
    try:
        assert st.store.password_for(alice_srv["id"]) == "alice-saved"
    finally:
        current_principal.reset(tok)
    c.post("/api/auth/logout")
    c.headers.pop("X-CSRF-Token", None)

    # admin disables alice
    login(c, "admin@example.org", ADMIN_PW)
    assert c.put(f"/api/admin/users/{alice_id}", json={"active": False}).status_code == 200
    me = c.get("/api/auth/status").json()["user"]
    assert c.put(f"/api/admin/users/{me['id']}", json={"role": "user"}).status_code == 400  # last admin
    c.post("/api/auth/logout")
    c.headers.pop("X-CSRF-Token", None)
    r = c.post("/api/auth/login", json={"username": "alice", "password": "alice-password-2"})
    assert r.status_code == 403
    login(c, "admin@example.org", ADMIN_PW)
    assert c.delete(f"/api/admin/users/{alice_id}").status_code == 204
    assert len(c.get("/api/admin/users").json()) == 1


def test_migration_of_desktop_registry(tmp_path, monkeypatch):
    (tmp_path / "servers.json").write_text(json.dumps({"servers": [
        {"id": "abc123", "name": "old", "host": "h", "password": "legacy-pw", "save_password": True}]}))
    with _client(tmp_path, monkeypatch, SERVER_MODE="1", ADMIN_USERNAME="root", ADMIN_PASSWORD=ADMIN_PW) as c:
        assert not (tmp_path / "servers.json").exists()
        moved = tmp_path / "users" / "1" / "servers.json"
        assert "legacy-pw" in moved.read_text()  # not yet: needs the admin's key
        login(c, "root", ADMIN_PW)
        assert [s["name"] for s in c.get("/api/servers").json()] == ["old"]
        assert "legacy-pw" not in moved.read_text()
        assert "fbenc:v1:" in moved.read_text()


def test_proxy_header_auth(tmp_path, monkeypatch):
    with _client(tmp_path, monkeypatch, SERVER_MODE="1", AUTH_SOURCES="proxy", ADMIN_USERNAME="root",
                 ADMIN_PASSWORD=ADMIN_PW, TRUSTED_PROXIES="10.0.0.0/8", PROXY_ADMIN_GROUPS="dba") as c:
        # TestClient's address ("testclient") is not trusted
        r = c.get("/api/servers", headers={"X-Forwarded-User": "mallory"})
        assert r.status_code == 401
    from fbadmin.api import deps

    with _client(tmp_path / "b", monkeypatch, SERVER_MODE="1", AUTH_SOURCES="proxy",
                 TRUSTED_PROXIES="testclient,127.0.0.1", PROXY_ADMIN_GROUPS="dba") as c:
        sec = deps.get_state().security
        sec.settings.proxy.trusted = ["0.0.0.0/0"]
        sec.settings.proxy.is_trusted = lambda ip: True  # TestClient reports the host "testclient"
        r = c.get("/api/auth/status", headers={"X-Forwarded-User": "bob", "X-Forwarded-Groups": "dev,dba"})
        assert r.json()["user"]["username"] == "bob" and r.json()["user"]["role"] == "admin"
        csrf = c.cookies.get("fbadmin_csrf")
        r = c.post("/api/servers", json={"name": "p", "host": "h"},
                   headers={"X-Forwarded-User": "bob", "X-CSRF-Token": csrf})
        assert r.status_code == 201
        # another user at the proxy gets their own session
        r = c.get("/api/servers", headers={"X-Forwarded-User": "carol"})
        assert r.status_code == 200 and r.json() == []


def test_ldap_login(tmp_path, monkeypatch):
    ldap3 = pytest.importorskip("ldap3")
    from fbadmin.security import ldap as ldap_backend

    server = ldap3.Server("fake_ldap", get_info=ldap3.OFFLINE_SLAPD_2_4)
    setup = ldap3.Connection(server, user="cn=admin,dc=example,dc=org", password="adminpw",
                             client_strategy=ldap3.MOCK_SYNC)
    setup.strategy.add_entry("cn=admin,dc=example,dc=org", {"userPassword": "adminpw", "objectClass": "person"})
    setup.strategy.add_entry("uid=dave,ou=people,dc=example,dc=org",
                             {"userPassword": "dave-ldap-pw", "uid": "dave", "mail": "dave@example.org",
                              "objectClass": "inetOrgPerson"})
    setup.strategy.add_entry("cn=dbas,ou=groups,dc=example,dc=org",
                             {"member": ["uid=dave,ou=people,dc=example,dc=org"], "cn": "dbas",
                              "objectClass": "groupOfNames"})
    monkeypatch.setattr(ldap_backend, "server_factory", lambda cfg: server)
    monkeypatch.setattr(ldap_backend, "connection_kwargs", {"client_strategy": ldap3.MOCK_SYNC})

    # search + bind
    with _client(tmp_path, monkeypatch, SERVER_MODE="1", AUTH_SOURCES="internal,ldap", ADMIN_USERNAME="root",
                 ADMIN_PASSWORD=ADMIN_PW, LDAP_URL="ldap://fake", LDAP_BASE_DN="dc=example,dc=org",
                 LDAP_BIND_DN="cn=admin,dc=example,dc=org", LDAP_BIND_PASSWORD="adminpw",
                 LDAP_ADMIN_GROUP="cn=dbas,ou=groups,dc=example,dc=org") as c:
        assert c.post("/api/auth/login", json={"username": "dave", "password": "wrong"}).status_code == 401
        assert c.post("/api/auth/login", json={"username": "dave", "password": ""}).status_code == 401
        data = login(c, "dave", "dave-ldap-pw")
        assert data["user"]["auth_source"] == "ldap"
        assert data["user"]["role"] == "admin" and data["user"]["email"] == "dave@example.org"
        assert data["vault"]["unlocked"]  # vault key derived from the LDAP password at login
        assert c.post("/api/auth/login", json={"username": "nobody", "password": "x"}).status_code == 401

    # simple bind with a DN template
    with _client(tmp_path / "t", monkeypatch, SERVER_MODE="1", AUTH_SOURCES="ldap", ADMIN_USERNAME="root",
                 ADMIN_PASSWORD=ADMIN_PW, LDAP_URL="ldap://fake",
                 LDAP_USER_DN_TEMPLATE="uid={username},ou=people,dc=example,dc=org") as c:
        data = login(c, "dave", "dave-ldap-pw")
        assert data["user"]["role"] == "user"
        # an internal account cannot log in when only LDAP is enabled
        assert c.post("/api/auth/login", json={"username": "root", "password": ADMIN_PW}).status_code == 401


def test_oidc_login(tmp_path, monkeypatch):
    from fbadmin.security import oidc

    calls = []

    def fake_http(method, url, *, data=None, headers=None):
        calls.append((method, url, data, headers))
        if url.endswith("/.well-known/openid-configuration"):
            return {"authorization_endpoint": "https://idp.example/auth", "token_endpoint": "https://idp.example/token",
                    "userinfo_endpoint": "https://idp.example/userinfo"}
        if url.endswith("/token"):
            assert data["code"] == "the-code" and data["code_verifier"]
            return {"access_token": "at-123", "token_type": "Bearer"}
        if url.endswith("/userinfo"):
            assert headers["Authorization"] == "Bearer at-123"
            return {"sub": "1", "email": "erin@corp.example", "email_verified": True, "groups": ["fb-admins"]}
        raise AssertionError(url)

    monkeypatch.setattr(oidc, "http_json", fake_http)
    providers = [{"name": "keycloak", "issuer": "https://idp.example/realms/x", "client_id": "fbadmin",
                  "client_secret": "s3cret", "admin_groups": ["fb-admins"], "allowed_domains": ["corp.example"]}]
    with _client(tmp_path, monkeypatch, SERVER_MODE="1", AUTH_SOURCES="internal,oidc", ADMIN_USERNAME="root",
                 ADMIN_PASSWORD=ADMIN_PW, OIDC_PROVIDERS=json.dumps(providers)) as c:
        st = c.get("/api/auth/status").json()
        assert {"type": "oidc", "name": "keycloak", "title": "Keycloak"} in st["auth_sources"]
        r = c.get("/api/auth/oidc/keycloak/login", follow_redirects=False)
        assert r.status_code == 302
        loc = r.headers["location"]
        assert loc.startswith("https://idp.example/auth?") and "code_challenge=" in loc
        from urllib.parse import parse_qs, urlsplit

        q = parse_qs(urlsplit(loc).query)
        assert q["redirect_uri"][0] == "http://testserver/api/auth/oidc/keycloak/callback"
        r = c.get("/api/auth/oidc/keycloak/callback", params={"code": "the-code", "state": "forged"},
                  follow_redirects=False)
        assert "login_error" in r.headers["location"]
        r = c.get("/api/auth/oidc/keycloak/callback", params={"code": "the-code", "state": q["state"][0]},
                  follow_redirects=False)
        assert r.status_code == 302 and r.headers["location"] == "/"
        st = c.get("/api/auth/status").json()
        assert st["user"]["username"] == "erin@corp.example" and st["user"]["role"] == "admin"
        assert st["user"]["auth_source"] == "oidc:keycloak"
        # OIDC users protect saved passwords with a separate master password
        assert st["vault"]["enabled"] is False
        c.headers["X-CSRF-Token"] = c.cookies.get("fbadmin_csrf")
        r = c.post("/api/servers", json={"name": "x", "host": "h", "password": "pw"})
        assert r.status_code == 401 and r.json()["code"] == "master_password_required"
        assert c.post("/api/auth/vault/setup", json={"password": "erin-master"}).status_code == 200
        assert c.post("/api/servers", json={"name": "x", "host": "h", "password": "pw"}).status_code == 201


def test_oidc_domain_restriction():
    from fbadmin.security.errors import AccessDenied
    from fbadmin.security.oidc import identity
    from fbadmin.security.settings import _oidc

    p = _oidc([{"name": "github", "client_id": "x", "allowed_domains": ["corp.example"]}])[0]
    assert p.authorization_endpoint.startswith("https://github.com/")
    with pytest.raises(AccessDenied):
        identity(p, {"login": "eve", "email": "eve@evil.example"})
    assert identity(p, {"login": "ann", "email": "ann@corp.example"})["username"] == "ann"


def test_audit_viewer_desktop_disabled(desktop):
    r = desktop.get("/api/admin/audit")
    assert r.status_code == 200 and r.json()["enabled"] is False
    assert desktop.get("/api/admin/users").status_code == 400
