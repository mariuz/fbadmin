"""Roadmap 2.2: audit log forwarding to syslog (UDP, TCP, TLS) and webhooks - cursors, catching up after an
outage, filters, secrets, the environment, the background thread and one forwarder per data directory."""

from __future__ import annotations

import datetime as dt
import hashlib
import hmac
import json
import socket
import ssl
import threading
import time

import pytest

from fbadmin.security import forward as F

from .test_alerts import Receiver
from .test_security import _client


# ------------------------------------------------------------------ receivers
class UdpReceiver:
    def __init__(self):
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind(("127.0.0.1", 0))
        self.port = self.sock.getsockname()[1]
        self.messages: list[str] = []
        threading.Thread(target=self._loop, daemon=True).start()

    def _loop(self):
        while True:
            try:
                data, _ = self.sock.recvfrom(70000)
            except OSError:
                return
            self.messages.append(data.decode())

    def close(self):
        self.sock.close()


class TcpReceiver:
    """Octet-counted syslog over TCP, or TLS with ``ctx``."""

    def __init__(self, ctx: ssl.SSLContext | None = None):
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen()
        self.port = self.sock.getsockname()[1]
        self.ctx = ctx
        self.messages: list[str] = []
        threading.Thread(target=self._loop, daemon=True).start()

    def _loop(self):
        while True:
            try:
                conn, _ = self.sock.accept()
            except OSError:
                return
            try:
                if self.ctx:
                    conn = self.ctx.wrap_socket(conn, server_side=True)
                buf = b""
                while True:
                    chunk = conn.recv(65536)
                    if not chunk:
                        break
                    buf += chunk
                while buf:
                    n, _, rest = buf.partition(b" ")
                    size = int(n)
                    self.messages.append(rest[:size].decode())
                    buf = rest[size:]
            except (OSError, ssl.SSLError, ValueError):
                pass
            finally:
                conn.close()

    def close(self):
        self.sock.close()


def _wait(cond, timeout=5.0):
    end = time.time() + timeout
    while time.time() < end:
        if cond():
            return True
        time.sleep(0.05)
    return False


def _self_signed(tmp_path):
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID

    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")])
    now = dt.datetime.now(dt.timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
            .serial_number(x509.random_serial_number()).not_valid_before(now - dt.timedelta(minutes=5))
            .not_valid_after(now + dt.timedelta(days=1))
            .add_extension(x509.SubjectAlternativeName([x509.DNSName("localhost")]), critical=False)
            .sign(key, hashes.SHA256()))
    cp, kp = tmp_path / "cert.pem", tmp_path / "key.pem"
    cp.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    kp.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                     serialization.NoEncryption()))
    return cp, kp


# ------------------------------------------------------------------ units
def test_syslog_format_and_webhook_signature():
    e = F.entry({"id": 7, "ts": 1761800000.5, "username": 'a"b]c\\', "category": "DDL", "success": 0,
                 "statement": "DROP TABLE T", "error": "in use", "server_name": "prod", "db_name": "app"})
    m = F.syslog_message(e, "local0", hostname="db1.example.com").decode()
    assert m.startswith("<132>1 2025-10-30T04:53:20.500+00:00 db1 fbadmin ")  # local0 (16) * 8 + warning (4)
    assert '[fbadmin@32473 id="7" user="a\\"b\\]c\\\\" category="DDL" success="False" server="prod" database="app"]' in m
    body = json.loads(m.split("] ", 1)[1])
    assert body["statement"] == "DROP TABLE T" and body["error"] == "in use" and "client_ip" not in body
    ok = F.syslog_message(F.entry({"id": 1, "ts": 1.0, "success": 1}), "auth").decode()
    assert ok.startswith("<37>1 ")  # auth (4) * 8 + notice (5)
    big = F.syslog_message(F.entry({"id": 2, "ts": 1.0, "success": 1, "statement": "x" * 20000}), "local0", max_len=4000)
    assert len(big) <= 4000 and b'"truncated": true' in big
    body = F.webhook_body([e])
    assert json.loads(body)["entries"][0]["id"] == 7
    assert F.signature("k", body) == "sha256=" + hmac.new(b"k", body, hashlib.sha256).hexdigest()
    with pytest.raises(ValueError):
        F.Destination(name="x", kind="syslog", host="h", facility="nope")
    with pytest.raises(ValueError):
        F.Destination(name="x", kind="webhook", url="ftp://x").check()


