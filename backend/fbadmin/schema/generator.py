"""Test data generator: random rows that respect types, domains, NOT NULL, simple CHECK constraints,
foreign keys (values sampled from the parent tables) and primary/unique keys."""

from __future__ import annotations

import datetime as dt
import decimal
import random
import re
import string
from typing import Any

from ..metadata import Catalog
from ..metadata.types import quote_ident
from .dataio import converter

_WORDS = ("alpha bravo charlie delta echo foxtrot golf hotel india juliet kilo lima mike november oscar papa "
          "quebec romeo sierra tango uniform victor whiskey xray yankee zulu firebird table index view").split()
_FIRST = "Ann Bob Carla Dan Eva Frank Gina Hugo Ida Jon Kim Leo Mia Nina Otto Paul Rita Sam Tina Uma".split()
_LAST = "Smith Jones Brown Garcia Miller Davis Lopez Wilson Moore Taylor Martin Lee White Clark Hall".split()
_CITIES = "Paris Berlin Rome Madrid Lisbon Vienna Prague Warsaw Oslo Dublin Bucharest Athens".split()

_NUM = r"(-?\d+(?:\.\d+)?)"


def parse_checks(sources: list[str], column: str) -> dict[str, Any]:
    """Extract simple bounds from CHECK sources for ``column`` (or VALUE in domain checks)."""
    out: dict[str, Any] = {}
    names = rf"(?:{re.escape(column)}|\"{re.escape(column)}\"|VALUE)"
    for src in sources:
        s = src or ""
        if re.search(r"\bOR\b", s, re.IGNORECASE) and not re.search(r"IS\s+NULL\s+OR", s, re.IGNORECASE):
            continue  # too complex to honour, rows that violate it are reported
        for m in re.finditer(rf"\b{names}\s*(>=|<=|<>|>|<|=)\s*{_NUM}", s, re.IGNORECASE):
            op, v = m.group(1), decimal.Decimal(m.group(2))
            if op in (">", ">="):
                lo = v if op == ">=" else v + (1 if "." not in m.group(2) else decimal.Decimal("0.01"))
                out["min"] = max(out.get("min", lo), lo)
            elif op in ("<", "<="):
                hi = v if op == "<=" else v - (1 if "." not in m.group(2) else decimal.Decimal("0.01"))
                out["max"] = min(out.get("max", hi), hi)
            elif op == "=":
                out["values"] = [v]
        for m in re.finditer(rf"\b{names}\s+BETWEEN\s+{_NUM}\s+AND\s+{_NUM}", s, re.IGNORECASE):
            out["min"], out["max"] = decimal.Decimal(m.group(1)), decimal.Decimal(m.group(2))
        m = re.search(rf"\b{names}\s+IN\s*\(([^)]*)\)", s, re.IGNORECASE)
        if m:
            vals = []
            for part in re.findall(r"'((?:[^']|'')*)'|(-?\d+(?:\.\d+)?)", m.group(1)):
                vals.append(part[0].replace("''", "'") if part[0] or not part[1] else decimal.Decimal(part[1]))
            if vals:
                out["values"] = vals
        m = re.search(rf"\b{names}\s+LIKE\s+'([^']*)'", s, re.IGNORECASE)
        if m:
            out["like"] = m.group(1)
    return out


