"""Anonymised copies of production (roadmap 2.3): copy a database to a development or test server with
masking rules per column, consistent across foreign keys.

How a copy is made (one job):

1. the source is backed up through the Services API into a temporary file on the fbadmin host (mode
   0600, deleted as soon as it was restored) and restored into a **scratch database on the source
   server**, next to the source; unmasked data never reaches the target server;
2. the scratch database is **masked in place**: triggers are switched off, foreign keys on masked
   columns are dropped, every row is updated, the foreign keys are created again (which proves the
   masked keys still match) and the triggers switched back on;
3. a few original values of each masked column are looked for in the masked copy (leak check);
4. the masked scratch database is backed up, restored on the **target** server (a new file, or
   replacing a registered copy), registered, and the scratch database is dropped.

Masking is **deterministic**: a value is replaced by a keyed hash (HMAC-SHA256 with the profile's
secret) turned into a value of the right kind - the same e-mail address becomes the same fake address
in every table and in every refresh of the copy. A rule on a column that is part of a foreign key
applies to every column linked to it by foreign keys, so parent and child keys keep matching.

Strategies: ``keep``, ``null``, ``fixed``, ``hash``, ``email``, ``name``, ``first_name``,
``last_name``, ``city``, ``phone``, ``words``, ``redact`` (keep the last characters),
``number_noise`` (± a percentage), ``date_shift`` (± days) and ``shuffle`` (values moved between
rows; not for key columns).

Profiles (the rules of a source database, its target and its secret) are kept in
``<data dir>/anonymise.json`` (mode 0600).
"""

from __future__ import annotations

import datetime as dt
import decimal
import hashlib
import hmac
import json
import os
import random
import re
import secrets
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Literal
from collections.abc import Callable

from pydantic import BaseModel, Field

from .config import settings
from .metadata import Catalog
from .metadata.types import quote_ident

Strategy = Literal["keep", "null", "fixed", "hash", "email", "name", "first_name", "last_name", "city", "phone",
                   "words", "redact", "number_noise", "date_shift", "shuffle"]
SCRATCH_PREFIX = "fbadmin_anon_"
LEAK_SAMPLE = 50

_FIRST = ("Ann Bob Carla Dan Eva Frank Gina Hugo Ida Jon Kim Leo Mia Nina Otto Paul Rita Sam Tina Uma Vera Will "
          "Xena Yuri Zoe Alma Bruno Cleo Dora Emil Flora Gus Hana Ivo Jana Karl Lena Max Nora Olaf").split()
_LAST = ("Smith Jones Brown Garcia Miller Davis Lopez Wilson Moore Taylor Martin Lee White Clark Hall Young King "
         "Wright Scott Green Baker Adams Nelson Hill Campbell Mitchell Roberts Carter Phillips Evans Turner Parker").split()
_CITIES = ("Paris Berlin Rome Madrid Lisbon Vienna Prague Warsaw Oslo Dublin Bucharest Athens Bern Riga Sofia "
           "Tallinn Vilnius Zagreb Ljubljana Brussels Amsterdam Copenhagen Helsinki Stockholm").split()
_WORDS = ("alpha bravo charlie delta echo foxtrot golf hotel india juliet kilo lima mike november oscar papa "
          "quebec romeo sierra tango uniform victor whiskey xray yankee zulu").split()


# ===================================================================== column kinds
def kind_of(data_type: str) -> str:
    t = (data_type or "").upper()
    if t.startswith("BLOB"):
        return "text_blob" if "TEXT" in t or "SUB_TYPE 1" in t else "blob"
    if t.startswith(("VARCHAR", "CHAR", "NCHAR", "NATIONAL", "VARBINARY", "BINARY")):
        return "binary" if "BINARY" in t or "OCTETS" in t else "text"
    if t.startswith(("SMALLINT", "INTEGER", "BIGINT", "INT128")):
        return "int"
    if t.startswith(("NUMERIC", "DECIMAL")):
        return "decimal"
    if t.startswith(("FLOAT", "DOUBLE", "REAL", "DECFLOAT")):
        return "float"
    if t.startswith("TIMESTAMP"):
        return "timestamp"
    if t.startswith("DATE"):
        return "date"
    if t.startswith("TIME"):
        return "time"
    if t.startswith("BOOLEAN"):
        return "bool"
    return "other"


