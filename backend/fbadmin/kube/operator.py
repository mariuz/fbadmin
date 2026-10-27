"""Firebird clusters of the cloudnative-firebird operator (https://github.com/mariuz/cloudnative-firebird).

fbadmin reads ``FirebirdCluster`` resources and what belongs to them (instance pods, ``FirebirdBackup``,
``FirebirdScheduledBackup``, ``FirebirdDatabase``, ``FirebirdUser`` and events) and edits them the way
the operator's ``kubectl firebird`` plugin does: the operator does the work.

* scale: ``spec.instances``; hibernate: ``spec.hibernated``;
* rolling restart: ``kubectl.kubernetes.io/restartedAt`` on the StatefulSet's pod template (the operator
  restarts the replicas, then the primary), or one instance by deleting its pod;
* switchover: the ``targetPrimary`` annotation; fencing: the ``fencedInstances`` annotation;
* backups and restores: new ``FirebirdBackup`` / ``FirebirdRestore`` resources;
* register: the cluster's primary Service as an fbadmin server, with the superuser Secret's password.

Namespaces: ``FBADMIN_K8S_NAMESPACES`` (comma-separated, ``*`` for all, which needs a ClusterRole), by
default the namespace fbadmin runs in (in the cluster) or the kubeconfig context's.
"""

from __future__ import annotations

import base64
import json
import os
import re
import time
from datetime import datetime, timezone
from typing import Any

from .client import KubeClient, KubeError, KubeNotConfigured, load

GROUP = "firebird.cloudnative-firebird.io"
VERSION = "v1"
CLUSTER_LABEL = f"{GROUP}/cluster"
ROLE_LABEL = f"{GROUP}/role"
TARGET_PRIMARY = f"{GROUP}/targetPrimary"
FENCED = f"{GROUP}/fencedInstances"
RESTARTED_AT = "kubectl.kubernetes.io/restartedAt"
DATA_DIR = "/var/lib/firebird/data"
NAME = re.compile(r"^[a-z0-9]([-a-z0-9.]{0,251}[a-z0-9])?$")

_client: KubeClient | None = None
_client_error: str | None = None


def client() -> KubeClient:
    global _client, _client_error
    if _client is None:
        try:
            _client = load()
            _client_error = None
        except KubeNotConfigured as e:
            _client_error = str(e)
            raise
    return _client


def reset() -> None:
    """Forget the client (tests, or after the configuration changed)."""
    global _client, _client_error
    _client = _client_error = None


def check_name(name: str, what: str = "name") -> str:
    if not NAME.match(name or ""):
        raise ValueError(f"Invalid Kubernetes {what}: {name!r}")
    return name


def namespaces() -> list[str] | None:
    """The namespaces to look in; None means all."""
    raw = os.environ.get("FBADMIN_K8S_NAMESPACES", "").strip()
    if raw == "*":
        return None
    if raw:
        return [check_name(n.strip(), "namespace") for n in raw.split(",") if n.strip()]
    return [client().namespace]


def _crd(plural: str, ns: str | None, name: str | None = None) -> str:
    base = f"/apis/{GROUP}/{VERSION}"
    path = f"{base}/namespaces/{ns}/{plural}" if ns else f"{base}/{plural}"
    return f"{path}/{name}" if name else path


def _items(plural: str) -> list[dict]:
    nss = namespaces()
    if nss is None:
        return client().get(_crd(plural, None)).get("items", [])
    out = []
    for ns in nss:
        out += client().get(_crd(plural, ns)).get("items", [])
    return out


def _allowed(ns: str) -> str:
    nss = namespaces()
    check_name(ns, "namespace")
    if nss is not None and ns not in nss:
        raise PermissionError(f"Namespace {ns!r} is not one fbadmin manages (FBADMIN_K8S_NAMESPACES)")
    return ns


# ------------------------------------------------------------------ status
def status() -> dict[str, Any]:
    """Is the integration available, and is the operator installed?"""
    try:
        c = client()
    except KubeNotConfigured as e:
        return {"enabled": False, "reason": str(e)}
    out: dict[str, Any] = {"enabled": True, "source": c.source, "server": c.server, "namespaces": None}
    try:
        out["namespaces"] = namespaces()
        c.get(f"/apis/{GROUP}/{VERSION}")
        out["operator"] = True
    except KubeError as e:
        out["operator"] = False
        out["error"] = ("The cloudnative-firebird CRDs are not installed" if e.status == 404 else str(e))
    except ValueError as e:
        out["operator"] = False
        out["error"] = str(e)
    return out


