"""Index advisor: suggest indices from the plans of slow statements.

Input is a workload: statements with (optionally) their plan, duration and how often they were seen
(MON$STATEMENTS, the history's slow-statement log, MON$COMPILED_STATEMENTS or a pasted script).
For each statement the plan is parsed for full scans (``NATURAL`` / ``Full Scan``); the SQL is parsed
(lightly, with regular expressions) for WHERE/JOIN predicates. A full scan of a large table that has a
predicate on columns without a usable index produces a ``CREATE INDEX`` suggestion with the reasoning.

This is a heuristic helper, not an optimizer: always check the suggestion against the real plan.
"""

from __future__ import annotations

import re
from typing import Any

from ..metadata import Catalog
from ..metadata.types import quote_ident
from ..query import explain

_IDENT = r'(?:"(?:[^"]|"")+"|[A-Za-z_][A-Za-z0-9_$]*)'
_STRINGS = re.compile(r"'(?:[^']|'')*'")
_COMMENTS = re.compile(r"--[^\n]*|/\*.*?\*/", re.S)
_KEYWORDS = {
    "SELECT", "FROM", "WHERE", "JOIN", "LEFT", "RIGHT", "INNER", "OUTER", "FULL", "CROSS", "NATURAL", "ON",
    "AND", "OR", "NOT", "GROUP", "ORDER", "BY", "HAVING", "UNION", "ALL", "DISTINCT", "AS", "ROWS", "FETCH",
    "FIRST", "SKIP", "OFFSET", "PLAN", "FOR", "UPDATE", "DELETE", "INSERT", "INTO", "SET", "VALUES", "WITH",
    "LATERAL", "USING", "RETURNING", "IS", "NULL", "IN", "LIKE", "STARTING", "CONTAINING", "BETWEEN", "EXISTS",
    "CASE", "WHEN", "THEN", "ELSE", "END", "MERGE", "MATCHED", "SIMILAR", "TO", "ESCAPE", "ASC", "DESC",
    "CURRENT_DATE", "CURRENT_TIMESTAMP", "CURRENT_TIME", "CURRENT_USER", "CURRENT_ROLE", "TRUE", "FALSE",
}
# Table references: FROM t [AS] a / JOIN t [AS] a / UPDATE t [AS] a / DELETE FROM t / MERGE INTO t
_TABLE_REF = re.compile(
    rf"\b(?:FROM|JOIN|UPDATE|INTO)\s+({_IDENT})(?:\s+(?:AS\s+)?({_IDENT}))?", re.I)
_CLAUSE_START = re.compile(r"\b(WHERE|ON|HAVING)\b", re.I)
_CLAUSE_END = re.compile(
    r"\b(GROUP\s+BY|ORDER\s+BY|HAVING|UNION|ROWS|FETCH|OFFSET|PLAN|FOR\s+UPDATE|WITH\s+LOCK|RETURNING|"
    r"LEFT|RIGHT|INNER|FULL|CROSS|JOIN|WHERE|WHEN|INTO|DO)\b", re.I)
_COL = rf"(?:({_IDENT})\.)?({_IDENT})"
_FUNC_COL = re.compile(rf"\b(UPPER|LOWER|TRIM)\s*\(\s*{_COL}\s*\)\s*(=|<=|>=|<|>|\bLIKE\b|\bSTARTING(?:\s+WITH)?\b|\bIN\b)", re.I)
_PRED_L = re.compile(
    rf"{_COL}\s*(=|<>|!=|<=|>=|<|>|\bNOT\s+IN\b|\bIN\b|\bIS\b|\bNOT\s+LIKE\b|\bLIKE\b|\bSTARTING(?:\s+WITH)?\b|"
    r"\bBETWEEN\b|\bCONTAINING\b|\bSIMILAR\b)\s*('(?:[^']|'')*')?", re.I)
_PRED_R = re.compile(rf"(=|<>|!=|<=|>=|<|>)\s*{_COL}(?!\s*\()", re.I)
# Firebird 6 qualifies tables with their schema: Table "PUBLIC"."ORD" as "O" Full Scan
_PLAN_FULL = re.compile(r'Table (?:"(?:[^"]|"")+"\.)?"((?:[^"]|"")+)"(?: as "((?:[^"]|"")+)")? Full Scan', re.I)
_PLAN_NATURAL = re.compile(r"([A-Za-z0-9_$\" ]+?)\s+NATURAL\b")