def length_of(data_type: str) -> int | None:
    m = re.match(r"^\s*(?:VAR)?(?:N?CHAR|CHARACTER|BINARY)\w*\s*\((\d+)\)", data_type or "", re.I)
    return int(m.group(1)) if m else None


ALLOWED: dict[str, set[str]] = {
    "text": {"keep", "null", "fixed", "hash", "email", "name", "first_name", "last_name", "city", "phone", "words",
             "redact", "shuffle"},
    "text_blob": {"keep", "null", "fixed", "words", "hash", "shuffle"},
    "blob": {"keep", "null"},
    "binary": {"keep", "null", "hash"},
    "int": {"keep", "null", "fixed", "hash", "number_noise", "shuffle"},
    "decimal": {"keep", "null", "fixed", "number_noise", "shuffle"},
    "float": {"keep", "null", "fixed", "number_noise", "shuffle"},
    "date": {"keep", "null", "fixed", "date_shift", "shuffle"},
    "timestamp": {"keep", "null", "fixed", "date_shift", "shuffle"},
    "time": {"keep", "null", "fixed", "shuffle"},
    "bool": {"keep", "null", "fixed", "shuffle"},
    "other": {"keep", "null"},
}


# ===================================================================== masking functions
class Rule(BaseModel):
    strategy: Strategy = "keep"
    value: Any = None            # fixed
    keep: int = Field(default=4, ge=0, le=100)          # redact: characters kept at the end
    percent: float = Field(default=10, ge=0, le=1000)   # number_noise
    days: int = Field(default=30, ge=0, le=36500)       # date_shift


def _digest(secret: bytes, salt: str, v: Any) -> bytes:
    if isinstance(v, (bytes, bytearray)):
        raw = bytes(v)
    elif isinstance(v, float) and v.is_integer():
        raw = repr(int(v)).encode()
    else:
        raw = str(v).encode("utf-8")
    return hmac.new(secret, salt.encode() + b"\x00" + raw, hashlib.sha256).digest()


def _n(d: bytes, k: int = 0) -> int:
    return int.from_bytes(d[k:k + 8], "big")


def _fit(s: str, length: int | None) -> str:
    return s if not length else s[:length]


def masker(rule: Rule, kind: str, length: int | None, secret: bytes, unique: bool = False) -> Callable[[Any], Any]:
    """A function that masks one value. Deterministic except ``shuffle`` (handled by the caller)."""
    st = rule.strategy
    salt = st  # the same strategy gives the same output for the same input, whatever the column

    def suffix(d: bytes) -> str:
        return "-" + d.hex()[:6] if unique else ""

    if st == "null":
        return lambda v: None
    if st == "fixed":
        return lambda v: None if v is None else rule.value
    if st == "hash":
        def f(v):
            if v is None:
                return None
            d = _digest(secret, salt, v)
            if kind == "int":
                return _n(d) % 2_000_000_000 + 1
            if kind == "binary":
                return d[: length or 16]
            if kind == "text_blob":
                return d.hex()
            n = max(1, min(length or 16, 16))
            return d.hex()[:n] if (length or 16) < 6 else _fit("x" + d.hex()[:n], length)
        return f
    if st == "email":
        def f(v):
            if v is None:
                return None
            d = _digest(secret, salt, v)
            return _fit(f"user{d.hex()[:10]}@example.invalid", length)
        return f
    if st in ("name", "first_name", "last_name", "city"):
        def f(v):
            if v is None:
                return None
            d = _digest(secret, salt, v)
            first, last, city = _FIRST[_n(d) % len(_FIRST)], _LAST[_n(d, 8) % len(_LAST)], _CITIES[_n(d, 16) % len(_CITIES)]
            s = {"name": f"{first} {last}", "first_name": first, "last_name": last, "city": city}[st]
            return _fit(s + suffix(d), length)
        return f
    if st == "phone":
        def f(v):
            if v is None:
                return None
            d = _digest(secret, salt, v)
            digits = iter(str(_n(d)) + str(_n(d, 8)) + str(_n(d, 16)))
            return "".join(next(digits) if ch.isdigit() else ch for ch in str(v))
        return f
    if st == "words":
        def f(v):
            if v is None:
                return None
            d = _digest(secret, salt, v)
            count = max(1, min(len(str(v).split()), 60))
            rnd = random.Random(d)
            s = " ".join(rnd.choice(_WORDS) for _ in range(count))
            return _fit(s + suffix(d), length)
        return f
    if st == "redact":
        def f(v):
            if v is None:
                return None
            s = str(v)
            k = min(rule.keep, len(s))
            head, tail = s[: len(s) - k], s[len(s) - k:] if k else ""
            return "".join("9" if ch.isdigit() else "X" if ch.isalpha() else ch for ch in head) + tail
        return f
    if st == "number_noise":
        def f(v):
            if v is None:
                return None
            d = _digest(secret, salt, v)
            factor = 1 + ((_n(d) % 2_000_001) / 1_000_000 - 1) * rule.percent / 100
            if kind == "int":
                return int(round(v * factor))
            if isinstance(v, decimal.Decimal):
                exp = v.as_tuple().exponent
                q = decimal.Decimal(1).scaleb(exp if isinstance(exp, int) else 0)
                return (v * decimal.Decimal(str(factor))).quantize(q, rounding=decimal.ROUND_HALF_UP)
            return float(v) * factor
        return f
    if st == "date_shift":
        def f(v):
            if v is None:
                return None
            d = _digest(secret, salt, v)
            shift = (_n(d) % (2 * rule.days + 1)) - rule.days if rule.days else 0
            if shift == 0 and rule.days:
                shift = rule.days
            return v + dt.timedelta(days=shift)
        return f
    return lambda v: v  # keep (and shuffle, done by the caller)


