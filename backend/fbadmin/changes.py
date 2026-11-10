"""Change requests with approval (roadmap 2.4).

A developer proposes a change to a database: a SQL script (DDL, or DML that fixes data) or versioned migration
scripts. A reviewer runs a **dry run** (the change on a scratch copy of the database, see ``migrations.dry_run``)
that shows the schema diff, **approves** or **rejects** it, and fbadmin **applies** it, with an audit trail on
the request, in the audit log and in the database's timeline.

Who may do what (server mode):

* **propose**, comment, see: the ``read`` level on the database (a developer who may not change it);
* **edit**, **withdraw**: the author, while the request is not applied;
* **dry run**, **approve**, **reject**, **apply**: the ``admin`` level on the database (the dry run restores a
  copy with the service API). The author cannot approve their own request unless
  ``FBADMIN_CHANGE_SELF_APPROVE=1``. ``FBADMIN_CHANGE_APPROVALS`` approvals are needed (default 1).

Approvals belong to one dry run: editing the request clears them, and a later dry run that finds different schema
changes clears them too. Applying is refused when the database's schema changed since the dry run.

In desktop mode (one user) the same steps apply, and self-approval is allowed.

Requests are kept in ``<data dir>/change_requests.json`` (mode 0600).
"""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import threading
import time
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field

from .config import settings
from .library import Actor

APPROVALS = max(1, int(os.environ.get("FBADMIN_CHANGE_APPROVALS", "1")))
SELF_APPROVE = os.environ.get("FBADMIN_CHANGE_SELF_APPROVE", "") in ("1", "true", "yes")
MAX_REQUESTS = 5000
Status = Literal["open", "approved", "rejected", "withdrawn", "applying", "applied", "failed"]
DONE = ("rejected", "withdrawn", "applied")


class ScriptItem(BaseModel):
    name: str = Field(min_length=1, max_length=500)  # migration file name: V3__add_orders.sql
    sql: str = Field(max_length=2_000_000)


class Event(BaseModel):
    at: float = Field(default_factory=time.time)
    by: str | None = None
    action: str           # submitted, revised, comment, dry_run, approved, rejected, withdrawn, applied, failed, ...
    detail: str = ""


class Approval(BaseModel):
    by: str | None = None
    user_id: int | None = None
    at: float = Field(default_factory=time.time)
    comment: str = ""
    changes: str = ""     # fingerprint of the dry run's schema changes that were approved


class ChangeRequest(BaseModel):
    id: str = Field(default_factory=lambda: secrets.token_hex(4))
    number: int = 0
    server_id: str
    database_id: str
    title: str = Field(min_length=1, max_length=200)
    description: str = Field(default="", max_length=20000)
    kind: Literal["sql", "migrations"] = "sql"
    sql: str = Field(default="", max_length=2_000_000)        # kind sql
    scripts: list[ScriptItem] = Field(default_factory=list, max_length=500)  # kind migrations
    status: Status = "open"
    author: str | None = None
    author_id: int | None = None
    created_at: float = Field(default_factory=time.time)
    updated_at: float = Field(default_factory=time.time)
    revision: int = 1
    dry_run: dict[str, Any] | None = None   # the last dry run report (+ revision, schema, changes fingerprints)
    dry_run_job: str | None = None
    approvals: list[Approval] = Field(default_factory=list)
    applied: dict[str, Any] | None = None   # {at, by, job_id, ok, results, error}
    apply_job: str | None = None
    history: list[Event] = Field(default_factory=list)

    def text(self) -> str:
        """The change as one text (what the reviewer reads; the fingerprint of a revision)."""
        if self.kind == "sql":
            return self.sql
        return "\n".join(f"-- {s.name}\n{s.sql}" for s in self.scripts)

    def log(self, who: Actor, action: str, detail: str = "") -> None:
        self.history.append(Event(by=who.username, action=action, detail=detail[:4000]))
        self.updated_at = time.time()


class Denied(Exception):
    """Not allowed for this user (answered with 403)."""


