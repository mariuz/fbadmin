"""Split a Firebird SQL script into individual statements.

Handles:
* ``--`` and ``/* */`` comments
* single-quoted strings (with ``''`` escapes), ``"quoted"`` identifiers and
  ``q'{...}'`` alternative string literals (Firebird 3+)
* isql's ``SET TERM <new> <old>`` command
* PSQL bodies without ``SET TERM``: a ``;`` inside BEGIN/CASE ... END
  does not end the statement, so ``CREATE PROCEDURE`` can be pasted as-is.
"""

from __future__ import annotations

import re
from bisect import bisect_left
from dataclasses import dataclass

_SET_TERM = re.compile(r"^\s*SET\s+TERM\s+(\S+)\s*$", re.IGNORECASE | re.DOTALL)
_Q_CLOSERS = {"(": ")", "[": "]", "{": "}", "<": ">"}


@dataclass
class Statement:
    sql: str
    line: int  # 1-based line where the statement starts
    col: int = 1  # 1-based column of the first character
    offset: int = 0  # character offset in the script


def split_sql(script: str) -> list[Statement]:
    stmts: list[Statement] = []
    term = ";"
    buf: list[str] = []
    depth = 0
    i = 0
    n = len(script)
    buf_start = 0
    newlines = [k for k, c in enumerate(script) if c == "\n"]
    word: list[str] = []
    # PSQL module tracking: after "AS" in CREATE PROCEDURE/... a DECLARE section
    # may contain ";" before the BEGIN, so wait until the body block was seen.
    lead: list[str] = []
    flags = {"as": False, "begin": False, "external": False}

    def is_psql() -> bool:
        if lead[:2] == ["EXECUTE", "BLOCK"]:
            return True
        return bool(lead) and lead[0] in ("CREATE", "ALTER", "RECREATE") and any(
            w in ("PROCEDURE", "FUNCTION", "TRIGGER", "PACKAGE") for w in lead[1:5]
        )

    def waiting_for_body() -> bool:
        return is_psql() and flags["as"] and not flags["begin"] and not flags["external"]

    def flush_word() -> None:
        nonlocal depth
        if not word:
            return
        w = "".join(word).upper()
        word.clear()
        if len(lead) < 5:
            lead.append(w)
        if w in ("BEGIN", "CASE"):
            depth += 1
            if w == "BEGIN":
                flags["begin"] = True
        elif w == "END" and depth > 0:
            depth -= 1
        elif w == "AS" and depth == 0:
            flags["as"] = True
        elif w == "EXTERNAL" and depth == 0:
            flags["external"] = True

    def add(chunk: str, at: int) -> None:
        nonlocal buf_start
        if not buf:
            buf_start = at
        buf.append(chunk)

    def emit() -> None:
        nonlocal buf, depth, term
        flush_word()
        raw = "".join(buf)
        text = raw.strip()
        off = buf_start + (len(raw) - len(raw.lstrip()))
        buf = []
        depth = 0
        lead.clear()
        flags.update({"as": False, "begin": False, "external": False})
        if text:
            m = _SET_TERM.match(text)
            if m:
                term = m.group(1)
            else:
                ln = bisect_left(newlines, off)
                col = off - (newlines[ln - 1] + 1 if ln else 0) + 1
                stmts.append(Statement(text, ln + 1, col, off))

    while i < n:
        ch = script[i]
        nxt = script[i + 1] if i + 1 < n else ""

        # line comment
        if ch == "-" and nxt == "-":
            flush_word()
            j = script.find("\n", i)
            j = n if j == -1 else j
            add(script[i:j], i)
            i = j
            continue
        # block comment
        if ch == "/" and nxt == "*":
            flush_word()
            j = script.find("*/", i + 2)
            j = n if j == -1 else j + 2
            chunk = script[i:j]
            add(chunk, i)
            i = j
            continue
        # q-string: q'{...}'
        if ch in "qQ" and nxt == "'" and not word and i + 2 < n:
            flush_word()
            opener = script[i + 2]
            closer = _Q_CLOSERS.get(opener, opener)
            j = script.find(closer + "'", i + 3)
            j = n if j == -1 else j + 2
            chunk = script[i:j]
            add(chunk, i)
            i = j
            continue
        # quoted string / identifier
        if ch in ("'", '"'):
            flush_word()
            j = i + 1
            while j < n:
                if script[j] == ch:
                    if j + 1 < n and script[j + 1] == ch:
                        j += 2
                        continue
                    break
                j += 1
            j = min(j + 1, n)
            chunk = script[i:j]
            add(chunk, i)
            i = j
            continue
        # terminator
        if script.startswith(term, i):
            flush_word()
            if term != ";" or (depth == 0 and not waiting_for_body()):
                i += len(term)
                emit()
                continue
        if ch.isalnum() or ch in "_$":
            word.append(ch)
        else:
            flush_word()
        add(ch, i)
        i += 1

    emit()
    return stmts