EQ_OPS = {"=", "IN", "IS"}
RANGE_OPS = {"<", ">", "<=", ">=", "BETWEEN", "STARTING", "STARTING WITH", "LIKE"}


def _unq(name: str | None) -> str | None:
    if name is None:
        return None
    if name.startswith('"'):
        return name[1:-1].replace('""', '"')
    return name.upper()


def _clean(sql: str) -> tuple[str, str]:
    """Return (sql without comments, same with string literals blanked)."""
    s = _COMMENTS.sub(" ", sql)
    return s, _STRINGS.sub(lambda m: "'" + "x" * (len(m.group(0)) - 2) + "'", s)


def table_refs(sql: str) -> dict[str, str]:
    """alias (or table name) -> table name."""
    _, blank = _clean(sql)
    refs: dict[str, str] = {}
    for m in _TABLE_REF.finditer(blank):
        t = _unq(m.group(1))
        a = _unq(m.group(2))
        if not t or t in _KEYWORDS or blank[m.end(1):m.end(1) + 1] == "(":
            continue
        refs.setdefault(t, t)
        if a and a not in _KEYWORDS:
            refs[a] = t
    return refs


def _clauses(sql: str) -> list[tuple[str, str]]:
    """Text of each WHERE/ON/HAVING clause (literal-preserving)."""
    orig, blank = _clean(sql)
    out = []
    for m in _CLAUSE_START.finditer(blank):
        start = m.end()
        end_m = _CLAUSE_END.search(blank, start)
        end = end_m.start() if end_m else len(blank)
        out.append((m.group(1).upper(), orig[start:end]))
    return out


def predicates(sql: str) -> list[dict[str, Any]]:
    """Column predicates: [{qualifier, column, op, kind: eq|range|like_prefix|unsargable|join, ...}]."""
    preds: list[dict[str, Any]] = []
    for clause, text in _clauses(sql):
        for m in _FUNC_COL.finditer(text):
            preds.append({"qualifier": _unq(m.group(2)), "column": _unq(m.group(3)), "op": m.group(4).upper(),
                          "kind": "function", "function": m.group(1).upper(), "join": False, "clause": clause})
        for m in _PRED_L.finditer(text):
            q, c, op, lit = _unq(m.group(1)), _unq(m.group(2)), " ".join(m.group(3).upper().split()), m.group(4)
            if c in _KEYWORDS or (c and c.isdigit()):
                continue
            kind = "eq" if op in EQ_OPS else "range" if op.startswith(("STARTING", "BETWEEN", "<", ">")) else "other"
            if op == "LIKE" and lit:
                kind = "range" if lit[1:2] not in ("%", "_") else "unsargable"
            if op in ("<>", "!=", "NOT IN", "NOT LIKE", "CONTAINING", "SIMILAR"):
                kind = "unsargable"
            rest = text[m.end():].lstrip()
            rm = re.match(rf"{_COL}", rest) if not lit else None
            is_join = bool(rm and rm.group(1) and op == "=")
            preds.append({"qualifier": q, "column": c, "op": op, "kind": kind, "function": None,
                          "join": is_join, "clause": clause})
            if is_join:
                preds.append({"qualifier": _unq(rm.group(1)), "column": _unq(rm.group(2)), "op": "=", "kind": "eq",
                              "function": None, "join": True, "clause": clause})
    # value = col (column on the right side, e.g. "WHERE 5 = t.x")
    for _clause, text in _clauses(sql):
        for m in _PRED_R.finditer(text):
            before = text[:m.start()].rstrip()
            if re.search(rf"{_IDENT}$", before) and not before.endswith("'"):
                continue  # col = col: handled above
            q, c = _unq(m.group(2)), _unq(m.group(3))
            if c in _KEYWORDS or not q:
                continue
            preds.append({"qualifier": q, "column": c, "op": m.group(1), "kind": "eq" if m.group(1) == "=" else "range",
                          "function": None, "join": False, "clause": _clause})
    return preds


