"""Cloud storage targets (S3, Azure Blob Storage, Google Cloud Storage) stored in the data directory.

Targets live in ``<data dir>/storage_targets.json`` (mode 0600). Any setting may be written
as ``env:NAME`` to read it from the environment when the target is used, so that secrets do
not have to be stored in the file. Secret settings are never returned by :meth:`TargetStore.view`.
"""

from __future__ import annotations

import json
import os
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field

from ..config import settings as app_settings

MASK = "********"
Kind = Literal["s3", "azure", "gcs"]

# kind -> setting -> (required, secret, default). Required "either/or" rules are checked in _validate.
FIELDS: dict[str, dict[str, tuple[bool, bool, Any]]] = {
    "s3": {
        "bucket": (True, False, ""),
        "region": (False, False, "us-east-1"),
        "access_key_id": (True, False, ""),
        "secret_access_key": (True, True, ""),
        "session_token": (False, True, ""),
        "endpoint": (False, False, ""),
        "path_style": (False, False, None),  # None: true when an endpoint is set or the bucket has dots
        "prefix": (False, False, ""),
    },
    "azure": {
        "account": (True, False, ""),
        "container": (True, False, ""),
        "account_key": (False, True, ""),
        "sas_token": (False, True, ""),
        "endpoint": (False, False, ""),
        "prefix": (False, False, ""),
    },
    "gcs": {
        "bucket": (True, False, ""),
        "service_account_json": (False, True, ""),
        "endpoint": (False, False, ""),
        "prefix": (False, False, ""),
    },
}


def secret_fields(kind: str) -> list[str]:
    return [k for k, (_req, secret, _d) in FIELDS[kind].items() if secret]


class Target(BaseModel):
    id: str = Field(default_factory=lambda: uuid.uuid4().hex[:10])
    name: str
    kind: Kind
    settings: dict[str, Any] = Field(default_factory=dict)
    created_at: float = Field(default_factory=time.time)


def resolve(value: Any) -> Any:
    """Expand an ``env:NAME`` reference; other values are returned unchanged."""
    if isinstance(value, str) and value.startswith("env:"):
        name = value[4:].strip()
        if name not in os.environ:
            raise ValueError(f"Environment variable {name} (referenced as {value}) is not set")
        return os.environ[name]
    return value


def resolved(t: Target) -> dict[str, Any]:
    """The target's settings with defaults applied and ``env:`` references expanded."""
    out: dict[str, Any] = {}
    for k, (_req, _secret, default) in FIELDS[t.kind].items():
        v = resolve(t.settings.get(k))
        out[k] = default if v in (None, "") else v
    if t.kind == "s3" and out["path_style"] is None:
        # virtual-hosted URLs of bucket names with dots do not match the provider's TLS certificate
        out["path_style"] = bool(out["endpoint"]) or "." in str(out["bucket"])
    return out


def _bool(v: Any) -> bool | None:
    if v is None or v == "" or isinstance(v, bool):
        return v if isinstance(v, bool) else None
    if isinstance(v, str) and v.lower() in ("1", "true", "yes", "on"):
        return True
    if isinstance(v, str) and v.lower() in ("0", "false", "no", "off"):
        return False
    raise ValueError(f"path_style must be true or false, not {v!r}")


def _clean(kind: str, raw: dict[str, Any]) -> dict[str, Any]:
    """Keep known settings only, trim strings, drop empty values."""
    if kind not in FIELDS:
        raise ValueError(f"Unknown storage kind {kind!r} (expected s3, azure or gcs)")
    out: dict[str, Any] = {}
    for k in FIELDS[kind]:
        v = raw.get(k)
        if k == "path_style":
            v = _bool(v)
        elif v is not None and not isinstance(v, str):
            raise ValueError(f"{k} must be a string")
        elif isinstance(v, str) and k != "service_account_json":
            v = v.strip()
        if v not in (None, ""):
            out[k] = v
    return out


