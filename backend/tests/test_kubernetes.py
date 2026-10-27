"""Kubernetes integration (roadmap 2.2): the cloudnative-firebird operator's clusters, against a fake
Kubernetes API that stores resources, applies merge patches and records every request."""

from __future__ import annotations

import base64
import copy
import json
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from fbadmin.kube import client as kc
from fbadmin.kube import operator as op

G = "/apis/firebird.cloudnative-firebird.io/v1"
TOKEN = "test-token"


def merge(target, patch):
    if not isinstance(patch, dict):
        return patch
    out = dict(target) if isinstance(target, dict) else {}
    for k, v in patch.items():
        if v is None:
            out.pop(k, None)
        else:
            out[k] = merge(out.get(k), v)
    return out


class FakeKube:
    def __init__(self):
        self.objects: dict[str, dict] = {}  # path -> object
        self.requests: list[dict] = []
        self.forbidden: set[str] = set()     # path prefixes answering 403
        self.logs = "firebird log line 1\nline 2\n"
        outer = self

        class H(BaseHTTPRequestHandler):
            def _send(self, code, body, ctype="application/json"):
                data = body.encode() if isinstance(body, str) else json.dumps(body).encode()
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def _status(self, code, msg, reason=""):
                self._send(code, {"kind": "Status", "status": "Failure", "message": msg, "reason": reason, "code": code})

            def _handle(self, method):
                url = urllib.parse.urlparse(self.path)
                q = dict(urllib.parse.parse_qsl(url.query))
                n = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(n)) if n else None
                outer.requests.append({"method": method, "path": url.path, "query": q, "body": body,
                                       "ctype": self.headers.get("Content-Type")})
                if self.headers.get("Authorization") != f"Bearer {TOKEN}":
                    return self._status(401, "Unauthorized", "Unauthorized")
                path = url.path
                if any(path.startswith(p) for p in outer.forbidden):
                    return self._status(403, f'"{path}" is forbidden: User "system:serviceaccount:x:fbadmin" '
                                             "cannot get resource", "Forbidden")
                if path == G:
                    return self._send(200, {"kind": "APIResourceList", "groupVersion": "firebird.cloudnative-firebird.io/v1"})
                if path.endswith("/log"):
                    return self._send(200, outer.logs, "text/plain")
                if method == "GET" and path in outer.objects:
                    return self._send(200, outer.objects[path])
                if method == "GET":  # a list
                    items = [o for p, o in outer.objects.items() if p.rsplit("/", 1)[0] == path]
                    sel = q.get("labelSelector")
                    if sel:
                        want = dict(kv.split("=", 1) for kv in sel.split(","))
                        items = [o for o in items if all((o["metadata"].get("labels") or {}).get(k) == v
                                                         for k, v in want.items())]
                    fs = q.get("fieldSelector")
                    if fs:
                        k, v = fs.split("=", 1)
                        items = [o for o in items if (o.get("involvedObject") or {}).get("name") == v]
                    if not items and not any(p.startswith(path + "/") for p in outer.objects) and "/namespaces/" not in path:
                        return self._status(404, "not found", "NotFound")
                    return self._send(200, {"items": items})
                if method == "PATCH":
                    if path not in outer.objects:
                        return self._status(404, f"{path} not found", "NotFound")
                    outer.objects[path] = merge(outer.objects[path], body)
                    return self._send(200, outer.objects[path])
                if method == "POST":
                    p = f"{path}/{body['metadata']['name']}"
                    if p in outer.objects:
                        return self._status(409, "already exists", "AlreadyExists")
                    outer.objects[p] = body
                    return self._send(201, body)
                if method == "DELETE":
                    outer.objects.pop(path, None)
                    return self._send(200, {"kind": "Status", "status": "Success"})
                return self._status(405, "method not allowed")

            def do_GET(self):  # noqa: N802
                self._handle("GET")

            def do_PATCH(self):  # noqa: N802
                self._handle("PATCH")

            def do_POST(self):  # noqa: N802
                self._handle("POST")

            def do_DELETE(self):  # noqa: N802
                self._handle("DELETE")

            def log_message(self, *a):
                pass

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def put(self, path: str, obj: dict) -> None:
        self.objects[path] = copy.deepcopy(obj)

    def last(self, method: str) -> dict:
        return next(r for r in reversed(self.requests) if r["method"] == method)

    def close(self):
        self.httpd.shutdown()


