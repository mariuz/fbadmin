"""Query library: saved queries and snippets, stored on the fbadmin server (milestone 2.0).

Stored as JSON in ``<data dir>/library.json`` (file mode 0600, written atomically), like the
other fbadmin registries (servers, schedules, alerts). One file holds the items of every user:

* **Desktop mode** - there is one user, so every item is simply "the local library".
* **Server mode** - each item has an owner (the fbadmin user who created it) and a visibility:
  ``private`` (only the owner sees it) or ``shared`` (every fbadmin user sees it). The owner and
  fbadmin administrators may change or delete a shared item; everybody else can only save a copy.
  Administrators do not see other users' private items.

Every save keeps a revision (author, time, content); the last :data:`REVISIONS_KEEP` revisions of
an item are kept and any of them can be restored (which itself creates a new revision).
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

from .config import settings

REVISIONS_KEEP = int(os.environ.get("FBADMIN_LIBRARY_REVISIONS", "20"))
CONTENT_FIELDS = ("name", "description", "sql", "tags", "folder", "server_id", "database_id", "visibility")

Visibility = Literal["private", "shared"]


def _id() -> str:
    return uuid.uuid4().hex[:12]


def clean_tags(tags: list[str] | None) -> list[str]:
    out: list[str] = []
    for t in tags or []:
        t = " ".join(str(t).split()).strip().lstrip("#")
        if t and t.lower() not in (x.lower() for x in out):
            out.append(t[:40])
    return out[:20]


def clean_folder(folder: str | None) -> str:
    parts = [p.strip() for p in (folder or "").replace("\\", "/").split("/")]
    return "/".join(p for p in parts if p)[:200]


class Revision(BaseModel):
    rev: int
    at: float
    author: str | None = None
    name: str
    description: str = ""
    sql: str
    tags: list[str] = Field(default_factory=list)
    folder: str = ""
    server_id: str | None = None
    database_id: str | None = None
    visibility: Visibility = "private"


class Item(BaseModel):
    id: str = Field(default_factory=_id)
    name: str
    description: str = ""
    sql: str
    tags: list[str] = Field(default_factory=list)
    folder: str = ""
    server_id: str | None = None      # default server/database to open the query on
    database_id: str | None = None
    visibility: Visibility = "private"
    owner_id: int | None = None       # fbadmin user id (None in desktop mode)
    owner: str | None = None          # user name at creation (display only)
    created_at: float = Field(default_factory=time.time)
    updated_at: float = Field(default_factory=time.time)
    updated_by: str | None = None
    rev: int = 1
    revisions: list[Revision] = Field(default_factory=list)


class Actor(BaseModel):
    """Who is asking: ``user_id`` None means desktop mode (single user, may do everything)."""

    user_id: int | None = None
    username: str | None = None
    is_admin: bool = True

    @property
    def desktop(self) -> bool:
        return self.user_id is None


def can_see(item: Item, who: Actor) -> bool:
    return who.desktop or item.owner_id == who.user_id or item.visibility == "shared"


def can_edit(item: Item, who: Actor) -> bool:
    if who.desktop or item.owner_id == who.user_id:
        return True
    return item.visibility == "shared" and who.is_admin


class LibraryStore:
    def __init__(self, path: Path):
        self.path = path
        self._lock = threading.RLock()
        self.items: dict[str, Item] = {}
        if path.exists():
            raw = json.loads(path.read_text("utf-8") or "{}")
            for d in raw.get("items", []):
                it = Item(**d)
                self.items[it.id] = it

    # ------------------------------------------------------------ persistence
    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"version": 1, "items": [i.model_dump() for i in self.items.values()]},
                                  indent=1), "utf-8")
        os.chmod(tmp, 0o600)
        os.replace(tmp, self.path)

    # ------------------------------------------------------------------ views
    @staticmethod
    def view(item: Item, who: Actor, *, with_revisions: bool = False) -> dict[str, Any]:
        d = item.model_dump(exclude={"revisions"})
        d["can_edit"] = can_edit(item, who)
        d["mine"] = who.desktop or item.owner_id == who.user_id
        d["revision_count"] = len(item.revisions)
        if with_revisions:
            d["revisions"] = [r.model_dump() for r in reversed(item.revisions)]
        return d

    def _get(self, item_id: str, who: Actor) -> Item:
        it = self.items.get(item_id)
        if it is None or not can_see(it, who):  # other users' private items do not exist for you
            raise KeyError(f"Unknown library item {item_id!r}")
        return it

    def _check_edit(self, it: Item, who: Actor) -> None:
        if not can_edit(it, who):
            from .security.errors import AccessDenied

            raise AccessDenied("Only the owner or an fbadmin administrator can change this item. "
                               "Save a copy to change it.")

    # ------------------------------------------------------------------ query
    def search(self, who: Actor, *, q: str | None = None, tag: str | None = None, folder: str | None = None,
               scope: str = "all", server_id: str | None = None, database_id: str | None = None) -> list[Item]:
        """Items visible to ``who``. ``q`` matches name, description, tags, folder and SQL text (every
        word must match, case-insensitive); ``folder`` includes sub-folders."""
        words = [w.lower() for w in (q or "").split() if w]
        folder = clean_folder(folder) if folder else None
        out = []
        with self._lock:
            items = list(self.items.values())
        for it in items:
            if not can_see(it, who):
                continue
            mine = who.desktop or it.owner_id == who.user_id
            if scope == "mine" and not mine:
                continue
            if scope == "shared" and it.visibility != "shared":
                continue
            if tag and tag.lower() not in (t.lower() for t in it.tags):
                continue
            if folder is not None and not (it.folder == folder or it.folder.startswith(folder + "/")):
                continue
            if server_id and it.server_id != server_id:
                continue
            if database_id and it.database_id != database_id:
                continue
            if words:
                hay = "\n".join([it.name, it.description, it.folder, " ".join(it.tags), it.sql, it.owner or ""]).lower()
                if not all(w in hay for w in words):
                    continue
            out.append(it)
        out.sort(key=lambda i: (i.folder.lower(), i.name.lower()))
        return out

    def summary(self, who: Actor) -> dict[str, Any]:
        """Folders and tags (with counts) of the items visible to ``who``."""
        folders: dict[str, int] = {}
        tags: dict[str, int] = {}
        for it in self.search(who):
            if it.folder:
                parts = it.folder.split("/")
                for i in range(1, len(parts) + 1):
                    k = "/".join(parts[:i])
                    folders[k] = folders.get(k, 0) + 1
            for t in it.tags:
                tags[t] = tags.get(t, 0) + 1
        return {"folders": [{"path": k, "count": v} for k, v in sorted(folders.items(), key=lambda x: x[0].lower())],
                "tags": [{"tag": k, "count": v} for k, v in sorted(tags.items(), key=lambda x: x[0].lower())]}

    def get(self, item_id: str, who: Actor) -> Item:
        with self._lock:
            return self._get(item_id, who)

    # ------------------------------------------------------------------ write
    @staticmethod
    def _revision(it: Item, author: str | None) -> Revision:
        return Revision(rev=it.rev, at=it.updated_at, author=author,
                        **{k: getattr(it, k) for k in CONTENT_FIELDS})

    @staticmethod
    def _normalise(data: dict[str, Any], who: Actor) -> dict[str, Any]:
        if "name" in data:
            data["name"] = (data["name"] or "").strip()[:200]
            if not data["name"]:
                raise ValueError("A name is required")
        if "sql" in data and not (data["sql"] or "").strip():
            raise ValueError("The SQL text is empty")
        if "tags" in data:
            data["tags"] = clean_tags(data["tags"])
        if "folder" in data:
            data["folder"] = clean_folder(data["folder"])
        if "description" in data:
            data["description"] = (data["description"] or "")[:4000]
        if who.desktop and "visibility" in data:
            data["visibility"] = "private"  # one user: nothing to share with
        if data.get("server_id") is None and "server_id" in data:
            data["database_id"] = None
        return data

    def create(self, data: dict[str, Any], who: Actor) -> Item:
        data = self._normalise({k: v for k, v in data.items() if k in CONTENT_FIELDS}, who)
        it = Item(**data, owner_id=who.user_id, owner=who.username, updated_by=who.username)
        it.revisions = [self._revision(it, who.username)]
        with self._lock:
            self.items[it.id] = it
            self._save()
        return it

    def update(self, item_id: str, changes: dict[str, Any], who: Actor) -> Item:
        with self._lock:
            it = self._get(item_id, who)
            self._check_edit(it, who)
            changes = self._normalise({k: v for k, v in changes.items() if k in CONTENT_FIELDS}, who)
            if changes.get("visibility") == "private" and it.owner_id != who.user_id and not who.desktop:
                raise ValueError("Only the owner can make a shared item private")
            if all(getattr(it, k) == v for k, v in changes.items()):
                return it
            data = it.model_dump()
            data.update(changes)
            new = Item(**data)
            new.rev = it.rev + 1
            new.updated_at = time.time()
            new.updated_by = who.username
            new.revisions = (it.revisions + [self._revision(new, who.username)])[-REVISIONS_KEEP:]
            self.items[item_id] = new
            self._save()
            return new

    def delete(self, item_id: str, who: Actor) -> None:
        with self._lock:
            it = self._get(item_id, who)
            self._check_edit(it, who)
            del self.items[item_id]
            self._save()

    def copy(self, item_id: str, who: Actor, name: str | None = None) -> Item:
        with self._lock:
            src = self._get(item_id, who)
            data = {k: getattr(src, k) for k in CONTENT_FIELDS}
        data["name"] = name or f"{src.name} (copy)"
        data["visibility"] = "private"
        return self.create(data, who)

    def restore(self, item_id: str, rev: int, who: Actor) -> Item:
        with self._lock:
            it = self._get(item_id, who)
            r = next((x for x in it.revisions if x.rev == rev), None)
            if r is None:
                raise KeyError(f"Revision {rev} is no longer kept")
            changes = {k: getattr(r, k) for k in CONTENT_FIELDS}
            if it.owner_id != who.user_id and not who.desktop:
                changes.pop("visibility")  # restoring content never changes who can see it
            return self.update(item_id, changes, who)

    def import_items(self, items: list[dict[str, Any]], who: Actor, folder: str = "") -> int:
        """Import snippets (name + SQL). Items identical to one this user already owns are skipped."""
        n = 0
        for d in items:
            name = (d.get("name") or "").strip()
            sql = d.get("sql") or ""
            if not name or not sql.strip():
                continue
            with self._lock:
                dup = any(i.name == name and i.sql == sql and (who.desktop or i.owner_id == who.user_id)
                          for i in self.items.values())
            if dup:
                continue
            self.create({"name": name, "sql": sql, "folder": d.get("folder") or folder,
                         "tags": d.get("tags") or [], "description": d.get("description") or ""}, who)
            n += 1
        return n


_store: LibraryStore | None = None
_store_lock = threading.Lock()


def get_store() -> LibraryStore:
    global _store
    with _store_lock:
        path = Path(settings.data_dir) / "library.json"
        if _store is None or _store.path != path:
            _store = LibraryStore(path)
        return _store