def full_scans(plan: str | None, refs: dict[str, str]) -> list[tuple[str, str | None]]:
    """[(table, alias)] scanned without an index, from an explained or a legacy plan."""
    if not plan:
        return []
    out = []
    for m in _PLAN_FULL.finditer(plan):
        out.append((m.group(1).replace('""', '"'), (m.group(2) or "").replace('""', '"') or None))
    if out:
        return out
    for m in _PLAN_NATURAL.finditer(plan.replace("(", " ").replace(",", " ")):
        words = m.group(1).split()
        if not words:
            continue
        alias = _unq(words[-1])
        out.append((refs.get(alias, alias), alias))
    return out


class _Tables:
    def __init__(self, cat: Catalog):
        self.cat = cat
        self._cols: dict[str, set[str]] = {}
        self._idx: dict[str, list[dict]] = {}
        self._rows: dict[str, int | None] = {}

    def columns(self, t: str) -> set[str]:
        if t not in self._cols:
            self._cols[t] = {r["n"] for r in self.cat.rows(
                "SELECT TRIM(RDB$FIELD_NAME) AS n FROM RDB$RELATION_FIELDS WHERE RDB$RELATION_NAME = ?", [t])}
        return self._cols[t]

    def indices(self, t: str) -> list[dict]:
        if t not in self._idx:
            self._idx[t] = self.cat.indices(t)
        return self._idx[t]

    def rows(self, t: str, cap: int) -> int | None:
        if t not in self._rows:
            try:
                r = self.cat.one(f"SELECT COUNT(*) AS n FROM (SELECT 1 AS x FROM {quote_ident(t)} ROWS {int(cap)})")
                self._rows[t] = int(r["n"]) if r else None
            except Exception:  # noqa: BLE001 - views, permissions...
                self._rows[t] = None
        return self._rows[t]


def _index_name(table: str, cols: list[str]) -> str:
    base = "IX_" + table + "_" + "_".join(cols)
    base = re.sub(r"[^A-Za-z0-9_$]", "_", base).upper()
    return base[:63]


