"""Data compare engine: compare the rows of tables in two databases by key, build a sync script.

How a table is compared
-----------------------
Both sides are read with ``SELECT <key columns>, <value columns> ... ORDER BY <key columns>`` in a
read-only snapshot transaction and merge-joined in Python, so memory use does not depend on the
table size. Rows only in the source become INSERTs, rows only in the target DELETEs, rows whose
values differ UPDATEs of the changed columns.

Key ordering: a merge join needs both streams sorted by the *same* ordering as the comparison of
keys in Python. For numbers, dates and UTF8 strings with the default (binary) collation, the
server's ORDER BY agrees with Python. It does not for e.g. case-insensitive or language
collations, single-byte character sets whose byte order differs from Unicode order, or CHAR
padding with control characters - and the two servers may even use different collations. So
every stream is checked while it is read: each key must be strictly greater (in Python) than the
previous one. When a key arrives out of order, the table is compared again with both sides
sorted in Python instead (an external merge sort: runs of rows are sorted in memory and spilled
to temporary files, then merged with ``heapq.merge``). The result is always computed with one
consistent ordering, so collation differences cannot produce false differences, and the common
case costs nothing extra.

Values are compared as returned by the driver, with a few normalisations: TIME/TIMESTAMP WITH
TIME ZONE are read as Firebird's text (so the zone counts: Python compares aware datetimes by
instant only), CHAR values have their padding removed (by the driver), NaN equals NaN, and BLOBs are compared by
content.

Key matching: the target decides which keys are equal. When a key column's collation in the target
ignores case, accents or trailing spaces (probed on the target server, see ``key_collations``),
keys are matched in the same way, so ``'abc'`` in the source and ``'ABC'`` in the target are one
row (an UPDATE of the key column), not an INSERT that the target's unique index would reject plus
a DELETE.

Script
------
Statements are written to per-table record files (length-prefixed UTF-8) while comparing, so the
script never has to be held in memory. Order: INSERTs parents first, then UPDATEs parents first,
then DELETEs children first (by the foreign keys between the compared tables, from
``RDB$REF_CONSTRAINTS`` of both databases). Inserting before deleting lets updated child rows move
to new parent rows before old parents are deleted. Within a table that references itself (a foreign
key to its own key, e.g. a tree), INSERTs are ordered parents first and DELETEs children first.

Runs (results, sample rows and statement files) are kept in ``<data dir>/datacompare`` and survive
restarts of fbadmin.
"""

from __future__ import annotations

import atexit
import datetime as dt
import decimal
import heapq
import json
import logging
import math
import os
import pickle
import shutil
import struct
import tempfile
import threading
import time
import unicodedata
from collections import OrderedDict
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from operator import itemgetter
from pathlib import Path
from typing import Any

from .. import jobs
from ..config import settings
from ..metadata import Catalog
from ..metadata.types import quote_ident
from ..query import TxOptions, apply_tpb
from ..schema.model import topo_sort
from ..serialize import to_json
from .literals import sql_literal

PREVIEW_CHARS = 200  # long text in the sample rows sent to the browser
SORT_RUN_ROWS = 20000  # rows per in-memory run of the external sort
FETCH = 500
PROGRESS_EVERY = 100_000
log_ = logging.getLogger("fbadmin.datacompare")


class OutOfOrder(Exception):
    """A stream returned keys not in ascending Python order: compare again with Python sorting."""


class TableError(Exception):
    """The table cannot be compared (duplicate keys, incomparable key types...)."""


# ------------------------------------------------------------------ columns
@dataclass
class Column:
    name: str
    data_type: str
    kind: str = ""  # literal hint, see literals.sql_literal
    select: str = ""
    conv: Callable[[Any], Any] | None = None
    identity: str | None = None
    not_null: bool = False
    has_default: bool = False
    computed: bool = False
    text: bool = False  # CHAR/VARCHAR


def column_info(c: dict) -> Column:
    ft, st = c.get("field_type"), c.get("field_sub_type")
    q = quote_ident(c["name"])
    col = Column(name=c["name"], data_type=c["data_type"], select=q, identity=c.get("identity"),
                 not_null=bool(c.get("not_null")), has_default=bool(c.get("default")),
                 computed=bool(c.get("computed")))
    # INT128 (ft 26) needs nothing special: fbadmin.driver_patches decodes it (and NUMERIC/DECIMAL
    # with precision > 18) correctly. The time zone types are read as Firebird's text on purpose:
    # the zone is part of the value (Python compares aware datetimes by instant only, so
    # 10:00 Europe/Bucharest would equal 08:00 UTC) and the text is the literal of the script.
    if ft == 28:
        col.kind, col.select = "time_tz", f"CAST({q} AS VARCHAR(80))"
    elif ft == 29:
        col.kind, col.select = "timestamp_tz", f"CAST({q} AS VARCHAR(80))"
    elif ft in (24, 25):
        col.kind = "decfloat"
    elif ft == 261:
        col.kind = "blob_text" if st == 1 else "blob"
    elif ft == 14:
        col.kind = "char"
    col.text = ft in (14, 37)
    return col


def open_catalog(conn) -> Catalog:
    """Catalog of the default schema (PUBLIC on Firebird 6; no schemas before)."""
    return Catalog(conn, "PUBLIC")


def snapshot_readonly(conn) -> None:
    """One consistent, read-only view of the data for the whole comparison."""
    apply_tpb(conn, TxOptions(isolation="snapshot", read_only=True).tpb())


# ------------------------------------------------------------------ planning
def _ignored(table: str, col: str, ignore: set[str]) -> bool:
    return col.upper() in ignore or f"{table}.{col}".upper() in ignore


def user_tables(cat: Catalog) -> dict[str, int | None]:
    return {t["name"]: t["relation_type"] for t in cat.list_objects("tables")}