# ===================================================================== store
class ChangeStore:
    def __init__(self, path: Path):
        self.path = path
        self._lock = threading.RLock()
        self.items: dict[str, ChangeRequest] = {}
        if path.exists():
            for d in json.loads(path.read_text("utf-8") or "{}").get("requests", []):
                try:
                    cr = ChangeRequest(**d)
                    if cr.status == "applying":  # fbadmin stopped while applying: what ran is unknown
                        cr.status = "failed"
                        cr.history.append(Event(action="failed", detail="fbadmin stopped while applying the change: "
                                                "check the database before trying again"))
                    self.items[cr.id] = cr
                except Exception:  # noqa: BLE001 - a damaged entry must not hide the others
                    continue

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"requests": [c.model_dump() for c in self.items.values()]}, default=str), "utf-8")
        os.chmod(tmp, 0o600)
        os.replace(tmp, self.path)

    def add(self, cr: ChangeRequest) -> ChangeRequest:
        with self._lock:
            if len(self.items) >= MAX_REQUESTS:
                done = sorted((c for c in self.items.values() if c.status in DONE), key=lambda c: c.updated_at)
                if not done:
                    raise ValueError("Too many open change requests")
                del self.items[done[0].id]
            cr.number = max((c.number for c in self.items.values()), default=0) + 1
            self.items[cr.id] = cr
            self._save()
        return cr

    def get(self, cid: str) -> ChangeRequest:
        with self._lock:
            cr = self.items.get(cid)
            if cr is None:
                raise KeyError("Unknown change request")
            return cr

    def update(self, cid: str, fn) -> ChangeRequest:
        """Change a request under the lock: ``fn(cr)`` mutates it (and may raise to refuse)."""
        with self._lock:
            cr = self.get(cid).model_copy(deep=True)
            fn(cr)
            self.items[cid] = cr
            self._save()
            return cr

    def all(self) -> list[ChangeRequest]:
        with self._lock:
            return sorted(self.items.values(), key=lambda c: -c.created_at)

    def forget_database(self, server_id: str, db_id: str) -> None:
        with self._lock:
            for k in [k for k, c in self.items.items() if (c.server_id, c.database_id) == (server_id, db_id)]:
                del self.items[k]
            self._save()


_store: ChangeStore | None = None
_store_lock = threading.Lock()


def get_store() -> ChangeStore:
    global _store
    path = Path(settings.data_dir) / "change_requests.json"
    with _store_lock:
        if _store is None or _store.path != path:
            _store = ChangeStore(path)
        return _store


# ===================================================================== permissions
def level(server_id: str, db_id: str) -> int:
    """The current user's level on the database (admin in desktop mode); KeyError when it is hidden."""
    from .api.deps import get_state
    from .links import _access
    from .security.access import ADMIN

    get_state().store.get_database(server_id, db_id)
    acc = _access()
    if acc is None or acc.free:
        return ADMIN
    return acc.level(server_id, db_id)


def can_see(cr: ChangeRequest) -> bool:
    from .security.access import READ

    try:
        return level(cr.server_id, cr.database_id) >= READ
    except Exception:  # noqa: BLE001 - hidden or unknown database
        return False


def is_author(cr: ChangeRequest, who: Actor) -> bool:
    return who.desktop or (cr.author_id is not None and cr.author_id == who.user_id)


def can_review(cr: ChangeRequest) -> bool:
    from .security.access import ADMIN

    try:
        return level(cr.server_id, cr.database_id) >= ADMIN
    except Exception:  # noqa: BLE001
        return False


def may_approve(cr: ChangeRequest, who: Actor) -> str | None:
    """None when ``who`` may approve now; otherwise why not."""
    if not can_review(cr):
        return "Approving needs the 'admin' level on the database"
    if cr.status != "open":
        return f"The request is {cr.status}"
    if not who.desktop and cr.author_id == who.user_id and not SELF_APPROVE:
        return "You proposed this change: someone else must approve it"
    if any(a.user_id == who.user_id and a.by == who.username for a in cr.approvals) and not who.desktop:
        return "You already approved it"
    why = dry_run_problem(cr)
    return why


