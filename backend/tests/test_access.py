"""Two-factor sign-in, API tokens and finer permissions (milestone 2.0) - tests without Firebird."""

from __future__ import annotations

import base64
import time

import pytest

from fbadmin.security import totp
from fbadmin.security.access import (
    ADMIN,
    DDL,
    NONE,
    READ,
    WRITE,
    Access,
    Grant,
    Need,
    _check_sql,
    identifiers,
    statement_level,
    statement_targets,
)
from fbadmin.security.tokens import hash_token, new_token

from .test_security import ADMIN_PW, _client, login


# ---------------------------------------------------------------- TOTP (RFC 6238 appendix B)
def _b32(raw: bytes) -> str:
    return base64.b32encode(raw).decode().rstrip("=")


@pytest.mark.parametrize("at,sha1,sha256,sha512", [
    (59, "94287082", "46119246", "90693936"),
    (1111111109, "07081804", "68084774", "25091201"),
    (1111111111, "14050471", "67062674", "99943326"),
    (1234567890, "89005924", "91819424", "93441116"),
    (2000000000, "69279037", "90698825", "38618901"),
    (20000000000, "65353130", "77737706", "47863826"),
])
def test_totp_rfc6238_vectors(at, sha1, sha256, sha512):
    assert totp.totp(_b32(b"12345678901234567890"), at, digits=8) == sha1
    assert totp.totp(_b32(b"12345678901234567890123456789012"), at, digits=8, algorithm="SHA256") == sha256
    assert totp.totp(_b32(b"1234567890" * 6 + b"1234"), at, digits=8, algorithm="SHA512") == sha512


def test_totp_window_and_uri():
    secret = totp.new_secret()
    assert len(secret) == 32 and base64.b32decode(secret)  # 160 bits
    now = 1_700_000_000
    code = totp.totp(secret, now)
    assert totp.match_step(secret, code, now) == totp.step(now)
    assert totp.match_step(secret, totp.totp(secret, now - 30), now) == totp.step(now) - 1  # one step back
    assert totp.match_step(secret, totp.totp(secret, now + 30), now) == totp.step(now) + 1
    assert totp.match_step(secret, totp.totp(secret, now - 90), now) is None  # outside the window
    assert totp.match_step(secret, "12345", now) is None and totp.match_step(secret, "abcdef", now) is None
    uri = totp.otpauth_uri(secret, "alice@example.org")
    assert uri.startswith("otpauth://totp/fbadmin:alice@example.org?secret=" + secret)
    assert "issuer=fbadmin" in uri and "digits=6" in uri and "period=30" in uri


# ---------------------------------------------------------------- tokens
def test_token_format_and_hash():
    t1, t2 = new_token(), new_token()
    assert t1.startswith("fbat_") and len(t1) >= 45 and t1 != t2
    assert hash_token(t1) == hash_token(t1) and len(hash_token(t1)) == 64 and t1 not in hash_token(t1)


# ---------------------------------------------------------------- permission evaluation
def G(i, level, server="S", db=None, obj=None, st="user", subj="2"):
    return Grant(i, st, subj, server, db, obj, level)


def test_most_specific_grant_wins():
    acc = Access(unrestricted=False, grants=[
        G(1, READ, server="*"),
        G(2, WRITE, db="D1"),
        G(3, NONE, db="D1", obj="SALARY*"),
        G(4, DDL, db="D1", obj="TMP_*"),
        G(5, ADMIN, server="S2"),
    ])
    assert acc.level("S") == READ and acc.level("S2") == ADMIN and acc.level("X") == READ
    assert acc.level("S", "D1") == WRITE and acc.level("S", "D2") == READ
    assert acc.level("S", "D1", "CUSTOMER") == WRITE  # falls back to the database grant
    assert acc.level("S", "D1", "salary_2024") == NONE  # patterns are case-insensitive
    assert acc.level("S", "D1", "PUBLIC.SALARY") == NONE  # schema-qualified names (Firebird 6)
    assert acc.level("S", "D1", "TMP_X") == DDL
    assert acc.hidden_objects("S", "D1") and not acc.hidden_objects("S", "D2")
    # equally specific grants (user + group): the higher level wins
    acc2 = Access(unrestricted=False, grants=[G(1, READ, db="D"), G(2, WRITE, db="D", st="group", subj="dev")])
    assert acc2.level("S", "D") == WRITE


