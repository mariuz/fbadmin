"""Cloud storage targets: request signing, target store, retention and the API (no network needed).

Tests against real emulators (SeaweedFS S3, Azurite, fake-gcs-server) are in test_storage_real.py.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import stat
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs

import pytest

from fbadmin import storage
from fbadmin.storage import azure, gcs, s3
from fbadmin.storage.base import StorageError, join_key
from fbadmin.storage.targets import MASK, Target, TargetStore, resolved

from .test_security import ADMIN_PW, login

EMPTY = hashlib.sha256(b"").hexdigest()


# ------------------------------------------------------------------ AWS SigV4
def test_sigv4_get_vanilla():
    """aws-sig-v4-test-suite/get-vanilla."""
    r = s3.sign_v4("GET", "/", [], {"host": "example.amazonaws.com", "x-amz-date": "20150830T123600Z"},
                   EMPTY, "AKIDEXAMPLE", "wJalrXUtnFEMI/K7MDENG+bPxRfiCYEXAMPLEKEY", "us-east-1", "service",
                   "20150830T123600Z")
    assert r["canonical_request"] == ("GET\n/\n\nhost:example.amazonaws.com\nx-amz-date:20150830T123600Z\n\n"
                                      "host;x-amz-date\n" + EMPTY)
    assert r["string_to_sign"] == ("AWS4-HMAC-SHA256\n20150830T123600Z\n20150830/us-east-1/service/aws4_request\n"
                                   "bb579772317eb040ac9ed261061d46c1f17a8133879d6129b6e1c25292927e63")
    assert r["signature"] == "5fa00fa31553b73ebf1942676e86291e8372ff2a2260956d9b8aae1d763fbf31"
    assert r["authorization"] == (
        "AWS4-HMAC-SHA256 Credential=AKIDEXAMPLE/20150830/us-east-1/service/aws4_request, "
        "SignedHeaders=host;x-amz-date, Signature=5fa00fa31553b73ebf1942676e86291e8372ff2a2260956d9b8aae1d763fbf31")


S3_SECRET = "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY"
S3_HOST = "examplebucket.s3.amazonaws.com"


def test_sigv4_s3_get_object_example():
    """The GET Object example of the S3 "Signature Calculations for the Authorization Header" page."""
    headers = {"Host": S3_HOST, "Range": "bytes=0-9", "x-amz-content-sha256": EMPTY,
               "x-amz-date": "20130524T000000Z"}
    r = s3.sign_v4("GET", "/test.txt", [], headers, EMPTY, "AKIAIOSFODNN7EXAMPLE", S3_SECRET, "us-east-1",
                   "s3", "20130524T000000Z")
    assert r["canonical_request"].split("\n")[-2] == "host;range;x-amz-content-sha256;x-amz-date"
    assert r["signature"] == "f0e8bdb87c964420e857bd35b5d6ed310bd44f0170aba48dd91039c6036bdb41"


def test_sigv4_s3_list_objects_example():
    """The GET Bucket (List Objects) example: query parameters are sorted and encoded."""
    headers = {"host": S3_HOST, "x-amz-content-sha256": EMPTY, "x-amz-date": "20130524T000000Z"}
    r = s3.sign_v4("GET", "/", [("prefix", "J"), ("max-keys", "2")], headers, EMPTY, "AKIAIOSFODNN7EXAMPLE",
                   S3_SECRET, "us-east-1", "s3", "20130524T000000Z")
    assert r["canonical_request"].split("\n")[2] == "max-keys=2&prefix=J"
    assert r["signature"] == "34b48302e7b5fa45bde8084f4b7868a86f0a534bc59db6670ed5711ef69dc6f7"


def test_s3_urls():
    vh = s3.S3Client("my-bucket", "k", "s", region="eu-west-1")
    assert vh.endpoint == "https://s3.eu-west-1.amazonaws.com" and not vh.path_style
    assert vh._url("a b/c+d.fbk") == ("https://my-bucket.s3.eu-west-1.amazonaws.com", "/a%20b/c%2Bd.fbk")
    assert vh._url("") == ("https://my-bucket.s3.eu-west-1.amazonaws.com", "/")
    ps = s3.S3Client("b", "k", "s", endpoint="http://127.0.0.1:9000/")
    assert ps.path_style and ps._url("x/y.fbk") == ("http://127.0.0.1:9000", "/b/x/y.fbk")
    assert ps._url("") == ("http://127.0.0.1:9000", "/b")


# --------------------------------------------------------- Azure Shared Key
def test_azure_string_to_sign():
    url = "https://myaccount.blob.core.windows.net/mycontainer/my%20blob?comp=block&blockid=QUJD%3D%3D"
    headers = {"Content-Length": "11", "Content-Type": "application/octet-stream",
               "x-ms-date": "Fri, 26 Jun 2015 23:39:12 GMT", "x-ms-version": "2021-08-06", "User-Agent": "x"}
    assert azure.string_to_sign("PUT", url, headers, "myaccount") == (
        "PUT\n\n\n11\n\napplication/octet-stream\n\n\n\n\n\n\n"
        "x-ms-date:Fri, 26 Jun 2015 23:39:12 GMT\nx-ms-version:2021-08-06\n"
        "/myaccount/mycontainer/my%20blob\nblockid:QUJD==\ncomp:block")
    # Content-Length 0 is an empty line; Azurite style URLs keep the account in the path
    listing = azure.string_to_sign("GET", "http://127.0.0.1:10000/devstoreaccount1/c?restype=container&comp=list"
                                   "&prefix=", {"Content-Length": "0", "x-ms-version": "v"}, "devstoreaccount1")
    assert listing == ("GET\n\n\n\n\n\n\n\n\n\n\n\nx-ms-version:v\n/devstoreaccount1/devstoreaccount1/c\n"
                       "comp:list\nprefix:\nrestype:container")
    key = base64.b64encode(b"secret").decode()
    auth = azure.shared_key("GET", "https://a.blob.core.windows.net/c", {"x-ms-version": "v"}, "a", key)
    import hmac

    expected = base64.b64encode(hmac.new(b"secret", azure.string_to_sign(
        "GET", "https://a.blob.core.windows.net/c", {"x-ms-version": "v"}, "a").encode(), "sha256").digest())
    assert auth == f"SharedKey a:{expected.decode()}"


# ------------------------------------------------------------ GCS JWT bearer
def _service_account(token_uri: str = "https://oauth2.googleapis.com/token") -> tuple[dict, object]:
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                            serialization.NoEncryption()).decode()
    return {"type": "service_account", "client_email": "backup@proj.iam.gserviceaccount.com",
            "private_key_id": "kid1", "private_key": pem, "token_uri": token_uri}, key.public_key()


def _b64d(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def _verify_jwt(jwt: str, pub) -> dict:
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import padding

    h, c, sig = jwt.split(".")
    pub.verify(_b64d(sig), f"{h}.{c}".encode(), padding.PKCS1v15(), hashes.SHA256())
    assert json.loads(_b64d(h)) == {"alg": "RS256", "typ": "JWT", "kid": "kid1"}
    return json.loads(_b64d(c))


def test_gcs_jwt_is_rs256_signed():
    info, pub = _service_account()
    claims = _verify_jwt(gcs.make_jwt(info, now=1_700_000_000), pub)
    assert claims == {"iss": info["client_email"], "scope": gcs.SCOPE, "aud": info["token_uri"],
                      "iat": 1_700_000_000, "exp": 1_700_003_600}


def test_gcs_token_flow_and_cache():
    """JWT bearer grant against a local token endpoint; the token is cached and sent as Bearer."""
    calls = {"token": 0}
    info, pub = _service_account()

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _send(self, status, data):
            body = json.dumps(data).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self):
            form = parse_qs(self.rfile.read(int(self.headers["Content-Length"])).decode())
            assert form["grant_type"] == ["urn:ietf:params:oauth:grant-type:jwt-bearer"]
            _verify_jwt(form["assertion"][0], pub)
            calls["token"] += 1
            self._send(200, {"access_token": "tok-1", "expires_in": 3600, "token_type": "Bearer"})

        def do_GET(self):
            if self.headers.get("Authorization") != "Bearer tok-1":
                return self._send(401, {"error": {"code": 401, "message": "Login Required",
                                                  "errors": [{"reason": "required"}]}})
            self._send(200, {"items": [{"name": "a/x.fbk", "size": "3", "updated": "2024-01-02T03:04:05.000Z"}]})

    srv = HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        base_url = f"http://127.0.0.1:{srv.server_port}"
        info["token_uri"] = base_url + "/token"
        c = gcs.GCSClient("bkt", json.dumps(info), endpoint=base_url)
        assert c.list("a/") == [{"key": "a/x.fbk", "size": 3, "modified": 1704164645.0}]
        c.list("a/")
        assert calls["token"] == 1
        anon = gcs.GCSClient("bkt", endpoint=base_url)
        with pytest.raises(StorageError, match="GCS: required - Login Required"):
            anon.list("")
    finally:
        srv.shutdown()


# -------------------------------------------------------------- target store
S3_SETTINGS = {"bucket": "b", "access_key_id": "AKID", "secret_access_key": "top-secret"}


def test_store_masks_secrets_and_keeps_them_on_update(tmp_path):
    st = TargetStore(tmp_path)
    t = st.add(Target(name=" s3 ", kind="s3", settings={**S3_SETTINGS, "junk": "x", "region": ""}))
    assert t.name == "s3" and t.settings == S3_SETTINGS  # unknown and empty settings dropped
    v = st.list()[0]
    assert v["settings"]["secret_access_key"] == MASK and v["settings"]["has_secret_access_key"] is True
    assert v["settings"]["has_session_token"] is False and "session_token" not in v["settings"]
    assert "top-secret" not in json.dumps(st.list())
    assert stat.S_IMODE(os.stat(tmp_path / "storage_targets.json").st_mode) == 0o600
    assert not list(tmp_path.glob("*.tmp"))
    # "********" and "" keep the stored secret; other settings merge
    t = st.update(t.id, {"settings": {"secret_access_key": MASK, "region": "eu-west-1"}})
    t = st.update(t.id, {"name": "renamed", "settings": {"secret_access_key": ""}})
    assert t.settings["secret_access_key"] == "top-secret" and t.settings["region"] == "eu-west-1"
    t = st.update(t.id, {"settings": {"secret_access_key": "new", "region": None, "path_style": "false"}})
    assert t.settings["secret_access_key"] == "new" and "region" not in t.settings
    assert t.settings["path_style"] is False
    # persisted and reloaded
    again = TargetStore(tmp_path).get(t.id)
    assert again == t and again.name == "renamed"
    st.remove(t.id)
    with pytest.raises(KeyError):
        st.get(t.id)
    with pytest.raises(KeyError):
        st.update("nope", {})


def test_store_validation(tmp_path):
    st = TargetStore(tmp_path)
    with pytest.raises(ValueError, match="missing: secret_access_key"):
        st.add(Target(name="x", kind="s3", settings={"bucket": "b", "access_key_id": "a"}))
    with pytest.raises(ValueError, match="account_key or sas_token"):
        st.add(Target(name="x", kind="azure", settings={"account": "a", "container": "c"}))
    with pytest.raises(ValueError, match="missing: service_account_json"):
        st.add(Target(name="x", kind="gcs", settings={"bucket": "b"}))
    with pytest.raises(ValueError, match="not valid JSON"):
        st.add(Target(name="x", kind="gcs", settings={"bucket": "b", "service_account_json": "{nope"}))
    with pytest.raises(ValueError, match="client_email"):
        st.add(Target(name="x", kind="gcs", settings={"bucket": "b", "service_account_json": "{}"}))
    with pytest.raises(ValueError, match="endpoint must start"):
        st.add(Target(name="x", kind="s3", settings={**S3_SETTINGS, "endpoint": "minio:9000"}))
    with pytest.raises(ValueError, match="path_style"):
        st.add(Target(name="x", kind="s3", settings={**S3_SETTINGS, "path_style": "maybe"}))
    st.add(Target(name="emulator", kind="gcs", settings={"bucket": "b", "endpoint": "http://127.0.0.1:4443"}))
    t = st.add(Target(name="sas", kind="azure", settings={"account": "a", "container": "c", "sas_token": "?sv=1"}))
    # changing the kind does not carry over the old settings
    with pytest.raises(ValueError, match="missing"):
        st.update(t.id, {"kind": "s3"})


def test_env_references(tmp_path, monkeypatch):
    st = TargetStore(tmp_path)
    t = st.add(Target(name="e", kind="s3", settings={**S3_SETTINGS, "secret_access_key": "env:FBA_TEST_SECRET"}))
    v = st.view(t)["settings"]
    assert v["secret_access_key"] == "env:FBA_TEST_SECRET" and v["has_secret_access_key"]  # not a secret itself
    monkeypatch.delenv("FBA_TEST_SECRET", raising=False)
    with pytest.raises(ValueError, match="FBA_TEST_SECRET"):
        storage.open_client(t)
    monkeypatch.setenv("FBA_TEST_SECRET", "from-env")
    s = resolved(t)
    assert s["secret_access_key"] == "from-env" and s["region"] == "us-east-1" and s["path_style"] is False
    c = storage.open_client(t)
    assert isinstance(c, s3.S3Client) and c.secret_key == "from-env"
    t2 = st.add(Target(name="e2", kind="s3", settings={**S3_SETTINGS, "endpoint": "http://h:1"}))
    assert storage.open_client(t2).path_style is True
    t3 = st.add(Target(name="e3", kind="s3", settings={**S3_SETTINGS, "bucket": "backups.example.com"}))
    assert storage.open_client(t3).path_style is True


# ------------------------------------------------------------------ retention
def _o(key, modified):
    return {"key": key, "size": 1, "modified": modified}


def test_select_expired():
    objs = [_o("db/a.fbk", 1), _o("db/b.fbk", 3), _o("db/c.fbk", 2), _o("db/d.fbk", 4),
            _o("db/notes.txt", 0), _o("db/sub/old.fbk", 0), _o("dbx/old.fbk", 0)]
    assert [o["key"] for o in storage.select_expired(objs, "db", 2)] == ["db/c.fbk", "db/a.fbk"]
    assert storage.select_expired(objs, "db", 0) == []
    assert storage.select_expired(objs, "db", 10) == []
    # the just-uploaded key is always kept, even if the provider's clock says it is older
    assert [o["key"] for o in storage.select_expired(objs, "db/", 1, protect="db/a.fbk")] == \
        ["db/d.fbk", "db/b.fbk", "db/c.fbk"]
    root = [_o("x.fbk", 1), _o("y.fbk", 2), _o("dir/z.fbk", 0)]
    assert [o["key"] for o in storage.select_expired(root, "", 1)] == ["x.fbk"]


def test_join_key():
    assert join_key("", "a.fbk") == "a.fbk"
    assert join_key("backups/", "/sched1/", "a.fbk") == "backups/sched1/a.fbk"


class FakeClient:
    def __init__(self, objs):
        self.objs, self.deleted, self.uploaded = objs, [], []

    def upload_file(self, path, key, log=None):
        self.uploaded.append(key)
        self.objs.append(_o(key, 100))
        return {"key": key, "size": path.stat().st_size}

    def list(self, prefix):
        return [o for o in self.objs if o["key"].startswith(prefix)]

    def delete(self, key):
        if key == "base/s1/broken.fbk":
            raise StorageError("S3: AccessDenied")
        self.deleted.append(key)


def test_upload_with_retention(tmp_path, monkeypatch):
    from fbadmin.config import settings

    monkeypatch.setattr(settings, "data_dir", tmp_path)
    t = storage.get_store().add(Target(name="t", kind="s3", settings={**S3_SETTINGS, "prefix": "/base/"}))
    fake = FakeClient([_o("base/s1/old1.fbk", 1), _o("base/s1/broken.fbk", 2), _o("base/s1/old3.fbk", 3)])
    monkeypatch.setattr(storage, "open_client", lambda target: fake)
    f = tmp_path / "db-20240101.fbk"
    f.write_bytes(b"12345")
    logs: list[str] = []
    r = storage.upload_with_retention(t.id, f, "s1/", 2, logs.append)
    assert fake.uploaded == ["base/s1/db-20240101.fbk"]
    assert r["target"] == "t" and r["key"] == "base/s1/db-20240101.fbk" and r["size"] == 5
    assert r["removed"] == ["base/s1/old1.fbk"] and len(r["errors"]) == 1
    assert any("could not delete base/s1/broken.fbk" in m for m in logs)
    r = storage.upload_with_retention(t.id, f, "", 0)
    assert r["key"] == "base/db-20240101.fbk" and r["removed"] == []
    with pytest.raises(KeyError):
        storage.upload_with_retention("missing", f, "", 1)


# ------------------------------------------------------------------------ API
def test_api_crud_desktop(client):
    r = client.post("/api/storage/targets", json={"name": "s3", "kind": "s3", "settings": S3_SETTINGS})
    assert r.status_code == 201, r.text
    t = r.json()
    assert t["settings"]["secret_access_key"] == MASK and "top-secret" not in r.text
    assert client.get("/api/storage/targets").json() == [t]
    assert client.get(f"/api/storage/targets/{t['id']}").json() == t
    r = client.put(f"/api/storage/targets/{t['id']}", json={"settings": {**t["settings"], "bucket": "b2"}})
    assert r.status_code == 200 and r.json()["settings"]["bucket"] == "b2"
    assert storage.get_store().get(t["id"]).settings["secret_access_key"] == "top-secret"
    assert client.post("/api/storage/targets", json={"name": "x", "kind": "s3", "settings": {}}).status_code == 400
    assert client.post("/api/storage/targets", json={"name": "x", "kind": "ftp", "settings": {}}).status_code == 422
    assert client.get("/api/storage/targets/nope").status_code == 404
    assert client.post("/api/storage/targets/nope/test").status_code == 404
    # an unreachable endpoint is a 502 with the reason
    r = client.post("/api/storage/targets", json={"name": "dead", "kind": "s3", "settings": {
        **S3_SETTINGS, "endpoint": "http://127.0.0.1:9"}})
    dead = r.json()["id"]
    r = client.post(f"/api/storage/targets/{dead}/test")
    assert r.status_code == 502 and "cannot reach http://127.0.0.1:9" in r.json()["detail"]
    assert client.delete(f"/api/storage/targets/{t['id']}").status_code == 204
    assert client.delete(f"/api/storage/targets/{t['id']}").status_code == 404


def test_api_admin_only_in_server_mode(tmp_path, monkeypatch):
    from .test_security import _client

    with _client(tmp_path, monkeypatch, SERVER_MODE="1", ADMIN_EMAIL="admin@example.org",
                 ADMIN_PASSWORD=ADMIN_PW) as c:
        assert c.get("/api/storage/targets").status_code == 401
        login(c, "admin@example.org", ADMIN_PW)
        assert c.post("/api/admin/users", json={"username": "alice", "password": "alice-password",
                                                 "role": "user"}).status_code == 201
        r = c.post("/api/storage/targets", json={"name": "s3", "kind": "s3", "settings": S3_SETTINGS})
        assert r.status_code == 201
        tid = r.json()["id"]
        c.post("/api/auth/logout")
        c.headers.pop("X-CSRF-Token", None)
        login(c, "alice", "alice-password")
        assert c.get("/api/storage/targets").status_code == 403
        assert c.get(f"/api/storage/targets/{tid}").status_code == 403
        assert c.post(f"/api/storage/targets/{tid}/test").status_code == 403
        assert c.delete(f"/api/storage/targets/{tid}").status_code == 403
    from fbadmin.api import deps

    deps.reset_state()