def _age(ts: str | None) -> float | None:
    if not ts:
        return None
    try:
        return datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc).timestamp()
    except ValueError:
        return None


def _fenced(cluster: dict) -> list[str]:
    raw = (cluster["metadata"].get("annotations") or {}).get(FENCED)
    try:
        v = json.loads(raw) if raw else []
        return [str(x) for x in v] if isinstance(v, list) else []
    except ValueError:
        return []


def _registered(ns: str, name: str) -> str | None:
    from ..api.deps import get_state

    sid = server_id(ns, name)
    try:
        get_state().store.get(sid)
        return sid
    except (KeyError, Exception):  # noqa: BLE001 - unknown, or not visible to this user
        return None


def summary(c: dict) -> dict[str, Any]:
    md, spec, st = c["metadata"], c.get("spec") or {}, c.get("status") or {}
    repl = spec.get("replication") or {}
    rs = st.get("replicationStatus") or {}
    return {
        "namespace": md["namespace"], "name": md["name"], "created": _age(md.get("creationTimestamp")),
        "phase": st.get("phase") or "Pending", "phase_reason": st.get("phaseReason"),
        "instances": spec.get("instances", 1), "ready": st.get("readyInstances", 0),
        "image": st.get("image") or spec.get("imageName"), "primary": rs.get("primaryPod"),
        "replication": bool(repl.get("enabled")), "replication_mode": repl.get("mode"),
        "hibernated": bool(spec.get("hibernated")), "suspended": bool(spec.get("suspended")),
        "database": spec.get("databaseName"), "fenced": _fenced(c),
        "superuser_secret": (spec.get("superuserSecret") or {}).get("name"),
        "backup_s3": bool(((spec.get("backup") or {}).get("s3") or {}).get("bucket")),
        "switchover_to": (md.get("annotations") or {}).get(TARGET_PRIMARY),
        "registered_server_id": _registered(md["namespace"], md["name"]),
    }


def list_clusters() -> list[dict[str, Any]]:
    items = _items("firebirdclusters")
    return sorted((summary(c) for c in items), key=lambda s: (s["namespace"], s["name"]))


def get_cluster(ns: str, name: str) -> dict:
    return client().get(_crd("firebirdclusters", _allowed(ns), check_name(name)))


def _pod_view(p: dict, primary: str | None, fenced: list[str]) -> dict[str, Any]:
    md, st = p["metadata"], p.get("status") or {}
    cs = next((c for c in st.get("containerStatuses") or [] if c.get("name") == "firebird"), None) or \
        (st.get("containerStatuses") or [{}])[0]
    ready = any(c.get("type") == "Ready" and c.get("status") == "True" for c in st.get("conditions") or [])
    role = (md.get("labels") or {}).get(ROLE_LABEL) or ("primary" if md["name"] == primary else "replica")
    return {"name": md["name"], "role": "primary" if md["name"] == primary else role, "ready": ready,
            "phase": st.get("phase"), "node": (p.get("spec") or {}).get("nodeName"),
            "restarts": (cs or {}).get("restartCount", 0), "started": _age(st.get("startTime")),
            "fenced": "*" in fenced or md["name"] in fenced}


def _of_cluster(items: list[dict], name: str) -> list[dict]:
    return [i for i in items if (i.get("spec") or {}).get("clusterName") == name]


