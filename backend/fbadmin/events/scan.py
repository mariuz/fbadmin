"""Find ``POST_EVENT`` calls in PSQL sources to suggest event names.

Only string literals give a name (``POST_EVENT 'ORDER_PAID'``; ``"..."`` too, which is a
string in dialect 1). Calls with an expression (``POST_EVENT :name``) are reported as
dynamic, with the expression text, because the name is only known at run time.
"""

from __future__ import annotations

import re

_WORD = re.compile(r"[A-Za-z0-9_$]")
_KEYWORD = "POST_EVENT"
_Q_CLOSE = {"(": ")", "[": "]", "{": "}", "<": ">"}


def _skip_space(src: str, i: int) -> int:
    n = len(src)
    while i < n:
        if src[i].isspace():
            i += 1
        elif src.startswith("--", i):
            nl = src.find("\n", i)
            i = n if nl == -1 else nl + 1
        elif src.startswith("/*", i):
            end = src.find("*/", i + 2)
            i = n if end == -1 else end + 2
        else:
            break
    return i


def _string_end(src: str, i: int) -> tuple[int, str] | None:
    """If a string literal starts at ``i``: (index after it, its value)."""
    n = len(src)
    ch = src[i] if i < n else ""
    if ch in ("q", "Q") and i + 2 < n and src[i + 1] == "'":
        close = _Q_CLOSE.get(src[i + 2], src[i + 2]) + "'"
        end = src.find(close, i + 3)
        if end == -1:
            return n, src[i + 3:]
        return end + 2, src[i + 3:end]
    if ch in ("'", '"'):
        out = []
        j = i + 1
        while j < n:
            if src[j] == ch:
                if j + 1 < n and src[j + 1] == ch:
                    out.append(ch)
                    j += 2
                    continue
                return j + 1, "".join(out)
            out.append(src[j])
            j += 1
        return n, "".join(out)
    return None


def find_post_events(src: str | None) -> list[dict]:
    """``[{"name": str | None, "expression": str | None}]`` for each POST_EVENT in ``src``."""
    out: list[dict] = []
    if not src:
        return out
    i, n = 0, len(src)
    kw = len(_KEYWORD)
    while i < n:
        j = _skip_space(src, i)
        if j != i:
            i = j
            continue
        q_string = (src[i] in "qQ" and src.startswith("'", i + 1) and (i == 0 or not _WORD.match(src[i - 1])))
        s = _string_end(src, i) if src[i] in "'\"" or q_string else None
        if s is not None:
            i = s[0]
            continue
        if (src[i:i + kw].upper() == _KEYWORD and (i == 0 or not _WORD.match(src[i - 1]))
                and (i + kw >= n or not _WORD.match(src[i + kw]))):
            j = _skip_space(src, i + kw)
            lit = _string_end(src, j) if j < n else None
            if lit is not None:
                k = _skip_space(src, lit[0])
                if k >= n or src[k] == ";":
                    out.append({"name": lit[1], "expression": None})
                    i = lit[0]
                    continue
            end = src.find(";", j)
            expr = src[j:end if end != -1 else n].strip()
            out.append({"name": None, "expression": expr[:200]})
            i = j
            continue
        i += 1
    return out


# Each query returns (object type, name, parent, source). Packages and functions need Firebird 3+;
# every query is tried on its own so older or restricted servers still give partial results.
_QUERIES = [
    ("trigger", "SELECT TRIM(RDB$TRIGGER_NAME), TRIM(RDB$RELATION_NAME), RDB$TRIGGER_SOURCE FROM RDB$TRIGGERS "
                "WHERE COALESCE(RDB$SYSTEM_FLAG, 0) = 0 AND RDB$TRIGGER_SOURCE IS NOT NULL"),
    ("procedure", "SELECT TRIM(RDB$PROCEDURE_NAME), TRIM(RDB$PACKAGE_NAME), RDB$PROCEDURE_SOURCE FROM RDB$PROCEDURES "
                  "WHERE COALESCE(RDB$SYSTEM_FLAG, 0) = 0 AND RDB$PROCEDURE_SOURCE IS NOT NULL"),
    ("function", "SELECT TRIM(RDB$FUNCTION_NAME), TRIM(RDB$PACKAGE_NAME), RDB$FUNCTION_SOURCE FROM RDB$FUNCTIONS "
                 "WHERE COALESCE(RDB$SYSTEM_FLAG, 0) = 0 AND RDB$FUNCTION_SOURCE IS NOT NULL"),
    ("package", "SELECT TRIM(RDB$PACKAGE_NAME), CAST(NULL AS VARCHAR(63)), RDB$PACKAGE_BODY_SOURCE FROM RDB$PACKAGES "
                "WHERE COALESCE(RDB$SYSTEM_FLAG, 0) = 0 AND RDB$PACKAGE_BODY_SOURCE IS NOT NULL"),
]


def _text(v) -> str | None:
    if v is None:
        return None
    if isinstance(v, bytes):
        return v.decode("utf-8", errors="replace")
    if hasattr(v, "read"):
        v = v.read()
        return v.decode("utf-8", errors="replace") if isinstance(v, bytes) else v
    return str(v)


def scan_sources(conn) -> dict:
    """Event names posted from triggers, procedures, functions and package bodies."""
    names: dict[str, list[dict]] = {}
    dynamic: list[dict] = []
    for kind, sql in _QUERIES:
        cur = conn.cursor()
        try:
            cur.execute(sql)
            rows = cur.fetchall()
        except Exception:  # noqa: BLE001 - e.g. no RDB$PACKAGES before Firebird 3
            try:
                conn.rollback()
            except Exception:  # noqa: BLE001
                pass
            continue
        for obj, parent, src in rows:
            where = {"type": kind, "name": obj, "parent": parent or None}
            for hit in find_post_events(_text(src)):
                if hit["name"] is not None:
                    lst = names.setdefault(hit["name"], [])
                    if where not in lst:
                        lst.append(where)
                else:
                    dynamic.append({**where, "expression": hit["expression"]})
    return {"names": [{"name": k, "sources": v} for k, v in sorted(names.items())], "dynamic": dynamic}