# ===================================================================== analysis
_SUGGEST: list[tuple[str, str, dict[str, Any]]] = [
    (r"E_?MAIL", "email", {}),
    (r"(^|_)(FIRST_?NAME|GIVEN_?NAME|FORENAME)($|_)", "first_name", {}),
    (r"(^|_)(LAST_?NAME|SURNAME|FAMILY_?NAME)($|_)", "last_name", {}),
    (r"(^|_)(FULL_?NAME|CUSTOMER_?NAME|CONTACT_?NAME|PERSON_?NAME|EMPLOYEE_?NAME)($|_)|^NAME$", "name", {}),
    (r"PHONE|MOBILE|FAX|TEL(EPHONE)?($|_)", "phone", {}),
    (r"(^|_)(CITY|TOWN)($|_)", "city", {}),
    (r"ADDRESS|STREET|ADDR($|_)", "words", {}),
    (r"BIRTH|(^|_)DOB($|_)", "date_shift", {"days": 180}),
    (r"IBAN|CARD|SSN|SOCIAL|TAX_?ID|NATIONAL_?ID|PASSPORT|ACCOUNT_?(NO|NUM)|CNP|BSN", "redact", {"keep": 4}),
    (r"PASSWORD|PASSWD|PWD|SECRET|TOKEN|API_?KEY|SALT", "null", {}),
    (r"SALARY|INCOME|WAGE|BONUS", "number_noise", {"percent": 15}),
    (r"(^|_)(NOTE|NOTES|COMMENT|COMMENTS|REMARK|REMARKS)($|_)", "words", {}),
    (r"(^|_)(IP|IP_?ADDRESS)($|_)", "null", {}),
]


def suggest(name: str, kind: str, not_null: bool) -> dict[str, Any] | None:
    for pat, strategy, params in _SUGGEST:
        if re.search(pat, name.upper()) and strategy in ALLOWED.get(kind, set()):
            if strategy == "null" and not_null:
                strategy, params = ("hash", {}) if "hash" in ALLOWED[kind] else ("keep", {})
                if strategy == "keep":
                    return None
            return {"strategy": strategy, **params}
    return None


def _uf_find(parent: dict, x):
    while parent[x] != x:
        parent[x] = parent[parent[x]]
        x = parent[x]
    return x