def test_visibility_and_token_cap():
    acc = Access(unrestricted=False, grants=[G(1, NONE, server="*"), G(2, READ, db="D1", obj="CUST*")])
    assert not acc.db_visible("S", "D2")
    assert acc.db_visible("S", "D1")  # only through the object grant
    assert acc.server_visible("S", ["D1", "D2"]) and not acc.server_visible("T", ["X"])
    assert acc.hidden_objects("S", "D1")  # database level none: everything but CUST* is hidden
    capped = Access(unrestricted=True, cap=READ, servers=frozenset({"S"}))
    assert capped.level("S", "D") == READ and capped.level("T", "D") == NONE
    assert not capped.free and Access().free


def test_statement_classification_for_rights():
    assert statement_level("select * from t") == READ
    assert statement_level("insert into t values (1)") == WRITE
    assert statement_level("execute procedure p") == WRITE  # may change data
    assert statement_level("execute block as begin end") == DDL  # EXECUTE STATEMENT can run DDL
    assert statement_level("create table t (a int)") == DDL
    assert statement_level("grant select on t to u") == DDL
    assert statement_level("commit") == READ
    assert statement_targets('insert into "Mixed" values (1)') == ["Mixed"]
    assert statement_targets("update or insert into cust values (1) matching (id)") == ["CUST"]
    assert statement_targets("delete from sales.orders where 1=0") == ["SALES.ORDERS"]
    assert statement_targets("create unique index ix1 on emp (a)") == ["IX1", "EMP"]
    assert statement_targets("create trigger trg for emp before insert as begin end") == ["TRG", "EMP"]
    assert statement_targets("grant select on table emp to public") == ["EMP"]
    assert identifiers("select a from emp where b = 'SALARY' -- salary\n") >= {"SELECT", "A", "EMP", "WHERE", "B"}
    assert "SALARY" not in identifiers("select a from emp where b = 'SALARY' -- salary\n")
    assert "SALARY" not in identifiers("select a /* salary */ from emp where b = q'{SALARY}'")
    assert "Salary" in identifiers('select "Salary" from emp')


def test_sql_checks():
    acc = Access(unrestricted=False, grants=[G(1, READ, db="D"), G(2, NONE, db="D", obj="SECRET*"),
                                             G(3, WRITE, db="D", obj="LOG")])
    need = lambda sql: Need("S", "D", READ, sql=sql)  # noqa: E731
    assert _check_sql(acc, need("select * from emp"), True) is None
    assert _check_sql(acc, need("insert into log values (1)"), True) is None
    r = _check_sql(acc, need("select 1 from rdb$database; update emp set a = 1"), True)
    assert r is not None and r.code == "permission_denied" and "update emp" in r.detail
    assert _check_sql(acc, need("select * from secret_pay"), True).code == "permission_denied"
    assert _check_sql(acc, need("insert into log select * from secret_x"), True) is not None
    assert _check_sql(acc, need("create table x (a int)"), True) is not None
    ro_token = Access(unrestricted=True, cap=READ)
    assert _check_sql(ro_token, need("delete from emp"), False).code == "token_read_only"


# ---------------------------------------------------------------- server mode integration
@pytest.fixture
def srv(tmp_path, monkeypatch):
    with _client(tmp_path, monkeypatch, SERVER_MODE="1", ADMIN_USERNAME="root", ADMIN_PASSWORD=ADMIN_PW,
                 LOGIN_RATE="20/60") as c:
        yield c
    from fbadmin.api import deps

    deps.reset_state()


def logout(c):
    c.post("/api/auth/logout")
    c.headers.pop("X-CSRF-Token", None)
    c.cookies.clear()


def _code(secret: str, offset: int = 0) -> str:
    return totp.totp(secret, time.time() + offset)


def _fresh_step() -> None:
    """Wait until at least 8 s of the current 30 s step are left (codes of "this step" stay valid)."""
    left = totp.PERIOD - time.time() % totp.PERIOD
    if left < 8:
        time.sleep(left + 0.1)