def plan_table(name: str, scat: Catalog | None, tcat: Catalog | None, s_rt: int | None = 0,
               t_rt: int | None = 0, ignore: set[str] | None = None) -> dict:
    """How ``name`` would be compared: key, compared columns, or why it cannot be."""
    ignore = {i.upper() for i in (ignore or set())}
    p: dict[str, Any] = {
        "name": name, "in_source": scat is not None, "in_target": tcat is not None, "comparable": False,
        "reason": None, "key": [], "key_type": None, "columns": [], "source_only": [], "target_only": [],
        "computed": [], "ignored": [], "type_mismatch": [], "warnings": [],
    }
    if scat is None:
        p["reason"] = "Only in the target database"
        return p
    if tcat is None:
        p["reason"] = "Only in the source database"
        return p
    if s_rt in (4, 5) or t_rt in (4, 5):
        p["reason"] = "Global temporary table"
        return p
    scols = {c["name"]: column_info(c) for c in scat.columns(name)}
    tcols = {c["name"]: column_info(c) for c in tcat.columns(name)}
    p["source_only"] = [n for n in scols if n not in tcols]
    p["target_only"] = [n for n in tcols if n not in scols]
    common = [n for n in scols if n in tcols]
    p["computed"] = [n for n in common if scols[n].computed or tcols[n].computed]
    usable = [n for n in common if n not in p["computed"]]
    p["type_mismatch"] = [{"column": n, "source": scols[n].data_type, "target": tcols[n].data_type}
                          for n in usable if scols[n].data_type != tcols[n].data_type]

    def keys(cat: Catalog) -> list[tuple[str, list[str]]]:
        return [(c["type"], c["columns"]) for c in cat.constraints(name) if c["type"] in ("PRIMARY KEY", "UNIQUE")]

    skeys, tkeys = keys(scat), keys(tcat)
    tsets = {frozenset(cols) for _, cols in tkeys}
    ssets = {frozenset(cols) for _, cols in skeys}
    candidates = [k for k in skeys if frozenset(k[1]) in tsets] + skeys + tkeys
    key = next((k for k in candidates if k[1] and all(c in usable for c in k[1])), None)
    if key is None:
        p["reason"] = "No primary key or unique constraint on columns present in both databases"
        return p
    p["key_type"], p["key"] = key
    if frozenset(key[1]) not in tsets:
        p["warnings"].append("The key is not unique in the target database")
    elif frozenset(key[1]) not in ssets:
        p["warnings"].append("The key is not unique in the source database")
    if key[0] == "UNIQUE":
        p["warnings"].append("No primary key: compared by a unique key (rows with NULL in the key are skipped)")
    kset = set(p["key"])
    if any(_ignored(name, k, ignore) for k in kset):
        p["warnings"].append("Key columns cannot be ignored")
    p["ignored"] = [n for n in usable if n not in kset and _ignored(name, n, ignore)]
    p["columns"] = p["key"] + [n for n in usable if n not in kset and n not in p["ignored"]]
    for n in p["target_only"]:
        c = tcols[n]
        if c.not_null and not c.has_default and not c.identity and not c.computed:
            p["warnings"].append(f"Target column {n} is NOT NULL without a default: INSERT statements will fail")
    for n in p["columns"][len(p["key"]):]:
        if tcols[n].identity == "ALWAYS":
            p["warnings"].append(f"{n} is GENERATED ALWAYS AS IDENTITY in the target: it is not updated")
    p["comparable"] = True
    p["_scols"] = [scols[n] for n in p["columns"]]
    p["_tcols"] = [tcols[n] for n in p["columns"]]
    _plan_key_matching(p, tcat, tcols)
    _plan_self_references(p, tcat)
    return p


def key_collation(cat: Catalog, table: str, column: str) -> tuple[bool, bool, bool]:
    """Whether the server treats values of a text column as equal when they differ only in case, in
    accents, or in trailing spaces - asked from the server itself (``CAST(... AS TYPE OF COLUMN)``
    keeps the column's collation), so every collation, built-in or ICU, is covered."""
    t = f"TYPE OF COLUMN {quote_ident(table)}.{quote_ident(column)}"

    def equal(a: str, b: str) -> bool:
        try:
            r = cat.one(f"SELECT IIF(CAST({a} AS {t}) = CAST({b} AS {t}), 1, 0) AS eq FROM RDB$DATABASE",
                        scoped=False)
            return bool(r and r["eq"])
        except Exception:  # noqa: BLE001 - e.g. the column's character set has no such character
            return False

    return equal("'a'", "'A'"), equal("'e'", "_UTF8 'é'"), equal("'a'", "'a '")


def key_normalizer(ci: bool, ai: bool, pad: bool) -> Callable[[Any], Any]:
    """A function that makes text keys equal exactly when such a collation finds them equal."""
    def norm(v: Any) -> Any:
        if not isinstance(v, str):
            return v
        if pad:
            v = v.rstrip(" ")
        if ai:
            v = unicodedata.normalize("NFC", "".join(ch for ch in unicodedata.normalize("NFD", v)
                                                     if not unicodedata.combining(ch)))
        if ci:
            v = v.casefold()
        return v

    return norm


def _plan_key_matching(p: dict, tcat: Catalog, tcols: dict[str, Column]) -> None:
    """Match keys like the target's collation does (see the module doc)."""
    p["_norm"] = [None] * len(p["key"])
    p["key_matching"] = []
    for i, k in enumerate(p["key"]):
        if not tcols[k].text:
            continue
        ci, ai, pad = key_collation(tcat, p["name"], k)
        if ci or ai or pad:
            p["_norm"][i] = key_normalizer(ci, ai, pad)
        if ci or ai:
            what = "case and accents" if ci and ai else "case" if ci else "accents"
            p["key_matching"].append({"column": k, "case": ci, "accents": ai})
            p["warnings"].append(f"Keys are matched ignoring {what}, like the target's collation of {k}")