def analyze(conn) -> dict[str, Any]:
    """Tables and columns of the database, with their kind, keys, suggested rules and foreign-key groups."""
    cat = Catalog(conn)
    tables = []
    parent: dict[tuple[str, str], tuple[str, str]] = {}
    fks: list[dict] = []
    names = [t["name"] for t in cat.list_objects("tables")]
    for t in names:
        cons = cat.constraints(t)
        keys = {c for k in cons if k["type"] in ("PRIMARY KEY", "UNIQUE") for c in k["columns"]}
        single_unique = {k["columns"][0] for k in cons if k["type"] in ("PRIMARY KEY", "UNIQUE") and len(k["columns"]) == 1}
        cols = []
        for c in cat.columns(t):
            if c.get("computed"):
                continue
            kind = kind_of(c["data_type"])
            nn = bool(c.get("not_null") or c.get("col_not_null"))
            cols.append({"name": c["name"], "data_type": c["data_type"], "kind": kind, "length": length_of(c["data_type"]),
                         "not_null": nn, "key": c["name"] in keys, "unique": c["name"] in single_unique,
                         "identity": bool(c.get("identity")), "allowed": sorted(ALLOWED.get(kind, {"keep"})),
                         "suggested": suggest(c["name"], kind, nn)})
            parent.setdefault((t, c["name"]), (t, c["name"]))
        for k in cons:
            if k["type"] == "FOREIGN KEY":
                fks.append({"table": t, "name": k["name"], "columns": k["columns"], "ref_table": k["ref_table"],
                            "ref_columns": k["ref_columns"]})
        tables.append({"name": t, "columns": cols})
    for k in fks:
        for a, b in zip(k["columns"], k["ref_columns"] or [], strict=False):
            x, y = (k["table"], a), (k["ref_table"], b)
            parent.setdefault(x, x)
            parent.setdefault(y, y)
            rx, ry = _uf_find(parent, x), _uf_find(parent, y)
            if rx != ry:
                parent[rx] = ry
    groups: dict[tuple[str, str], list[str]] = {}
    for node in parent:
        groups.setdefault(_uf_find(parent, node), []).append(f"{node[0]}.{node[1]}")
    linked = {m: sorted(g) for g in groups.values() if len(g) > 1 for m in g}
    for t in tables:
        for c in t["columns"]:
            c["linked"] = [x for x in linked.get(f"{t['name']}.{c['name']}", []) if x != f"{t['name']}.{c['name']}"]
    return {"tables": tables, "foreign_keys": fks, "engine_version": cat.engine_version}


def effective_rules(rules: dict[str, dict[str, dict]], schema: dict[str, Any]) -> dict[str, dict[str, Rule]]:
    """Rules per table and column, with each rule copied to the columns linked to it by foreign keys.
    Raises ValueError for rules that do not fit their column or conflict across a foreign key."""
    cols = {f"{t['name']}.{c['name']}": c for t in schema["tables"] for c in t["columns"]}
    out: dict[str, dict[str, Rule]] = {}
    origin: dict[str, str] = {}
    problems = []
    for table, cr in (rules or {}).items():
        for col, raw in (cr or {}).items():
            r = Rule(**raw) if not isinstance(raw, Rule) else raw
            if r.strategy == "keep":
                continue
            key = f"{table}.{col}"
            meta = cols.get(key)
            if meta is None:
                problems.append(f"{key}: no such column")
                continue
            for member in [key] + meta["linked"]:
                m = cols[member]
                if r.strategy not in ALLOWED.get(m["kind"], set()):
                    problems.append(f"{member} ({m['data_type']}): '{r.strategy}' does not fit this column")
                    continue
                if r.strategy == "null" and m["not_null"]:
                    problems.append(f"{member}: NOT NULL, it cannot be emptied")
                    continue
                if r.strategy == "shuffle" and (m["key"] or m["linked"]):
                    problems.append(f"{member}: a key column cannot be shuffled (its rows would point elsewhere)")
                    continue
                if member in origin and out[member.split(".", 1)[0]][member.split(".", 1)[1]] != r:
                    problems.append(f"{member}: linked by foreign keys to {origin[member]} and {key}, which have "
                                    "different rules")
                    continue
                origin.setdefault(member, key)
                t, c = member.split(".", 1)
                out.setdefault(t, {})[c] = r
    if problems:
        raise ValueError("Masking rules: " + "; ".join(problems))
    return out


