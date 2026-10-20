"""Keyword lists per Firebird version and a small SQL/PSQL tokenizer for finding identifiers."""

from __future__ import annotations

import re
from collections.abc import Iterator
from dataclasses import dataclass
from functools import cache

from .keywords_data import KEYWORDS

VERSIONS = ("3.0", "4.0", "5.0", "6.0")
# On-disk structure of a database created by each version (checked on the test servers by creating a
# database and reading MON$ODS_MAJOR / MON$ODS_MINOR).
ODS = {"3.0": (12, 0), "4.0": (13, 0), "5.0": (13, 1), "6.0": (14, 0)}


def version_key(v: str) -> str:
    """'5.0.1' / '5' / 5 -> '5.0' (the keyword lists are per major version)."""
    major = str(v).split(".")[0]
    key = f"{major}.0"
    if key not in KEYWORDS:
        raise ValueError(f"Unsupported Firebird version {v!r}: choose one of {', '.join(VERSIONS)}")
    return key


@cache
def reserved(version: str) -> frozenset[str]:
    return frozenset(KEYWORDS[version_key(version)]["reserved"])


@cache
def keywords(version: str) -> frozenset[str]:
    return frozenset(KEYWORDS[version_key(version)]["keywords"])


@cache
def new_reserved(source: str, target: str) -> frozenset[str]:
    """Words that are reserved in ``target`` but were not reserved in ``source``."""
    return reserved(target) - reserved(source)


@cache
def new_keywords(source: str, target: str) -> frozenset[str]:
    """Words that are keywords (reserved or not) in ``target`` but not in ``source``."""
    return keywords(target) - keywords(source)


ALL_RESERVED = frozenset().union(*(frozenset(d["reserved"]) for d in KEYWORDS.values()))
_PLAIN = re.compile(r"^[A-Z][A-Z0-9_$]*$")


def qi(name: str) -> str:
    """Quote an identifier when any Firebird version (3 to 6) would need it."""
    name = (name or "").rstrip()
    if _PLAIN.match(name) and name not in ALL_RESERVED:
        return name
    return '"' + name.replace('"', '""') + '"'


# ------------------------------------------------------------------- tokenizer
@dataclass(frozen=True)
class Word:
    text: str       # as written
    upper: str
    pos: int        # offset in the source
    line: int       # 1-based
    before: str     # previous non-blank character ('' at the start)
    after: str      # next non-blank character ('' at the end)


_Q_CLOSE = {"(": ")", "[": "]", "{": "}", "<": ">"}
_WORD_START = re.compile(r"[A-Za-z]")
_WORD = re.compile(r"[A-Za-z][A-Za-z0-9_$]*")


def words(src: str | None) -> Iterator[Word]:
    """Unquoted identifier-like words of a SQL/PSQL text.

    Skips comments (``--`` and ``/* */``), string literals (``'...'`` and ``q'{...}'``) and
    double-quoted identifiers.
    """
    if not src:
        return
    n = len(src)
    i = 0
    line = 1
    last = ""  # last significant character
    pending: list[tuple[str, int, int, str]] = []  # word waiting for its "after" character

    def flush(after: str) -> Iterator[Word]:
        while pending:
            text, pos, ln, before = pending.pop(0)
            yield Word(text, text.upper(), pos, ln, before, after)

    while i < n:
        c = src[i]
        if c == "\n":
            line += 1
            i += 1
            continue
        if c.isspace():
            i += 1
            continue
        if c == "-" and src.startswith("--", i):
            j = src.find("\n", i)
            i = n if j < 0 else j
            continue
        if c == "/" and src.startswith("/*", i):
            j = src.find("*/", i + 2)
            j = n if j < 0 else j + 2
            line += src.count("\n", i, j)
            i = j
            continue
        if c in "qQ" and i + 2 < n and src[i + 1] == "'" and not (i and (src[i - 1].isalnum() or src[i - 1] in "_$")):
            close = _Q_CLOSE.get(src[i + 2], src[i + 2])
            j = src.find(close + "'", i + 3)
            j = n if j < 0 else j + 2
            line += src.count("\n", i, j)
            yield from flush("'")
            last, i = "'", j
            continue
        if c in "'\"":
            j = i + 1
            while j < n:
                if src[j] == c:
                    if j + 1 < n and src[j + 1] == c:  # doubled quote
                        j += 2
                        continue
                    break
                j += 1
            j = min(j + 1, n)
            line += src.count("\n", i, j)
            yield from flush(c)
            last, i = c, j
            continue
        if _WORD_START.match(c) and not (i and (src[i - 1].isalnum() or src[i - 1] in "_$")):
            m = _WORD.match(src, i)
            assert m
            yield from flush(c)
            pending.append((m.group(0), i, line, last))
            last = m.group(0)[-1]
            i = m.end()
            continue
        yield from flush(c)
        last = c
        i += 1
    yield from flush("")


def line_of(src: str, line: int) -> str:
    lines = src.splitlines()
    return lines[line - 1].strip() if 0 < line <= len(lines) else ""