def analyze(conn, workload: list[dict[str, Any]], min_rows: int = 1000, row_cap: int = 1_000_000) -> dict[str, Any]:
    """Analyze statements ``[{sql, plan?, seconds?, seen?}]`` and return suggestions."""
    cat = Catalog(conn)
    tables = _Tables(cat)
    analyzed = []
    suggestions: dict[tuple, dict[str, Any]] = {}
    notes: dict[tuple, dict[str, Any]] = {}

    for item in workload:
        sql = (item.get("sql") or item.get("sql_text") or "").strip()
        if not sql:
            continue
        plan = item.get("plan")
        error = None
        if not plan:
            try:
                plan = explain(conn, sql, detailed=True) or explain(conn, sql)
            except Exception as exc:  # noqa: BLE001
                error = str(exc).strip().splitlines()[-1] if str(exc).strip() else "cannot prepare"
        refs = table_refs(sql)
        scans = [(t, a) for t, a in full_scans(plan, refs)
                 if not t.startswith(("RDB$", "MON$", "SEC$", "PLG$"))]
        preds = predicates(sql)
        entry = {"sql": sql, "plan": plan, "error": error, "full_scans": [t for t, _ in scans],
                 "seconds": item.get("seconds"), "seen": item.get("seen") or 1, "suggestions": []}
        for table, alias in scans:
            cols = tables.columns(table)
            if not cols:
                continue
            names = {alias, table} - {None}
            single = len(set(refs.values())) <= 1
            mine = [p for p in preds if (p["qualifier"] in names or (p["qualifier"] is None and (single or (
                p["column"] in cols and sum(p["column"] in tables.columns(t) for t in set(refs.values())) == 1))))
                and p["column"] in cols]
            nrows = tables.rows(table, row_cap)
            if not mine:
                continue
            if nrows is not None and nrows < min_rows:
                key = (table, "small")
                notes.setdefault(key, {"table": table, "rows": nrows, "kind": "small_table",
                                       "message": f"{table} is scanned but has only {nrows} rows: an index would "
                                                  "not help much."})
                continue
            idx = [i for i in tables.indices(table) if not i["inactive"] and not i["expr"]]
            leading = {i["columns"][0]: i for i in idx if i["columns"]}
            inactive = [i for i in tables.indices(table) if i["inactive"]]
            usable = [p for p in mine if p["kind"] in ("eq", "range")]
            for p in mine:
                if p["kind"] == "function":
                    cols_ = [p["column"]]
                    ddl = (f"CREATE INDEX {_index_name(table, [p['function'], p['column']])} ON {quote_ident(table)} "
                           f"COMPUTED BY ({p['function']}({quote_ident(p['column'])}));")
                    key = (table, "expr", p["function"], p["column"])
                    s = suggestions.setdefault(key, {
                        "table": table, "columns": cols_, "ddl": ddl, "rows": nrows, "statements": [],
                        "occurrences": 0, "total_seconds": 0.0, "kind": "expression",
                        "reason": f"{p['function']}({p['column']}) in a predicate cannot use a plain index on "
                                  f"{p['column']}; an expression index matches it."})
                    _attach(s, entry, item)
                elif p["kind"] == "unsargable":
                    key = (table, "unsarg", p["column"], p["op"])
                    notes.setdefault(key, {"table": table, "kind": "unsargable", "column": p["column"],
                                           "message": f"{table}.{p['column']} {p['op']} ... cannot use an index "
                                                      "(negation, CONTAINING or LIKE with a leading wildcard)."})
            if not usable:
                continue
            already = [p for p in usable if p["column"] in leading]
            missing = [p for p in usable if p["column"] not in leading]
            if not missing:
                for p in already:
                    i = leading[p["column"]]
                    key = (table, "unused", i["name"])
                    notes.setdefault(key, {
                        "table": table, "kind": "index_not_used", "column": p["column"], "index": i["name"],
                        "message": f"{table} is fully scanned although index {i['name']} starts with "
                                   f"{p['column']}. Possible causes: outdated selectivity (run SET STATISTICS INDEX "
                                   f"{i['name']}), a data type mismatch in the comparison, an OR between "
                                   "predicates, or the optimizer chose a full scan for a low-selectivity value."})
                continue
            eq = []
            for p in missing:
                if p["kind"] == "eq" and p["column"] not in eq:
                    eq.append(p["column"])
            rng = [p["column"] for p in missing if p["kind"] == "range" and p["column"] not in eq][:1]
            cols_ = (eq + rng)[:3]
            ddl = f"CREATE INDEX {_index_name(table, cols_)} ON {quote_ident(table)} ({', '.join(quote_ident(c) for c in cols_)});"
            key = (table, "cols", tuple(cols_))
            ops = ", ".join(sorted({f"{p['column']} {p['op']}" for p in missing}))
            reason = (f"{table} ({_fmt_rows(nrows)}) is read with a full scan (NATURAL) while the statement "
                      f"filters/joins on {ops} and no active index starts with "
                      f"{' or '.join(dict.fromkeys(p['column'] for p in missing))}.")
            if len(cols_) > 1:
                reason += " Equality columns come first in the compound index, then the range column."
            if any(p["join"] for p in missing):
                reason += " A join column without an index forces a full scan (or a hash join) for every lookup."
            if inactive:
                reason += f" Note: inactive index(es) exist on this table: {', '.join(i['name'] for i in inactive)}."
            s = suggestions.setdefault(key, {"table": table, "columns": cols_, "ddl": ddl, "rows": nrows,
                                             "statements": [], "occurrences": 0, "total_seconds": 0.0,
                                             "kind": "index", "reason": reason})
            _attach(s, entry, item)
        analyzed.append(entry)

    out = sorted(suggestions.values(), key=lambda s: (-(s["total_seconds"] or 0), -s["occurrences"],
                                                      -(s["rows"] or 0)))
    return {"suggestions": out, "notes": list(notes.values()), "statements": analyzed,
            "min_rows": min_rows}


def _attach(s: dict, entry: dict, item: dict) -> None:
    seen = int(item.get("seen") or 1)
    s["occurrences"] += seen
    s["total_seconds"] += float(item.get("seconds") or 0) * seen
    if len(s["statements"]) < 5 and entry["sql"] not in s["statements"]:
        s["statements"].append(entry["sql"])
    entry["suggestions"].append(s["ddl"])


def _fmt_rows(n: int | None) -> str:
    if n is None:
        return "row count unknown"
    return f"{n:,}+ rows" if n >= 1_000_000 else f"{n:,} rows"