def strip_comments(sql: str) -> str:
    """Remove leading comments/whitespace (used to detect statement kind)."""
    s = sql.lstrip()
    while True:
        if s.startswith("--"):
            nl = s.find("\n")
            s = "" if nl == -1 else s[nl + 1 :].lstrip()
        elif s.startswith("/*"):
            end = s.find("*/")
            s = "" if end == -1 else s[end + 2 :].lstrip()
        else:
            return s


# ---------------------------------------------------------------- parameters
_PSQL_HEAD = re.compile(
    r"^(CREATE\s+(OR\s+ALTER\s+)?|ALTER\s+|RECREATE\s+)(PROCEDURE|FUNCTION|TRIGGER|PACKAGE)\b|^EXECUTE\s+BLOCK\b",
    re.IGNORECASE,
)


def _scan_params(sql: str):
    """Yield (start, end, name) for each parameter marker outside strings and comments.

    ``:name`` gives its name (upper-cased unless quoted), ``?`` gives ``None``.
    """
    i, n = 0, len(sql)
    while i < n:
        ch = sql[i]
        nxt = sql[i + 1] if i + 1 < n else ""
        if ch == "-" and nxt == "-":
            j = sql.find("\n", i)
            i = n if j == -1 else j
        elif ch == "/" and nxt == "*":
            j = sql.find("*/", i + 2)
            i = n if j == -1 else j + 2
        elif ch in "qQ" and nxt == "'" and (i == 0 or not (sql[i - 1].isalnum() or sql[i - 1] in "_$")) and i + 2 < n:
            closer = _Q_CLOSERS.get(sql[i + 2], sql[i + 2])
            j = sql.find(closer + "'", i + 3)
            i = n if j == -1 else j + 2
        elif ch in ("'", '"'):
            j = i + 1
            while j < n:
                if sql[j] == ch:
                    if j + 1 < n and sql[j + 1] == ch:
                        j += 2
                        continue
                    break
                j += 1
            i = j + 1
        elif ch == "?":
            yield i, i + 1, None
            i += 1
        elif ch == ":" and (i == 0 or sql[i - 1] != ":") and nxt != ":" and (nxt.isalpha() or nxt in "_\""):
            j = i + 1
            if nxt == '"':
                k = sql.find('"', j + 1)
                k = n if k == -1 else k + 1
                name = sql[j + 1 : k - 1]
            else:
                k = j
                while k < n and (sql[k].isalnum() or sql[k] in "_$"):
                    k += 1
                name = sql[j:k].upper()
            yield i, k, name
            i = k
        elif ch.isalnum() or ch in "_$":
            # skip a whole word so "q" inside identifiers is not a q-string
            while i < n and (sql[i].isalnum() or sql[i] in "_$"):
                i += 1
        else:
            i += 1


def takes_params(sql: str) -> bool:
    """PSQL modules use :var for local variables - they never take client parameters."""
    return not _PSQL_HEAD.match(strip_comments(sql))


def bind_params(sql: str, counter: list[int]) -> tuple[str, list[str]]:
    """Rewrite ``:name`` markers to ``?`` and return the parameter order.

    Positional ``?`` markers are named ``$1``, ``$2``... continuing ``counter``
    across the statements of a script.
    """
    if not takes_params(sql):
        return sql, []
    out, names, last = [], [], 0
    for start, end, name in _scan_params(sql):
        out.append(sql[last:start])
        out.append("?")
        if name is None:
            counter[0] += 1
            name = f"${counter[0]}"
        names.append(name)
        last = end
    out.append(sql[last:])
    return "".join(out), names


def script_params(script: str) -> list[str]:
    """Unique parameter names used in a script, in order of appearance."""
    seen: list[str] = []
    counter = [0]
    for st in split_sql(script):
        for name in bind_params(st.sql, counter)[1]:
            if name not in seen:
                seen.append(name)
    return seen
