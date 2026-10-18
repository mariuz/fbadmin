"""Property-based (fuzz) tests for the SQL splitter and parameter scanner (hypothesis).

Run more examples with ``HYPOTHESIS_PROFILE=thorough``.
"""

from __future__ import annotations

import os

import pytest

hypothesis = pytest.importorskip("hypothesis")
from hypothesis import HealthCheck, given, settings  # noqa: E402
from hypothesis import strategies as st  # noqa: E402

from fbadmin.sqlsplit import bind_params, script_params, split_sql, strip_comments  # noqa: E402

settings.register_profile("default", max_examples=200, deadline=None,
                          suppress_health_check=[HealthCheck.too_slow])
settings.register_profile("thorough", max_examples=5000, deadline=None,
                          suppress_health_check=[HealthCheck.too_slow])
settings.load_profile(os.environ.get("HYPOTHESIS_PROFILE", "default"))

# --- building blocks -------------------------------------------------------------
KEYWORDS = ["SELECT", "FROM", "WHERE", "AND", "OR", "INSERT", "INTO", "VALUES", "UPDATE", "SET", "DELETE"]
ident = st.from_regex(r"[A-Z][A-Z0-9_$]{0,10}", fullmatch=True).filter(
    lambda s: s not in {"BEGIN", "END", "CASE", "AS", "TERM", "EXTERNAL", "DECLARE", "SET", "Q"}
)
any_text = st.text(alphabet=st.characters(blacklist_categories=("Cs",)), max_size=30)
# string literal that may contain terminators, quotes (escaped) and newlines
string_lit = any_text.map(lambda s: "'" + s.replace("'", "''") + "'")
quoted_ident = st.text(alphabet=st.characters(blacklist_categories=("Cs",), blacklist_characters='"\x00'),
                       min_size=1, max_size=15).map(lambda s: f'"{s}"')
line_comment = st.text(alphabet=st.characters(blacklist_categories=("Cs",), blacklist_characters="\n\r"),
                       max_size=20).map(lambda s: f"-- {s}\n")
block_comment = any_text.filter(lambda s: "*/" not in s).map(lambda s: f"/* {s} */")
number = st.integers(-10**6, 10**6).map(str)
token = st.one_of(ident, st.sampled_from(KEYWORDS), string_lit, quoted_ident, number,
                  st.sampled_from(["(", ")", ",", "=", "+", "*", "<>", "||"]))
space = st.sampled_from([" ", "\n", "\t", "  ", " \r\n "])


@st.composite
def simple_statement(draw) -> str:
    """A non-PSQL statement: tokens, strings and comments, never ending in a comment."""
    parts = [draw(st.sampled_from(["SELECT", "INSERT", "UPDATE", "DELETE", "COMMENT"]))]
    for _ in range(draw(st.integers(1, 12))):
        if draw(st.integers(0, 9)) == 0:
            parts.append(draw(st.one_of(line_comment, block_comment)))
        parts.append(draw(token))
    parts.append(draw(token))
    out = parts[0]
    for p in parts[1:]:
        out += draw(space) + p
    return out


@st.composite
def psql_block(draw, depth: int = 0) -> str:
    """BEGIN ... END with ';' inside, nested blocks and CASE ... END expressions."""
    inner = []
    for _ in range(draw(st.integers(0, 4))):
        kind = draw(st.integers(0, 3 if depth < 3 else 2))
        if kind == 0:
            inner.append(f"x = {draw(string_lit)};")
        elif kind == 1:
            inner.append(f"x = CASE WHEN a = 1 THEN {draw(number)} ELSE {draw(string_lit)} END;")
        elif kind == 2:
            inner.append(draw(line_comment) + "y = y + 1;")
        else:
            inner.append(draw(psql_block(depth + 1)))
    return "BEGIN\n  " + "\n  ".join(inner) + "\nEND"


@st.composite
def psql_statement(draw) -> str:
    head = draw(st.sampled_from([
        "EXECUTE BLOCK AS\nDECLARE x VARCHAR(100);\nDECLARE y INTEGER = 0;\nDECLARE a INTEGER = 1;\n",
        "CREATE OR ALTER PROCEDURE P_{n} AS\nDECLARE x VARCHAR(100);\nDECLARE y INTEGER;\nDECLARE a INTEGER;\n",
        "CREATE TRIGGER T_{n} FOR T ACTIVE BEFORE INSERT AS\nDECLARE x VARCHAR(100);\n",
    ])).format(n=draw(st.integers(0, 999)))
    return head + draw(psql_block())


statement = st.one_of(simple_statement(), simple_statement(), psql_statement())


# --- properties ------------------------------------------------------------------
@given(st.text(max_size=300))
def test_never_crashes_and_offsets_are_consistent(script):
    for s in split_sql(script):
        assert s.sql and s.sql == s.sql.strip()
        assert script[s.offset:s.offset + len(s.sql)] == s.sql
        assert s.line == script.count("\n", 0, s.offset) + 1
        assert s.col == s.offset - (script.rfind("\n", 0, s.offset) + 1) + 1


@given(st.lists(statement, min_size=1, max_size=6), st.sampled_from([";", " ;", ";\n", "\n;\n"]))
def test_round_trip(stmts, sep):
    script = sep.join(stmts) + sep
    assert [s.sql for s in split_sql(script)] == [s.strip() for s in stmts]


@given(st.lists(statement, min_size=1, max_size=5))
def test_set_term_round_trip(stmts):
    script = "SET TERM ^ ;\n" + "".join(s + "^\n" for s in stmts) + "SET TERM ; ^\nSELECT 1 FROM RDB$DATABASE;"
    out = [s.sql for s in split_sql(script)]
    assert out == [s.strip() for s in stmts] + ["SELECT 1 FROM RDB$DATABASE"]


@given(st.lists(statement, min_size=1, max_size=5))
def test_offsets_point_into_script(stmts):
    script = ";\n".join(stmts)
    for s in split_sql(script):
        assert script[s.offset:].startswith(s.sql)


@given(st.text(max_size=200))
def test_strip_comments_idempotent(text):
    once = strip_comments(text)
    assert strip_comments(once) == once
    assert text.endswith(once) or once == ""


@given(st.text(max_size=200))
def test_param_scanning_never_crashes(script):
    names = script_params(script)
    assert len(names) == len(set(names))


@given(st.lists(st.one_of(ident.map(lambda s: ":" + s), st.just("?")), min_size=1, max_size=6), string_lit)
def test_bind_params_replaces_markers_outside_strings(markers, lit):
    sql = "SELECT " + ", ".join(markers) + f", {lit} FROM RDB$DATABASE"
    bound, names = bind_params(sql, [0])
    assert len(names) == len(markers)
    assert bound.count("?") - lit.count("?") == len(markers)
    assert lit in bound  # the literal is untouched


# --- regressions found by the firebird-qa corpus (tests/test_firebird_qa.py) ------------
def test_comment_before_set_term():
    script = "-- section 4\nSET TERM ^;\nEXECUTE BLOCK AS BEGIN x = 1; END^\n-- back\nSET TERM ;^\nSELECT 1 FROM T;"
    assert [s.sql for s in split_sql(script)] == ["EXECUTE BLOCK AS BEGIN x = 1; END", "SELECT 1 FROM T"]


def test_create_collation_case_insensitive():
    script = "create collation c_ci for utf8 from unicode case insensitive accent insensitive;\nselect 1 from t;"
    assert len(split_sql(script)) == 2


def test_alter_database_begin_backup():
    script = "alter database begin backup; select 1 from t; alter database end backup; select 2 from t;"
    assert len(split_sql(script)) == 4
