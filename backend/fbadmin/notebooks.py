"""SQL notebooks (roadmap 2.3): SQL cells, notes, results and charts in one saved document.

A notebook belongs to one database (the one its SQL cells run on) and holds an ordered list of cells:

* **sql** cells: the SQL text, the output of their last run (results capped at
  :data:`MAX_ROWS` rows each, messages, errors, who ran it and when) and an optional chart of the
  last result (the settings of the Query Tool's chart view: type, category, values, grouping);
* **markdown** cells: notes.

Cells run through the Query Tool's sessions (``/api/query/sessions``), so fbadmin's permissions apply
to every statement exactly as in the Query Tool; the notebook only stores what was shown.

Storage: one JSON file per notebook in ``<data dir>/notebooks/`` (mode 0600, written atomically).
Ownership and sharing work like the query library: in server mode a notebook is ``private`` or
``shared`` with every fbadmin user; the owner and fbadmin administrators may change a shared notebook,
everybody else can open it and save a copy. Saved results are only shown to users who may read the
notebook's database (whole database, no per-object restrictions); others see the SQL and notes.

Every save checks the revision it was based on (``base_rev``), so two people editing the same
notebook cannot overwrite each other silently. Revisions of the content (cells without their results)
are kept: saves by the same person within :data:`REVISION_MERGE_SECONDS` update one revision, so
autosave does not flood the history.
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator

from .config import settings
from .library import Actor

MAX_ROWS = int(os.environ.get("FBADMIN_NOTEBOOK_ROWS", "1000"))      # saved rows per result
MAX_RESULTS = 5                                                     # saved results per cell
MAX_CELLS = 500
MAX_BYTES = int(os.environ.get("FBADMIN_NOTEBOOK_MAX_BYTES", str(8 * 1024 * 1024)))
REVISIONS_KEEP = int(os.environ.get("FBADMIN_NOTEBOOK_REVISIONS", "30"))
REVISION_MERGE_SECONDS = 600
FORMAT = "fbadmin-notebook"

Visibility = Literal["private", "shared"]


def _id() -> str:
    return uuid.uuid4().hex[:12]


class Column(BaseModel):
    name: str
    type: str = ""


class ResultOut(BaseModel):
    kind: Literal["select", "command", "ignored", "error"] = "command"
    message: str = ""
    columns: list[Column] = Field(default_factory=list)
    rows: list[list[Any]] = Field(default_factory=list)
    row_count: int | None = None
    truncated: bool = False
    elapsed_ms: float | None = None

    @field_validator("rows")
    @classmethod
    def _cap(cls, rows: list[list[Any]]) -> list[list[Any]]:
        return rows[:MAX_ROWS]


class Output(BaseModel):
    ran_at: float = Field(default_factory=time.time)
    ran_by: str | None = None
    elapsed_ms: float | None = None
    error: str | None = None
    results: list[ResultOut] = Field(default_factory=list)

    @field_validator("results")
    @classmethod
    def _last(cls, results: list[ResultOut]) -> list[ResultOut]:
        return results[-MAX_RESULTS:]


class Chart(BaseModel):
    """The settings of the Query Tool's chart view (frontend ``features/charts/shape.ts`` ChartConfig);
    columns are positions in the cell's last result."""

    type: Literal["bar", "hbar", "stacked", "line", "area", "pie", "donut", "scatter"] = "bar"
    x: int = Field(ge=0, le=10000)
    ys: list[int] = Field(default_factory=list, max_length=8)
    group: int | None = Field(default=None, ge=0, le=10000)
    agg: Literal["sum", "avg", "count", "min", "max", "first"] = "sum"
    sort: Literal["auto", "label", "value-desc", "value-asc"] = "auto"
    topN: int | None = Field(default=None, ge=1, le=10000)  # noqa: N815 - the frontend's name


class Cell(BaseModel):
    id: str = Field(default_factory=_id)
    type: Literal["sql", "markdown"] = "sql"
    source: str = ""
    chart: Chart | None = None
    output: Output | None = None


class Revision(BaseModel):
    rev: int
    at: float
    author: str | None = None
    name: str
    description: str = ""
    cells: list[Cell] = Field(default_factory=list)  # without outputs


class Notebook(BaseModel):
    id: str = Field(default_factory=_id)
    name: str
    description: str = ""
    server_id: str | None = None
    database_id: str | None = None
    visibility: Visibility = "private"
    cells: list[Cell] = Field(default_factory=list)
    owner_id: int | None = None
    owner: str | None = None
    created_at: float = Field(default_factory=time.time)
    updated_at: float = Field(default_factory=time.time)
    updated_by: str | None = None
    rev: int = 1
    revisions: list[Revision] = Field(default_factory=list)


