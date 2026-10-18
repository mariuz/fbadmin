"""pgAdmin-style "Search Objects": find objects by name, comment or PSQL/view source."""

from __future__ import annotations

from typing import Any

from ..metadata import Catalog

# (type, table, name expr, parent expr, description column, source columns, extra condition)
_SPECS = [
    ("tables", "RDB$RELATIONS", "RDB$RELATION_NAME", None, "RDB$DESCRIPTION", [],
     "RDB$VIEW_BLR IS NULL AND COALESCE(RDB$SYSTEM_FLAG, 0) = 0"),
    ("views", "RDB$RELATIONS", "RDB$RELATION_NAME", None, "RDB$DESCRIPTION", ["RDB$VIEW_SOURCE"],
     "RDB$VIEW_BLR IS NOT NULL AND COALESCE(RDB$SYSTEM_FLAG, 0) = 0"),
    ("columns", "RDB$RELATION_FIELDS", "RDB$FIELD_NAME", "RDB$RELATION_NAME", "RDB$DESCRIPTION", [],
     "COALESCE(RDB$SYSTEM_FLAG, 0) = 0"),
    ("procedures", "RDB$PROCEDURES", "RDB$PROCEDURE_NAME", "RDB$PACKAGE_NAME", "RDB$DESCRIPTION",
     ["RDB$PROCEDURE_SOURCE"], "COALESCE(RDB$SYSTEM_FLAG, 0) = 0"),
    ("functions", "RDB$FUNCTIONS", "RDB$FUNCTION_NAME", "RDB$PACKAGE_NAME", "RDB$DESCRIPTION",
     ["RDB$FUNCTION_SOURCE"], "COALESCE(RDB$SYSTEM_FLAG, 0) = 0"),
    ("packages", "RDB$PACKAGES", "RDB$PACKAGE_NAME", None, "RDB$DESCRIPTION",
     ["RDB$PACKAGE_HEADER_SOURCE", "RDB$PACKAGE_BODY_SOURCE"], "COALESCE(RDB$SYSTEM_FLAG, 0) = 0"),
    ("triggers", "RDB$TRIGGERS", "RDB$TRIGGER_NAME", "RDB$RELATION_NAME", "RDB$DESCRIPTION", ["RDB$TRIGGER_SOURCE"],
     "COALESCE(RDB$SYSTEM_FLAG, 0) = 0 AND NOT EXISTS (SELECT 1 FROM RDB$CHECK_CONSTRAINTS cc "
     "WHERE cc.RDB$TRIGGER_NAME = x.RDB$TRIGGER_NAME)"),
    ("sequences", "RDB$GENERATORS", "RDB$GENERATOR_NAME", None, "RDB$DESCRIPTION", [],
     "COALESCE(RDB$SYSTEM_FLAG, 0) = 0"),
    ("domains", "RDB$FIELDS", "RDB$FIELD_NAME", None, "RDB$DESCRIPTION", ["RDB$VALIDATION_SOURCE"],
     "RDB$FIELD_NAME NOT STARTING WITH 'RDB$' AND COALESCE(RDB$SYSTEM_FLAG, 0) = 0"),
    ("exceptions", "RDB$EXCEPTIONS", "RDB$EXCEPTION_NAME", None, "RDB$DESCRIPTION", ["RDB$MESSAGE"],
     "COALESCE(RDB$SYSTEM_FLAG, 0) = 0"),
    ("indices", "RDB$INDICES", "RDB$INDEX_NAME", "RDB$RELATION_NAME", "RDB$DESCRIPTION", ["RDB$EXPRESSION_SOURCE"],
     "COALESCE(RDB$SYSTEM_FLAG, 0) = 0"),
    ("roles", "RDB$ROLES", "RDB$ROLE_NAME", None, "RDB$DESCRIPTION", [], "COALESCE(RDB$SYSTEM_FLAG, 0) = 0"),
]


def _snippet(text: str, q: str, width: int = 140) -> tuple[str, int | None]:
    low, ql = text.lower(), q.lower()
    i = low.find(ql)
    if i < 0:
        return text[:width], None
    line = text.count("\n", 0, i) + 1
    start = text.rfind("\n", 0, i) + 1
    end = text.find("\n", i)
    end = len(text) if end < 0 else end
    s = text[start:end].strip()
    if len(s) > width:
        j = s.lower().find(ql)
        s = ("…" if j > 40 else "") + s[max(0, j - 40): j + width - 40] + "…"
    return s, line


def search_objects(cat: Catalog, q: str, scopes: set[str] | None = None, types: set[str] | None = None,
                   limit: int = 500) -> list[dict[str, Any]]:
    q = (q or "").strip()
    if not q:
        return []
    scopes = scopes or {"name", "comment", "source"}
    out: list[dict[str, Any]] = []
    for otype, table, name_col, parent_col, desc_col, src_cols, cond in _SPECS:
        if types and otype not in types:
            continue
        if otype == "columns":
            cond += (" AND EXISTS (SELECT 1 FROM RDB$RELATIONS r WHERE r.RDB$RELATION_NAME = x.RDB$RELATION_NAME "
                     "AND COALESCE(r.RDB$SYSTEM_FLAG, 0) = 0)")
        if parent_col and not cat.has_column(table, parent_col):
            parent_col = None
        checks, params = [], []
        if "name" in scopes:
            checks.append(f"x.{name_col} CONTAINING ?")
            params.append(q)
        if "comment" in scopes:
            checks.append(f"x.{desc_col} CONTAINING ?")
            params.append(q)
        if "source" in scopes:
            for s in src_cols:
                checks.append(f"x.{s} CONTAINING ?")
                params.append(q)
        if not checks:
            continue
        cols = [f"TRIM(x.{name_col}) AS name", f"x.{desc_col} AS descr"]
        cols.append(f"TRIM(x.{parent_col}) AS parent" if parent_col else "CAST(NULL AS VARCHAR(63)) AS parent")
        for i, s in enumerate(src_cols):
            cols.append(f"x.{s} AS src{i}")
        sql = f"SELECT {', '.join(cols)} FROM {table} x WHERE ({cond}) AND ({' OR '.join(checks)}) ORDER BY 1"
        for r in cat.rows(sql, params):
            item: dict[str, Any] = {"type": otype, "name": r["name"], "parent": r["parent"]}
            if otype == "indices" and r["parent"]:
                item["table"] = r["parent"]
            if "name" in scopes and q.lower() in (r["name"] or "").lower():
                item.update(match="name", snippet=None)
            elif "comment" in scopes and r["descr"] and q.lower() in r["descr"].lower():
                item.update(match="comment", snippet=_snippet(r["descr"], q)[0])
            else:
                for i, _s in enumerate(src_cols):
                    text = r.get(f"src{i}") or ""
                    if q.lower() in text.lower():
                        snip, line = _snippet(text, q)
                        item.update(match="source", snippet=snip, line=line)
                        break
                else:
                    item.update(match="name", snippet=None)
            if otype in ("procedures", "functions") and r["parent"]:
                # packaged routine: open the package in the tree
                item.update(package=r["parent"])
            out.append(item)
            if len(out) >= limit:
                return out
    return out
