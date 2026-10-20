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

Script
------
Statements are written to per-table record files (length-prefixed UTF-8) while comparing, so the
script never has to be held in memory. Order: INSERTs parents first, then UPDATEs parents first,
then DELETEs children first (by the foreign keys between the compared tables, from
``RDB$REF_CONSTRAINTS`` of both databases). Inserting before deleting lets updated child rows move
to new parent rows before old parents are deleted.
"""

from __future__ import annotations

import atexit
import datetime as dt
import decimal
import heapq
import math
import pickle
import shutil
import struct
import tempfile
import threading
import time
from collections import OrderedDict
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from operator import itemgetter
from pathlib import Path
from typing import Any

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
    return p


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

    def __init__(self, path: Path):
        self.path = path
        self._f = None
        self.count = 0
        self.bytes = 0  # approximate size in the script

    def add(self, *pieces: Piece) -> None:
        st = make_stmt(list(pieces))
        if self._f is None:
            self._f = open(self.path, "wb")  # noqa: SIM115 - closed in close()
        b = pickle.dumps((st.frags, st.values), protocol=pickle.HIGHEST_PROTOCOL)
        self._f.write(struct.pack(">I", len(b)))
        self._f.write(b)
        self.count += 1
        self.bytes += sum(len(f) for f in st.frags) + sum(2 * len(v) + 40 for v, _ in st.values) + 2

    def reset(self) -> None:
        if self._f is not None:
            self._f.seek(0)
            self._f.truncate()
        self.count = self.bytes = 0

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
_base_dir: Path | None = None
_base_lock = threading.Lock()


def _base() -> Path:
    global _base_dir
    with _base_lock:
        if _base_dir is None:
            _base_dir = Path(tempfile.mkdtemp(prefix="fbadmin-datacompare-"))
            atexit.register(shutil.rmtree, _base_dir, True)
        return _base_dir


@dataclass
class Run:
    """One comparison: live result, sample rows and the statement files of the sync script."""

    source: tuple[str, str]
    target: tuple[str, str]
    source_name: str = ""
    target_name: str = ""
    result: dict = field(default_factory=dict)
    samples: dict[str, dict] = field(default_factory=dict)
    segments: list[tuple[str, str, StmtFile]] = field(default_factory=list)  # (table, kind, file)
    dir: Path = field(default_factory=lambda: Path(tempfile.mkdtemp(dir=_base())))
    finished: bool = False
    applying: bool = False
    applied: bool = False
    created: float = field(default_factory=time.time)

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


class Runs:
    """Recent comparisons by job id (the oldest finished ones are dropped with their files)."""

    def __init__(self, keep: int = 20):
        self.keep = keep
        self._runs: OrderedDict[str, Run] = OrderedDict()
        self._lock = threading.Lock()

    def add(self, run_id: str, run: Run) -> None:
        with self._lock:
            self._runs[run_id] = run
            old = [k for k, r in self._runs.items() if r.finished and not r.applying]
            for k in old[: max(0, len(self._runs) - self.keep)]:
                self._runs.pop(k).cleanup()

    def get(self, run_id: str) -> Run:
        with self._lock:
            try:
                return self._runs[run_id]
            except KeyError:
                raise KeyError(f"Unknown data comparison {run_id!r} (it may have expired)") from None


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


def _checked(rows: Iterator[tuple[tuple, list]], side: str) -> Iterator[tuple[tuple, list]]:
    """Pass rows through, verifying that keys strictly increase."""
    prev = None
    for key, vals in rows:
        if prev is not None:
            try:
                ok = prev < key
                dup = not ok and prev == key
            except TypeError:
                raise TableError(f"Key values in the {side} database cannot be ordered") from None
            if dup:
                raise TableError(f"Duplicate key {to_json(list(key))} in the {side} database")
            if not ok:
                raise OutOfOrder(side)
        prev = key
        yield key, vals


def _spill(rows: list) -> Any:
    f = tempfile.TemporaryFile(dir=_base())  # noqa: SIM115 - closed by _read_spilled
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
            while True:
                rows = cur.fetchmany(FETCH)
                if not rows:
                    break
                for r in rows:
                    r = list(r)
                    for i, f in convs:
                        if r[i] is not None:
                            r[i] = f(r[i])
                    key = tuple(r[:nk])
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

    def on_insert(self, s: list) -> None:
        self.stats["only_source"] += 1
        self._keep("insert", s, None)
        if "insert" in self.directions:
            self.files["INSERT"].add(self.ins_head, *self._values(s, list(range(len(s)))), ")")

    def on_delete(self, t: list) -> None:
        self.stats["only_target"] += 1
        self._keep("delete", None, t)
        if "delete" in self.directions:
            self.files["DELETE"].add(f"DELETE FROM {self.qt} WHERE {self._where(t)}")

    def on_both(self, s: list, t: list) -> None:
        changed = [i for i in range(self.nk, len(s)) if not same(s[i], t[i])]
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
        try:
            if ordered:
                self._merge(_checked(sgen, "source"), _checked(tgen, "target"))
            else:
                self._merge(_checked(external_sort(sgen), "source"), _checked(external_sort(tgen), "target"))
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

    def _reset(self) -> None:
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