CONTENT = ("name", "description", "server_id", "database_id", "visibility", "cells")


class Conflict(Exception):
    """The notebook changed since the editor loaded it."""

    def __init__(self, nb: Notebook):
        super().__init__(f"{nb.updated_by or 'Someone'} saved this notebook in the meantime (revision {nb.rev})")
        self.notebook = nb


def can_see(nb: Notebook, who: Actor) -> bool:
    return who.desktop or nb.owner_id == who.user_id or nb.visibility == "shared"


def can_edit(nb: Notebook, who: Actor) -> bool:
    if who.desktop or nb.owner_id == who.user_id:
        return True
    return nb.visibility == "shared" and who.is_admin


def results_allowed(nb: Notebook) -> bool:
    """May the current fbadmin user see the saved results of this notebook's database?"""
    if not nb.server_id or not nb.database_id:
        return True
    try:
        from .api.deps import get_state

        sec = getattr(get_state(), "security", None)
        if sec is None:
            return True
        acc = sec.access.current()
    except Exception:  # noqa: BLE001 - no security context: desktop mode
        return True
    if acc.free:
        return True
    from .security.access import READ

    try:
        return acc.level(nb.server_id, nb.database_id) >= READ and not acc.object_rules(nb.server_id, nb.database_id)
    except Exception:  # noqa: BLE001 - unknown server or database
        return False


def _strip(cells: list[Cell]) -> list[Cell]:
    return [c.model_copy(update={"output": None}) for c in cells]


