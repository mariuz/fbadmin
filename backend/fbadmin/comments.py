"""Comments on objects and notebooks (roadmap 2.4): short threads ("this index is needed by the nightly report")
shown where the object is.

A comment belongs to a **target**: a database object (``otype`` such as ``tables`` or ``indices``, its name and,
on Firebird 6, its schema) or a notebook. Seeing and writing the comments of a target needs what seeing the
target needs:

* an object: the ``read`` level on that object (server mode; an object rule that hides it hides its comments);
* a notebook: being able to see the notebook (the owner's, or shared).

Authors edit and delete their own comments; fbadmin administrators delete any. Comments stay when the object
is dropped (recreating it brings them back) and go with their notebook. Each new comment raises the
``comment.added`` alert event, so a rule can tell a team about it.

Kept in ``<data dir>/comments.json`` (mode 0600).
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

MAX_TEXT = 4000
MAX_COMMENTS = 50_000


class Target(BaseModel):
    kind: Literal["object", "notebook"] = "object"
    server_id: str | None = None
    database_id: str | None = None
    otype: str | None = Field(default=None, max_length=40)       # object: tables, views, indices...
    name: str | None = Field(default=None, max_length=252)       # object name
    schema_name: str | None = Field(default=None, max_length=252, alias="schema")
    notebook_id: str | None = Field(default=None, max_length=64)

    model_config = {"populate_by_name": True}

    def key(self) -> str:
        if self.kind == "notebook":
            return f"nb:{self.notebook_id}"
        return f"obj:{self.server_id}:{self.database_id}:{self.otype}:{self.schema_name or ''}:{self.name}"


class Comment(BaseModel):
    id: str = Field(default_factory=lambda: secrets.token_hex(5))
    target: Target
    text: str = Field(min_length=1, max_length=MAX_TEXT)
    author: str | None = None
    author_id: int | None = None
    created_at: float = Field(default_factory=time.time)
    edited_at: float | None = None


class Denied(Exception):
    """Not allowed (403)."""


class CommentStore:
    def __init__(self, path: Path):
        self.path = path
        self._lock = threading.RLock()
        self.items: dict[str, Comment] = {}
        if path.exists():
            for d in json.loads(path.read_text("utf-8") or "{}").get("comments", []):
                try:
                    c = Comment(**d)
                    self.items[c.id] = c
                except Exception:  # noqa: BLE001 - a damaged entry must not hide the others
                    continue

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"comments": [c.model_dump(by_alias=True) for c in self.items.values()]},
                                  default=str), "utf-8")
        os.chmod(tmp, 0o600)
        os.replace(tmp, self.path)

    def add(self, c: Comment) -> Comment:
        with self._lock:
            if len(self.items) >= MAX_COMMENTS:
                raise ValueError("Too many comments")
            self.items[c.id] = c
            self._save()
        return c

    def get(self, cid: str) -> Comment:
        with self._lock:
            c = self.items.get(cid)
            if c is None:
                raise KeyError("Unknown comment")
            return c

    def update(self, cid: str, text: str) -> Comment:
        with self._lock:
            c = self.get(cid).model_copy(update={"text": text, "edited_at": time.time()})
            self.items[cid] = c
            self._save()
            return c

    def delete(self, cid: str) -> None:
        with self._lock:
            self.items.pop(cid, None)
            self._save()

    def thread(self, key: str) -> list[Comment]:
        with self._lock:
            return sorted((c for c in self.items.values() if c.target.key() == key), key=lambda c: c.created_at)

    def in_database(self, server_id: str, db_id: str) -> list[Comment]:
        with self._lock:
            return [c for c in self.items.values() if c.target.kind == "object"
                    and (c.target.server_id, c.target.database_id) == (server_id, db_id)]

    def forget(self, pred) -> int:
        with self._lock:
            gone = [k for k, c in self.items.items() if pred(c)]
            for k in gone:
                del self.items[k]
            if gone:
                self._save()
            return len(gone)


_store: CommentStore | None = None
_store_lock = threading.Lock()


def get_store() -> CommentStore:
    global _store
    path = Path(settings.data_dir) / "comments.json"
    with _store_lock:
        if _store is None or _store.path != path:
            _store = CommentStore(path)
        return _store


# ===================================================================== permissions
def _qualified(t: Target) -> str:
    return f"{t.schema_name}.{t.name}" if t.schema_name else (t.name or "")


def check(t: Target, who: Actor) -> None:
    """Raise KeyError (as for an unknown target) unless ``who`` may see the target."""
    from .api.deps import get_state
    from .links import may_read

    if t.kind == "notebook":
        if not t.notebook_id:
            raise ValueError("The notebook is missing")
        from . import notebooks

        notebooks.get_store().get(t.notebook_id, who)  # KeyError when it is not visible
        return
    if not (t.server_id and t.database_id and t.otype and t.name):
        raise ValueError("The object is missing")
    get_state().store.get_database(t.server_id, t.database_id)  # KeyError when hidden or unknown
    if not may_read(t.server_id, t.database_id, t.name):
        raise KeyError("Unknown object")


def may_change(c: Comment, who: Actor) -> bool:
    return who.desktop or (c.author_id is not None and c.author_id == who.user_id)


def may_delete(c: Comment, who: Actor) -> bool:
    return may_change(c, who) or who.is_admin


def view(c: Comment, who: Actor) -> dict[str, Any]:
    return {**c.model_dump(by_alias=True), "mine": may_change(c, who), "can_delete": may_delete(c, who)}


def notify(c: Comment) -> None:
    """The ``comment.added`` alert event; never raises."""
    from .alerts import emit

    t = c.target
    if t.kind == "notebook":
        try:
            from . import notebooks

            nb = notebooks.get_store().items.get(t.notebook_id)
            where, sid, dbid = f'notebook "{nb.name if nb else t.notebook_id}"', getattr(nb, "server_id", None), \
                getattr(nb, "database_id", None)
        except Exception:  # noqa: BLE001
            where, sid, dbid = "a notebook", None, None
    else:
        where, sid, dbid = f"{t.otype[:-1] if t.otype.endswith('s') else t.otype} {_qualified(t)}", t.server_id, \
            t.database_id
    first = " ".join(c.text.split())
    emit("comment.added", "info", f"Comment on {where} by {c.author or 'fbadmin'}",
         f"{c.author or 'fbadmin'} commented on {where}:\n\n{c.text}",
         server_id=sid, database_id=dbid, key=f"comment:{c.id}", force=True,
         details={"comment_id": c.id, "target": t.model_dump(by_alias=True), "excerpt": first[:200]})