class ColumnGen:
    def __init__(self, col: dict, rng: random.Random, checks: dict, null_ratio: float, unique: bool,
                 override: dict | None, start: int = 1):
        self.c, self.rng, self.checks, self.unique = col, rng, checks, unique
        self.null_ratio = 0.0 if col["not_null"] or unique else null_ratio
        self.o = override or {}
        self.conv = converter(col["data_type"] or "VARCHAR(255)")
        self.t =(col["data_type"] or "").upper()
        self.n = start - 1
        self.seen: set = set()

    def value(self) -> Any:
        o, rng = self.o, self.rng
        mode = o.get("mode", "auto")
        if mode == "null":
            return None
        if mode == "value":
            return self.conv(o.get("value"))
        if mode == "list":
            vals = [v.strip() for v in str(o.get("values", "")).split(",") if v.strip()]
            return self.conv(rng.choice(vals)) if vals else None
        if mode == "sequence":
            self.n += 1
            return self.n
        if self.null_ratio and rng.random() < self.null_ratio:
            return None
        if "values" in self.checks:
            return rng.choice(self.checks["values"])
        for _ in range(50):
            v = self._random(mode)
            if not self.unique or v not in self.seen:
                self.seen.add(v)
                return v
        raise ValueError(f"cannot find a unique value for {self.c['name']}")

    def _bounds(self, lo: float, hi: float) -> tuple[float, float]:
        o = self.o
        if o.get("mode") == "range":
            lo = float(o.get("min", lo))
            hi = float(o.get("max", hi))
        lo = float(self.checks.get("min", lo))
        hi = float(self.checks.get("max", hi))
        return lo, max(lo, hi)

    def _random(self, mode: str) -> Any:
        rng, t, name = self.rng, self.t, self.c["name"].upper()
        if t.startswith(("SMALLINT", "INTEGER", "BIGINT", "INT128")):
            if self.unique:
                self.n += 1
                return self.n
            limit = {"SMALLINT": 32767, "INTEGER": 2**31 - 1}.get(t.split("(")[0], 2**63 - 1)
            lo, hi = self._bounds(1, min(limit, 100000))
            return rng.randint(int(lo), int(min(hi, limit)))
        m = re.match(r"(NUMERIC|DECIMAL)\((\d+),(\d+)\)", t)
        if m:
            p, s = int(m.group(2)), int(m.group(3))
            maxv = float(10 ** min(p - s, 9) - 1)
            lo, hi = self._bounds(0, min(maxv, 10000))
            if self.unique:
                self.n += 1
                return decimal.Decimal(self.n)
            return decimal.Decimal(str(round(rng.uniform(lo, hi), s))).quantize(decimal.Decimal(1).scaleb(-s))
        if t.startswith(("FLOAT", "DOUBLE", "DECFLOAT", "REAL")):
            lo, hi = self._bounds(0, 10000)
            v = round(rng.uniform(lo, hi), 4)
            return decimal.Decimal(str(v)) if t.startswith("DECFLOAT") else v
        if t.startswith("BOOLEAN"):
            return rng.random() < 0.5
        if t.startswith("TIMESTAMP"):
            base = dt.datetime(2020, 1, 1)
            return base + dt.timedelta(seconds=rng.randint(0, 6 * 365 * 86400))
        if t.startswith("DATE"):
            return dt.date(2020, 1, 1) + dt.timedelta(days=rng.randint(0, 6 * 365))
        if t.startswith("TIME"):
            return dt.time(rng.randint(0, 23), rng.randint(0, 59), rng.randint(0, 59))
        if t.startswith("BLOB") and "TEXT" not in t:
            return bytes(rng.getrandbits(8) for _ in range(rng.randint(8, 64)))
        m = re.match(r"(VAR)?CHAR\((\d+)\)", t)
        size = int(m.group(2)) if m else 400
        if "OCTETS" in t or t.startswith(("BINARY", "VARBINARY")):
            return bytes(rng.getrandbits(8) for _ in range(min(size, 16)))
        return self._text(name, size)

    def _text(self, name: str, size: int) -> str:
        rng = self.rng
        self.n += 1
        like = self.checks.get("like")
        if "EMAIL" in name or (like and "@" in like):
            s = f"{rng.choice(_FIRST).lower()}.{rng.choice(_LAST).lower()}{self.n}@example.com"
        elif "NAME" in name:
            s = f"{rng.choice(_FIRST)} {rng.choice(_LAST)}"
        elif "CITY" in name:
            s = rng.choice(_CITIES)
        elif "PHONE" in name:
            s = "+1 555 " + "".join(rng.choice(string.digits) for _ in range(7))
        elif "CODE" in name or size <= 8:
            s = "".join(rng.choice(string.ascii_uppercase + string.digits) for _ in range(min(size, 8)))
        else:
            s = " ".join(rng.choice(_WORDS) for _ in range(rng.randint(2, 8))).capitalize()
        if self.unique and "EMAIL" not in name:
            suffix = f"-{self.n}"
            s = s[: max(0, size - len(suffix))] + suffix
        return s[:size]