def test_two_factor_enrol_login_recovery_and_reset(srv):
    c = srv
    login(c, "root", ADMIN_PW)
    assert c.post("/api/admin/users", json={"username": "alice", "password": "alice-password"}).status_code == 201
    logout(c)

    login(c, "alice", "alice-password")
    st = c.get("/api/auth/2fa").json()
    assert st["available"] and not st["enabled"] and not st["required"]
    setup = c.post("/api/auth/2fa/setup").json()
    secret = setup["secret"]
    _fresh_step()
    assert setup["otpauth_uri"].startswith("otpauth://totp/fbadmin:alice?secret=")
    # not active before confirmation
    assert c.post("/api/auth/2fa/enable", json={"code": "000000"}).status_code == 400
    r = c.post("/api/auth/2fa/enable", json={"code": _code(secret)})
    assert r.status_code == 200, r.text
    codes = r.json()["recovery_codes"]
    assert len(codes) == 10 and r.json()["enabled"] and r.json()["recovery_codes_left"] == 10
    # secrets are encrypted at rest, recovery codes hashed
    from fbadmin.api.deps import get_state

    db = get_state().security.db
    row = db.execute("SELECT secret FROM user_mfa").fetchone()
    assert secret not in row[0] and row[0].startswith("fbenc:v1:")
    assert all(codes[0].replace("-", "") not in h[0] for h in db.execute("SELECT code_hash FROM mfa_recovery"))
    logout(c)

    # password alone no longer opens a session
    r = c.post("/api/auth/login", json={"username": "alice", "password": "alice-password"})
    assert r.status_code == 200 and r.json()["mfa_required"] and not r.json()["authenticated"]
    assert "fbadmin_session" not in r.cookies
    token = r.json()["mfa_token"]
    assert c.get("/api/servers").status_code == 401
    assert c.post("/api/auth/login/mfa", json={"mfa_token": token, "code": "123456"}).status_code == 401
    # the code used at enrolment cannot be replayed; the next step's code works (±1 step window)
    assert c.post("/api/auth/login/mfa", json={"mfa_token": token, "code": _code(secret)}).status_code == 401
    r = c.post("/api/auth/login/mfa", json={"mfa_token": token, "code": _code(secret, 30)})
    assert r.status_code == 200, r.text
    assert r.json()["user"]["username"] == "alice" and r.json()["mfa_method"] == "totp"
    c.headers["X-CSRF-Token"] = r.json()["csrf_token"]
    assert c.get("/api/servers").status_code == 200
    logout(c)

    # recovery code: single use
    for expect in (200, 401):
        t = c.post("/api/auth/login", json={"username": "alice", "password": "alice-password"}).json()["mfa_token"]
        r = c.post("/api/auth/login/mfa", json={"mfa_token": t, "code": codes[0].upper()})
        assert r.status_code == expect, r.text
        if expect == 200:
            assert r.json()["mfa_method"] == "recovery"
            c.headers["X-CSRF-Token"] = r.json()["csrf_token"]
            assert c.get("/api/auth/2fa").json()["recovery_codes_left"] == 9
            logout(c)
    # an expired / unknown challenge
    assert c.post("/api/auth/login/mfa", json={"mfa_token": "nope", "code": "123456"}).json()["code"] == "mfa_expired"

    # the admin resets alice's 2FA (lost phone): password-only sign-in again
    login(c, "root", ADMIN_PW)
    alice_id = next(u["id"] for u in c.get("/api/admin/users").json() if u["username"] == "alice")
    assert next(u for u in c.get("/api/admin/2fa").json()["users"] if u["id"] == alice_id)["enabled"]
    assert c.delete(f"/api/admin/users/{alice_id}/2fa").status_code == 200
    logout(c)
    data = login(c, "alice", "alice-password")
    assert data["authenticated"]


