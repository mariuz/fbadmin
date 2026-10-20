"""The files of a schema snapshot: one file per object in a stable layout, plus ``full.sql``.

Layout (relative to the repository folder)::

    tables/CUSTOMER.sql        views/V_TOTALS.sql      procedures/P_SUM.sql
    functions/F_X.sql          packages/PKG.sql        triggers/ORDERS_BI.sql
    domains/D_MONEY.sql        sequences/SEQ_ORDERS.sql exceptions/E_INVALID.sql
    indices/IX_ORDERS_AMOUNT.sql roles/R_READER.sql     collations/...
    grants.sql                 database.sql (database comment)
    full.sql                   the whole schema as one script in dependency order (like isql -x)
    .fbadmin/snapshot.json     the normalized snapshot, used to build migration scripts between commits

On Firebird 6 every schema is a top-level folder (``PUBLIC/tables/CUSTOMER.sql``) and ``full.sql``
covers all schemas. The output is deterministic: no timestamps or versions, objects sorted by name,
trailing whitespace removed, LF line endings and sequence values left out, so an unchanged database
produces byte-identical files.
"""

from __future__ import annotations

import json
import re
from collections import defaultdict
from typing import Any

from ..metadata.types import quote_ident
from ..schema.extract import build_sections, render
from ..schema.model import Snapshot

META_DIR = ".fbadmin"
SNAPSHOT_FILE = f"{META_DIR}/snapshot.json"
FORMAT = 1

# object type (as in Snapshot.objects()) -> (folder, singular label)
KINDS: dict[str, tuple[str, str]] = {
    "collations": ("collations", "collation"),
    "domains": ("domains", "domain"),
    "sequences": ("sequences", "sequence"),
    "exceptions": ("exceptions", "exception"),
    "roles": ("roles", "role"),
    "functions": ("functions", "function"),
    "tables": ("tables", "table"),
    "indices": ("indices", "index"),
    "views": ("views", "view"),
    "procedures": ("procedures", "procedure"),
    "packages": ("packages", "package"),
    "triggers": ("triggers", "trigger"),
}
_FOLDER_KIND = {folder: kind for kind, (folder, _l) in KINDS.items()}
_SAFE = re.compile(r"[A-Z0-9_$]")


def file_name(name: str) -> str:
    """Object name -> file name: upper-case letters, digits, _ and $ stay; anything else (lower case,
    spaces, quotes, dots, slashes) becomes %XX, so names never collide on case-insensitive file
    systems and can never escape their folder."""
    out = []
    for ch in name:
        if _SAFE.fullmatch(ch):
            out.append(ch)
        else:
            out.extend(f"%{b:02X}" for b in ch.encode("utf-8"))
    return "".join(out) or "%00"


def object_name(fname: str) -> str:
    """Inverse of :func:`file_name` (a trailing ``.sql`` is ignored)."""
    base = fname[:-4] if fname.endswith(".sql") else fname
    raw = bytearray()
    i = 0
    while i < len(base):
        if base[i] == "%" and re.fullmatch(r"[0-9A-F]{2}", base[i + 1:i + 3]):
            raw.append(int(base[i + 1:i + 3], 16))
            i += 3
        else:
            raw.extend(base[i].encode("utf-8"))
            i += 1
    return raw.decode("utf-8", "replace")


def normalize(text: str) -> str:
    lines = [ln.rstrip() for ln in text.replace("\r\n", "\n").replace("\r", "\n").split("\n")]
    while lines and not lines[-1]:
        lines.pop()
    return "\n".join(lines) + "\n"


# ------------------------------------------------------------------ snapshot <-> JSON
def snapshot_data(snap: Snapshot) -> dict[str, Any]:
    """The snapshot's state as plain, sorted JSON data (no live connection; sequence values dropped)."""
    d = {k: v for k, v in vars(snap).items() if k != "cat"}
    d["sequences"] = {n: {**o, "value_ddl": None} for n, o in d["sequences"].items()}
    d["views"] = {n: {**v, "view_deps": sorted(v["view_deps"])} for n, v in d["views"].items()}
    d["deps"] = sorted(d["deps"], key=lambda r: json.dumps(r, sort_keys=True, default=str))
    return json.loads(json.dumps(d, sort_keys=True, default=str))


def snapshot_from_data(data: dict[str, Any]) -> Snapshot:
    """A Snapshot rebuilt from :func:`snapshot_data` (enough for the diff and the migration script)."""
    snap = Snapshot.__new__(Snapshot)
    for k, v in data.items():
        setattr(snap, k, v)
    snap.cat = None
    snap.views = {n: {**v, "view_deps": set(v.get("view_deps") or ())} for n, v in data.get("views", {}).items()}
    return snap