def generate_rows(conn, log, *, table: str, rows: int, overrides: dict | None = None, null_ratio: float = 0.1,
                  seed: int | None = None, result: dict | None = None) -> dict:
    result = result if result is not None else {}
    result.update(inserted=0, failed=0, errors=[], table=table)
    rng = random.Random(seed)
    cat = Catalog(conn)
    cols = cat.columns(table)
    if not cols:
        raise LookupError(f"Table {table!r} not found")
    cons = cat.constraints(table)
    overrides = overrides or {}
    table_checks = [c.get("source", "") for c in cons if c["type"] == "CHECK"]
    domain_checks: dict[str, str] = {}
    for c in cols:
        if c["domain"] and c["domain"] not in domain_checks:
            d = cat._get_domains(c["domain"])
            domain_checks[c["domain"]] = (d or {}).get("properties", {}).get("Check") or ""
    unique_single = {c["columns"][0] for c in cons if c["type"] in ("PRIMARY KEY", "UNIQUE") and len(c["columns"]) == 1}
    composite = [tuple(c["columns"]) for c in cons if c["type"] in ("PRIMARY KEY", "UNIQUE") and len(c["columns"]) > 1]
    cur = conn.cursor()

    # foreign keys: sample existing parent values (composite keys as tuples)
    fk_cols: dict[tuple[str, ...], list[tuple]] = {}
    for c in cons:
        if c["type"] != "FOREIGN KEY":
            continue
        refs = ", ".join(quote_ident(x) for x in c["ref_columns"])
        cur.execute(f"SELECT FIRST 5000 DISTINCT {refs} FROM {quote_ident(c['ref_table'])} "
                    f"WHERE {' AND '.join(quote_ident(x) + ' IS NOT NULL' for x in c['ref_columns'])}")
        fk_cols[tuple(c["columns"])] = [tuple(r) for r in cur.fetchall()]
        if not fk_cols[tuple(c["columns"])]:
            log(f"Parent table {c['ref_table']} is empty: {', '.join(c['columns'])} will be NULL where allowed")

    gens: dict[str, ColumnGen] = {}
    insert_cols: list[dict] = []
    for c in cols:
        o = overrides.get(c["name"]) or {}
        if c["computed"] or o.get("mode") == "skip":
            continue
        if c["identity"] and o.get("mode", "auto") == "auto":
            continue  # generated by the server
        insert_cols.append(c)
        start = 1
        if c["name"] in unique_single and re.match(r"(SMALLINT|INTEGER|BIGINT|INT128|NUMERIC|DECIMAL)",
                                                  (c["data_type"] or "").upper()):
            cur.execute(f"SELECT MAX({quote_ident(c['name'])}) FROM {quote_ident(table)}")
            start = int(cur.fetchone()[0] or 0) + 1
        checks = parse_checks(table_checks + [domain_checks.get(c["domain"] or "", "")], c["name"])
        gens[c["name"]] = ColumnGen(c, rng, checks, null_ratio, c["name"] in unique_single, o, start)
    if not insert_cols:
        raise ValueError("No column to generate (all are computed, identity or skipped)")

    # existing composite key values, to avoid duplicates
    used: dict[tuple, set] = {}
    for key in composite:
        cur.execute(f"SELECT {', '.join(quote_ident(x) for x in key)} FROM {quote_ident(table)}")
        used[key] = {tuple(r) for r in cur.fetchall()}

    names = [c["name"] for c in insert_cols]
    sql = (f"INSERT INTO {quote_ident(table)} (" + ", ".join(quote_ident(n) for n in names) + ") VALUES ("
           + ", ".join("?" for _ in names) + ")")
    stmt = cur.prep(sql)
    for i in range(rows):
        try:
            row: dict[str, Any] = {}
            for fk, parents in fk_cols.items():
                if any((overrides.get(x) or {}).get("mode", "auto") != "auto" for x in fk):
                    continue
                if parents:
                    nullable = all(not next(c for c in cols if c["name"] == x)["not_null"] for x in fk)
                    pick = None if nullable and rng.random() < null_ratio else rng.choice(parents)
                    for j, x in enumerate(fk):
                        row[x] = pick[j] if pick else None
                else:
                    for x in fk:
                        row[x] = None
            for key in composite:
                for _attempt in range(30):
                    for x in key:
                        if x in gens and not any(x in fk for fk in fk_cols):
                            row[x] = gens[x].value()
                    for fk, parents in fk_cols.items():
                        if parents and set(fk) & set(key):
                            pick = rng.choice(parents)
                            for j, x in enumerate(fk):
                                row[x] = pick[j]
                    tup = tuple(row.get(x) for x in key)
                    if tup not in used[key]:
                        used[key].add(tup)
                        break
                else:
                    raise ValueError(f"no unused combination left for ({', '.join(key)})")
            for n in names:
                if n not in row:
                    row[n] = gens[n].value()
            cur.execute(stmt, [row[n] for n in names])
            result["inserted"] += 1
        except Exception as exc:  # noqa: BLE001 - report and continue
            result["failed"] += 1
            if len(result["errors"]) < 50:
                result["errors"].append({"row": i + 1, "message": str(exc).strip().replace("\n", " ")})
        if (i + 1) % 1000 == 0:
            conn.commit()
            log(f"{result['inserted']} / {rows} rows")
    conn.commit()
    log(f"Generated {result['inserted']} row(s) in {table}"
        + (f", {result['failed']} failed" if result["failed"] else ""))
    for e in result["errors"][:10]:
        log(f"  row {e['row']}: {e['message']}")
    return result
