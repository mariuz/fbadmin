"""Kubernetes: Firebird clusters of the cloudnative-firebird operator (roadmap 2.2).

Everything here acts with fbadmin's own Kubernetes credentials (its service account), so in server mode
only administrators use it. Every action is written to the audit log.
"""

from __future__ import annotations

from contextlib import contextmanager
from typing import Literal

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from ..kube import operator as op
from ..kube.client import KubeError, KubeNotConfigured
from ..security.core import require_admin
from .deps import get_state

router = APIRouter(prefix="/api/kubernetes", tags=["kubernetes"])
CL = "/clusters/{namespace}/{name}"


@contextmanager
def _errors():
    try:
        yield
    except KubeNotConfigured as e:
        raise HTTPException(409, str(e)) from None
    except PermissionError as e:
        raise HTTPException(403, str(e)) from None
    except KubeError as e:
        code = e.status if e.status in (403, 404, 409, 422) else 502
        prefix = "Kubernetes refused this: " if e.status == 403 else ""
        raise HTTPException(code, prefix + str(e)) from None


def _audit(what: str, ok: bool = True, error: str | None = None) -> None:
    try:
        get_state().security.audit.record(server_id=None, db_id=None, statement=f"-- kubernetes: {what}",
                                          success=ok, error=error, cat="ACTION")
    except Exception:  # noqa: BLE001 - auditing never breaks the request
        pass


@contextmanager
def _action(what: str):
    require_admin()
    try:
        with _errors():
            yield
    except HTTPException as e:
        _audit(what, False, str(e.detail))
        raise
    except Exception as e:
        _audit(what, False, str(e))
        raise
    _audit(what)


@router.get("/status")
def status():
    require_admin()
    return op.status()


@router.get("/clusters")
def clusters():
    require_admin()
    with _errors():
        return op.list_clusters()


@router.get(CL)
def cluster(namespace: str, name: str):
    require_admin()
    with _errors():
        return op.detail(namespace, name)


class ScaleIn(BaseModel):
    instances: int = Field(ge=1, le=32)


@router.post(CL + "/scale")
def scale(namespace: str, name: str, body: ScaleIn):
    with _action(f"scale {namespace}/{name} to {body.instances}"):
        return op.scale(namespace, name, body.instances)


class OnOff(BaseModel):
    on: bool


@router.post(CL + "/hibernate")
def hibernate(namespace: str, name: str, body: OnOff):
    with _action(f"hibernate {namespace}/{name} {'on' if body.on else 'off'}"):
        return op.hibernate(namespace, name, body.on)


class RestartIn(BaseModel):
    instance: str | None = None  # pod name or ordinal; none: rolling restart, the primary last


@router.post(CL + "/restart")
def restart(namespace: str, name: str, body: RestartIn):
    with _action(f"restart {namespace}/{name}" + (f" instance {body.instance}" if body.instance else "")):
        return op.restart(namespace, name, body.instance)


class SwitchoverIn(BaseModel):
    target: str


@router.post(CL + "/switchover")
def switchover(namespace: str, name: str, body: SwitchoverIn):
    with _action(f"switchover {namespace}/{name} to {body.target}"):
        return op.switchover(namespace, name, body.target)


class FenceIn(BaseModel):
    on: bool
    instance: str  # pod name, ordinal, or "*" for all


@router.post(CL + "/fence")
def fence(namespace: str, name: str, body: FenceIn):
    with _action(f"fence {'on' if body.on else 'off'} {namespace}/{name} {body.instance}"):
        return op.fence(namespace, name, body.on, body.instance)


class BackupIn(BaseModel):
    type: Literal["logical", "physical"] = "logical"
    level: int | None = None
    database: str | None = None  # a FirebirdDatabase of the cluster (logical backups)
    name: str | None = None


@router.post(CL + "/backups", status_code=201)
def backup(namespace: str, name: str, body: BackupIn):
    with _action(f"backup {namespace}/{name} ({body.type})"):
        return op.backup(namespace, name, type=body.type, level=body.level, database=body.database,
                         backup_name=body.name)


class RestoreIn(BaseModel):
    backup: str
    target: str | None = None  # file name in the data directory (default restore-<restore>.fdb)


@router.post(CL + "/restores", status_code=201)
def restore(namespace: str, name: str, body: RestoreIn):
    with _action(f"restore {namespace}/{name} from {body.backup}"):
        return op.restore(namespace, name, body.backup, target=body.target)


@router.get(CL + "/logs")
def logs(namespace: str, name: str, pod: str, tail: int = 200, container: str = "firebird"):
    require_admin()
    with _errors():
        return {"pod": pod, "container": container, "text": op.logs(namespace, name, pod, tail, container)}


class RegisterIn(BaseModel):
    host: str | None = None       # default: the primary Service, <name>.<namespace>.svc
    port: int = 3050
    password: str | None = None   # default: from the cluster's superuser Secret
    shared: bool = False          # server mode: visible to every fbadmin user


@router.post(CL + "/register")
def register(namespace: str, name: str, body: RegisterIn):
    with _action(f"register {namespace}/{name} as an fbadmin server"):
        return op.register(namespace, name, host=body.host, port=body.port, password=body.password,
                           shared=body.shared)