def test_two_factor_required_by_admin_and_rate_limit(srv):
    c = srv
    login(c, "root", ADMIN_PW)
    bob = c.post("/api/admin/users", json={"username": "bob", "password": "bob-password"}).json()
    r = c.put("/api/admin/2fa", json={"require_all": True})
    assert r.status_code == 200 and r.json()["require_all"]
    logout(c)
    # bob must enrol during sign-in
    r = c.post("/api/auth/login", json={"username": "bob", "password": "bob-password"}).json()
    assert r["mfa_setup_required"] and not r["authenticated"]
    secret = r["enrol"]["secret"]
    assert r["enrol"]["otpauth_uri"].startswith("otpauth://totp/")
    assert c.post("/api/auth/login/mfa-setup", json={"mfa_token": r["mfa_token"], "code": "000000"}).status_code == 400
    ok = c.post("/api/auth/login/mfa-setup", json={"mfa_token": r["mfa_token"], "code": _code(secret)})
    assert ok.status_code == 200, ok.text
    assert len(ok.json()["recovery_codes"]) == 10
    c.headers["X-CSRF-Token"] = ok.json()["csrf_token"]
    # required: cannot be switched off
    r = c.post("/api/auth/2fa/disable", json={"code": _code(secret, 30)})
    assert r.status_code == 400 and r.json()["code"] == "mfa_required"
    logout(c)
    # wrong codes are rate limited (LOGIN_RATE=20/60 here; each challenge allows 5 attempts)
    statuses = []
    for _ in range(5):
        t = c.post("/api/auth/login", json={"username": "bob", "password": "bob-password"})
        if t.status_code != 200:
            statuses.append(t.status_code)
            break
        for _ in range(5):
            statuses.append(c.post("/api/auth/login/mfa", json={"mfa_token": t.json()["mfa_token"],
                                                                 "code": "000000"}).status_code)
    assert 429 in statuses
    # admins keep their own accounts usable: root (internal) must enrol too, policy applies to everyone
    r = c.post("/api/auth/login", json={"username": "root", "password": ADMIN_PW}).json()
    assert r.get("mfa_setup_required")
    assert bob["id"]


def test_two_factor_for_ldap_users_not_oidc_or_proxy(tmp_path, monkeypatch):
    ldap3 = pytest.importorskip("ldap3")
    from fbadmin.security import ldap as ldap_backend

    server = ldap3.Server("fake_ldap", get_info=ldap3.OFFLINE_SLAPD_2_4)
    setup = ldap3.Connection(server, user="cn=admin,dc=example,dc=org", password="adminpw",
                             client_strategy=ldap3.MOCK_SYNC)
    setup.strategy.add_entry("uid=lena,ou=people,dc=example,dc=org",
                             {"userPassword": "lena-ldap-pw", "uid": "lena", "objectClass": "inetOrgPerson"})
    monkeypatch.setattr(ldap_backend, "server_factory", lambda cfg: server)
    monkeypatch.setattr(ldap_backend, "connection_kwargs", {"client_strategy": ldap3.MOCK_SYNC})
    with _client(tmp_path, monkeypatch, SERVER_MODE="1", AUTH_SOURCES="internal,ldap", ADMIN_USERNAME="root",
                 ADMIN_PASSWORD=ADMIN_PW, LDAP_URL="ldap://fake",
                 LDAP_USER_DN_TEMPLATE="uid={username},ou=people,dc=example,dc=org") as c:
        login(c, "lena", "lena-ldap-pw")
        assert c.get("/api/auth/2fa").json()["available"] is True
        secret = c.post("/api/auth/2fa/setup").json()["secret"]
        _fresh_step()
        assert c.post("/api/auth/2fa/enable", json={"code": _code(secret)}).status_code == 200
        logout(c)
        r = c.post("/api/auth/login", json={"username": "lena", "password": "lena-ldap-pw"}).json()
        assert r["mfa_required"]
        ok = c.post("/api/auth/login/mfa", json={"mfa_token": r["mfa_token"], "code": _code(secret, 30)})
        assert ok.status_code == 200 and ok.json()["user"]["auth_source"] == "ldap"
        assert ok.json()["vault"]["unlocked"]  # the vault opens with the LDAP password after the second step
        # OIDC / proxy accounts: the identity provider's job
        from fbadmin.api.deps import get_state

        sec = get_state().security
        user = sec.create_user("olga", email=None, password=None, auth_source="oidc:kc")
        assert sec.mfa.status(user)["available"] is False
        assert sec.mfa.challenge(user, "", None) is None
        sec.mfa.set_require_all(True)
        assert sec.mfa.challenge(user, "", None) is None
    from fbadmin.api import deps

    deps.reset_state()