def _plan_self_references(p: dict, tcat: Catalog) -> None:
    """Foreign keys of the table to itself: INSERTs are ordered parents first, DELETEs children first."""
    p["_self_fks"] = []
    p["self_references"] = []
    idx = {n: i for i, n in enumerate(p["columns"])}
    for c in tcat.constraints(p["name"]):
        if c["type"] != "FOREIGN KEY" or c.get("ref_table") != p["name"]:
            continue
        cols, refs = c["columns"], c.get("ref_columns") or []
        if cols and len(cols) == len(refs) and all(x in idx for x in cols + refs):
            p["_self_fks"].append(([idx[x] for x in cols], [idx[x] for x in refs]))
            p["self_references"].append({"name": c["name"], "columns": cols, "ref_columns": refs})
        else:
            p["warnings"].append(f"The table references itself ({c['name']}), but not all columns of that "
                                 "foreign key are compared: rows are not ordered by it")


def plan_tables(scat: Catalog, tcat: Catalog, ignore: set[str] | None = None) -> list[dict]:
    st, tt = user_tables(scat), user_tables(tcat)
    return [plan_table(n, scat if n in st else None, tcat if n in tt else None, st.get(n), tt.get(n), ignore)
            for n in sorted(set(st) | set(tt))]


def public_plan(p: dict) -> dict:
    return {k: v for k, v in p.items() if not k.startswith("_")}


def fk_parents(cat: Catalog) -> dict[str, set[str]]:
    """child table -> parent tables, from RDB$REF_CONSTRAINTS."""
    out: dict[str, set[str]] = {}
    for r in cat.rows(
        "SELECT TRIM(ch.RDB$RELATION_NAME) AS child, TRIM(pa.RDB$RELATION_NAME) AS parent "
        "FROM RDB$REF_CONSTRAINTS fk "
        "JOIN RDB$RELATION_CONSTRAINTS ch ON ch.RDB$CONSTRAINT_NAME = fk.RDB$CONSTRAINT_NAME "
        "JOIN RDB$RELATION_CONSTRAINTS pa ON pa.RDB$CONSTRAINT_NAME = fk.RDB$CONST_NAME_UQ"
    ):
        if r["child"] != r["parent"]:
            out.setdefault(r["child"], set()).add(r["parent"])
    return out


def fk_order(names: list[str], *deps: dict[str, set[str]]) -> list[str]:
    """``names`` with parents before children (cycles broken alphabetically)."""
    merged: dict[str, set[str]] = {}
    for d in deps:
        for child, parents in d.items():
            merged.setdefault(child, set()).update(parents)
    return topo_sort(sorted(names), merged)


# ------------------------------------------------------------------ statement files
PARAM_MIN = 1024  # BLOB values at least this long are parameters when the script is applied


@dataclass
class Stmt:
    """A statement as SQL fragments around values that are best sent as parameters (long BLOBs).

    The downloadable script renders every value as a literal (``sql``); the Apply job sends long
    BLOBs as parameters instead (``param_sql`` / ``params``), because Firebird concatenates the
    chunks of a long literal in quadratic time.
    """

    frags: list[str]
    values: list[tuple[Any, str]]  # (value, literal kind)

    @property
    def sql(self) -> str:
        out = [self.frags[0]]
        for (v, kind), frag in zip(self.values, self.frags[1:], strict=True):
            out += [sql_literal(v, kind), frag]
        return "".join(out)

    @property
    def param_sql(self) -> str:
        return "?".join(self.frags)

    @property
    def params(self) -> list:
        return [v for v, _ in self.values]


Piece = str | tuple[Any, str]


def make_stmt(pieces: list[Piece]) -> Stmt:
    """SQL text pieces and (value, kind) pairs -> Stmt (short values become literals right away)."""
    frags, values = [""], []
    for piece in pieces:
        if isinstance(piece, str):
            frags[-1] += piece
            continue
        v, kind = piece
        if kind.startswith("blob") and isinstance(v, (str, bytes)) and len(v) >= PARAM_MIN:
            values.append((v, kind))
            frags.append("")
        else:
            frags[-1] += sql_literal(v, kind)
    return Stmt(frags, values)


class StmtFile:
    """Statements (``Stmt``) as length-prefixed pickled records on disk."""

    def __init__(self, path: Path, count: int = 0, nbytes: int = 0):
        self.path = path
        self._f = None
        self._pos = 0
        self.count = count
        self.bytes = nbytes  # approximate size in the script

    def add(self, *pieces: Piece) -> int:
        """Append a statement; returns its position (for ``reorder``)."""
        st = make_stmt(list(pieces))
        if self._f is None:
            self._f = open(self.path, "wb")  # noqa: SIM115 - closed in close()
            self._pos = 0
        b = pickle.dumps((st.frags, st.values), protocol=pickle.HIGHEST_PROTOCOL)
        pos = self._pos
        self._f.write(struct.pack(">I", len(b)))
        self._f.write(b)
        self._pos += 4 + len(b)
        self.count += 1
        self.bytes += sum(len(f) for f in st.frags) + sum(2 * len(v) + 40 for v, _ in st.values) + 2
        return pos

    def reset(self) -> None:
        if self._f is not None:
            self._f.seek(0)
            self._f.truncate()
        self._pos = 0
        self.count = self.bytes = 0

    def reorder(self, positions: list[int]) -> None:
        """Rewrite the file with the statements at ``positions`` (from ``add``) in that order."""
        self.close()
        tmp = self.path.with_suffix(".tmp")
        with open(self.path, "rb") as src, open(tmp, "wb") as dst:
            for pos in positions:
                src.seek(pos)
                head = src.read(4)
                (n,) = struct.unpack(">I", head)
                dst.write(head)
                dst.write(src.read(n))
        os.replace(tmp, self.path)

    def close(self) -> None:
        if self._f is not None:
            self._f.close()
            self._f = None

    def __iter__(self) -> Iterator[Stmt]:
        if not self.count:
            return
        with open(self.path, "rb") as f:
            while head := f.read(4):
                (n,) = struct.unpack(">I", head)
                frags, values = pickle.loads(f.read(n))
                yield Stmt(frags, values)