def test_environment_destinations(monkeypatch):
    monkeypatch.setenv("FBADMIN_AUDIT_SYSLOG", "tls://logs.example.com:6514?facility=auth&verify=0")
    monkeypatch.setenv("FBADMIN_AUDIT_WEBHOOK", "https://siem.example.com/in")
    monkeypatch.setenv("FBADMIN_AUDIT_WEBHOOK_SECRET", "s")
    s, w = F.env_destinations()
    assert (s.protocol, s.host, s.port, s.facility, s.tls_verify) == ("tls", "logs.example.com", 6514, "auth", False)
    assert w.url == "https://siem.example.com/in" and w.secret == "env:FBADMIN_AUDIT_WEBHOOK_SECRET" and w.token is None
    monkeypatch.setenv("FBADMIN_AUDIT_SYSLOG", "http://x")
    with pytest.raises(ValueError):
        F.env_destinations()


# ------------------------------------------------------------------ through the API
@pytest.fixture
def audited(tmp_path, monkeypatch):
    with _client(tmp_path, monkeypatch, AUDIT="1", AUDIT_FORWARD_INTERVAL="3600") as c:
        _sec().forwarder.stop()  # these tests run the forwarder themselves
        time.sleep(0.05)
        yield c
    from fbadmin.api import deps

    deps.reset_state()


def _sec():
    from fbadmin.api.deps import get_state

    return get_state().security


def _record(n=1, **kw):
    for i in range(n):
        _sec().audit.record(server_id=None, db_id=None, statement=kw.get("statement", f"CREATE TABLE T{i} (ID INTEGER)"),
                            success=kw.get("success", True), error=kw.get("error"), cat=kw.get("cat"))


def test_webhook_cursor_outage_filters_and_secrets(audited):
    c = audited
    rcv = Receiver()
    try:
        _record(2)  # before the destination: not sent ("now")
        r = c.post("/api/admin/audit/forwarding", json={"name": "siem", "kind": "webhook", "url": rcv.url + "/in",
                                                         "secret": "k3y", "token": "tok"})
        assert r.status_code == 201, r.text
        d = r.json()
        assert d["secret"] == F.MASK and d["token"] == F.MASK
        assert d["backlog"] == 1  # adding a destination is audited (and forwarded) too
        _record(1, statement="DROP TABLE GONE", success=False, error="in use")
        _record(1, statement="INSERT INTO T VALUES (1)", cat="DML")
        res = _sec().forwarder.run_once()
        assert "3 entries: HTTP 200" in res[d["id"]]
        req = rcv.requests[-1]
        assert req["headers"]["X-Fbadmin-Signature"] == F.signature("k3y", req["body"])
        assert req["headers"]["Authorization"] == "Bearer tok"
        got = json.loads(req["body"])["entries"]
        assert [g["statement"] for g in got] == ["-- audit forwarding: added webhook destination 'siem'",
                                                 "DROP TABLE GONE", "INSERT INTO T VALUES (1)"]
        assert got[1]["success"] is False and got[1]["error"] == "in use" and got[2]["category"] == "DML"
        view = c.get("/api/admin/audit/forwarding").json()["destinations"][0]
        assert view["backlog"] == 0 and view["sent"] == 3 and view["last_error"] is None

        # the destination is down: nothing is lost, it catches up after a pause
        rcv.status = 500
        _record(3)
        res = _sec().forwarder.run_once()
        assert res[d["id"]].startswith("error: HTTP 500")
        view = c.get("/api/admin/audit/forwarding").json()["destinations"][0]
        assert view["backlog"] == 3 and view["last_error"].startswith("HTTP 500")
        n = len(rcv.requests)
        assert _sec().forwarder.run_once() == {} and len(rcv.requests) == n  # waiting before the next attempt
        rcv.status = 200
        # a change (here: keep the secret, only filters) retries at once
        r = c.put(f"/api/admin/audit/forwarding/{d['id']}", json={"secret": F.MASK, "categories": ["DDL"],
                                                                  "failures_only": False})
        assert r.status_code == 200, r.text
        res = _sec().forwarder.run_once()
        got = json.loads(rcv.requests[-1]["body"])["entries"]
        assert len(got) == 3 and all(g["category"] == "DDL" for g in got)
        assert rcv.requests[-1]["headers"]["X-Fbadmin-Signature"] == F.signature("k3y", rcv.requests[-1]["body"])
        view = c.get("/api/admin/audit/forwarding").json()["destinations"][0]
        assert view["backlog"] == 0 and view["sent"] == 6 and view["last_error"] is None

        # only failures
        c.put(f"/api/admin/audit/forwarding/{d['id']}", json={"categories": [], "failures_only": True})
        _sec().forwarder.run_once()  # flushes the audit entry of that change (filtered)
        n = len(rcv.requests)
        _record(2)
        _record(1, statement="ALTER TABLE X", success=False, error="no")
        _sec().forwarder.run_once()
        assert len(rcv.requests) == n + 1
        assert [g["statement"] for g in json.loads(rcv.requests[-1]["body"])["entries"]] == ["ALTER TABLE X"]

        # resend everything; a test message; delete
        c.put(f"/api/admin/audit/forwarding/{d['id']}", json={"failures_only": False})
        c.post(f"/api/admin/audit/forwarding/{d['id']}/resend", json={"after_id": 0})
        _sec().forwarder.run_once()
        assert json.loads(rcv.requests[-1]["body"])["entries"][0]["id"] == 1
        t = c.post(f"/api/admin/audit/forwarding/{d['id']}/test").json()
        assert t["ok"] and "HTTP 200" in t["result"]
        assert c.delete(f"/api/admin/audit/forwarding/{d['id']}").status_code == 204
        assert c.get("/api/admin/audit/forwarding").json()["destinations"] == []
        assert c.post("/api/admin/audit/forwarding", json={"name": "x", "kind": "webhook", "url": "nope"}).status_code == 400
    finally:
        rcv.close()