def detail(ns: str, name: str) -> dict[str, Any]:
    c = get_cluster(ns, name)
    s = summary(c)
    kc = client()
    pods = kc.get(f"/api/v1/namespaces/{ns}/pods",
                  labelSelector=f"{CLUSTER_LABEL}={name},app.kubernetes.io/component=database").get("items", [])
    primary = s["primary"] or next((p["metadata"]["name"] for p in pods
                                    if (p["metadata"].get("labels") or {}).get(ROLE_LABEL) == "primary"), None) \
        or f"{name}-0"
    s["primary"] = primary

    def optional(plural: str) -> list[dict]:
        try:
            return _of_cluster(kc.get(_crd(plural, ns)).get("items", []), name)
        except KubeError as e:
            if e.status in (403, 404):
                return []  # an older operator without the CRD, or no RBAC for it
            raise

    backups = sorted(optional("firebirdbackups"), key=lambda b: b["metadata"].get("creationTimestamp") or "",
                     reverse=True)
    events = []
    try:
        evs = kc.get(f"/api/v1/namespaces/{ns}/events", fieldSelector=f"involvedObject.name={name}").get("items", [])
        events = [{"type": e.get("type"), "reason": e.get("reason"), "message": e.get("message"),
                   "count": e.get("count"), "time": _age(e.get("lastTimestamp") or e.get("eventTime")
                                                         or (e.get("metadata") or {}).get("creationTimestamp"))}
                  for e in evs]
        events.sort(key=lambda e: e["time"] or 0, reverse=True)
    except KubeError:
        pass
    st = c.get("status") or {}
    return {
        **s,
        "conditions": [{"type": x.get("type"), "status": x.get("status"), "reason": x.get("reason"),
                        "message": x.get("message")} for x in st.get("conditions") or []],
        "pods": sorted((_pod_view(p, primary, s["fenced"]) for p in pods), key=lambda p: p["name"]),
        "backups": [{"name": b["metadata"]["name"], "type": (b.get("spec") or {}).get("type", "logical"),
                     "level": (b.get("spec") or {}).get("level"), "database": (b.get("spec") or {}).get("database"),
                     "phase": (b.get("status") or {}).get("phase") or "Pending",
                     "started": _age((b.get("status") or {}).get("startTime")),
                     "completed": _age((b.get("status") or {}).get("completionTime")),
                     "location": (b.get("status") or {}).get("location") or (b.get("status") or {}).get("backupFileName"),
                     "verified": (b.get("status") or {}).get("verified"), "error": (b.get("status") or {}).get("error")}
                    for b in backups[:50]],
        "scheduled_backups": [{"name": b["metadata"]["name"], "schedule": (b.get("spec") or {}).get("schedule"),
                               "type": (b.get("spec") or {}).get("type", "logical"),
                               "suspended": bool((b.get("spec") or {}).get("suspend")),
                               "last_successful": _age((b.get("status") or {}).get("lastSuccessfulTime")),
                               "last_backup": (b.get("status") or {}).get("lastBackupName")}
                              for b in optional("firebirdscheduledbackups")],
        "databases": [{"name": d["metadata"]["name"], "file": (d.get("spec") or {}).get("databaseName"),
                       "phase": (d.get("status") or {}).get("phase") or "Pending",
                       "path": (d.get("status") or {}).get("path")} for d in optional("firebirddatabases")],
        "users": [{"name": u["metadata"]["name"], "username": (u.get("spec") or {}).get("username"),
                   "admin": bool((u.get("spec") or {}).get("admin")),
                   "phase": (u.get("status") or {}).get("phase") or "Pending"} for u in optional("firebirdusers")],
        "events": events[:30],
    }


# ------------------------------------------------------------------ actions
def _patch(ns: str, name: str, patch: dict) -> dict:
    return client().merge_patch(_crd("firebirdclusters", ns, name), patch)


def _instance(c: dict, arg: str) -> str:
    """An instance by pod name or ordinal (as the plugin takes it)."""
    name = c["metadata"]["name"]
    if re.fullmatch(r"\d+", str(arg)):
        pod = f"{name}-{arg}"
    else:
        pod = str(arg)
    m = re.fullmatch(rf"{re.escape(name)}-(\d+)", pod)
    if not m or int(m.group(1)) >= int((c.get("spec") or {}).get("instances", 1)):
        raise ValueError(f"{arg!r} is not an instance of cluster {name}")
    return pod


def scale(ns: str, name: str, instances: int) -> dict:
    get_cluster(ns, name)
    if not 1 <= instances <= 32:
        raise ValueError("Instances must be between 1 and 32")
    _patch(ns, name, {"spec": {"instances": instances}})
    return {"message": f"Cluster {name}: {instances} instance(s) requested"}


def hibernate(ns: str, name: str, on: bool) -> dict:
    get_cluster(ns, name)
    _patch(ns, name, {"spec": {"hibernated": bool(on)}})
    return {"message": f"Cluster {name} hibernating (its volumes are kept)" if on else f"Cluster {name} resuming"}