class NotebookStore:
    def __init__(self, folder: Path):
        self.folder = folder
        self._lock = threading.RLock()
        self.items: dict[str, Notebook] = {}
        if folder.is_dir():
            for f in sorted(folder.glob("*.json")):
                try:
                    nb = Notebook(**json.loads(f.read_text("utf-8")))
                    self.items[nb.id] = nb
                except Exception:  # noqa: BLE001 - a damaged file must not hide the others
                    continue

    # ------------------------------------------------------------ persistence
    def _file(self, nb_id: str) -> Path:
        if not re.fullmatch(r"[0-9a-f]{6,32}", nb_id):
            raise KeyError(f"Unknown notebook {nb_id!r}")
        return self.folder / f"{nb_id}.json"

    def _save(self, nb: Notebook) -> None:
        text = nb.model_dump_json()
        if len(text.encode()) > MAX_BYTES:
            raise ValueError(f"The notebook is too large to save ({len(text) // 1024 // 1024} MB, at most "
                             f"{MAX_BYTES // 1024 // 1024} MB): clear some results or split it")
        self.folder.mkdir(parents=True, exist_ok=True)
        path = self._file(nb.id)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(text, "utf-8")
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)

    # ------------------------------------------------------------------ views
    @staticmethod
    def view(nb: Notebook, who: Actor, *, full: bool = True) -> dict[str, Any]:
        d = nb.model_dump(exclude={"revisions", "cells"})
        d["can_edit"] = can_edit(nb, who)
        d["mine"] = who.desktop or nb.owner_id == who.user_id
        d["cell_count"] = len(nb.cells)
        d["revision_count"] = len(nb.revisions)
        if full:
            allowed = results_allowed(nb)
            d["results_hidden"] = not allowed and any(c.output for c in nb.cells)
            d["cells"] = [c.model_dump() for c in (nb.cells if allowed else _strip(nb.cells))]
        return d

    def _get(self, nb_id: str, who: Actor) -> Notebook:
        nb = self.items.get(nb_id)
        if nb is None or not can_see(nb, who):  # other users' private notebooks do not exist for you
            raise KeyError(f"Unknown notebook {nb_id!r}")
        return nb

    def get(self, nb_id: str, who: Actor) -> Notebook:
        with self._lock:
            return self._get(nb_id, who)

    def search(self, who: Actor, q: str | None = None, scope: str = "all",
               server_id: str | None = None, database_id: str | None = None) -> list[Notebook]:
        words = [w.lower() for w in (q or "").split() if w]
        with self._lock:
            items = list(self.items.values())
        out = []
        for nb in items:
            if not can_see(nb, who):
                continue
            mine = who.desktop or nb.owner_id == who.user_id
            if (scope == "mine" and not mine) or (scope == "shared" and nb.visibility != "shared"):
                continue
            if (server_id and nb.server_id != server_id) or (database_id and nb.database_id != database_id):
                continue
            if words:
                hay = "\n".join([nb.name, nb.description, nb.owner or ""] + [c.source for c in nb.cells]).lower()
                if not all(w in hay for w in words):
                    continue
            out.append(nb)
        out.sort(key=lambda n: -n.updated_at)
        return out

    # ------------------------------------------------------------------ write
    @staticmethod
    def _normalise(data: dict[str, Any], who: Actor) -> dict[str, Any]:
        if "name" in data:
            data["name"] = " ".join((data["name"] or "").split())[:200]
            if not data["name"]:
                raise ValueError("A name is required")
        if "description" in data:
            data["description"] = (data["description"] or "")[:4000]
        if who.desktop and "visibility" in data:
            data["visibility"] = "private"
        if "server_id" in data and not data["server_id"]:
            data["database_id"] = None
        if "cells" in data:
            cells = [c if isinstance(c, Cell) else Cell(**c) for c in data["cells"] or []]
            if len(cells) > MAX_CELLS:
                raise ValueError(f"A notebook holds at most {MAX_CELLS} cells")
            ids = [c.id for c in cells]
            if len(set(ids)) != len(ids):
                raise ValueError("Cell ids must be unique")
            for c in cells:
                if c.type == "markdown":
                    c.output = None
                    c.chart = None
            data["cells"] = cells
        return data

    @staticmethod
    def _revision(nb: Notebook, author: str | None) -> Revision:
        return Revision(rev=nb.rev, at=nb.updated_at, author=author, name=nb.name, description=nb.description,
                        cells=_strip(nb.cells))

    def create(self, data: dict[str, Any], who: Actor) -> Notebook:
        data = self._normalise({k: v for k, v in data.items() if k in CONTENT}, who)
        data.setdefault("cells", [Cell(type="sql", source="")])
        nb = Notebook(**data, owner_id=who.user_id, owner=who.username, updated_by=who.username)
        nb.revisions = [self._revision(nb, who.username)]
        with self._lock:
            self._save(nb)
            self.items[nb.id] = nb
        return nb

    def update(self, nb_id: str, changes: dict[str, Any], who: Actor, base_rev: int | None = None) -> Notebook:
        with self._lock:
            nb = self._get(nb_id, who)
            if not can_edit(nb, who):
                from .security.errors import AccessDenied

                raise AccessDenied("Only the owner or an fbadmin administrator can change this notebook. "
                                   "Save a copy to change it.")
            if base_rev is not None and base_rev != nb.rev:
                raise Conflict(nb)
            changes = self._normalise({k: v for k, v in changes.items() if k in CONTENT}, who)
            if changes.get("visibility") == "private" and nb.owner_id != who.user_id and not who.desktop:
                raise ValueError("Only the owner can make a shared notebook private")
            if "cells" in changes and not results_allowed(nb):
                # results this user cannot see were not sent back: keep the saved ones
                old = {c.id: c for c in nb.cells}
                for c in changes["cells"]:
                    if c.output is None and c.id in old and old[c.id].source == c.source:
                        c.output = old[c.id].output
            if all(getattr(nb, k) == v for k, v in changes.items()):
                return nb
            data = nb.model_dump()
            data.update({k: ([c.model_dump() for c in v] if k == "cells" else v) for k, v in changes.items()})
            new = Notebook(**data)
            new.rev = nb.rev + 1
            new.updated_at = time.time()
            new.updated_by = who.username
            rev = self._revision(new, who.username)
            revs = list(nb.revisions)
            last = revs[-1] if revs else None
            content_same = last is not None and (last.name, last.description, last.cells) == (rev.name, rev.description, rev.cells)
            if content_same:
                pass  # only results changed: no new revision
            elif last is not None and last.author == who.username and new.updated_at - last.at < REVISION_MERGE_SECONDS \
                    and len(revs) > 1:
                revs[-1] = rev
            else:
                revs.append(rev)
            new.revisions = revs[-REVISIONS_KEEP:]
            self._save(new)
            self.items[nb_id] = new
            return new

    def delete(self, nb_id: str, who: Actor) -> None:
        with self._lock:
            nb = self._get(nb_id, who)
            if not can_edit(nb, who):
                from .security.errors import AccessDenied

                raise AccessDenied("Only the owner or an fbadmin administrator can delete this notebook")
            self._file(nb_id).unlink(missing_ok=True)
            del self.items[nb_id]

    def copy(self, nb_id: str, who: Actor, name: str | None = None) -> Notebook:
        with self._lock:
            src = self._get(nb_id, who)
            cells = src.cells if results_allowed(src) else _strip(src.cells)
            data = {"name": name or f"{src.name} (copy)", "description": src.description,
                    "server_id": src.server_id, "database_id": src.database_id,
                    "cells": [c.model_copy(update={"id": _id()}) for c in cells]}
        return self.create(data, who)

    def restore(self, nb_id: str, rev: int, who: Actor) -> Notebook:
        with self._lock:
            nb = self._get(nb_id, who)
            r = next((x for x in nb.revisions if x.rev == rev), None)
            if r is None:
                raise KeyError(f"Revision {rev} is no longer kept")
            # keep the results of cells whose SQL is unchanged
            outputs = {c.id: c for c in nb.cells}
            cells = [c.model_copy(update={"output": outputs[c.id].output})
                     if c.id in outputs and outputs[c.id].source == c.source else c for c in r.cells]
            return self.update(nb_id, {"name": r.name, "description": r.description, "cells": cells}, who)

    # ------------------------------------------------------------ export / import
    def export(self, nb: Notebook, with_results: bool = True) -> dict[str, Any]:
        cells = nb.cells if with_results and results_allowed(nb) else _strip(nb.cells)
        return {"format": FORMAT, "version": 1, "name": nb.name, "description": nb.description,
                "exported_at": time.time(), "cells": [c.model_dump() for c in cells]}

    def import_(self, doc: dict[str, Any], who: Actor, server_id: str | None, database_id: str | None) -> Notebook:
        if doc.get("format") != FORMAT:
            raise ValueError("Not an fbadmin notebook (expected \"format\": \"fbadmin-notebook\")")
        cells = [Cell(**{**c, "id": _id()}) for c in doc.get("cells") or []]
        return self.create({"name": doc.get("name") or "Imported notebook", "description": doc.get("description") or "",
                            "server_id": server_id, "database_id": database_id, "cells": cells}, who)