# ------------------------------------------------------------------ runs (results + scripts)
KEEP_RUNS = 20  # finished comparisons kept per user (with their scripts)
_spill_dir: Path | None = None
_dir_lock = threading.Lock()


def runs_dir() -> Path:
    """Where comparisons are kept: ``<data dir>/datacompare`` (mode 0700: rows of user tables)."""
    d = Path(settings.data_dir) / "datacompare"
    d.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(d, 0o700)
    except OSError:
        pass
    return d


def _spill_base() -> Path:
    """Temporary files of the external sort (deleted at exit)."""
    global _spill_dir
    with _dir_lock:
        if _spill_dir is None:
            _spill_dir = Path(tempfile.mkdtemp(prefix="fbadmin-datacompare-"))
            atexit.register(shutil.rmtree, _spill_dir, True)
        return _spill_dir


def _owner() -> int | None:
    """The fbadmin user (server mode) a comparison belongs to; None in desktop mode."""
    from ..security.sessions import current_principal

    p = current_principal.get()
    return p.user_id if p is not None else None


@dataclass
class Run:
    """One comparison: result, sample rows and the statement files of the sync script."""

    source: tuple[str, str]
    target: tuple[str, str]
    source_name: str = ""
    target_name: str = ""
    result: dict = field(default_factory=dict)
    samples: dict[str, dict] = field(default_factory=dict)
    segments: list[tuple[str, str, StmtFile]] = field(default_factory=list)  # (table, kind, file)
    dir: Path = field(default_factory=lambda: Path(tempfile.mkdtemp(prefix="run_", dir=runs_dir())))
    id: str = ""
    status: str = "running"  # running | finished | failed | cancelled
    error: str | None = None
    log: list[str] = field(default_factory=list)
    owner: int | None = field(default_factory=_owner)
    finished: bool = False
    applying: bool = False
    applied: bool = False
    created: float = field(default_factory=time.time)
    finished_at: float | None = None

    def statement_count(self) -> int:
        return sum(f.count for _, _, f in self.segments)

    def statements(self) -> Iterator[tuple[str, str, Stmt]]:
        for table, kind, f in self.segments:
            for st in f:
                yield table, kind, st

    def script_chunks(self) -> Iterator[bytes]:
        def clean(s: str) -> str:
            return s.replace("*/", "* /").replace("\n", " ")

        counts = {k: sum(f.count for _, kk, f in self.segments if kk == k) for k in ("INSERT", "UPDATE", "DELETE")}
        stamp = dt.datetime.fromtimestamp(self.created).strftime("%Y-%m-%d %H:%M:%S")
        yield (
            "/*\n"
            f" * Data sync script generated by fbadmin on {stamp}\n"
            f" * Source: {clean(self.source_name)}\n"
            f" * Target: {clean(self.target_name)}\n"
            " * Makes the rows of the compared tables in the target equal to the source.\n"
            " * Run it connected to the TARGET database, with the UTF8 connection character set\n"
            " * (isql -ch UTF8). Order: INSERTs (parents first), UPDATEs, DELETEs (children first).\n"
            f" * Statements: {self.statement_count()} (INSERT {counts['INSERT']}, UPDATE {counts['UPDATE']}, "
            f"DELETE {counts['DELETE']})\n"
            " */\n"
        ).encode()
        for table, kind, f in self.segments:
            if not f.count:
                continue
            yield f"\n-- {clean(table)}: {f.count} {kind} statement(s)\n".encode()
            buf: list[str] = []
            size = 0
            for st in f:
                sql = st.sql
                buf.append(sql + ";\n")
                size += len(sql)
                if size > 1 << 16:
                    yield "".join(buf).encode("utf-8")
                    buf, size = [], 0
            if buf:
                yield "".join(buf).encode("utf-8")
        yield b"\nCOMMIT;\n"

    def close_files(self) -> None:
        for _, _, f in self.segments:
            f.close()

    def cleanup(self) -> None:
        self.close_files()
        shutil.rmtree(self.dir, ignore_errors=True)

    # --- persistence
    def summary(self) -> dict:
        return {"job_id": self.id, "status": self.status, "error": self.error, "created": self.created,
                "finished_at": self.finished_at, "source": list(self.source), "target": list(self.target),
                "source_name": self.source_name, "target_name": self.target_name,
                "totals": self.result.get("totals") or {}, "statements": self.result.get("statements", 0),
                "tables": len(self.result.get("tables") or []), "applied": self.applied}

    def save(self) -> None:
        data = {
            "version": 1, "id": self.id, "source": list(self.source), "target": list(self.target),
            "source_name": self.source_name, "target_name": self.target_name, "status": self.status,
            "error": self.error, "owner": self.owner, "created": self.created, "finished_at": self.finished_at,
            "applied": self.applied, "result": self.result, "samples": self.samples, "log": self.log[-1000:],
            "segments": [[t, k, f.path.name, f.count, f.bytes] for t, k, f in self.segments],
        }
        tmp = self.dir / f"run.json.{threading.get_ident()}.tmp"  # the job and the API may save at once
        tmp.write_text(json.dumps(to_json(data)))
        os.replace(tmp, self.dir / "run.json")

    @classmethod
    def load(cls, d: Path) -> Run:
        data = json.loads((d / "run.json").read_text())
        run = cls(source=tuple(data["source"]), target=tuple(data["target"]), source_name=data["source_name"],
                  target_name=data["target_name"], result=data["result"], samples=data["samples"], dir=d,
                  id=data["id"], status=data["status"], error=data.get("error"), log=data.get("log") or [],
                  owner=data.get("owner"), applied=data.get("applied", False), created=data["created"],
                  finished_at=data.get("finished_at"))
        run.segments = [(t, k, StmtFile(d / name, count, nbytes)) for t, k, name, count, nbytes in data["segments"]]
        run.finished = True
        if run.status == "running":  # fbadmin stopped during the comparison
            run.status, run.error = "failed", "fbadmin was stopped before the comparison finished"
            for _, _, f in run.segments:
                f.reset()
                f.path.unlink(missing_ok=True)
            run.save()
        return run