def test_syslog_udp_tcp_tls(audited, tmp_path):
    c = audited
    cp, kp = _self_signed(tmp_path)
    sctx = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
    sctx.load_cert_chain(cp, kp)
    udp, tcp, tls = UdpReceiver(), TcpReceiver(), TcpReceiver(sctx)
    try:
        ids = {}
        for name, body in (("udp", {"protocol": "udp", "host": "127.0.0.1", "port": udp.port}),
                           ("tcp", {"protocol": "tcp", "host": "127.0.0.1", "port": tcp.port, "facility": "authpriv"}),
                           ("tls", {"protocol": "tls", "host": "localhost", "port": tls.port, "ca_file": str(cp)})):
            r = c.post("/api/admin/audit/forwarding", json={"name": name, "kind": "syslog", "start": "all", **body})
            assert r.status_code == 201, r.text
            ids[name] = r.json()["id"]
        _record(2)
        res = _sec().forwarder.run_once()
        assert all("message(s) to" in res[i] for i in ids.values()), res
        assert _wait(lambda: len(udp.messages) >= 5 and len(tcp.messages) >= 5 and len(tls.messages) >= 5)
        # "start": "all": the three "added ... destination" entries came first
        for msgs, pri in ((udp.messages, "<133>1 "), (tcp.messages, "<85>1 "), (tls.messages, "<133>1 ")):
            assert all(m.startswith(pri) for m in msgs), msgs[0]
            assert "AUDIT [fbadmin@32473" in msgs[-1]
            assert json.loads(msgs[-1].split("] ", 1)[1])["statement"] == "CREATE TABLE T1 (ID INTEGER)"
        # a TLS certificate that isn't trusted is an error (and retried later)
        r = c.post("/api/admin/audit/forwarding", json={"name": "untrusted", "kind": "syslog", "protocol": "tls",
                                                         "host": "localhost", "port": tls.port})
        assert c.post(f"/api/admin/audit/forwarding/{r.json()['id']}/test").json()["ok"] is False
    finally:
        for x in (udp, tcp, tls):
            x.close()


def test_background_thread_and_one_forwarder(tmp_path, monkeypatch):
    rcv = Receiver()
    monkeypatch.setenv("FBADMIN_TEST_DUMMY", "1")
    try:
        with _client(tmp_path, monkeypatch, AUDIT="1", AUDIT_WEBHOOK=rcv.url + "/env",
                     AUDIT_FORWARD_INTERVAL="3600") as c:
            dests = c.get("/api/admin/audit/forwarding").json()["destinations"]
            assert [(d["id"], d["source"]) for d in dests] == [("env-webhook", "environment")]
            assert c.put("/api/admin/audit/forwarding/env-webhook", json={"enabled": False}).status_code == 404
            _sec().forwarder.notify()
            assert _wait(lambda: _sec().forwarder.store.state("env-webhook").get("cursor") is not None)
            _record(1, statement="CREATE TABLE FROM_THREAD (ID INTEGER)")  # wakes the thread: no 3600 s wait
            assert _wait(lambda: any(b"FROM_THREAD" in r["body"] for r in rcv.requests))
            # a second process on the same data directory doesn't forward
            other = F.Forwarder(_sec().db, F.Store(tmp_path))
            _record(1)
            assert other.run_once() == {}
    finally:
        rcv.close()
        from fbadmin.api import deps

        deps.reset_state()
