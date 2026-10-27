"""A small Kubernetes API client (standard library only): enough for the cloudnative-firebird integration.

Credentials, in this order:

* ``FBADMIN_KUBERNETES=off`` turns the integration off;
* **in the cluster**: the pod's service account (token, CA and namespace under
  ``/var/run/secrets/kubernetes.io/serviceaccount``, the API server from ``KUBERNETES_SERVICE_HOST``);
* **a kubeconfig file**: ``FBADMIN_KUBECONFIG``, or ``KUBECONFIG`` / ``~/.kube/config`` when
  ``FBADMIN_KUBERNETES=on``. Its current context (or ``FBADMIN_KUBE_CONTEXT``), with a bearer token or a
  client certificate; ``exec`` and auth-provider plugins are not supported.
"""

from __future__ import annotations

import base64
import json
import os
import ssl
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

SA_DIR = "/var/run/secrets/kubernetes.io/serviceaccount"
TIMEOUT = 20


class KubeError(Exception):
    """An error from the Kubernetes API (``status`` is the HTTP status, 0 when unreachable)."""

    def __init__(self, status: int, message: str, reason: str = ""):
        super().__init__(message)
        self.status = status
        self.reason = reason


class KubeNotConfigured(Exception):
    """No Kubernetes credentials (not in a cluster, no kubeconfig) or the integration is off."""


class KubeClient:
    def __init__(self, server: str, *, token: str | None = None, ca_file: str | None = None,
                 ca_data: bytes | None = None, cert_file: str | None = None, key_file: str | None = None,
                 insecure: bool = False, namespace: str = "default", source: str = ""):
        self.server = server.rstrip("/")
        self.token = token
        self.namespace = namespace
        self.source = source
        ctx = ssl.create_default_context()
        if insecure:
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
        elif ca_file:
            ctx.load_verify_locations(cafile=ca_file)
        elif ca_data:
            ctx.load_verify_locations(cadata=ca_data.decode("ascii", "replace"))
        if cert_file and key_file:
            ctx.load_cert_chain(cert_file, key_file)
        self._ctx = ctx if self.server.startswith("https") else None

    # ------------------------------------------------------------------ plumbing
    def request(self, method: str, path: str, body: Any = None, *, params: dict | None = None,
                content_type: str = "application/json", raw: bool = False) -> Any:
        url = self.server + path
        if params:
            url += "?" + urllib.parse.urlencode({k: v for k, v in params.items() if v is not None})
        data = None if body is None else (body if isinstance(body, bytes) else json.dumps(body).encode())
        headers = {"Accept": "application/json"}
        if data is not None:
            headers["Content-Type"] = content_type
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        req = urllib.request.Request(url, data=data, method=method, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT, context=self._ctx) as r:
                payload = r.read()
        except urllib.error.HTTPError as e:
            text = e.read().decode("utf-8", "replace")
            try:
                st = json.loads(text)
                raise KubeError(e.code, st.get("message") or text, st.get("reason") or "") from None
            except ValueError:
                raise KubeError(e.code, text.strip() or e.reason) from None
        except (urllib.error.URLError, OSError) as e:
            reason = getattr(e, "reason", e)
            raise KubeError(0, f"Cannot reach the Kubernetes API at {self.server}: {reason}") from None
        if raw:
            return payload.decode("utf-8", "replace")
        return json.loads(payload) if payload else None

    def get(self, path: str, **params) -> Any:
        return self.request("GET", path, params=params or None)

    def merge_patch(self, path: str, patch: dict) -> Any:
        return self.request("PATCH", path, patch, content_type="application/merge-patch+json")

    def create(self, path: str, body: dict) -> Any:
        return self.request("POST", path, body)

    def delete(self, path: str) -> Any:
        return self.request("DELETE", path)


# ------------------------------------------------------------------ configuration
def _setting() -> str:
    return (os.environ.get("FBADMIN_KUBERNETES") or "auto").strip().lower()