class Runs:
    """Comparisons by job id, kept on disk (``runs_dir``). In server mode each user sees only their own."""

    def __init__(self, keep: int = KEEP_RUNS):
        self.keep = keep
        self._runs: OrderedDict[str, Run] = OrderedDict()
        self._lock = threading.Lock()
        self._loaded_from: Path | None = None

    def _load(self) -> None:
        """(Re)load the runs of the current data directory (once per directory)."""
        d = Path(settings.data_dir) / "datacompare"
        if self._loaded_from == d:
            return
        self._runs.clear()
        self._loaded_from = d
        if not d.is_dir():
            return
        loaded = []
        for sub in d.iterdir():
            if (sub / "run.json").is_file():
                try:
                    loaded.append(Run.load(sub))
                except Exception:  # noqa: BLE001 - a damaged run is skipped, not fatal
                    log_.warning("could not load the data comparison in %s", sub, exc_info=True)
        for r in sorted(loaded, key=lambda r: r.created):
            self._runs[r.id] = r

    def _visible(self, r: Run) -> bool:
        return r.owner == _owner()

    def add(self, run_id: str, run: Run) -> None:
        with self._lock:
            self._load()
            run.id = run_id
            self._runs[run_id] = run
            mine = [k for k, r in self._runs.items() if r.owner == run.owner and r.finished and not r.applying]
            for k in mine[: max(0, len([r for r in self._runs.values() if r.owner == run.owner]) - self.keep)]:
                self._runs.pop(k).cleanup()
        run.save()

    def get(self, run_id: str) -> Run:
        with self._lock:
            self._load()
            r = self._runs.get(run_id)
            if r is None or not self._visible(r):
                raise KeyError(f"Unknown data comparison {run_id!r} (it may have expired)")
            return r

    def list(self) -> list[Run]:
        with self._lock:
            self._load()
            return [r for r in reversed(self._runs.values()) if self._visible(r)]

    def delete(self, run_id: str) -> None:
        r = self.get(run_id)
        if not r.finished or r.applying:
            raise ValueError("The comparison is still running")
        with self._lock:
            self._runs.pop(run_id, None)
        r.cleanup()


runs = Runs()


# ------------------------------------------------------------------ comparison
def _is_nan(v: Any) -> bool:
    return (isinstance(v, float) and math.isnan(v)) or (isinstance(v, decimal.Decimal) and v.is_nan())


def same(a: Any, b: Any) -> bool:
    if a is None or b is None:
        return a is None and b is None
    if isinstance(a, bool) != isinstance(b, bool):
        return False
    try:
        if a == b:
            return True
    except TypeError:
        return False
    return _is_nan(a) and _is_nan(b)


def preview(v: Any) -> Any:
    """A value for the browser (long text and binary values shortened)."""
    if isinstance(v, str) and len(v) > PREVIEW_CHARS:
        return v[:PREVIEW_CHARS] + f"… ({len(v)} characters)"
    if isinstance(v, (bytes, bytearray, memoryview)):
        b = bytes(v)
        if len(b) > 32:
            return "0x" + b[:32].hex().upper() + f"… ({len(b)} bytes)"
    return to_json(v)


def _checked(rows: Iterator[tuple[tuple, list]], side: str, nk: int = 0,
             normalized: bool = False) -> Iterator[tuple[tuple, list]]:
    """Pass rows through, verifying that keys strictly increase."""
    prev = prev_vals = None
    for key, vals in rows:
        if prev is not None:
            try:
                ok = prev < key
                dup = not ok and prev == key
            except TypeError:
                raise TableError(f"Key values in the {side} database cannot be ordered") from None
            if dup and normalized and prev_vals is not None and prev_vals[:nk] != vals[:nk]:
                raise TableError(
                    f"The keys {to_json(prev_vals[:nk])} and {to_json(vals[:nk])} are different in the {side} "
                    "database but equal in the target, whose collation ignores case, accents or trailing "
                    "spaces: these rows cannot be synchronized")
            if dup:
                raise TableError(f"Duplicate key {to_json(list(key))} in the {side} database")
            if not ok:
                raise OutOfOrder(side)
        prev, prev_vals = key, vals
        yield key, vals


def _spill(rows: list) -> Any:
    f = tempfile.TemporaryFile(dir=_spill_base())  # noqa: SIM115 - closed by _read_spilled
    p = pickle.Pickler(f, protocol=pickle.HIGHEST_PROTOCOL)
    for r in rows:
        p.dump(r)
    f.seek(0)
    return f


def _read_spilled(f) -> Iterator:
    with f:
        u = pickle.Unpickler(f)
        while True:
            try:
                yield u.load()
            except EOFError:
                return


def external_sort(rows: Iterator[tuple[tuple, list]], run_rows: int = SORT_RUN_ROWS) -> Iterator[tuple[tuple, list]]:
    """Sort (key, values) rows by key with bounded memory."""
    key = itemgetter(0)
    files, buf = [], []
    try:
        for r in rows:
            buf.append(r)
            if len(buf) >= run_rows:
                buf.sort(key=key)
                files.append(_spill(buf))
                buf = []
        buf.sort(key=key)
    except TypeError:
        raise TableError("Key values cannot be ordered") from None
    if not files:
        yield from buf
        return
    if buf:
        files.append(_spill(buf))
    yield from heapq.merge(*(_read_spilled(f) for f in files), key=key)


