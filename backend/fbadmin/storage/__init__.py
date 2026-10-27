"""Cloud backup targets: Amazon S3 (and S3-compatible), Azure Blob Storage and Google Cloud Storage.

No cloud SDKs: requests are made with ``urllib`` and signed here (AWS SigV4, Azure Shared Key,
Google service account JWT). See docs/MAINTENANCE.md, "Cloud storage targets".
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Protocol

from .base import StorageError, join_key, nolog, sort_newest
from .targets import MASK, Target, TargetStore, get_store, resolved


class Client(Protocol):
    def upload_file(self, path: Path, key: str, log: Callable[[str], None] = ...) -> dict: ...
    def list(self, prefix: str) -> list[dict]: ...
    def delete(self, key: str) -> None: ...
    def test(self) -> dict: ...


def open_client(t: Target) -> Client:
    """A client for the target, with ``env:`` references resolved now."""
    s = resolved(t)
    if t.kind == "s3":
        from .s3 import S3Client

        return S3Client(s["bucket"], s["access_key_id"], s["secret_access_key"], region=s["region"],
                        endpoint=s["endpoint"], path_style=s["path_style"], session_token=s["session_token"],
                        prefix=s["prefix"])
    if t.kind == "azure":
        from .azure import AzureClient

        return AzureClient(s["account"], s["container"], account_key=s["account_key"],
                           sas_token=s["sas_token"], endpoint=s["endpoint"], prefix=s["prefix"])
    if t.kind == "gcs":
        from .gcs import GCSClient

        return GCSClient(s["bucket"], s["service_account_json"], endpoint=s["endpoint"], prefix=s["prefix"])
    raise ValueError(f"Unknown storage kind {t.kind!r}")


def object_prefix(t: Target, prefix: str = "") -> str:
    """Full key prefix: the target's own ``prefix`` setting followed by ``prefix``."""
    return join_key(str(resolved(t).get("prefix") or ""), prefix)


def select_expired(objects: list[dict], folder: str, keep: int, protect: str = "") -> list[dict]:
    """The ``.fbk`` (and encrypted ``.fbk.enc``) objects directly under ``folder`` beyond the newest ``keep`` (``protect`` always counts
    as the newest). ``keep <= 0`` keeps everything."""
    if keep <= 0:
        return []
    base = folder.strip("/") + "/" if folder.strip("/") else ""
    backups = [o for o in objects if o["key"].startswith(base) and o["key"].endswith((".fbk", ".fbk.enc"))
               and "/" not in o["key"][len(base):]]
    ordered = sort_newest(backups)
    ordered.sort(key=lambda o: o["key"] != protect)  # stable: the protected key first
    return ordered[keep:]


def upload_with_retention(target_id: str, path: Path, prefix: str, keep: int,
                          log: Callable[[str], None] = nolog) -> dict:
    """Upload ``path`` to ``<target prefix>/<prefix>/<file name>``, then delete the oldest ``.fbk``
    objects in that folder so that ``keep`` remain (``keep <= 0``: keep all).

    Raises StorageError when the upload fails; failures to delete old backups are logged and
    returned in ``errors``."""
    t = get_store().get(target_id)
    client = open_client(t)
    folder = object_prefix(t, prefix)
    key = join_key(folder, path.name)
    log(f"Uploading {path.name} to storage target {t.name!r} as {key}")
    up = client.upload_file(Path(path), key, log)
    removed: list[str] = []
    errors: list[str] = []
    if keep > 0:
        listing = client.list(folder + "/" if folder else "")
        for o in select_expired(listing, folder, keep, protect=key):
            try:
                client.delete(o["key"])
                removed.append(o["key"])
                log(f"Retention: deleted {o['key']} from {t.name!r}")
            except StorageError as e:
                errors.append(str(e))
                log(f"Retention: could not delete {o['key']}: {e}")
    return {"target": t.name, "key": up["key"], "size": up["size"], "removed": removed, "errors": errors}


__all__ = ["MASK", "Client", "StorageError", "Target", "TargetStore", "get_store", "object_prefix",
           "open_client", "select_expired", "upload_with_retention"]
