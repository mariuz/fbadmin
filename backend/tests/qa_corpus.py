"""Helpers for the firebird-qa compatibility tests (see test_firebird_qa.py).

The test files of https://github.com/FirebirdSQL/firebird-qa are Python modules holding isql
scripts in string variables (``test_script``, ``init_script``, ...). They are read with
``ast`` only - never imported or executed.
"""

from __future__ import annotations

import ast
import os
import re
import subprocess
import warnings
from pathlib import Path

SCRIPT_VARS = {"test_script", "init_script", "init_sql", "test_sql", "init_ddl", "ddl_script", "sql_init"}
QA_REPO = "https://github.com/FirebirdSQL/firebird-qa"

# isql client commands (not SQL): dropped before running a script through fbadmin.
_ISQL_ONLY = re.compile(
    r"^(SET\s+(LIST|COUNT|BAIL|ECHO|HEADING|PLAN|PLANONLY|STATS|PER_TABLE_STATS|WIDTH|AUTODDL|SQLDA_DISPLAY|"
    r"BLOB(DISPLAY)?|NAMES|SQL\s+DIALECT|WARNINGS|WNG|TIME|EXPLAIN|EXEC_PATH_DISPLAY|KEEP_TRAN_PARAMS|"
    r"MAXROWS|LOCAL_TIMEOUT|DECFLOAT|TERM|ROWCOUNT|PLANONLY|TRANSACTION|AUTO)\b|SHOW\b|IN\b|INPUT\b|OUTPUT\b|"
    r"CONNECT\b|CREATE\s+DATABASE\b|DROP\s+DATABASE\b|QUIT\b|EXIT\b|SHELL\b|EDIT\b|HELP\b|COMMIT\b|ROLLBACK\b|"
    r"SET\s+STATISTICS\b|ALTER\s+DATABASE\b|ALTER\s+EXTERNAL\b|CREATE\s+SHADOW\b|DROP\s+SHADOW\b|"
    r"CREATE\s+(OR\s+ALTER\s+)?USER\b|ALTER\s+USER\b|DROP\s+USER\b|CREATE\s+MAPPING\b|SET\s+BIND\b)",
    re.IGNORECASE,
)


def qa_checkout() -> Path | None:
    """Locate (or shallow-clone, when FBADMIN_QA_CLONE=1) a firebird-qa checkout."""
    env = os.environ.get("FBADMIN_QA_DIR")
    if env:
        p = Path(env)
        return p if (p / "tests").is_dir() else None
    if os.environ.get("FBADMIN_QA_CLONE") != "1":
        return None
    dest = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")) / "fbadmin" / "firebird-qa"
    if not (dest / "tests").is_dir():
        dest.parent.mkdir(parents=True, exist_ok=True)
        try:
            subprocess.run(["git", "clone", "-q", "--depth", "1", QA_REPO, str(dest)], check=True, timeout=600)
        except (OSError, subprocess.SubprocessError):
            return None
    return dest


def extract_scripts(path: Path) -> list[str]:
    """String constants assigned to the well-known script variables of a QA test module."""
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")  # invalid escape sequences in some QA modules
            tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
    except (SyntaxError, ValueError):
        return []
    out = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
            if any(isinstance(t, ast.Name) and t.id in SCRIPT_VARS for t in node.targets):
                out.append(node.value.value)
    return out


def corpus(root: Path, areas: tuple[str, ...] = ()) -> list[tuple[str, str]]:
    """[(relative test path, script)] for all scripts under tests/ (or the given functional areas)."""
    base = root / "tests"
    dirs = [base / "functional" / a for a in areas] if areas else [base]
    items = []
    for d in dirs:
        for f in sorted(d.rglob("test_*.py")):
            for s in extract_scripts(f):
                items.append((str(f.relative_to(root)), s))
    return items


def reference_split(script: str) -> list[str]:
    """isql's splitting rules: statements end at the current terminator outside strings and
    comments, ``SET TERM`` changes the terminator. Used as the oracle for split_sql()."""
    out: list[str] = []
    term = ";"
    buf: list[str] = []
    i, n = 0, len(script)
    while i < n:
        ch = script[i]
        if script.startswith("--", i):
            j = script.find("\n", i)
            j = n if j == -1 else j
            buf.append(script[i:j])
            i = j
        elif script.startswith("/*", i):
            j = script.find("*/", i + 2)
            j = n if j == -1 else j + 2
            buf.append(script[i:j])
            i = j
        elif ch in "qQ" and script.startswith("'", i + 1) and (i == 0 or not (script[i - 1].isalnum() or script[i - 1] in "_$")) and i + 2 < n:
            closer = {"(": ")", "[": "]", "{": "}", "<": ">"}.get(script[i + 2], script[i + 2])
            j = script.find(closer + "'", i + 3)
            j = n if j == -1 else j + 2
            buf.append(script[i:j])
            i = j
        elif ch in "'\"":
            j = i + 1
            while j < n:
                if script[j] == ch:
                    if j + 1 < n and script[j + 1] == ch:
                        j += 2
                        continue
                    break
                j += 1
            buf.append(script[i:j + 1])
            i = j + 1
        elif script.startswith(term, i):
            stmt = "".join(buf).strip()
            buf = []
            i += len(term)
            m = re.match(r"^(?:(?:--[^\n]*\n|/\*.*?\*/)\s*)*SET\s+TERM\s+(\S+)\s*$", stmt, re.IGNORECASE | re.DOTALL)
            if m:
                term = m.group(1)
            elif stmt:
                out.append(stmt)
        elif ch.isalnum() or ch in "_$":
            j = i
            while j < n and (script[j].isalnum() or script[j] in "_$"):
                j += 1
            buf.append(script[i:j])
            i = j
        else:
            buf.append(ch)
            i += 1
    rest = "".join(buf).strip()
    if rest:
        out.append(rest)
    return out


def sql_only(stmts: list[str]) -> list[str]:
    """Drop isql commands and comment-only statements."""
    from fbadmin.sqlsplit import strip_comments

    return [s for s in stmts if strip_comments(s) and not _ISQL_ONLY.match(strip_comments(s))]