def cluster(name="fbcluster", ns="db", *, instances=1, replication=False, s3=None, **status):
    spec = {"instances": instances, "imageName": "firebirdsql/firebird:5", "databaseName": "demo.fdb",
            "superuserSecret": {"name": f"{name}-superuser"}}
    if replication:
        spec["replication"] = {"enabled": True, "mode": "async"}
    if s3:
        spec["backup"] = {"s3": s3}
    return {"apiVersion": "firebird.cloudnative-firebird.io/v1", "kind": "FirebirdCluster",
            "metadata": {"name": name, "namespace": ns, "creationTimestamp": "2026-10-20T10:00:00Z"},
            "spec": spec, "status": {"phase": "Running", "readyInstances": instances, "image": "firebirdsql/firebird:5",
                                     **status}}


def pod(cluster_name, i, ns="db", *, role=None, ready=True):
    labels = {"firebird.cloudnative-firebird.io/cluster": cluster_name, "app.kubernetes.io/component": "database"}
    if role:
        labels["firebird.cloudnative-firebird.io/role"] = role
    return {"metadata": {"name": f"{cluster_name}-{i}", "namespace": ns, "labels": labels},
            "spec": {"nodeName": f"node-{i}"},
            "status": {"phase": "Running", "startTime": "2026-10-20T10:01:00Z",
                       "conditions": [{"type": "Ready", "status": "True" if ready else "False"}],
                       "containerStatuses": [{"name": "firebird", "restartCount": i}]}}


@pytest.fixture
def kube(tmp_path, monkeypatch):
    fake = FakeKube()
    cfg = tmp_path / "kubeconfig"
    cfg.write_text(f"""
apiVersion: v1
kind: Config
current-context: test
contexts:
  - name: test
    context: {{cluster: fake, user: fbadmin, namespace: db}}
clusters:
  - name: fake
    cluster: {{server: "{fake.url}"}}
users:
  - name: fbadmin
    user: {{token: "{TOKEN}"}}
""")
    monkeypatch.setenv("FBADMIN_KUBECONFIG", str(cfg))
    monkeypatch.delenv("FBADMIN_K8S_NAMESPACES", raising=False)
    monkeypatch.delenv("FBADMIN_KUBERNETES", raising=False)
    op.reset()
    ns = "/namespaces/db"
    fake.put(f"{G}{ns}/firebirdclusters/fbcluster", cluster())
    fake.put(f"{G}{ns}/firebirdclusters/repl", cluster("repl", instances=3, replication=True,
                                                       replicationStatus={"primaryPod": "repl-1"},
                                                       s3=None))
    for i in range(1):
        fake.put(f"/api/v1{ns}/pods/fbcluster-{i}", pod("fbcluster", i, role="primary"))
    for i in range(3):
        fake.put(f"/api/v1{ns}/pods/repl-{i}", pod("repl", i, role="primary" if i == 1 else "replica", ready=i != 2))
    fake.put(f"/apis/apps/v1{ns}/statefulsets/fbcluster", {"metadata": {"name": "fbcluster"}, "spec": {"template": {}}})
    fake.put(f"/api/v1{ns}/secrets/fbcluster-superuser",
             {"data": {"password": base64.b64encode(b"s3cret-pw").decode()}})
    fake.put(f"{G}{ns}/firebirdbackups/fbcluster-old", {
        "metadata": {"name": "fbcluster-old", "creationTimestamp": "2026-10-21T00:00:00Z"},
        "spec": {"clusterName": "fbcluster", "type": "logical"},
        "status": {"phase": "Completed", "backupFileName": "fbcluster-old.fbk", "startTime": "2026-10-21T00:00:01Z",
                   "completionTime": "2026-10-21T00:00:09Z", "verified": True}})
    fake.put(f"{G}{ns}/firebirdbackups/fbcluster-new", {
        "metadata": {"name": "fbcluster-new", "creationTimestamp": "2026-10-22T00:00:00Z"},
        "spec": {"clusterName": "fbcluster", "type": "logical"}, "status": {"phase": "Running"}})
    fake.put(f"{G}{ns}/firebirdbackups/repl-x", {"metadata": {"name": "repl-x"}, "spec": {"clusterName": "repl"}})
    fake.put(f"{G}{ns}/firebirddatabases/sales", {
        "metadata": {"name": "sales"}, "spec": {"clusterName": "fbcluster", "databaseName": "sales.fdb"},
        "status": {"phase": "Ready", "path": "/var/lib/firebird/data/sales.fdb"}})
    fake.put(f"{G}{ns}/firebirdscheduledbackups/nightly", {
        "metadata": {"name": "nightly"}, "spec": {"clusterName": "fbcluster", "schedule": "0 2 * * *"},
        "status": {"lastSuccessfulTime": "2026-10-22T02:00:10Z", "lastBackupName": "nightly-1"}})
    fake.put(f"/api/v1{ns}/events/e1", {"involvedObject": {"name": "fbcluster"}, "type": "Normal",
                                         "reason": "BackupCompleted", "message": "fbcluster-old completed",
                                         "lastTimestamp": "2026-10-21T00:00:09Z", "count": 1})
    yield fake
    fake.close()
    op.reset()