def dry_run_problem(cr: ChangeRequest) -> str | None:
    d = cr.dry_run
    if not d:
        return "Run the dry run first: it shows what the change does to the schema"
    if d.get("revision") != cr.revision:
        return "The change was edited after its dry run: run it again"
    if not d.get("ok"):
        return "The dry run failed"
    return None


def actions(cr: ChangeRequest, who: Actor) -> dict[str, bool]:
    review = can_review(cr)
    mine = is_author(cr, who)
    return {
        "edit": mine and cr.status in ("open", "approved", "rejected", "failed"),
        "withdraw": mine and cr.status in ("open", "approved", "failed"),
        "dry_run": review and cr.status in ("open", "approved", "failed"),
        "approve": may_approve(cr, who) is None,
        "reject": review and cr.status in ("open", "approved"),
        "apply": review and cr.status == "approved",
        "comment": True,
    }


# ===================================================================== fingerprints
def _hash(*parts: str) -> str:
    h = hashlib.sha256()
    for p in parts:
        h.update(p.encode("utf-8", "replace") + b"\0")
    return h.hexdigest()[:16]


def schema_fingerprint(server_id: str, db_id: str) -> str:
    """Identifies the live schema, to notice a change made since the dry run."""
    from .maintenance.drift import _live

    parts = []
    for schema, snap in sorted(_live(server_id, db_id).items()):
        for (typ, name), ddl in sorted(snap.objects().items()):
            parts += [schema, typ, name, ddl]
    return _hash(*parts)


def changes_fingerprint(report: dict[str, Any]) -> str:
    parts = []
    for c in report.get("changes") or []:
        parts += [c.get("schema") or "", c["type"], c["name"], c["change"], c.get("before") or "", c.get("after") or ""]
    return _hash(*parts)


# ===================================================================== notifications
def notify(cr: ChangeRequest, action: str, who: Actor, detail: str = "") -> None:
    """The change.request alert event and the database timeline; never raises."""
    from .alerts import emit

    db = _db_name(cr)
    level_ = {"failed": "critical", "rejected": "warning"}.get(action, "info")
    titles = {
        "submitted": f"Change request #{cr.number} for {db}: {cr.title}",
        "approved": f"Change request #{cr.number} approved: {cr.title}",
        "rejected": f"Change request #{cr.number} rejected: {cr.title}",
        "applied": f"Change request #{cr.number} applied to {db}: {cr.title}",
        "failed": f"Change request #{cr.number} failed on {db}: {cr.title}",
        "withdrawn": f"Change request #{cr.number} withdrawn: {cr.title}",
    }
    title = titles.get(action, f"Change request #{cr.number}: {action}")
    msg = f"{title}\nBy {who.username or 'fbadmin'}." + (f"\n\n{detail}" if detail else "") \
        + "\n\nOpen Change requests… on the database to review it."
    emit("change.request", level_, title, msg, server_id=cr.server_id, database_id=cr.database_id,
         key=f"change:{cr.id}:{action}", details={"change_id": cr.id, "number": cr.number, "action": action,
                                                  "author": cr.author, "by": who.username}, force=True)
    try:
        from .timeline import get_store as timeline

        timeline().add(cr.server_id, cr.database_id, "schema", f"change_{action}", title,
                       detail=(detail + "\n\n" if detail else "") + cr.text()[:15000], username=who.username,
                       status={"applied": "ok", "failed": "failed", "rejected": "warning"}.get(action, "ok"))
    except Exception:  # noqa: BLE001 - the timeline must not break the request
        pass


def audit(cr: ChangeRequest, what: str, ok: bool = True, error: str | None = None) -> None:
    try:
        from .api.deps import get_state

        sec = getattr(get_state(), "security", None)
        if sec is not None and sec.audit.enabled:
            sec.audit.record(server_id=cr.server_id, db_id=cr.database_id,
                             statement=f"-- change request #{cr.number} ({cr.id}): {what}", success=ok, error=error,
                             cat="DDL")
    except Exception:  # noqa: BLE001
        pass


def _db_name(cr: ChangeRequest) -> str:
    try:
        from .api.deps import get_state

        return get_state().store.get_database(cr.server_id, cr.database_id).name
    except Exception:  # noqa: BLE001
        return "the database"
