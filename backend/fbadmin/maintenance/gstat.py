"""Parser for ``gstat`` (database statistics) output, Firebird 3, 4 and 5.

Produces structured JSON for charts plus a list of "tables needing attention".
"""

from __future__ import annotations

import re
from typing import Any

_TABLE_RE = re.compile(r"^(\S.*?) \((\d+)\)\s*$")
_INDEX_RE = re.compile(r"^\s+Index (.+?) \((\d+)\)\s*$")
_QUALIFIED_RE = re.compile(r'^"((?:[^"]|"")+)"\."((?:[^"]|"")+)"$')


def _split_name(raw: str) -> tuple[str | None, str]:
    """Firebird 6 prints schema-qualified, quoted names: "APP"."T1". Returns (schema, name)."""
    m = _QUALIFIED_RE.match(raw.strip())
    if not m:
        return None, raw
    return m.group(1).replace('""', '"'), m.group(2).replace('""', '"')


def _display(schema: str | None, name: str) -> str:
    return name if schema in (None, "PUBLIC", "SYSTEM") else f"{schema}.{name}"


_FILL_RE = re.compile(r"^\s*(\d+)\s*-\s*(\d+)%\s*=\s*(\d+)\s*$")
_LEVEL_RE = re.compile(r"Level (\d+): (\d+)")

FILL_BUCKETS = ["0-19%", "20-39%", "40-59%", "60-79%", "80-99%"]