def parents_first(rows: list[tuple]) -> tuple[list[int] | None, int]:
    """Order rows so that a row comes after the rows it references; rows are ``_refs`` tuples
    ((own key, parent key) per foreign key). Returns (order, rows in cycles), or (None, 0) when no
    row references another row of the list. Otherwise rows keep their order where possible."""
    owners: list[dict[tuple, int]] = [{} for _ in (rows[0] if rows else ())]
    for i, r in enumerate(rows):
        for f, (own, _parent) in enumerate(r):
            owners[f].setdefault(own, i)
    parents: list[list[int]] = []
    any_edge = False
    for i, r in enumerate(rows):
        ps = []
        for f, (_own, parent) in enumerate(r):
            if any(v is None for v in parent):
                continue
            j = owners[f].get(parent)
            if j is not None and j != i:
                ps.append(j)
        parents.append(ps)
        any_edge = any_edge or bool(ps)
    if not any_edge:
        return None, 0
    state = [0] * len(rows)  # 0 new, 1 on the stack, 2 placed
    order: list[int] = []
    cycles = 0
    for start in range(len(rows)):
        if state[start]:
            continue
        stack = [(start, 0)]
        state[start] = 1
        while stack:
            node, k = stack[-1]
            if k < len(parents[node]):
                stack[-1] = (node, k + 1)
                p = parents[node][k]
                if state[p] == 0:
                    state[p] = 1
                    stack.append((p, 0))
                elif state[p] == 1:
                    cycles += 1  # p is an ancestor of node: a cycle (placed in visiting order)
                continue
            stack.pop()
            state[node] = 2
            order.append(node)
    return order, cycles