def _validate(t: Target) -> None:
    if not t.name.strip():
        raise ValueError("A storage target needs a name")
    s = t.settings
    missing = [k for k, (req, _s, _d) in FIELDS[t.kind].items() if req and not s.get(k)]
    if t.kind == "azure" and not (s.get("account_key") or s.get("sas_token")):
        missing.append("account_key or sas_token")
    if t.kind == "gcs" and not s.get("service_account_json") and not s.get("endpoint"):
        missing.append("service_account_json")
    if missing:
        raise ValueError(f"{t.kind} target {t.name!r} is missing: {', '.join(missing)}")
    for k in ("endpoint",):
        v = s.get(k, "")
        if v and not v.startswith("env:") and not v.startswith(("http://", "https://")):
            raise ValueError(f"endpoint must start with http:// or https:// (got {v!r})")
    sa = s.get("service_account_json")
    if sa and not sa.startswith("env:"):
        try:
            info = json.loads(sa)
        except ValueError as e:
            raise ValueError(f"service_account_json is not valid JSON: {e}") from None
        if not isinstance(info, dict) or not info.get("client_email") or not info.get("private_key"):
            raise ValueError("service_account_json must be a service account key with client_email "
                             "and private_key")


class TargetStore:
    def __init__(self, data_dir: Path | None = None):
        self.dir = Path(data_dir or app_settings.data_dir)
        self.file = self.dir / "storage_targets.json"
        self._lock = threading.RLock()
        self.targets: dict[str, Target] = {}
        if self.file.exists():
            data = json.loads(self.file.read_text())
            self.targets = {t["id"]: Target(**t) for t in data.get("targets", [])}

    def _save(self) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        tmp = self.file.with_suffix(".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump({"targets": [t.model_dump() for t in self.targets.values()]}, f, indent=1)
        os.chmod(tmp, 0o600)
        os.replace(tmp, self.file)

    def view(self, t: Target) -> dict:
        """The target as the API shows it: secrets masked, ``has_<secret>`` flags added.

        ``env:NAME`` references are not secret and are shown as they are."""
        s = dict(t.settings)
        for k in secret_fields(t.kind):
            v = s.get(k)
            s[f"has_{k}"] = bool(v)
            if v and not (isinstance(v, str) and v.startswith("env:")):
                s[k] = MASK
        return {**t.model_dump(), "settings": s}

    def list(self) -> list[dict]:
        with self._lock:
            return [self.view(t) for t in sorted(self.targets.values(), key=lambda t: t.created_at)]

    def get(self, tid: str) -> Target:
        with self._lock:
            if tid not in self.targets:
                raise KeyError(f"Storage target {tid} not found")
            return self.targets[tid]

    def add(self, t: Target) -> Target:
        t = t.model_copy(update={"name": t.name.strip(), "settings": _clean(t.kind, t.settings)})
        _validate(t)
        with self._lock:
            self.targets[t.id] = t
            self._save()
        return t

    def update(self, tid: str, changes: dict) -> Target:
        """Change name, kind and/or settings. Settings are merged into the stored ones; a secret
        sent as ``********`` or ``""`` keeps its stored value, ``null`` clears a setting."""
        with self._lock:
            old = self.get(tid)
            kind = changes.get("kind") or old.kind
            if kind not in FIELDS:
                raise ValueError(f"Unknown storage kind {kind!r} (expected s3, azure or gcs)")
            base = dict(old.settings) if kind == old.kind else {}
            incoming = changes.get("settings") or {}
            merged = dict(base)
            secrets = secret_fields(kind)
            for k, v in incoming.items():
                if k in secrets and v in (MASK, ""):
                    continue
                if v is None or v == "":
                    merged.pop(k, None)
                else:
                    merged[k] = v
            t = Target(id=old.id, name=(changes.get("name") or old.name).strip(), kind=kind,
                       settings=_clean(kind, merged), created_at=old.created_at)
            _validate(t)
            self.targets[tid] = t
            self._save()
            return t

    def remove(self, tid: str) -> None:
        with self._lock:
            self.get(tid)
            del self.targets[tid]
            self._save()


_store: TargetStore | None = None


def get_store() -> TargetStore:
    global _store
    if _store is None or _store.dir != Path(app_settings.data_dir):
        _store = TargetStore()
    return _store