def test_api_tokens(srv):
    c = srv
    login(c, "root", ADMIN_PW)
    c.post("/api/admin/users", json={"username": "carol", "password": "carol-password"})
    logout(c)
    login(c, "carol", "carol-password")
    own = c.post("/api/servers", json={"name": "carol-srv", "host": "h", "password": "saved-pw"}).json()
    other = c.post("/api/servers", json={"name": "carol-2", "host": "h2"}).json()
    r = c.post("/api/access/me/tokens", json={"name": "ci", "scope": "read-only", "expires_days": 30,
                                               "server_ids": [own["id"]]})
    assert r.status_code == 201, r.text
    ro = r.json()["token"]
    assert r.json()["vault"] is True and r.json()["hint"].startswith("fbat_")
    rw = c.post("/api/access/me/tokens", json={"name": "deploy", "scope": "read-write", "expires_days": None}).json()
    listed = c.get("/api/access/me/tokens").json()
    assert {t["name"] for t in listed} == {"ci", "deploy"} and all("token" not in t for t in listed)
    from fbadmin.api.deps import get_state

    assert ro not in str(get_state().security.db.execute("SELECT * FROM api_tokens").fetchall())
    logout(c)

    bearer = {"Authorization": f"Bearer {ro}"}
    # the token works without a session or CSRF token, limited to its server, read-only
    r = c.get("/api/servers", headers=bearer)
    assert r.status_code == 200 and [s["name"] for s in r.json()] == ["carol-srv"]
    assert r.json()[0]["read_only"] is True and r.json()[0]["password_known"] is True  # vault key via the token
    assert c.get(f"/api/servers/{other['id']}", headers=bearer).status_code == 404
    r = c.post("/api/servers", json={"name": "x", "host": "h"}, headers=bearer)
    assert r.status_code == 403 and r.json()["code"] == "token_read_only"
    r = c.put(f"/api/servers/{own['id']}", json={"name": "renamed"}, headers=bearer)
    assert r.status_code == 403
    # tokens cannot manage tokens, passwords or 2FA
    assert c.get("/api/access/me/tokens", headers=bearer).status_code == 403
    assert c.post("/api/auth/2fa/setup", headers=bearer).status_code == 403
    assert c.get("/api/access/me", headers=bearer).json()["cap"] == "read"
    # a cookie is never accepted in place of a token: an invalid bearer is refused even with a session
    login(c, "carol", "carol-password")
    r = c.get("/api/servers", headers={"Authorization": "Bearer fbat_wrong"})
    assert r.status_code == 401 and r.json()["code"] == "invalid_token"
    logout(c)

    rwh = {"Authorization": f"Bearer {rw['token']}"}
    r = c.post("/api/servers", json={"name": "by-token", "host": "h"}, headers=rwh)
    assert r.status_code == 201  # no CSRF token needed with a bearer token
    # last-used timestamps, revoke
    login(c, "carol", "carol-password")
    toks = {t["name"]: t for t in c.get("/api/access/me/tokens").json()}
    assert toks["ci"]["last_used_at"] and toks["ci"]["expires_at"] and toks["deploy"]["expires_at"] is None
    assert c.delete(f"/api/access/me/tokens/{toks['ci']['id']}").json()["status"] == "revoked"
    logout(c)
    assert c.get("/api/servers", headers=bearer).status_code == 401
    # admins see and revoke all tokens; disabled users' tokens stop working
    login(c, "root", ADMIN_PW)
    all_t = c.get("/api/admin/tokens").json()
    assert {(t["username"], t["name"]) for t in all_t} == {("carol", "ci"), ("carol", "deploy")}
    carol_id = all_t[0]["user_id"]
    assert c.put(f"/api/admin/users/{carol_id}", json={"active": False}).status_code == 200
    assert c.get("/api/servers", headers=rwh).status_code == 401
    c.put(f"/api/admin/users/{carol_id}", json={"active": True})
    assert c.get("/api/servers", headers=rwh).status_code == 200
    dep = next(t for t in all_t if t["name"] == "deploy")
    assert c.delete(f"/api/admin/tokens/{dep['id']}").json()["status"] == "revoked"
    assert c.get("/api/servers", headers=rwh).status_code == 401
    # expired tokens
    r = c.post("/api/access/me/tokens", json={"name": "old", "expires_days": 1}).json()
    get_state().security.db.execute("UPDATE api_tokens SET expires_at = ? WHERE id = ?", (time.time() - 1, r["id"]))
    assert c.get("/api/servers", headers={"Authorization": f"Bearer {r['token']}"}).status_code == 401
    assert c.post("/api/access/me/tokens", json={"name": "x", "scope": "everything"}).status_code == 422