class TableCompare:
    """Compare one planned table and write its statements."""

    def __init__(self, plan: dict, src, dst, log: Callable[[str], None], *, stats: dict, sample: dict,
                 files: dict[str, StmtFile], where: str | None, directions: set[str], max_samples: int,
                 target_major: int):
        self.p, self.src, self.dst, self.log = plan, src, dst, log
        self.stats, self.sample, self.files = stats, sample, files
        self.where = (where or "").strip().rstrip(";").strip() or None
        self.directions, self.max_samples = directions, max_samples
        self.nk = len(plan["key"])
        self.names = plan["columns"]
        self.tkinds = [c.kind for c in plan["_tcols"]]
        qt = quote_ident(plan["name"])
        self.qt = qt
        overriding = target_major >= 4 and any(c.identity == "ALWAYS" for c in plan["_tcols"])
        self.ins_head = (f"INSERT INTO {qt} ({', '.join(quote_ident(n) for n in self.names)})"
                         + (" OVERRIDING SYSTEM VALUE" if overriding else "") + " VALUES (")
        self.no_update = {i for i, c in enumerate(plan["_tcols"]) if i >= self.nk and c.identity == "ALWAYS"}
        self.qnames = [quote_ident(n) for n in self.names]
        self.norms = plan.get("_norm") or [None] * self.nk
        self.normalized = any(self.norms)
        self.self_fks: list[tuple[list[int], list[int]]] = plan.get("_self_fks") or []
        # self-referencing rows: (statement position, row) of INSERTs and DELETEs, ordered at the end
        self.ins_rows: list[tuple[int, tuple]] = []
        self.del_rows: list[tuple[int, tuple]] = []

    # --- reading
    def _sql(self, cols: list[Column], ordered: bool) -> str:
        sql = f"SELECT {', '.join(c.select for c in cols)} FROM {self.qt}"
        if self.where:
            sql += f" WHERE ({self.where})"
        if ordered:
            sql += " ORDER BY " + ", ".join(self.qnames[: self.nk])
        return sql

    def _stream(self, conn, cols: list[Column], side: str, ordered: bool) -> Iterator[tuple[tuple, list]]:
        cur = conn.cursor()
        try:
            cur.execute(self._sql(cols, ordered))
            convs = [(i, c.conv) for i, c in enumerate(cols) if c.conv]
            nk = self.nk
            counter = f"rows_{side}"
            norms = list(enumerate(self.norms)) if self.normalized else []
            while True:
                jobs.check()
                rows = cur.fetchmany(FETCH)
                if not rows:
                    break
                for r in rows:
                    r = list(r)
                    for i, f in convs:
                        if r[i] is not None:
                            r[i] = f(r[i])
                    key = tuple(r[:nk])
                    if norms:
                        key = tuple(f(key[i]) if f is not None and key[i] is not None else key[i] for i, f in norms)
                    self.stats[counter] += 1
                    n = self.stats[counter]
                    if n % PROGRESS_EVERY == 0:
                        self.log(f"{self.p['name']}: {n:,} {side} rows read")
                    if any(k is None for k in key):
                        self.stats["null_keys"] += 1
                        continue
                    yield key, r
        finally:
            try:
                cur.close()
            except Exception:  # noqa: BLE001
                pass

    # --- writing
    def _where(self, row: list) -> str:
        return " AND ".join(f"{self.qnames[i]} = {sql_literal(row[i], self.tkinds[i])}" for i in range(self.nk))

    def _values(self, vals: list, idx: list[int]) -> list[Piece]:
        """``a, b, c`` as pieces: (value, kind) pairs separated by commas."""
        out: list[Piece] = []
        for n, i in enumerate(idx):
            if n:
                out.append(", ")
            out.append((vals[i], self.tkinds[i]))
        return out

    def _keep(self, kind: str, s: list | None, t: list | None, changed: list[str] | None = None) -> None:
        bucket = self.sample.setdefault(kind, [])
        if len(bucket) < self.max_samples:
            bucket.append({"source": [preview(v) for v in s] if s is not None else None,
                           "target": [preview(v) for v in t] if t is not None else None,
                           "changed": changed or []})

    def _refs(self, row: list) -> tuple:
        """What ordering by the self-references needs of a row: (own key, parent key) per foreign key."""
        return tuple((tuple(row[i] for i in refs), tuple(row[i] for i in cols)) for cols, refs in self.self_fks)

    def on_insert(self, s: list) -> None:
        self.stats["only_source"] += 1
        self._keep("insert", s, None)
        if "insert" in self.directions:
            pos = self.files["INSERT"].add(self.ins_head, *self._values(s, list(range(len(s)))), ")")
            if self.self_fks:
                self.ins_rows.append((pos, self._refs(s)))

    def on_delete(self, t: list) -> None:
        self.stats["only_target"] += 1
        self._keep("delete", None, t)
        if "delete" in self.directions:
            pos = self.files["DELETE"].add(f"DELETE FROM {self.qt} WHERE {self._where(t)}")
            if self.self_fks:
                self.del_rows.append((pos, self._refs(t)))

    def on_both(self, s: list, t: list) -> None:
        # with collation-aware keys, the key itself may differ (e.g. only in case): update it too
        first = 0 if self.normalized else self.nk
        changed = [i for i in range(first, len(s)) if not same(s[i], t[i])]
        if not changed:
            self.stats["identical"] += 1
            return
        self.stats["different"] += 1
        cc = self.stats["changed_columns"]
        for i in changed:
            cc[self.names[i]] = cc.get(self.names[i], 0) + 1
        self._keep("update", s, t, [self.names[i] for i in changed])
        sets = [i for i in changed if i not in self.no_update]
        if "update" in self.directions and sets:
            pieces: list[Piece] = [f"UPDATE {self.qt} SET "]
            for n, i in enumerate(sets):
                pieces += [", " if n else "", f"{self.qnames[i]} = ", (s[i], self.tkinds[i])]
            self.files["UPDATE"].add(*pieces, f" WHERE {self._where(t)}")

    # --- merge
    @staticmethod
    def _less(a: tuple, b: tuple) -> bool:
        try:
            return a < b
        except TypeError:
            raise TableError("The key columns have incompatible types in the two databases") from None

    def _merge(self, srows: Iterator, trows: Iterator) -> None:
        s, t = next(srows, None), next(trows, None)
        while s is not None or t is not None:
            if t is None or (s is not None and self._less(s[0], t[0])):
                self.on_insert(s[1])
                s = next(srows, None)
            elif s is None or self._less(t[0], s[0]):
                self.on_delete(t[1])
                t = next(trows, None)
            else:
                self.on_both(s[1], t[1])
                s, t = next(srows, None), next(trows, None)

    def _attempt(self, ordered: bool) -> None:
        scols, tcols = self.p["_scols"], self.p["_tcols"]
        sgen = self._stream(self.src, scols, "source", ordered)
        tgen = self._stream(self.dst, tcols, "target", ordered)
        nk, nz = self.nk, self.normalized
        try:
            if ordered:
                self._merge(_checked(sgen, "source", nk, nz), _checked(tgen, "target", nk, nz))
            else:
                self._merge(_checked(external_sort(sgen), "source", nk, nz),
                            _checked(external_sort(tgen), "target", nk, nz))
        finally:
            sgen.close()  # closes the cursors of an abandoned attempt
            tgen.close()

    def run(self) -> None:
        try:
            self._attempt(ordered=True)
            self.stats["ordering"] = "server"
        except OutOfOrder as e:
            side = e.args[0]
            self.log(f"{self.p['name']}: the {side} database returned keys in a different order than "
                     "Python compares them (collation); comparing again with the rows sorted in fbadmin")
            self._reset()
            self._attempt(ordered=False)
            self.stats["ordering"] = "python"
        self._order_self_references()

    def _order_self_references(self) -> None:
        """Rows of a table that references itself: INSERT parents before their children, DELETE
        children before their parents (a cascading delete would otherwise remove a child first
        and its own DELETE would change no row)."""
        for kind, rows, reverse in (("INSERT", self.ins_rows, False), ("DELETE", self.del_rows, True)):
            if len(rows) < 2:
                continue
            order, cycles = parents_first([r for _, r in rows])
            if order is None:
                continue  # no row references another one in this set
            if reverse:
                order.reverse()
            self.files[kind].reorder([rows[i][0] for i in order])
            if cycles:
                self.stats["warnings"].append(
                    f"{cycles} row(s) to {kind.lower()} reference each other in a cycle: their {kind} statements "
                    "cannot be ordered and may fail")

    def _reset(self) -> None:
        self.ins_rows.clear()
        self.del_rows.clear()
        for f in self.files.values():
            f.reset()
        for k in ("rows_source", "rows_target", "only_source", "only_target", "different", "identical", "null_keys"):
            self.stats[k] = 0
        self.stats["changed_columns"] = {}
        self.sample.clear()


def new_stats(plan: dict) -> dict:
    return {
        "name": plan["name"], "key": plan["key"], "key_type": plan["key_type"], "status": "pending",
        "error": None, "warnings": list(plan["warnings"]), "rows_source": 0, "rows_target": 0,
        "only_source": 0, "only_target": 0, "different": 0, "identical": 0, "null_keys": 0,
        "changed_columns": {}, "ordering": None, "columns": plan["columns"],
        "source_only": plan["source_only"], "target_only": plan["target_only"],
        "type_mismatch": plan["type_mismatch"], "ignored": plan["ignored"],
    }