def _key(k: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", k.strip().lower()).strip("_")


def _value(v: str) -> Any:
    v = v.strip()
    m = re.fullmatch(r"(-?\d+)(?:\s*(?:%|bytes))?", v)
    if m:
        return int(m.group(1))
    m = re.fullmatch(r"-?\d+\.\d+", v)
    if m:
        return float(v)
    return v


def _pairs(line: str) -> dict[str, Any]:
    """Parse ``Key: value, Other key: value`` into a dict."""
    out: dict[str, Any] = {}
    for part in line.strip().split(", "):
        if ":" not in part:
            continue
        k, v = part.split(":", 1)
        out[_key(k)] = _value(v)
    return out


def parse_header(lines: list[str]) -> dict[str, Any]:
    """Parse the 'Database header page information' block (also used for gstat -h)."""
    hdr: dict[str, Any] = {}
    in_hdr = False
    for raw in lines:
        line = raw.rstrip()
        if line.startswith("Database header page information"):
            in_hdr = True
            continue
        if not in_hdr:
            continue
        if not line.strip():
            if hdr:
                # Blank line before "Variable header data" ends the fixed part.
                in_hdr = False
            continue
        s = line.strip()
        parts = re.split(r"\t+|\s{2,}", s, maxsplit=1)
        if len(parts) == 2:
            hdr[_key(parts[0])] = _value(parts[1])
    # Variable header data (GUID, sweep interval, replica mode, ...)
    for raw in lines:
        s = raw.strip()
        m = re.match(r"(Sweep interval|Database GUID|Replica mode|Database backup GUID|Encryption key name"
                     r"|Crypt checksum|Key hash|Last logical page):\s*(.*)$", s)
        if m:
            hdr[_key(m.group(1))] = _value(m.group(2))
    attrs = hdr.get("attributes")
    hdr["attributes"] = [a.strip() for a in str(attrs).split(",") if a.strip()] if attrs not in (None, "") else []
    return hdr


def shutdown_mode(attributes: list[str]) -> str:
    a = " ".join(attributes)
    if "full shutdown" in a:
        return "full"
    if "single-user maintenance" in a:
        return "single"
    if "multi-user maintenance" in a:
        return "multi"
    return "online"


def parse(text: str) -> dict[str, Any]:
    lines = text.splitlines()
    result: dict[str, Any] = {"database": None, "header": parse_header(lines), "tables": []}
    m = re.search(r'^Database "(.*)"', text, re.M)
    if m:
        result["database"] = m.group(1)
    table: dict | None = None
    index: dict | None = None
    analyzing = False
    in_fill = False
    for raw in lines:
        line = raw.rstrip()
        if line.startswith("Analyzing database pages"):
            analyzing = True
            continue
        if not analyzing:
            continue
        if line.startswith("Gstat completion time"):
            break
        if not line.strip():
            in_fill = False
            continue
        mi = _INDEX_RE.match(line)
        if mi and table is not None:
            sch, nm = _split_name(mi.group(1))
            index = {"name": _display(sch, nm), "schema": sch, "id": int(mi.group(2)), "fill_distribution": [0] * 5}
            table["indices"].append(index)
            in_fill = False
            continue
        mt = _TABLE_RE.match(line)
        if mt:
            sch, nm = _split_name(mt.group(1))
            table = {"name": _display(sch, nm), "schema": sch, "id": int(mt.group(2)), "fill_distribution": [0] * 5,
                     "indices": []}
            result["tables"].append(table)
            index = None
            in_fill = False
            continue
        if table is None:
            continue
        s = line.strip()
        if s.startswith("Fill distribution"):
            in_fill = True
            continue
        mf = _FILL_RE.match(line)
        if in_fill and mf:
            bucket = min(int(mf.group(1)) // 20, 4)
            (index or table)["fill_distribution"][bucket] = int(mf.group(3))
            continue
        target = index if index is not None else table
        if s.startswith("Level ") and index is None:
            # Blob levels (FB3: "Level 0: n, Level 1: n, Level 2: n"; FB4+: one line per level)
            levels = table.setdefault("blob_levels", {})
            for lv, cnt in _LEVEL_RE.findall(s):
                levels[lv] = int(cnt)
            continue
        target.update(_pairs(s))
    for t in result["tables"]:
        _derive(t, result["header"].get("page_size"))
    return result


def _derive(t: dict, page_size: int | None) -> None:
    rec = t.get("total_records") or 0
    t["version_ratio"] = round((t.get("total_versions") or 0) / rec, 4) if rec else 0.0
    t["fragment_ratio"] = round((t.get("total_fragments") or 0) / rec, 4) if rec else 0.0
    if "table_size" not in t and page_size:
        # FB3 does not print the table size; estimate from data + pointer pages.
        t["table_size"] = ((t.get("data_pages") or 0) + (t.get("pointer_pages") or 0)) * page_size
        t["table_size_estimated"] = True


DEFAULT_THRESHOLDS = {
    "version_ratio": 0.2,       # back versions per record
    "min_records": 100,         # ignore tiny tables for ratios
    "max_versions": 5,          # longest version chain
    "fragment_ratio": 0.1,
    "low_fill": 50,             # average data page fill %
    "min_pages": 10,
    "index_depth": 4,
    "dup_ratio": 0.9,           # max dup / nodes
    "transaction_gap": 10000,   # next - oldest active
}


def attention(stats: dict, thresholds: dict | None = None) -> list[dict]:
    """Return findings sorted by severity: tables and indices that need maintenance."""
    th = {**DEFAULT_THRESHOLDS, **(thresholds or {})}
    out: list[dict] = []

    def add(sev: str, table: str | None, index: str | None, issue: str, detail: str, advice: str) -> None:
        out.append({"severity": sev, "table": table, "index": index, "issue": issue, "detail": detail,
                    "advice": advice})

    hdr = stats.get("header", {})
    nxt, oat = hdr.get("next_transaction"), hdr.get("oldest_active")
    if isinstance(nxt, int) and isinstance(oat, int) and nxt - oat > th["transaction_gap"]:
        add("warning", None, None, "Transaction gap", f"next {nxt} - oldest active {oat} = {nxt - oat}",
            "A long-running transaction stops garbage collection; find it in Monitor.")
    for t in stats.get("tables", []):
        name = t["name"]
        rec = t.get("total_records") or 0
        if rec >= th["min_records"] and t.get("version_ratio", 0) >= th["version_ratio"]:
            sev = "critical" if t["version_ratio"] >= 1 else "warning"
            add(sev, name, None, "Many record versions",
                f"{t.get('total_versions')} back versions for {rec} records ({t['version_ratio']:.0%})",
                "Run a sweep or a full SELECT COUNT(*) to collect garbage; check for long transactions.")
        if (t.get("max_versions") or 0) >= th["max_versions"]:
            add("warning", name, None, "Long version chains", f"max versions {t.get('max_versions')}",
                "Rows updated many times while an old snapshot was open.")
        if rec >= th["min_records"] and t.get("fragment_ratio", 0) >= th["fragment_ratio"]:
            add("info", name, None, "Fragmented records",
                f"{t.get('total_fragments')} fragments for {rec} records",
                "Records are wider than a page; consider a larger page size (backup/restore).")
        pages = t.get("data_pages") or 0
        fill = t.get("average_fill")
        if pages >= th["min_pages"] and isinstance(fill, int) and fill < th["low_fill"]:
            add("info", name, None, "Low page fill", f"average fill {fill}% over {pages} data pages",
                "Space from deleted rows is reused over time; a backup/restore compacts the table.")
        for ix in t.get("indices", []):
            depth = ix.get("depth") or 0
            if depth >= th["index_depth"]:
                add("warning", name, ix["name"], "Deep index", f"depth {depth}",
                    "Indices deeper than 3 levels are slower; use a larger page size.")
            nodes = ix.get("nodes") or 0
            if nodes >= 1000 and (ix.get("max_dup") or 0) / nodes >= th["dup_ratio"]:
                add("info", name, ix["name"], "Poor selectivity",
                    f"max dup {ix.get('max_dup')} of {nodes} nodes", "The index rarely helps; consider dropping it.")
    order = {"critical": 0, "warning": 1, "info": 2}
    out.sort(key=lambda f: (order[f["severity"]], f["table"] or "", f["index"] or ""))
    return out
