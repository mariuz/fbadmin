"""Schema drift detection (roadmap 2.2).

A ``schema_drift`` schedule compares the live schema of a database with a *baseline*:

* ``history``: the schema stored in a schema history repository (``fbadmin.schemahistory``), at a branch,
  tag, ``origin/<branch>`` (optionally fetched first) or commit; by default the database's own repository
  and its configured branch, so changes made outside fbadmin since the last recorded snapshot show up;
* ``database``: another registered database, for example production against staging.

Differences are reported per object as *missing* (in the baseline, not in the database), *extra* (in the
database, not in the baseline) or *changed*, together with the script that makes the database match the
baseline. Object types and name patterns can be ignored. The latest report of each schedule is kept in
``<data dir>/drift/<schedule id>.json``; the scheduler records the summary in the run history and raises
the ``schema.drift`` alert (see ``maintenance.scheduler``).
"""

from __future__ import annotations

import fnmatch
import hashlib
import json
import os
import time
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator

from ..config import settings
from ..metadata.types import quote_ident

#: object types that can be ignored ("comments" strips COMMENT ON from every object)
IGNORABLE = ["database", "collations", "roles", "domains", "sequences", "exceptions", "tables", "indices", "views",
             "functions", "procedures", "packages", "triggers", "grants", "comments"]
STATUS = {"create": "missing", "drop": "extra", "alter": "changed"}
MAX_ITEMS = 2000  # objects listed in a report (the summary and the script cover all of them)


class DriftOptions(BaseModel):
    baseline: Literal["history", "database"] = "history"
    # baseline "history": this database's schema history repository unless another database is named
    history_server_id: str | None = None
    history_database_id: str | None = None
    ref: str | None = None  # branch, tag, origin/<branch> or commit (default: the configured branch)
    fetch: bool = False     # fetch the remote first (for origin/<branch>)
    # baseline "database"
    server_id: str | None = None
    database_id: str | None = None
    ignore_types: list[str] = Field(default_factory=list)
    ignore_names: list[str] = Field(default_factory=list)  # glob patterns, case-insensitive (e.g. TMP_*)

    @field_validator("ignore_types")
    @classmethod
    def _types(cls, v: list[str]) -> list[str]:
        bad = [t for t in v if t not in IGNORABLE]
        if bad:
            raise ValueError(f"Unknown object types to ignore: {', '.join(bad)}")
        return sorted(set(v))

    @field_validator("ref")
    @classmethod
    def _ref(cls, v: str | None) -> str | None:
        from ..schemahistory.gitrepo import check_ref

        return check_ref(v.strip()) if v and v.strip() else None

    @field_validator("ignore_names")
    @classmethod
    def _names(cls, v: list[str]) -> list[str]:
        return [p.strip() for p in v if p.strip()]

    def history_db(self, server_id: str, database_id: str) -> tuple[str, str]:
        return (self.history_server_id or server_id, self.history_database_id or database_id)


# ------------------------------------------------------------------ comparison
def _strip_comments(ddl: str) -> str:
    """The DDL of an object without its COMMENT ON statements."""
    out, skipping = [], False
    for line in ddl.splitlines(keepends=True):
        if not skipping and line.startswith("COMMENT ON "):
            skipping = True
        if skipping:
            if line.rstrip().endswith(";"):
                skipping = False
            continue
        out.append(line)
    return "".join(out)


def _objects(snap, opts: DriftOptions) -> dict[tuple[str, str], str]:
    objs = snap.objects()
    if "comments" in opts.ignore_types:
        objs = {k: _strip_comments(v) for k, v in objs.items() if k != ("database", "comment")}
    return objs


def _ignored(key: tuple[str, str], opts: DriftOptions) -> bool:
    if key[0] in opts.ignore_types:
        return True
    name = key[1].upper()
    return any(fnmatch.fnmatchcase(name, p.upper()) for p in opts.ignore_names)


def compare(baseline: dict[str, Any], live: dict[str, Any], opts: DriftOptions, *, header: str = "") -> dict:
    """Compare ``{schema: Snapshot}`` maps ("" before Firebird 6). The script makes ``live`` match
    ``baseline``; ignored differences are counted but neither listed nor scripted."""
    from ..schema.diff import TYPE_ORDER, compare as diff_compare
    from ..schemahistory import layout

    items: list[dict] = []
    ignored = 0
    parts: list[str] = []
    warnings: list[str] = []
    schemas = sorted(set(baseline) | set(live), key=lambda s: (s not in ("", "PUBLIC"), s))
    for schema in schemas:
        ref = baseline.get(schema) or live.get(schema)
        src = baseline.get(schema) or layout.empty_snapshot(ref.charset, ref.engine_major)
        tgt = live.get(schema) or layout.empty_snapshot(ref.charset, ref.engine_major)
        so, to = _objects(src, opts), _objects(tgt, opts)
        found = []
        for key in sorted(set(so) | set(to), key=lambda k: (TYPE_ORDER.index(k[0]) if k[0] in TYPE_ORDER else 99,
                                                             k[1])):
            a, b = so.get(key), to.get(key)
            if a == b:
                continue
            if _ignored(key, opts):
                ignored += 1
                continue
            found.append({"schema": schema or None, "type": key[0], "name": key[1],
                          "status": "missing" if b is None else "extra" if a is None else "changed",
                          "baseline_ddl": a, "database_ddl": b})
        items += found
        if not found:
            continue
        res = diff_compare(src, tgt, set_term=True)
        warnings += res["warnings"]
        head = ""
        if schema:
            if schema not in live:
                head += f"CREATE SCHEMA {quote_ident(schema)};\n"
            head += f"/* ===== Schema {schema} ===== */\nSET SEARCH_PATH TO {quote_ident(schema)}, SYSTEM;\n"
        parts.append(head + res["script"])
        if schema and schema not in baseline:
            parts.append(f"DROP SCHEMA {quote_ident(schema)};\n")
    if not parts:
        script = header + "/* No differences: nothing to do */\n"
    else:
        # the schema diff works on whole schemas: ignored differences end up in the script too
        note = ("/* Some differences were ignored, but this script also changes those objects: review it */\n"
                if ignored else "")
        script = header + note + "\n".join(parts)
    summary = {k: sum(1 for i in items if i["status"] == k) for k in ("missing", "extra", "changed")}
    return {"drift": bool(items), "summary": summary, "items": items, "ignored": ignored,
            "warnings": warnings, "script": script, "fingerprint": fingerprint(items)}