# ===================================================================== masking
def _sample(cur, table: str, col: str, n: int = LEAK_SAMPLE) -> list[Any]:
    cur.execute(f"SELECT FIRST {n} DISTINCT {quote_ident(col)} FROM {quote_ident(table)} "
                f"WHERE {quote_ident(col)} IS NOT NULL")
    return [r[0] for r in cur.fetchall() if not isinstance(r[0], (bytes, bytearray)) or len(r[0]) < 256]


def mask_database(connect: Callable[[], Any], rules: dict[str, dict[str, Rule]], secret: bytes,
                  log: Callable[[str], None], check: Callable[[], None] = lambda: None) -> dict[str, Any]:
    """Mask the database ``connect()`` opens, in place (it must be a copy!). Returns per-table counts and the
    leak check. Every connection opened here is closed again (the scratch copy is dropped right after)."""
    opened: list[Any] = []

    def fresh():
        c = connect()
        opened.append(c)
        return c

    try:
        return _mask(fresh, rules, secret, log, check)
    finally:
        for c in opened:
            try:
                c.close()
            except Exception:  # noqa: BLE001 - already closed
                pass


def _mask(fresh: Callable[[], Any], rules: dict[str, dict[str, Rule]], secret: bytes, log: Callable[[str], None],
          check: Callable[[], None]) -> dict[str, Any]:
    conn = fresh()
    schema = analyze(conn)
    meta = {t["name"]: {c["name"]: c for c in t["columns"]} for t in schema["tables"]}
    cat = Catalog(conn)
    cur = conn.cursor()
    result: dict[str, Any] = {"tables": {}, "columns": 0, "rows": 0, "leaks": [], "foreign_keys": 0}

    # originals to look for afterwards (skip strategies that may keep a value on purpose)
    samples = {}
    for t, cr in rules.items():
        for c, r in cr.items():
            # values that may legitimately equal another row's original are not checked
            if r.strategy not in ("fixed", "shuffle", "redact", "number_noise", "date_shift") \
                    and meta[t][c]["kind"] not in ("blob", "text_blob"):
                samples[(t, c)] = _sample(cur, t, c)
    conn.commit()

    # triggers off (remember which were active)
    cur.execute("SELECT TRIM(RDB$TRIGGER_NAME) FROM RDB$TRIGGERS WHERE COALESCE(RDB$SYSTEM_FLAG, 0) = 0 "
                "AND COALESCE(RDB$TRIGGER_INACTIVE, 0) = 0 AND RDB$RELATION_NAME IS NOT NULL")
    active = [r[0] for r in cur.fetchall()]
    for trg in active:
        cur.execute(f"ALTER TRIGGER {quote_ident(trg)} INACTIVE")
    conn.commit()
    if active:
        log(f"Switched off {len(active)} trigger(s) while masking")

    # foreign keys on masked columns: dropped now, created again afterwards
    masked = {(t, c) for t, cr in rules.items() for c in cr}
    dropped: list[tuple[str, str]] = []
    for fk in schema["foreign_keys"]:
        if any((fk["table"], c) in masked for c in fk["columns"]) or \
                any((fk["ref_table"], c) in masked for c in fk["ref_columns"] or []):
            con = next(k for k in cat.constraints(fk["table"]) if k["name"] == fk["name"])
            dropped.append((fk["table"], f"ALTER TABLE {quote_ident(fk['table'])} ADD {cat._constraint_ddl(con)}"))
            cur.execute(f"ALTER TABLE {quote_ident(fk['table'])} DROP CONSTRAINT {quote_ident(fk['name'])}")
    conn.commit()
    if dropped:
        log(f"Dropped {len(dropped)} foreign key(s) on masked columns")
    # a new attachment: Firebird 6 keeps enforcing a dropped constraint in the attachment that dropped it
    conn.close()
    conn = fresh()
    cur = conn.cursor()

    try:
        for t, cr in rules.items():
            check()
            cols = list(cr)
            fns = {c: masker(cr[c], meta[t][c]["kind"], meta[t][c]["length"], secret, meta[t][c]["unique"]) for c in cols}
            shuffled = [c for c in cols if cr[c].strategy == "shuffle"]
            sel = ", ".join(quote_ident(c) for c in cols)
            upd = conn.cursor()
            stmt = upd.prep(f"UPDATE {quote_ident(t)} SET " + ", ".join(f"{quote_ident(c)} = ?" for c in cols)
                            + " WHERE RDB$DB_KEY = ?")
            cur.execute(f"SELECT RDB$DB_KEY, {sel} FROM {quote_ident(t)}")
            rows = cur.fetchall() if shuffled else None
            pools = {}
            if shuffled and rows is not None:
                for c in shuffled:
                    i = cols.index(c) + 1
                    vals = [r[i] for r in rows]
                    random.SystemRandom().shuffle(vals)
                    pools[c] = iter(vals)
            n = 0
            cache: dict[tuple[str, Any], Any] = {}
            batches = [rows] if rows is not None else iter(lambda: cur.fetchmany(500), [])
            for batch in batches:
                for r in batch:
                    vals = []
                    for i, c in enumerate(cols, start=1):
                        v = r[i]
                        if c in pools:
                            vals.append(next(pools[c]))
                            continue
                        k = (c, v if not isinstance(v, (bytes, bytearray)) else bytes(v))
                        try:
                            if k not in cache:
                                if len(cache) > 200_000:
                                    cache.clear()
                                cache[k] = fns[c](v)
                            vals.append(cache[k])
                        except TypeError:  # unhashable value
                            vals.append(fns[c](v))
                    upd.execute(stmt, vals + [r[0]])
                    n += 1
                    if n % 5000 == 0:
                        log(f"{t}: {n} rows")
            conn.commit()
            result["tables"][t] = {"rows": n, "columns": {c: cr[c].strategy for c in cols}}
            result["rows"] += n
            result["columns"] += len(cols)
            log(f"{t}: masked {len(cols)} column(s) in {n} row(s)")
    finally:
        # foreign keys and triggers back, even after a failure (the scratch copy is dropped anyway)
        fk_errors = []
        for t, ddl in dropped:
            try:
                cur.execute(ddl)
                conn.commit()
                result["foreign_keys"] += 1
            except Exception as exc:  # noqa: BLE001
                conn.rollback()
                fk_errors.append(f"{t}: {' '.join(str(exc).split())}")
        for trg in active:
            cur.execute(f"ALTER TRIGGER {quote_ident(trg)} ACTIVE")
        conn.commit()
    if fk_errors:
        raise RuntimeError("The masked keys no longer match: " + "; ".join(fk_errors))
    if dropped:
        log(f"Created the {len(dropped)} foreign key(s) again: the masked keys still match")

    # leak check: originals still present in the masked copy
    for (t, c), vals in samples.items():
        if not vals:
            continue
        found = 0
        for chunk in range(0, len(vals), 20):
            part = vals[chunk:chunk + 20]
            cur.execute(f"SELECT COUNT(*) FROM {quote_ident(t)} WHERE {quote_ident(c)} IN ("
                        + ", ".join("?" for _ in part) + ")", part)
            found += cur.fetchone()[0]
        if found:
            result["leaks"].append({"table": t, "column": c, "rows": found, "sampled": len(vals)})
    conn.commit()
    log("Leak check: " + ("no original value found in the masked columns" if not result["leaks"] else
                          f"{len(result['leaks'])} column(s) still contain original values"))
    return result


