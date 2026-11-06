"""Shareable links (roadmap 2.3): a short link to an object, a saved query, a notebook or a result snapshot.

A link never grants anything: it is resolved with the permissions of whoever opens it.

* **object** (a table, view, procedure...): the viewer must see the server and database and have
  read access to the object;
* **query** (a query library item) and **notebook**: the viewer must be able to see the item (its
  owner's, or shared), as in the library and the notebooks list;
* **snapshot**: a copy of a query result (columns and rows, at most :data:`MAX_ROWS` rows), taken when
  the link was made, with the SQL that produced it. Shown only to users who may read the database it
  came from as a whole (as saved notebook results), because it holds data.

Links are kept in ``<data dir>/links.json`` (mode 0600). They can expire, and the person who made a
link or an fbadmin administrator can revoke it. Opening a link records nothing but its open count.
"""

from __future__ import annotations

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

MAX_ROWS = int(os.environ.get("FBADMIN_SNAPSHOT_ROWS", "1000"))
MAX_LINKS_PER_USER = 1000
Kind = Literal["object", "query", "notebook", "snapshot"]


class Snapshot(BaseModel):
    sql: str = ""
    columns: list[dict[str, Any]] = Field(default_factory=list)  # {name, type}
    rows: list[list[Any]] = Field(default_factory=list)
    row_count: int | None = None   # rows the query had fetched (may be more than kept)
    truncated: bool = False
    taken_at: float = Field(default_factory=time.time)


class Link(BaseModel):
    id: str = Field(default_factory=lambda: secrets.token_urlsafe(9))
    kind: Kind
    title: str = ""
    server_id: str | None = None
    database_id: str | None = None
    otype: str | None = None          # object: tables, views, procedures...
    name: str | None = None           # object name
    schema_name: str | None = None    # Firebird 6 schema of the object
    item_id: str | None = None        # query: library item id; notebook: notebook id
    snapshot: Snapshot | None = None
    created_by: str | None = None
    owner_id: int | None = None
    created_at: float = Field(default_factory=time.time)
    expires_at: float | None = None
    opened: int = 0
    last_opened: float | None = None


class Denied(Exception):
    """The viewer may not open what the link points to (shown to them as the reason)."""


def _can_manage(link: Link, who: Actor) -> bool:
    return who.desktop or who.is_admin or link.owner_id == who.user_id


class LinkStore:
    def __init__(self, path: Path):
        self.path = path
        self._lock = threading.Lock()
        self.links: dict[str, Link] = {}
        if path.exists():
            for d in json.loads(path.read_text("utf-8") or "{}").get("links", []):
                try:
                    lk = Link(**d)
                    self.links[lk.id] = lk
                except Exception:  # noqa: BLE001 - a damaged entry must not hide the others
                    continue

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"links": [lk.model_dump() for lk in self.links.values()]}, default=str), "utf-8")
        os.chmod(tmp, 0o600)
        os.replace(tmp, self.path)

    def _prune(self) -> None:
        now = time.time()
        for k in [k for k, lk in self.links.items() if lk.expires_at and lk.expires_at < now]:
            del self.links[k]

    def add(self, link: Link, who: Actor) -> Link:
        with self._lock:
            self._prune()
            mine = sum(1 for lk in self.links.values() if lk.owner_id == who.user_id)
            if mine >= MAX_LINKS_PER_USER:
                raise ValueError(f"You have {mine} links: revoke some first")
            link.created_by, link.owner_id = who.username, who.user_id
            self.links[link.id] = link
            self._save()
        return link

    def get(self, link_id: str) -> Link:
        with self._lock:
            lk = self.links.get(link_id)
            if lk is None or (lk.expires_at and lk.expires_at < time.time()):
                raise KeyError("This link does not exist, has expired or was revoked")
            return lk

    def opened(self, link_id: str) -> None:
        with self._lock:
            lk = self.links.get(link_id)
            if lk is not None:
                lk.opened += 1
                lk.last_opened = time.time()
                self._save()

    def mine(self, who: Actor) -> list[Link]:
        with self._lock:
            self._prune()
            items = [lk for lk in self.links.values() if who.desktop or lk.owner_id == who.user_id]
        return sorted(items, key=lambda lk: -lk.created_at)

    def all(self) -> list[Link]:
        with self._lock:
            self._prune()
            return sorted(self.links.values(), key=lambda lk: -lk.created_at)

    def revoke(self, link_id: str, who: Actor) -> None:
        with self._lock:
            lk = self.links.get(link_id)
            if lk is None:
                raise KeyError("Unknown link")
            if not _can_manage(lk, who):
                from .security.errors import AccessDenied

                raise AccessDenied("Only the person who made the link or an fbadmin administrator can revoke it")
            del self.links[link_id]
            self._save()