def to_markdown(nb: Notebook, preview_rows: int = 50) -> str:
    """The notebook as Markdown: notes as they are, SQL in code blocks, results as tables."""
    out = [f"# {nb.name}", ""]
    if nb.description:
        out += [nb.description, ""]
    allowed = results_allowed(nb)
    for c in nb.cells:
        if c.type == "markdown":
            out += [c.source.rstrip(), ""]
            continue
        out += ["```sql", c.source.rstrip(), "```", ""]
        o = c.output if allowed else None
        if o is None:
            continue
        if o.error:
            out += [f"> Error: {o.error}", ""]
        for r in o.results:
            if r.kind == "select" and r.columns:
                cols = [col.name for col in r.columns]
                out.append("| " + " | ".join(_md(x) for x in cols) + " |")
                out.append("|" + "---|" * len(cols))
                for row in r.rows[:preview_rows]:
                    out.append("| " + " | ".join(_md(v) for v in row) + " |")
                more = (r.row_count or len(r.rows)) - min(len(r.rows), preview_rows)
                if more > 0 or r.truncated:
                    out.append(f"\n*{more if more > 0 else 'more'} more row(s) not shown*")
                out.append("")
            elif r.message:
                out += [f"*{r.message}*", ""]
        if c.chart:
            sel = [r for r in o.results if r.kind == "select"]
            cols = [col.name for col in sel[-1].columns] if sel else []
            what = ", ".join(_col(cols, i) for i in c.chart.ys) or "row count"
            out += [f"*Chart: {c.chart.type} of {what} by {_col(cols, c.chart.x)}*", ""]
        when = time.strftime("%Y-%m-%d %H:%M", time.localtime(o.ran_at))
        out += [f"<sub>Run {when}{' by ' + o.ran_by if o.ran_by else ''}</sub>", ""]
    return "\n".join(out).rstrip() + "\n"


def _col(cols: list[str], i: int) -> str:
    return cols[i] if 0 <= i < len(cols) else f"column {i + 1}"


def _md(v: Any) -> str:
    s = "NULL" if v is None else str(v)
    return s.replace("|", "\\|").replace("\n", " ")[:200]


_store: NotebookStore | None = None
_store_lock = threading.Lock()


def get_store() -> NotebookStore:
    global _store
    with _store_lock:
        folder = Path(settings.data_dir) / "notebooks"
        if _store is None or _store.folder != folder:
            _store = NotebookStore(folder)
        return _store