def _in_cluster() -> KubeClient | None:
    sa = Path(os.environ.get("FBADMIN_KUBE_SA_DIR") or SA_DIR)
    host, port = os.environ.get("KUBERNETES_SERVICE_HOST"), os.environ.get("KUBERNETES_SERVICE_PORT", "443")
    if not host or not (sa / "token").exists():
        return None
    token = (sa / "token").read_text().strip()
    ns = (sa / "namespace").read_text().strip() if (sa / "namespace").exists() else "default"
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"  # IPv6
    ca = sa / "ca.crt"
    return KubeClient(f"https://{host}:{port}", token=token, ca_file=str(ca) if ca.exists() else None,
                      namespace=ns, source="in-cluster")


def _file_or_data(entry: dict, key: str, base: Path) -> tuple[str | None, bytes | None]:
    if entry.get(f"{key}-data"):
        return None, base64.b64decode(entry[f"{key}-data"])
    if entry.get(key):
        p = Path(entry[key])
        return str(p if p.is_absolute() else base / p), None
    return None, None


def _temp_file(data: bytes) -> str:
    fd, path = tempfile.mkstemp(prefix="fbadmin-kube-", suffix=".pem")
    with os.fdopen(fd, "wb") as f:
        f.write(data)
    return path


def from_kubeconfig(path: str | Path, context: str | None = None) -> KubeClient:
    import yaml

    path = Path(path).expanduser()
    cfg = yaml.safe_load(path.read_text()) or {}
    name = context or cfg.get("current-context")
    ctxs = {c["name"]: c.get("context") or {} for c in cfg.get("contexts") or []}
    if name not in ctxs:
        raise KubeNotConfigured(f"Context {name!r} not found in {path}")
    ctx = ctxs[name]
    cluster = next((c.get("cluster") or {} for c in cfg.get("clusters") or [] if c["name"] == ctx.get("cluster")), None)
    user = next((u.get("user") or {} for u in cfg.get("users") or [] if u["name"] == ctx.get("user")), {})
    if not cluster or not cluster.get("server"):
        raise KubeNotConfigured(f"Context {name!r} in {path} has no cluster server")
    if user.get("exec") or user.get("auth-provider"):
        raise KubeNotConfigured(f"The user of context {name!r} uses an exec or auth-provider plugin, which fbadmin "
                                "does not run: use a token or a client certificate")
    base = path.parent
    ca_file, ca_data = _file_or_data(cluster, "certificate-authority", base)
    cert_file, cert_data = _file_or_data(user, "client-certificate", base)
    key_file, key_data = _file_or_data(user, "client-key", base)
    if cert_data:
        cert_file = _temp_file(cert_data)
    if key_data:
        key_file = _temp_file(key_data)
    token = user.get("token")
    if not token and user.get("tokenFile"):
        token = Path(user["tokenFile"]).read_text().strip()
    try:
        return KubeClient(cluster["server"], token=token, ca_file=ca_file, ca_data=ca_data, cert_file=cert_file,
                          key_file=key_file, insecure=bool(cluster.get("insecure-skip-tls-verify")),
                          namespace=ctx.get("namespace") or "default", source=f"kubeconfig {path} ({name})")
    finally:
        for f, d in ((cert_file, cert_data), (key_file, key_data)):
            if d and f:
                os.unlink(f)  # loaded into the SSL context already


def load() -> KubeClient:
    """The client for the configured credentials; raises KubeNotConfigured."""
    setting = _setting()
    if setting in ("off", "0", "false", "no"):
        raise KubeNotConfigured("The Kubernetes integration is turned off (FBADMIN_KUBERNETES=off)")
    explicit = os.environ.get("FBADMIN_KUBECONFIG")
    if explicit:
        return from_kubeconfig(explicit, os.environ.get("FBADMIN_KUBE_CONTEXT"))
    c = _in_cluster()
    if c:
        return c
    if setting in ("on", "1", "true", "yes"):
        path = (os.environ.get("KUBECONFIG") or "~/.kube/config").split(os.pathsep)[0]
        if Path(path).expanduser().exists():
            return from_kubeconfig(path, os.environ.get("FBADMIN_KUBE_CONTEXT"))
        raise KubeNotConfigured(f"No kubeconfig at {path}")
    raise KubeNotConfigured("Not running in a Kubernetes cluster: set FBADMIN_KUBECONFIG to use a kubeconfig file")