def restart(ns: str, name: str, instance: str | None = None) -> dict:
    c = get_cluster(ns, name)
    if (c.get("spec") or {}).get("hibernated"):
        raise ValueError(f"Cluster {name} is hibernated")
    if instance is not None and str(instance) != "":
        pod = _instance(c, instance)
        client().delete(f"/api/v1/namespaces/{ns}/pods/{pod}")
        return {"message": f"Instance {pod} restarting"}
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    client().merge_patch(f"/apis/apps/v1/namespaces/{ns}/statefulsets/{name}",
                         {"spec": {"template": {"metadata": {"annotations": {RESTARTED_AT: now}}}}})
    return {"message": f"Rolling restart of cluster {name} requested (the primary last)"}


def switchover(ns: str, name: str, target: str) -> dict:
    d = detail(ns, name)
    if not d["replication"]:
        raise ValueError(f"Cluster {name} has no replication: there is no replica to promote")
    c = get_cluster(ns, name)
    pod = _instance(c, target)
    if pod == d["primary"]:
        raise ValueError(f"{pod} is the primary already")
    _patch(ns, name, {"metadata": {"annotations": {TARGET_PRIMARY: pod}}})
    ready = next((p["ready"] for p in d["pods"] if p["name"] == pod), False)
    return {"message": f"Switchover of cluster {name} requested: {d['primary']} -> {pod}"
                       + ("" if ready else f" ({pod} is not ready: the switchover waits for it)")}


def fence(ns: str, name: str, on: bool, instance: str) -> dict:
    c = get_cluster(ns, name)
    target = "*" if instance in ("*", "all") else _instance(c, instance)
    cur = _fenced(c)
    if on:
        nxt = ["*"] if target == "*" else (cur if "*" in cur or target in cur else [*cur, target])
    elif target == "*":
        nxt = []
    elif "*" in cur:  # lifting one instance out of "all" keeps the others fenced
        n = int((c.get("spec") or {}).get("instances", 1))
        nxt = [f"{name}-{i}" for i in range(n) if f"{name}-{i}" != target]
    else:
        nxt = [x for x in cur if x != target]
    _patch(ns, name, {"metadata": {"annotations": {FENCED: json.dumps(nxt) if nxt else None}}})
    return {"message": f"Fenced instances of {name}: {', '.join(nxt)}" if nxt else f"No instance of {name} is fenced",
            "fenced": nxt}


def _stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")


def backup(ns: str, name: str, *, type: str = "logical", level: int | None = None, database: str | None = None,
           backup_name: str | None = None) -> dict:
    c = get_cluster(ns, name)
    if type not in ("logical", "physical"):
        raise ValueError("The backup type is logical or physical")
    if level is not None and (type != "physical" or level not in (0, 1, 2)):
        raise ValueError("The level is 0, 1 or 2, for physical backups")
    if database and type != "logical":
        raise ValueError("A FirebirdDatabase is backed up logically")
    bname = check_name(backup_name or f"{name}-{_stamp()}"[:253])
    spec: dict[str, Any] = {"clusterName": name, "type": type}
    if level is not None:
        spec["level"] = level
    if database:
        spec["database"] = check_name(database, "FirebirdDatabase name")
    s3 = ((c.get("spec") or {}).get("backup") or {}).get("s3")
    if s3:
        spec["s3"] = s3  # the cluster's own destination, as its scheduled backups have it
    client().create(_crd("firebirdbackups", ns), {"apiVersion": f"{GROUP}/{VERSION}", "kind": "FirebirdBackup",
                                                  "metadata": {"name": bname, "namespace": ns}, "spec": spec})
    return {"message": f"FirebirdBackup {bname} created ({type}" + (f", to s3://{s3.get('bucket')}" if s3 else "")
            + ")", "name": bname}


def restore(ns: str, name: str, backup_name: str, *, target: str | None = None) -> dict:
    get_cluster(ns, name)
    b = client().get(_crd("firebirdbackups", ns, check_name(backup_name)))
    if (b.get("spec") or {}).get("clusterName") != name:
        raise ValueError(f"{backup_name} is a backup of another cluster")
    if (b.get("spec") or {}).get("type") == "volumeSnapshot":
        raise ValueError(f"{backup_name} is a volume snapshot: a new cluster bootstraps from it")
    if (b.get("status") or {}).get("phase") != "Completed":
        raise ValueError(f"{backup_name} is {(b.get('status') or {}).get('phase') or 'Pending'}: only a completed "
                         "backup can be restored")
    if target and not re.fullmatch(r"[A-Za-z0-9_.-]{1,200}\.fdb", target):
        raise ValueError("The target is a file name like restored.fdb")
    rname = check_name(f"{backup_name}-restore-{_stamp()}"[:253])
    spec: dict[str, Any] = {"clusterName": name, "backupName": backup_name}
    if target:
        spec["targetDatabase"] = target
    client().create(_crd("firebirdrestores", ns), {"apiVersion": f"{GROUP}/{VERSION}", "kind": "FirebirdRestore",
                                                   "metadata": {"name": rname, "namespace": ns}, "spec": spec})
    return {"message": f"FirebirdRestore {rname} created: {backup_name} into {target or f'restore-{rname}.fdb'} "
                       "on the primary", "name": rname, "file": target or f"restore-{rname}.fdb"}