def fingerprint(items: list[dict]) -> str:
    """Identifies a set of differences, so an unchanged drift is not reported as new."""
    h = hashlib.sha256()
    for i in items:
        for part in (i["schema"] or "", i["type"], i["name"], i["status"], i["baseline_ddl"] or "",
                     i["database_ddl"] or ""):
            h.update(part.encode("utf-8", "replace") + b"\0")
    return h.hexdigest()[:16] if items else ""


# ------------------------------------------------------------------ baselines
def _live(server_id: str, database_id: str) -> dict[str, Any]:
    from ..api.schema import db_conn
    from ..schemahistory import collect_snapshots

    with db_conn(server_id, database_id) as conn:
        return {schema or "": snap for schema, snap in collect_snapshots(conn)}


def baseline(server_id: str, database_id: str, opts: DriftOptions, log) -> tuple[dict[str, Any], dict]:
    """``({schema: Snapshot}, description)`` of the baseline."""
    from ..api.deps import get_state

    st = get_state()
    if opts.baseline == "database":
        if not (opts.server_id and opts.database_id):
            raise ValueError("Choose the database to compare with")
        if (opts.server_id, opts.database_id) == (server_id, database_id):
            raise ValueError("The baseline database is the database itself")
        db = st.store.get_database(opts.server_id, opts.database_id)
        srv = st.store.get(opts.server_id)
        log(f"Reading the schema of the baseline database {srv.name} / {db.name}")
        return _live(opts.server_id, opts.database_id), {
            "kind": "database", "server_id": opts.server_id, "database_id": opts.database_id,
            "label": f"{srv.name} / {db.name}"}
    from .. import schemahistory

    hsid, hdid = opts.history_db(server_id, database_id)
    res = schemahistory.snapshots_at(hsid, hdid, opts.ref, fetch=opts.fetch, log=log)
    own = (hsid, hdid) == (server_id, database_id)
    where = "its schema history" if own else f"the schema history of {st.store.get_database(hsid, hdid).name}"
    log(f"Baseline: {where} at {res['ref']} ({res['commit'][:10]} {res['subject']})")
    return res["snapshots"], {"kind": "history", "server_id": hsid, "database_id": hdid, "ref": res["ref"],
                              "commit": res["commit"], "subject": res["subject"], "commit_time": res["time"],
                              "label": f"{where}, {res['ref']} @ {res['commit'][:10]}"}


def check(server_id: str, database_id: str, opts: DriftOptions, log=None) -> dict[str, Any]:
    """Compare the database with its baseline and return the report."""
    from ..api.deps import get_state

    log = log or (lambda _m: None)
    db = get_state().store.get_database(server_id, database_id)
    t0 = time.time()
    base, desc = baseline(server_id, database_id, opts, log)
    log(f"Reading the schema of {db.name}")
    live = _live(server_id, database_id)
    header = f"/* Generated by fbadmin drift detection: makes {db.name} match {desc['label']} */\n"
    res = compare(base, live, opts, header=header)
    report = {"server_id": server_id, "database_id": database_id, "database": db.name, "checked_at": t0,
              "duration_ms": round((time.time() - t0) * 1000), "baseline": desc,
              "ignore_types": opts.ignore_types, "ignore_names": opts.ignore_names, **res}
    if len(report["items"]) > MAX_ITEMS:
        report["items"] = report["items"][:MAX_ITEMS]
        report["truncated"] = True
    s = res["summary"]
    log("No drift: the schema matches the baseline" if not res["drift"] else
        f"Drift: {s['missing']} missing, {s['extra']} extra, {s['changed']} changed"
        + (f" ({res['ignored']} ignored)" if res["ignored"] else ""))
    return report


# ------------------------------------------------------------------ reports
def _dir() -> Path:
    return Path(settings.data_dir) / "drift"


def _file(schedule_id: str) -> Path:
    safe = "".join(ch for ch in schedule_id if ch.isalnum() or ch in "-_") or "_"
    return _dir() / f"{safe}.json"


def save_report(schedule_id: str, report: dict) -> None:
    _dir().mkdir(parents=True, exist_ok=True)
    p = _file(schedule_id)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(report, default=str))
    os.replace(tmp, p)


def load_report(schedule_id: str) -> dict | None:
    p = _file(schedule_id)
    if not p.exists():
        return None
    return json.loads(p.read_text())


def delete_report(schedule_id: str) -> None:
    _file(schedule_id).unlink(missing_ok=True)