def compare_tables(src, dst, log: Callable[[str], None], run: Run, *, tables: list[dict],
                   ignore: set[str] | None = None, directions: set[str] | None = None,
                   max_samples: int = 100) -> dict:
    """Compare ``tables`` ([{"name", "where"?, "ignore_columns"?}]) and fill ``run``."""
    directions = directions if directions is not None else {"insert", "update", "delete"}
    snapshot_readonly(src)
    snapshot_readonly(dst)
    scat, tcat = open_catalog(src), open_catalog(dst)
    st, tt = user_tables(scat), user_tables(tcat)
    res = run.result
    res.update(tables=[], skipped=[], totals={}, statements=0, script_bytes=0)
    plans: dict[str, dict] = {}
    opts: dict[str, dict] = {}
    for t in tables:
        name = t["name"]
        ign = set(ignore or ()) | {f"{name}.{c}".upper() for c in t.get("ignore_columns") or ()}
        p = plan_table(name, scat if name in st else None, tcat if name in tt else None, st.get(name), tt.get(name), ign)
        if not p["comparable"]:
            res["skipped"].append({"name": name, "reason": p["reason"]})
            log(f"{name}: skipped - {p['reason']}")
            continue
        plans[name], opts[name] = p, t
    order = fk_order(list(plans), fk_parents(scat), fk_parents(tcat))
    files: dict[str, dict[str, StmtFile]] = {}
    for i, name in enumerate(order):
        files[name] = {k: StmtFile(run.dir / f"{i}_{k.lower()}.bin") for k in ("INSERT", "UPDATE", "DELETE")}
    run.segments = ([(n, "INSERT", files[n]["INSERT"]) for n in order]
                    + [(n, "UPDATE", files[n]["UPDATE"]) for n in order]
                    + [(n, "DELETE", files[n]["DELETE"]) for n in reversed(order)])
    stats_by_name = {n: new_stats(plans[n]) for n in order}
    res["tables"] = [stats_by_name[n] for n in order]
    major = tcat.engine_major
    for name in order:
        if jobs.cancelling():
            for s in res["tables"]:
                if s["status"] in ("pending", "running"):
                    s["status"] = "cancelled"
            raise jobs.Cancelled()
        p, stats = plans[name], stats_by_name[name]
        stats["status"] = "running"
        log(f"{name}: comparing by {p['key_type'].lower()} ({', '.join(p['key'])}), "
            f"{len(p['columns']) - len(p['key'])} value column(s)")
        started = time.time()
        sample = run.samples.setdefault(name, {})
        tc = TableCompare(p, src, dst, log, stats=stats, sample=sample, files=files[name],
                          where=opts[name].get("where"), directions=directions, max_samples=max_samples,
                          target_major=major)
        try:
            tc.run()
            stats["status"] = "done"
        except TableError as e:
            stats["status"], stats["error"] = "error", str(e)
            for f in files[name].values():
                f.reset()
            log(f"{name}: {e}")
            continue
        except Exception as e:
            if jobs.cancelling():  # a cancelled statement, or the check in the read loop
                for s in res["tables"]:
                    if s["status"] in ("pending", "running"):
                        s["status"] = "cancelled"
                for f in files[name].values():
                    f.reset()
                raise jobs.Cancelled() from None
            stats["status"], stats["error"] = "error", str(e).strip()
            for f in files[name].values():
                f.reset()
            log(f"{name}: failed - {str(e).strip().splitlines()[0] if str(e).strip() else type(e).__name__}")
            # a failed statement (e.g. a bad WHERE filter) ends the transaction's usefulness on Firebird 3
            for c in (src, dst):
                try:
                    c.rollback()
                except Exception:  # noqa: BLE001
                    pass
            continue
        finally:
            for f in files[name].values():
                f.close()
            res["statements"] = run.statement_count()
            res["script_bytes"] = sum(f.bytes for _, _, f in run.segments)
        log(f"{name}: {stats['only_source']} only in source, {stats['only_target']} only in target, "
            f"{stats['different']} different, {stats['identical']} identical ({time.time() - started:.1f}s)")
    tot = {"only_source": 0, "only_target": 0, "different": 0, "identical": 0}
    for s in res["tables"]:
        for k in tot:
            tot[k] += s[k]
    res["totals"] = tot
    res["statements"] = run.statement_count()
    res["script_bytes"] = sum(f.bytes for _, _, f in run.segments)
    errors = sum(1 for s in res["tables"] if s["status"] == "error")
    log(f"Done: {tot['only_source']} to insert, {tot['different']} to update, {tot['only_target']} to delete, "
        f"{tot['identical']} identical; {res['statements']} statement(s) in the sync script"
        + (f"; {errors} table(s) failed" if errors else ""))
    for c in (src, dst):
        try:
            c.commit()
        except Exception:  # noqa: BLE001
            pass
    return res


def apply_script(run: Run, conn, log: Callable[[str], None]) -> int:
    """Execute the sync script on the target in one transaction; roll back on any error."""
    cur = conn.cursor()
    n = 0
    total = run.statement_count()
    log(f"Applying {total} statement(s) to the target in one transaction")
    try:
        for table, kind, st in run.statements():
            jobs.check()
            sql = st.param_sql
            try:
                cur.execute(sql, st.params)
            except Exception as e:
                raise RuntimeError(f"Statement {n + 1} ({kind} {table}) failed: {str(e).strip()}\n{sql[:500]}") from e
            if kind in ("UPDATE", "DELETE") and cur.rowcount == 0:
                raise RuntimeError(f"Statement {n + 1} ({kind} {table}) changed no row: the target was changed "
                                   f"after the comparison. Compare again.\n{sql[:500]}")
            n += 1
            if n % 1000 == 0:
                log(f"{n} / {total} statements")
        conn.commit()
    except Exception:
        try:
            conn.rollback()
        except Exception:  # noqa: BLE001
            pass
        log("Rolled back: the target is unchanged")
        raise
    log(f"Committed {n} statement(s)")
    return n