# ===================================================================== profiles
class Target(BaseModel):
    server_id: str
    path: str = Field(min_length=1, max_length=1000)        # database file on the target server
    register_as: str | None = Field(default=None, max_length=200)
    replace: bool = False                                    # replace an existing file (a registered copy)


class Profile(BaseModel):
    id: str = Field(default_factory=lambda: uuid.uuid4().hex[:10])
    name: str
    server_id: str
    database_id: str
    rules: dict[str, dict[str, Rule]] = Field(default_factory=dict)
    target: Target | None = None
    scratch_path: str | None = None       # on the source server; default: next to the source database
    secret: str = Field(default_factory=lambda: secrets.token_hex(32))
    owner: str | None = None
    created_at: float = Field(default_factory=time.time)
    updated_at: float = Field(default_factory=time.time)
    last_run: dict[str, Any] | None = None


class ProfileStore:
    def __init__(self, path: Path):
        self.path = path
        self._lock = threading.Lock()
        self.profiles: dict[str, Profile] = {}
        if path.exists():
            for d in json.loads(path.read_text("utf-8") or "{}").get("profiles", []):
                p = Profile(**d)
                self.profiles[p.id] = p

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"profiles": [p.model_dump() for p in self.profiles.values()]}, indent=1, default=str))
        os.chmod(tmp, 0o600)
        os.replace(tmp, self.path)

    def list(self, server_id: str | None = None, database_id: str | None = None) -> list[Profile]:
        with self._lock:
            return [p for p in self.profiles.values()
                    if (not server_id or p.server_id == server_id) and (not database_id or p.database_id == database_id)]

    def get(self, pid: str) -> Profile:
        with self._lock:
            if pid not in self.profiles:
                raise KeyError(f"Unknown anonymisation profile {pid!r}")
            return self.profiles[pid]

    def put(self, p: Profile) -> Profile:
        p.updated_at = time.time()
        with self._lock:
            self.profiles[p.id] = p
            self._save()
        return p

    def delete(self, pid: str) -> None:
        with self._lock:
            self.profiles.pop(pid, None)
            self._save()

    def set_last_run(self, pid: str, info: dict[str, Any]) -> None:
        with self._lock:
            if pid in self.profiles:
                self.profiles[pid].last_run = info
                self._save()