# ------------------------------------------------------------------ client configuration
def test_in_cluster_and_kubeconfig_loading(tmp_path, monkeypatch):
    for k in ("FBADMIN_KUBECONFIG", "FBADMIN_KUBERNETES", "KUBERNETES_SERVICE_HOST"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("FBADMIN_KUBE_SA_DIR", str(tmp_path / "sa"))
    with pytest.raises(kc.KubeNotConfigured):
        kc.load()
    (tmp_path / "sa").mkdir()
    (tmp_path / "sa" / "token").write_text("sa-token\n")
    (tmp_path / "sa" / "namespace").write_text("fbadmin-demo")
    monkeypatch.setenv("KUBERNETES_SERVICE_HOST", "10.96.0.1")
    monkeypatch.setenv("KUBERNETES_SERVICE_PORT", "443")
    c = kc.load()
    assert (c.server, c.token, c.namespace, c.source) == ("https://10.96.0.1:443", "sa-token", "fbadmin-demo", "in-cluster")
    monkeypatch.setenv("FBADMIN_KUBERNETES", "off")
    with pytest.raises(kc.KubeNotConfigured, match="off"):
        kc.load()
    monkeypatch.delenv("FBADMIN_KUBERNETES")
    # kubeconfig: exec plugins are refused, unknown contexts too
    cfg = tmp_path / "kc"
    cfg.write_text("current-context: a\ncontexts: [{name: a, context: {cluster: c, user: u}}]\n"
                   "clusters: [{name: c, cluster: {server: 'https://k:6443', insecure-skip-tls-verify: true}}]\n"
                   "users: [{name: u, user: {exec: {command: aws}}}]\n")
    monkeypatch.setenv("FBADMIN_KUBECONFIG", str(cfg))
    with pytest.raises(kc.KubeNotConfigured, match="exec"):
        kc.load()
    with pytest.raises(kc.KubeNotConfigured, match="not found"):
        kc.from_kubeconfig(cfg, "nope")


# ------------------------------------------------------------------ API
def test_status_list_and_detail(client, kube):
    st = client.get("/api/kubernetes/status").json()
    assert st["enabled"] and st["operator"] and st["namespaces"] == ["db"] and "kubeconfig" in st["source"]
    items = client.get("/api/kubernetes/clusters").json()
    assert [c["name"] for c in items] == ["fbcluster", "repl"]
    fb = items[0]
    assert fb["phase"] == "Running" and fb["instances"] == 1 and fb["ready"] == 1 and fb["replication"] is False
    assert fb["registered_server_id"] is None and items[1]["primary"] == "repl-1"
    d = client.get("/api/kubernetes/clusters/db/fbcluster").json()
    assert [p["name"] for p in d["pods"]] == ["fbcluster-0"] and d["pods"][0]["role"] == "primary"
    assert [b["name"] for b in d["backups"]] == ["fbcluster-new", "fbcluster-old"]  # newest first, own only
    assert d["backups"][1]["phase"] == "Completed" and d["backups"][1]["location"] == "fbcluster-old.fbk"
    assert d["databases"] == [{"name": "sales", "file": "sales.fdb", "phase": "Ready",
                               "path": "/var/lib/firebird/data/sales.fdb"}]
    assert d["scheduled_backups"][0]["schedule"] == "0 2 * * *" and d["events"][0]["reason"] == "BackupCompleted"
    r = client.get("/api/kubernetes/clusters/db/repl").json()
    assert {p["name"]: (p["role"], p["ready"]) for p in r["pods"]} == {
        "repl-0": ("replica", True), "repl-1": ("primary", True), "repl-2": ("replica", False)}
    assert client.get("/api/kubernetes/clusters/db/missing").status_code == 404
    # namespaces fbadmin does not manage
    assert client.get("/api/kubernetes/clusters/other/fbcluster").status_code == 403
    assert client.get("/api/kubernetes/clusters/db/Bad_Name").status_code == 400
    assert client.get("/api/kubernetes/clusters/db/fbcluster/logs", params={"pod": "0"}).json()["text"].startswith("firebird log")
    assert kube.last("GET")["query"] == {"container": "firebird", "tailLines": "200"}


def test_actions_edit_the_resources_like_the_plugin(client, kube):
    b = "/api/kubernetes/clusters/db/"
    cl = f"{G}/namespaces/db/firebirdclusters/"
    assert client.post(b + "fbcluster/scale", json={"instances": 2}).status_code == 200
    assert kube.last("PATCH")["ctype"] == "application/merge-patch+json"
    assert kube.objects[cl + "fbcluster"]["spec"]["instances"] == 2
    assert client.post(b + "fbcluster/scale", json={"instances": 0}).status_code == 422
    client.post(b + "fbcluster/hibernate", json={"on": True})
    assert kube.objects[cl + "fbcluster"]["spec"]["hibernated"] is True
    assert client.post(b + "fbcluster/restart", json={}).status_code == 400  # hibernated
    client.post(b + "fbcluster/hibernate", json={"on": False})
    # rolling restart: the StatefulSet's pod template annotation
    assert client.post(b + "fbcluster/restart", json={}).status_code == 200
    ann = kube.objects["/apis/apps/v1/namespaces/db/statefulsets/fbcluster"]["spec"]["template"]["metadata"]["annotations"]
    assert "kubectl.kubernetes.io/restartedAt" in ann
    # one instance: its pod is deleted; unknown instances are refused
    assert client.post(b + "fbcluster/restart", json={"instance": "0"}).status_code == 200
    assert "/api/v1/namespaces/db/pods/fbcluster-0" not in kube.objects
    assert client.post(b + "fbcluster/restart", json={"instance": "7"}).status_code == 400
    # switchover needs replication, a replica, and not the primary
    assert client.post(b + "fbcluster/switchover", json={"target": "1"}).status_code == 400
    assert client.post(b + "repl/switchover", json={"target": "repl-1"}).status_code == 400
    r = client.post(b + "repl/switchover", json={"target": "2"})
    assert r.status_code == 200 and "not ready" in r.json()["message"]
    assert kube.objects[cl + "repl"]["metadata"]["annotations"]["firebird.cloudnative-firebird.io/targetPrimary"] == "repl-2"
    # fencing: the annotation as a JSON list; lifting one out of "all" keeps the others
    fenced = lambda: kube.objects[cl + "repl"]["metadata"].get("annotations", {}).get(  # noqa: E731
        "firebird.cloudnative-firebird.io/fencedInstances")
    client.post(b + "repl/fence", json={"on": True, "instance": "0"})
    assert json.loads(fenced()) == ["repl-0"]
    client.post(b + "repl/fence", json={"on": True, "instance": "all"})
    assert json.loads(fenced()) == ["*"]
    r = client.post(b + "repl/fence", json={"on": False, "instance": "repl-1"}).json()
    assert r["fenced"] == ["repl-0", "repl-2"]
    client.post(b + "repl/fence", json={"on": False, "instance": "*"})
    assert fenced() is None


def test_backups_and_restores(client, kube):
    b = "/api/kubernetes/clusters/db/"
    r = client.post(b + "fbcluster/backups", json={"type": "logical", "name": "manual-1"})
    assert r.status_code == 201, r.text
    created = kube.objects[f"{G}/namespaces/db/firebirdbackups/manual-1"]
    assert created["kind"] == "FirebirdBackup" and created["spec"] == {"clusterName": "fbcluster", "type": "logical"}
    assert client.post(b + "fbcluster/backups", json={"type": "logical", "name": "manual-1"}).status_code == 409
    assert client.post(b + "fbcluster/backups", json={"type": "logical", "level": 1}).status_code == 400
    assert client.post(b + "fbcluster/backups", json={"type": "physical", "level": 1}).status_code == 201
    # a cluster with S3: the backup goes there too
    kube.put(f"{G}/namespaces/db/firebirdclusters/s3c", cluster("s3c", s3={"bucket": "bk", "prefix": "fb"}))
    client.post(b + "s3c/backups", json={"name": "s3-1"})
    assert kube.objects[f"{G}/namespaces/db/firebirdbackups/s3-1"]["spec"]["s3"] == {"bucket": "bk", "prefix": "fb"}
    # restores: only completed backups of this cluster
    assert client.post(b + "fbcluster/restores", json={"backup": "fbcluster-new"}).status_code == 400
    assert client.post(b + "fbcluster/restores", json={"backup": "repl-x"}).status_code == 400
    assert client.post(b + "fbcluster/restores", json={"backup": "fbcluster-old", "target": "../x.fdb"}).status_code == 400
    r = client.post(b + "fbcluster/restores", json={"backup": "fbcluster-old", "target": "restored.fdb"})
    assert r.status_code == 201 and r.json()["file"] == "restored.fdb"
    rest = kube.last("POST")["body"]
    assert rest["kind"] == "FirebirdRestore" and rest["spec"] == {"clusterName": "fbcluster", "backupName": "fbcluster-old",
                                                                  "targetDatabase": "restored.fdb"}


def test_register_as_fbadmin_server(client, kube):
    b = "/api/kubernetes/clusters/db/"
    r = client.post(b + "fbcluster/register", json={})
    assert r.status_code == 200, r.text
    res = r.json()
    assert res["server_id"] == "k8s-db-fbcluster" and res["created"] and res["password_from_secret"]
    srv = next(s for s in client.get("/api/servers").json() if s["id"] == "k8s-db-fbcluster")
    assert srv["host"] == "fbcluster.db.svc" and srv["port"] == 3050 and srv["group"] == "Kubernetes"
    assert sorted(d["path"] for d in srv["databases"]) == ["/var/lib/firebird/data/demo.fdb",
                                                           "/var/lib/firebird/data/sales.fdb"]
    from fbadmin.api.deps import get_state

    assert get_state().store.password_for("k8s-db-fbcluster") == "s3cret-pw"
    assert client.get("/api/kubernetes/clusters").json()[0]["registered_server_id"] == "k8s-db-fbcluster"
    # again: updated in place, no duplicate databases
    r = client.post(b + "fbcluster/register", json={"host": "127.0.0.1", "port": 13050})
    assert r.json()["created"] is False
    srv = next(s for s in client.get("/api/servers").json() if s["id"] == "k8s-db-fbcluster")
    assert (srv["host"], srv["port"], len(srv["databases"])) == ("127.0.0.1", 13050, 2)
    # without RBAC for the Secret a password must be given
    kube.forbidden.add("/api/v1/namespaces/db/secrets/")
    r = client.post(b + "repl/register", json={})
    assert r.status_code == 400 and "password" in r.json()["detail"]
    assert client.post(b + "repl/register", json={"password": "typed"}).status_code == 200


def test_rbac_errors_off_switch_and_namespaces(client, kube, monkeypatch):
    kube.forbidden.add(f"{G}/namespaces/db/firebirdclusters")
    r = client.get("/api/kubernetes/clusters")
    assert r.status_code == 403 and "forbidden" in r.json()["detail"]
    kube.forbidden.clear()
    monkeypatch.setenv("FBADMIN_K8S_NAMESPACES", "db, other")
    assert client.get("/api/kubernetes/status").json()["namespaces"] == ["db", "other"]
    assert len(client.get("/api/kubernetes/clusters").json()) == 2  # "other" is empty
    monkeypatch.setenv("FBADMIN_KUBERNETES", "off")
    monkeypatch.delenv("FBADMIN_KUBECONFIG")
    op.reset()
    st = client.get("/api/kubernetes/status").json()
    assert st == {"enabled": False, "reason": "The Kubernetes integration is turned off (FBADMIN_KUBERNETES=off)"}
    assert client.get("/api/kubernetes/clusters").status_code == 409


def test_server_mode_admins_only_and_audit(tmp_path, monkeypatch, kube):
    from .test_access_fb import logout
    from .test_security import ADMIN_PW, _client, login

    kubeconfig = __import__("os").environ["FBADMIN_KUBECONFIG"]
    with _client(tmp_path, monkeypatch, SERVER_MODE="1", ADMIN_USERNAME="root", ADMIN_PASSWORD=ADMIN_PW,
                 KUBECONFIG=kubeconfig, LOGIN_RATE="50/60") as c:
        login(c, "root", ADMIN_PW)
        assert c.post("/api/admin/users", json={"username": "ops", "password": "ops-password"}).status_code == 201
        assert c.post("/api/kubernetes/clusters/db/fbcluster/hibernate", json={"on": True}).status_code == 200
        assert c.post("/api/kubernetes/clusters/db/fbcluster/switchover", json={"target": "1"}).status_code == 400
        audit = c.get("/api/admin/audit").json()
        entries = audit["rows"]
        stmts = [(e["statement"], e["success"]) for e in entries]
        assert ("-- kubernetes: hibernate db/fbcluster on", 1) in stmts
        assert any(s.startswith("-- kubernetes: switchover db/fbcluster") and ok == 0 for s, ok in stmts)
        # a shared registration is visible to every user
        assert c.post("/api/kubernetes/clusters/db/fbcluster/register", json={"shared": True}).status_code == 200
        logout(c)
        login(c, "ops", "ops-password")
        for path in ("/api/kubernetes/status", "/api/kubernetes/clusters"):
            assert c.get(path).status_code == 403
        assert c.post("/api/kubernetes/clusters/db/fbcluster/hibernate", json={"on": False}).status_code == 403
        assert any(s["id"] == "k8s-db-fbcluster" for s in c.get("/api/servers").json())
        logout(c)
    from fbadmin.api import deps

    monkeypatch.undo()
    deps.reset_state()