def test_permissions_filter_servers_and_keep_defaults(srv):
    c = srv
    login(c, "root", ADMIN_PW)
    dave = c.post("/api/admin/users", json={"username": "dave", "password": "dave-password"}).json()
    erin = c.post("/api/admin/users", json={"username": "erin", "password": "erin-password"}).json()
    s1 = c.post("/api/servers", json={"name": "prod", "host": "h1", "shared": True}).json()
    s2 = c.post("/api/servers", json={"name": "test", "host": "h2", "shared": True}).json()
    d1 = c.post(f"/api/servers/{s1['id']}/databases", json={"name": "sales", "path": "/db/sales.fdb"}).json()
    c.post(f"/api/servers/{s1['id']}/databases", json={"name": "hr", "path": "/db/hr.fdb"})
    # no grants yet: everybody sees everything (behaviour of earlier versions)
    assert c.get("/api/admin/access").json() == {**c.get("/api/admin/access").json(), "default": "full", "grants": []}
    r = c.post("/api/admin/access/grants", json={"subject_type": "user", "subject": "dave", "server_id": s1["id"],
                                                  "db_id": d1["id"], "level": "read"})
    assert r.status_code == 201, r.text
    assert c.post("/api/admin/access/grants", json={"subject_type": "user", "subject": "nobody",
                                                     "level": "read"}).status_code == 400
    gid = r.json()["id"]
    r = c.put(f"/api/admin/access/grants/{gid}", json={"level": "write"})
    assert r.status_code == 200 and r.json()["level"] == "write" and r.json()["db_id"] == d1["id"]
    assert r.json()["id"] == gid
    assert c.put(f"/api/admin/access/grants/{r.json()['id']}", json={"level": "bogus"}).status_code == 422
    c.put(f"/api/admin/access/grants/{r.json()['id']}", json={"level": "read"})
    eff = c.get(f"/api/admin/access/effective/{dave['id']}").json()
    assert eff["restricted"] and [s["server_name"] for s in eff["servers"]] == ["prod"]
    assert [d["name"] for d in eff["servers"][0]["databases"]] == ["sales"]
    logout(c)

    login(c, "dave", "dave-password")
    servers = c.get("/api/servers").json()
    assert [s["name"] for s in servers] == ["prod"]
    assert [d["name"] for d in servers[0]["databases"]] == ["sales"]
    assert servers[0]["read_only"] is True  # read level only: shown (and opened) read-only
    assert c.get(f"/api/servers/{s2['id']}").status_code == 404
    # restricted users cannot change registrations, but can store their own Firebird password
    assert c.post("/api/servers", json={"name": "mine", "host": "h"}).json()["code"] == "permission_denied"
    assert c.put(f"/api/servers/{s1['id']}", json={"password": "dave-fb-pw"}).status_code == 200
    assert c.post(f"/api/servers/{s1['id']}/databases/create", json={"name": "x", "path": "/x"}).status_code == 403
    assert c.post(f"/api/servers/{s1['id']}/databases/{d1['id']}/sweep").status_code == 403
    me = c.get("/api/access/me").json()
    assert me["restricted"] and me["servers"][0]["databases"][0]["level"] == "read"
    # global settings are not for restricted users
    assert c.put("/api/monitoring/settings", json={"interval": 5}).status_code == 403
    logout(c)

    # erin has no grant: unchanged behaviour (sees both shared servers)
    login(c, "erin", "erin-password")
    assert {s["name"] for s in c.get("/api/servers").json()} == {"prod", "test"}
    assert c.get("/api/access/me").json()["restricted"] is False
    logout(c)

    # default "no access": erin sees nothing until granted; groups
    login(c, "root", ADMIN_PW)
    assert c.put("/api/admin/access/settings", json={"default": "none"}).json()["default"] == "none"
    logout(c)
    login(c, "erin", "erin-password")
    assert c.get("/api/servers").json() == []
    logout(c)
    login(c, "root", ADMIN_PW)
    g = c.put("/api/admin/access/groups/devs", json={"description": "developers", "members": [erin["id"]]})
    assert g.status_code == 200 and g.json()["members"] == [erin["id"]]
    c.post("/api/admin/access/grants", json={"subject_type": "group", "subject": "devs", "server_id": s2["id"],
                                              "level": "ddl"})
    # admins are never restricted
    assert {s["name"] for s in c.get("/api/servers").json()} == {"prod", "test"}
    logout(c)
    login(c, "erin", "erin-password")
    servers = c.get("/api/servers").json()
    assert [s["name"] for s in servers] == ["test"] and servers[0]["read_only"] is False
    logout(c)