_store: ProfileStore | None = None
_store_lock = threading.Lock()


def get_store() -> ProfileStore:
    global _store
    path = Path(settings.data_dir) / "anonymise.json"
    with _store_lock:
        if _store is None or _store.path != path:
            _store = ProfileStore(path)
        return _store


def public(p: Profile) -> dict[str, Any]:
    d = p.model_dump(exclude={"secret"})
    d["rule_count"] = sum(1 for cr in p.rules.values() for r in cr.values() if r.strategy != "keep")
    return d


# ===================================================================== the copy
def _same(a: str, b: str) -> bool:
    return a.replace("\\", "/").rstrip("/").lower() == b.replace("\\", "/").rstrip("/").lower()


def scratch_for(db_path: str, run_id: str, override: str | None) -> str:
    if override and override.strip():
        return override.strip()
    m = re.match(r"^(.*[\\/])[^\\/]+$", db_path)
    if not m:
        raise ValueError(f"The source is registered by alias ({db_path!r}), so fbadmin cannot tell its directory: "
                         "set a scratch path on the source server in the profile.")
    return f"{m.group(1)}{SCRATCH_PREFIX}{run_id}.fdb"


def run_copy(profile_id: str, log: Callable[[str], None]) -> dict[str, Any]:
    """Make the anonymised copy described by a profile (see the module doc). Runs inside a job."""
    from . import jobs
    from .api.deps import get_state
    from .connections import connect
    from .maintenance import ops

    st = get_state()
    p = get_store().get(profile_id)
    if p.target is None:
        raise ValueError("The profile has no target: choose the server and the file of the copy")
    src_srv = st.store.get(p.server_id)
    src_db = st.store.get_database(p.server_id, p.database_id)
    tgt_srv = st.store.get(p.target.server_id)
    tgt_path = p.target.path.strip()
    if p.target.server_id == p.server_id and _same(tgt_path, src_db.path):
        raise ValueError("The target is the source database")
    registered_tgt = next((d for d in tgt_srv.databases if _same(d.path, tgt_path)), None)
    run_id = uuid.uuid4().hex[:8]
    scratch = scratch_for(src_db.path, run_id, p.scratch_path)
    if any(_same(d.path, scratch) for d in src_srv.databases):
        raise ValueError(f"The scratch path {scratch!r} is a registered database; choose another one")
    work = Path(settings.data_dir) / "anonymise" / "work"
    work.mkdir(parents=True, exist_ok=True)
    os.chmod(work, 0o700)
    raw_fbk, masked_fbk = work / f"{run_id}-source.fbk", work / f"{run_id}-masked.fbk"
    src_pw = st.manager._password(p.server_id)  # the user's own saved password (server mode: per user)
    started = time.time()
    scratch_made = False
    result: dict[str, Any] = {"run_id": run_id, "target": tgt_path}
    try:
        with st.manager.service(p.server_id) as svc:
            major = ops.option_major(svc)
            log(f"1/5 Backing up {src_db.name} …")
            ops.backup_to_local_file(svc, src_db.path, raw_fbk, ops.BackupOptions(), major, log)
        jobs.check()
        with st.manager.service(p.server_id) as svc:
            log(f"2/5 Restoring it as a scratch copy on the source server: {scratch}")
            scratch_made = True
            ops.restore_from_local_file(svc, raw_fbk, scratch, ops.RestoreOptions(replace=False), major, log)
        raw_fbk.unlink(missing_ok=True)
        log("The unmasked backup file was deleted")
        jobs.check()
        log("3/5 Masking the scratch copy …")
        conn = connect(src_srv, scratch, src_pw)
        try:
            schema = analyze(conn)
        finally:
            conn.close()
        rules = effective_rules({t: {c: r.model_dump() for c, r in cr.items()} for t, cr in p.rules.items()}, schema)
        if not rules:
            log("No masking rules: the copy is not anonymised")
        masked = mask_database(lambda: connect(src_srv, scratch, src_pw), rules, bytes.fromhex(p.secret), log,
                               jobs.check)
        result.update(masked)
        jobs.check()
        with st.manager.service(p.server_id) as svc:
            log("4/5 Backing up the masked copy …")
            ops.backup_to_local_file(svc, scratch, masked_fbk, ops.BackupOptions(), major, log)
        connect(src_srv, scratch, src_pw).drop_database()
        scratch_made = False
        log("Dropped the scratch copy")
        jobs.check()
        if registered_tgt is not None:
            st.manager.disconnect(p.target.server_id, registered_tgt.id)
        with st.manager.service(p.target.server_id) as svc:
            tmajor = ops.option_major(svc)
            log(f"5/5 Restoring the masked copy on {tgt_srv.name}: {tgt_path}")
            ops.restore_from_local_file(svc, masked_fbk, tgt_path, ops.RestoreOptions(replace=p.target.replace),
                                        tmajor, log)
        if registered_tgt is None and p.target.register_as:
            from .store import DatabaseEntry

            entry = st.store.add_database(p.target.server_id, DatabaseEntry(name=p.target.register_as, path=tgt_path))
            result["database_id"] = getattr(entry, "id", None)
            log(f"Registered the copy as {p.target.register_as!r}")
        elif registered_tgt is not None:
            result["database_id"] = registered_tgt.id
        result["seconds"] = round(time.time() - started, 1)
        log(f"Done in {result['seconds']} s: {result['columns']} column(s) masked in {len(result['tables'])} table(s)")
        get_store().set_last_run(profile_id, {"at": time.time(), "ok": True, "rows": result["rows"],
                                              "columns": result["columns"], "leaks": result["leaks"],
                                              "tables": result["tables"], "foreign_keys": result["foreign_keys"],
                                              "seconds": result["seconds"], "target": tgt_path,
                                              "database_id": result.get("database_id")})
        _timeline(p, src_db, tgt_srv, tgt_path, result, None)
        return result
    except Exception as exc:
        get_store().set_last_run(profile_id, {"at": time.time(), "ok": False, "error": " ".join(str(exc).split()),
                                              "target": tgt_path})
        _timeline(p, src_db, tgt_srv, tgt_path, None, " ".join(str(exc).split()))
        raise
    finally:
        raw_fbk.unlink(missing_ok=True)
        masked_fbk.unlink(missing_ok=True)
        if scratch_made:  # never leave unmasked data behind on the source server
            try:
                connect(src_srv, scratch, src_pw).drop_database()
                log("Dropped the scratch copy")
            except Exception:  # noqa: BLE001 - not created, or already gone
                ops.drop_partial(lambda: connect(src_srv, scratch, src_pw), scratch, log)


def _timeline(p: Profile, src_db, tgt_srv, tgt_path: str, result: dict | None, error: str | None) -> None:
    try:
        from . import timeline

        title = f"Anonymised copy to {tgt_srv.name}: {tgt_path}"
        detail = None if result is None else (f"{result['columns']} column(s) masked in {len(result['tables'])} "
                                              f"table(s), {result['rows']} row(s); leaks found: {len(result['leaks'])}")
        timeline.get_store().add(p.server_id, p.database_id, "backup", "anonymised_copy", title, detail=detail,
                                 status="failed" if error else "ok", error=error)
    except Exception:  # noqa: BLE001
        pass