_store: LinkStore | None = None
_store_lock = threading.Lock()


def get_store() -> LinkStore:
    global _store
    path = Path(settings.data_dir) / "links.json"
    with _store_lock:
        if _store is None or _store.path != path:
            _store = LinkStore(path)
        return _store


# ===================================================================== permissions
def _access():
    try:
        from .api.deps import get_state

        sec = getattr(get_state(), "security", None)
        return None if sec is None else sec.access.current()
    except Exception:  # noqa: BLE001 - desktop mode
        return None


def check_database(server_id: str | None, db_id: str | None) -> tuple[Any, Any]:
    """The server and database, if the current user can see them (KeyError otherwise, as for unknown ones)."""
    from .api.deps import get_state

    st = get_state()
    if not server_id or not db_id:
        raise Denied("The link has no database")
    try:
        return st.store.get(server_id), st.store.get_database(server_id, db_id)
    except KeyError:
        raise Denied("The database of this link is not registered for you in fbadmin") from None


def may_read(server_id: str, db_id: str, obj: str | None = None, whole: bool = False) -> bool:
    acc = _access()
    if acc is None or acc.free:
        return True
    from .security.access import READ

    try:
        level = acc.level(server_id, db_id, obj) if obj else acc.level(server_id, db_id)
        if level < READ:
            return False
        return not (whole and acc.object_rules(server_id, db_id))  # whole: no per-object restrictions
    except Exception:  # noqa: BLE001
        return False


def check_create(link: Link, who: Actor) -> None:
    """Whoever makes a link must be able to open it themselves."""
    resolve(link, who)


def resolve(link: Link, who: Actor) -> dict[str, Any]:
    """What the link opens, for the current user, or Denied/KeyError."""
    out: dict[str, Any] = {"id": link.id, "kind": link.kind, "title": link.title, "created_by": link.created_by,
                           "created_at": link.created_at, "expires_at": link.expires_at}
    if link.kind == "object":
        srv, db = check_database(link.server_id, link.database_id)
        if not may_read(link.server_id, link.database_id, link.name):
            raise Denied(f"Your fbadmin permissions do not allow reading {link.name}")
        out.update(server_id=link.server_id, database_id=link.database_id, otype=link.otype, name=link.name,
                   schema=link.schema_name, server=srv.name, database=db.name)
    elif link.kind == "query":
        from . import library

        try:
            item = library.get_store().get(link.item_id or "", who)
        except KeyError:
            raise Denied("The saved query of this link was deleted, or it is private") from None
        out.update(item_id=item.id, name=item.name, sql=item.sql, server_id=item.server_id, database_id=item.database_id)
    elif link.kind == "notebook":
        from . import notebooks

        try:
            nb = notebooks.get_store().get(link.item_id or "", who)
        except KeyError:
            raise Denied("The notebook of this link was deleted, or it is private") from None
        out.update(item_id=nb.id, name=nb.name, server_id=nb.server_id, database_id=nb.database_id)
    elif link.kind == "snapshot":
        srv, db = check_database(link.server_id, link.database_id)
        if not may_read(link.server_id, link.database_id, whole=True):
            raise Denied("This result came from a database your fbadmin permissions do not let you read")
        out.update(server_id=link.server_id, database_id=link.database_id, server=srv.name, database=db.name,
                   snapshot=link.snapshot.model_dump() if link.snapshot else None)
    return out