def empty_snapshot(charset: str = "NONE", engine_major: int = 0) -> Snapshot:
    data: dict[str, Any] = {k: {} for k in ("collations", "domains", "sequences", "exceptions", "roles", "udfs",
                                            "procedures", "functions", "packages", "tables", "indices", "views",
                                            "triggers", "comments")}
    data.update(include_global=True, engine_major=engine_major, charset=charset, description=None, deps=[],
                view_order=[], grants=[])
    return snapshot_from_data(data)


# ------------------------------------------------------------------ files
def _object_files(snap: Snapshot, prefix: str) -> dict[str, str]:
    files: dict[str, str] = {}
    grants: list[str] = []
    for (kind, name), text in snap.objects().items():
        if kind == "grants":
            grants.append(text)
        elif kind == "database":
            files[prefix + "database.sql"] = normalize(text)
        elif kind in KINDS:
            files[f"{prefix}{KINDS[kind][0]}/{file_name(name)}.sql"] = normalize(text)
    if grants:
        files[prefix + "grants.sql"] = normalize("".join(grants))
    return files


def _full_sql(snaps: list[tuple[str | None, Snapshot]], db_name: str) -> str:
    first = snaps[0][1]
    head = [f"/* Schema of {db_name.replace('*/', '* /')}, written by fbadmin schema history */",
            f"/* CREATE DATABASE 'database.fdb' DEFAULT CHARACTER SET {first.charset}; */",
            "SET SQL DIALECT 3;"]
    out = ["\n".join(head)]
    schemas = [s for s, _ in snaps if s]
    if schemas:
        creates = "".join(f"CREATE SCHEMA {quote_ident(s)};\n" for s in schemas if s != "PUBLIC").rstrip()
        if creates:
            out.append(creates)
    for schema, snap in snaps:
        body = render(build_sections(snap, sequence_values=False), set_term=True).rstrip()
        if schema:
            body = f"/* ===== Schema {schema} ===== */\nSET SEARCH_PATH TO {quote_ident(schema)}, SYSTEM;\n\n" + body
        out.append(body)
    return normalize("\n\n".join(x for x in out if x))


def build_files(snaps: list[tuple[str | None, Snapshot]], db_name: str) -> dict[str, str]:
    """All files of a snapshot, keyed by their path relative to the repository folder.

    ``snaps`` is ``[(None, snapshot)]`` for Firebird 3-5, ``[(schema, snapshot), ...]`` for Firebird 6."""
    files: dict[str, str] = {}
    for schema, snap in snaps:
        files.update(_object_files(snap, f"{file_name(schema)}/" if schema else ""))
    files["full.sql"] = _full_sql(snaps, db_name)
    data = {"format": FORMAT, "schemas": {schema or "": snapshot_data(snap) for schema, snap in snaps}}
    files[SNAPSHOT_FILE] = json.dumps(data, indent=1, sort_keys=True, ensure_ascii=False) + "\n"
    return dict(sorted(files.items()))


def snapshots_from_json(text: str) -> dict[str, Snapshot]:
    """``.fbadmin/snapshot.json`` -> {schema ("" before Firebird 6): Snapshot}."""
    data = json.loads(text)
    if data.get("format") != FORMAT:
        raise ValueError(f"Unsupported snapshot format {data.get('format')!r}")
    return {schema: snapshot_from_data(d) for schema, d in data["schemas"].items()}


def describe_path(path: str) -> dict[str, str | None] | None:
    """A repository path -> {schema, kind, label, name}; None for files that are not schema objects."""
    parts = path.split("/")
    schema = None
    if len(parts) >= 2 and parts[0] not in _FOLDER_KIND and parts[0] != META_DIR \
            and (len(parts) == 3 or parts[1] in ("grants.sql", "database.sql")):
        schema, parts = object_name(parts[0]), parts[1:]
    if len(parts) == 2 and parts[0] in _FOLDER_KIND and parts[1].endswith(".sql"):
        kind = _FOLDER_KIND[parts[0]]
        return {"schema": schema, "kind": kind, "label": KINDS[kind][1], "name": object_name(parts[1])}
    if parts == ["grants.sql"]:
        return {"schema": schema, "kind": "grants", "label": "grants", "name": None}
    if parts == ["database.sql"]:
        return {"schema": schema, "kind": "database", "label": "database comment", "name": None}
    return None


def change_summary(changes: list[tuple[str, str]]) -> dict[str, list[str]]:
    """``[(status A/M/D, path)]`` -> {"added": [...], "changed": [...], "dropped": [...]} of object labels."""
    out: dict[str, list[str]] = defaultdict(list)
    key = {"A": "added", "M": "changed", "D": "dropped"}
    for status, path in changes:
        d = describe_path(path)
        if not d or status[:1] not in key:
            continue
        label = d["label"] if d["name"] is None else f"{d['label']} {d['name']}"
        if d["schema"]:
            label = f"{label} (schema {d['schema']})"
        out[key[status[:1]]].append(label)
    return {k: sorted(out.get(k, [])) for k in ("added", "changed", "dropped")}
