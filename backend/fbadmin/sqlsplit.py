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
from dataclasses import dataclass

_SET_TERM = re.compile(r"^\s*SET\s+TERM\s+(\S+)\s*$", re.IGNORECASE | re.DOTALL)
_Q_CLOSERS = {"(": ")", "[": "]", "{": "}", "<": ">"}


@dataclass
class Statement:
    sql: str
    line: int  # 1-based line where the statement starts


def split_sql(script: str) -> list[Statement]:
    stmts: list[Statement] = []
    term = ";"
    buf: list[str] = []
    depth = 0
    i = 0
    n = len(script)
    line = 1
    start_line: int | None = None
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

    def emit() -> None:
        nonlocal buf, depth, start_line, term
        flush_word()
        text = "".join(buf).strip()
        buf = []
        depth = 0
        lead.clear()
        flags.update({"as": False, "begin": False, "external": False})
        if text:
            m = _SET_TERM.match(text)
            if m:
                term = m.group(1)
            else:
                stmts.append(Statement(text, start_line or line))
        start_line = None

    while i < n:
        ch = script[i]
        nxt = script[i + 1] if i + 1 < n else ""

        # line comment
        if ch == "-" and nxt == "-":
            flush_word()
            j = script.find("\n", i)
            j = n if j == -1 else j
            buf.append(script[i:j])
            i = j
            continue
        # block comment
        if ch == "/" and nxt == "*":
            flush_word()
            j = script.find("*/", i + 2)
            j = n if j == -1 else j + 2
            chunk = script[i:j]
            line += chunk.count("\n")
            buf.append(chunk)
            i = j
            continue
        # q-string: q'{...}'
        if ch in "qQ" and nxt == "'" and not word and i + 2 < n:
            flush_word()
            if start_line is None:
                start_line = line
            opener = script[i + 2]
            closer = _Q_CLOSERS.get(opener, opener)
            j = script.find(closer + "'", i + 3)
            j = n if j == -1 else j + 2
            chunk = script[i:j]
            line += chunk.count("\n")
            buf.append(chunk)
            i = j
            continue
        # quoted string / identifier
        if ch in ("'", '"'):
            flush_word()
            if start_line is None:
                start_line = line
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
            line += chunk.count("\n")
            buf.append(chunk)
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
        if ch == "\n":
            line += 1
        elif start_line is None and not ch.isspace():
            start_line = line
        buf.append(ch)
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