def test_schedules_follow_permissions(srv):
    c = srv
    login(c, "root", ADMIN_PW)
    c.post("/api/admin/users", json={"username": "frank", "password": "frank-password"})
    s1 = c.post("/api/servers", json={"name": "prod", "host": "h1", "shared": True}).json()
    d1 = c.post(f"/api/servers/{s1['id']}/databases", json={"name": "sales", "path": "/db/sales.fdb"}).json()
    target = c.post("/api/storage/targets", json={"name": "bucket", "kind": "s3",
                                                   "settings": {"bucket": "b", "access_key_id": "k", "secret_access_key": "s"}})
    assert target.status_code == 201, target.text
    tid = target.json()["id"]
    body = {"name": "nightly", "server_id": s1["id"], "database_id": d1["id"], "cron": "0 3 * * *"}
    admin_sched = c.post("/api/schedules", json={**body, "upload": {"target_id": tid}})
    assert admin_sched.status_code == 201, admin_sched.text
    logout(c)

    # frank has no grants (unrestricted, like before) but may not upload to cloud storage targets
    login(c, "frank", "frank-password")
    r = c.post("/api/schedules", json={**body, "name": "exfil", "upload": {"target_id": tid}})
    assert r.status_code == 403, r.text
    own = c.post("/api/schedules", json=body)
    assert own.status_code == 201
    r = c.put(f"/api/schedules/{own.json()['id']}", json={"upload": {"target_id": tid}})
    assert r.status_code == 403
    logout(c)

    # with a read grant frank can no longer see, create or run schedules of that database
    login(c, "root", ADMIN_PW)
    c.post("/api/admin/access/grants", json={"subject_type": "user", "subject": "frank", "server_id": s1["id"],
                                              "db_id": d1["id"], "level": "write"})
    logout(c)
    login(c, "frank", "frank-password")
    assert c.get("/api/schedules").json() == []
    assert c.post("/api/schedules", json=body).status_code == 403
    assert c.post(f"/api/schedules/{admin_sched.json()['id']}/run").status_code == 403
    assert c.delete(f"/api/schedules/{admin_sched.json()['id']}").status_code == 403
    logout(c)
    login(c, "root", ADMIN_PW)
    c.post("/api/admin/access/grants", json={"subject_type": "user", "subject": "frank", "server_id": s1["id"],
                                              "db_id": d1["id"], "level": "admin"})
    logout(c)
    login(c, "frank", "frank-password")
    assert len(c.get("/api/schedules").json()) == 2  # admin level: allowed again (still no uploads)
    assert c.post("/api/schedules", json={**body, "upload": {"target_id": tid}}).status_code == 403


def test_desktop_mode_unaffected(tmp_path, monkeypatch):
    with _client(tmp_path, monkeypatch) as c:
        assert c.get("/api/access/me").json()["restricted"] is False
        assert c.get("/api/servers", headers={"Authorization": "Bearer fbat_whatever"}).status_code == 200
        assert c.post("/api/access/me/tokens", json={"name": "x"}).json()["code"] == "desktop_mode"
        assert c.post("/api/auth/2fa/setup").json()["code"] == "desktop_mode"
    from fbadmin.api import deps

    deps.reset_state()