def logs(ns: str, name: str, pod: str, tail: int = 200, container: str = "firebird") -> str:
    c = get_cluster(ns, name)
    pod = _instance(c, pod)
    if not re.fullmatch(r"[a-z0-9-]{1,63}", container):
        raise ValueError("Invalid container name")
    return client().request("GET", f"/api/v1/namespaces/{ns}/pods/{pod}/log",
                            params={"container": container, "tailLines": max(1, min(int(tail), 5000))}, raw=True)


# ------------------------------------------------------------------ register in fbadmin
def server_id(ns: str, name: str) -> str:
    return f"k8s-{ns}-{name}"


def superuser(ns: str, c: dict) -> tuple[str, str] | None:
    """(user, password) from the cluster's superuser Secret, or None without RBAC to read it."""
    secret = ((c.get("spec") or {}).get("superuserSecret") or {}).get("name")
    if not secret:
        return None
    try:
        s = client().get(f"/api/v1/namespaces/{ns}/secrets/{check_name(secret, 'Secret name')}")
    except KubeError as e:
        if e.status in (403, 404):
            return None
        raise
    data = s.get("data") or {}
    pw = data.get("password")
    if not pw:
        return None
    user = base64.b64decode(data["username"]).decode() if data.get("username") else "SYSDBA"
    return user, base64.b64decode(pw).decode()


def register(ns: str, name: str, *, host: str | None = None, port: int = 3050, password: str | None = None,
             shared: bool = False) -> dict:
    """Register (or update) the cluster's primary Service as an fbadmin server with its databases."""
    from ..api.deps import get_state
    from ..store import DatabaseEntry, ServerEntry

    c = get_cluster(ns, name)
    st = get_state()
    cred = superuser(ns, c)
    user = cred[0] if cred else "SYSDBA"
    pw = password or (cred[1] if cred else None)
    if not pw:
        raise ValueError("fbadmin cannot read the cluster's superuser Secret: enter the SYSDBA password")
    host = host or f"{name}.{ns}.svc"
    dbs: list[tuple[str, str]] = []
    if (c.get("spec") or {}).get("databaseName"):
        f = c["spec"]["databaseName"]
        dbs.append((f.rsplit(".", 1)[0] or f, f"{DATA_DIR}/{f}"))
    try:
        for d in _of_cluster(client().get(_crd("firebirddatabases", ns)).get("items", []), name):
            p = (d.get("status") or {}).get("path") or f"{DATA_DIR}/{(d.get('spec') or {}).get('databaseName') or d['metadata']['name'] + '.fdb'}"
            dbs.append((d["metadata"]["name"], p))
    except KubeError:
        pass
    sid = server_id(ns, name)
    try:
        cur = st.store.get(sid)
    except KeyError:
        cur = None
    label = f"{name} ({ns})"
    if cur is None:
        srv = st.store.add(ServerEntry(id=sid, name=label, host=host, port=port, user=user, password=pw,
                                       save_password=True, shared=shared, group="Kubernetes"))
    else:
        srv = st.store.update(sid, {"host": host, "port": port, "user": user, "password": pw, "save_password": True})
    known = {d.path for d in srv.databases}
    for dname, path in dbs:
        if path not in known:
            st.store.add_database(sid, DatabaseEntry(name=dname, path=path))
    return {"server_id": sid, "host": host, "databases": [p for _, p in dbs], "created": cur is None,
            "password_from_secret": password is None and cred is not None}


def wait_phase(ns: str, plural: str, name: str, phases: set[str], timeout: float = 300) -> dict:
    """Poll a resource until its phase is one of ``phases`` (used by the CLI and tests)."""
    deadline = time.time() + timeout
    while True:
        r = client().get(_crd(plural, ns, name))
        if (r.get("status") or {}).get("phase") in phases or time.time() > deadline:
            return r
        time.sleep(2)
