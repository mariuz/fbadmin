"""Unit tests of the PSQL parser and trace instrumentation (no server needed)."""

from __future__ import annotations

import re

import pytest

from fbadmin.debug import psql
from fbadmin.debug.psql import PsqlError, Var


def inst(src: str, kind: str = "executable", inputs=None, outputs=None, values=None, returns=None, **kw):
    parsed = psql.parse_source(src)
    return psql.instrument(parsed, kind=kind, inputs=inputs or [], outputs=outputs or [], values=values or {},
                           returns=returns, **kw)


def squash(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip()


def test_tokenizer_skips_comments_and_keeps_strings():
    toks = psql.tokenize("A = 'x; END' || q'{it's ; BEGIN}' -- END;\n/* BEGIN */ B; \"Mixed Case\"")
    kinds = [(t.kind, t.text) for t in toks]
    assert ("str", "'x; END'") in kinds
    assert ("str", "q'{it's ; BEGIN}'") in kinds
    assert ("qid", '"Mixed Case"') in kinds
    assert not any(t.up in ("END", "BEGIN") for t in toks)
    assert toks[-1].name == "Mixed Case" and toks[-1].line == 2


@pytest.mark.parametrize("bad,msg", [
    ("BEGIN X = 1; ", "end of the source"),
    ("BEGIN X = 1 END", "missing ';'"),
    ("BEGIN IF (X = 1) X = 2; END", "expected THEN"),
    ("BEGIN X = 'abc; END", "unterminated string"),
    ("BEGIN /* x END", "unterminated /*"),
    ("BEGIN ; END", "empty statement"),
    ("BEGIN WHEN ANY DO X = 1; X = 2; END", "after a WHEN"),
    ("BEGIN X = 1; END extra", "after the final END"),
    ("BEGIN L1: X = 1; END", "label"),
    ("DECLARE 1 INTEGER; BEGIN END", "variable name"),
])
def test_syntax_errors_are_refused(bad, msg):
    with pytest.raises(PsqlError) as ei:
        psql.parse_source(bad)
    assert msg in str(ei.value)


def test_declarations():
    p = psql.parse_source("""DECLARE VARIABLE A INTEGER = 1;
DECLARE B NUMERIC(10, 2) NOT NULL DEFAULT 0;
DECLARE "Quoted" VARCHAR(20) CHARACTER SET UTF8 COLLATE UNICODE_CI = 'x;y';
DECLARE C CURSOR FOR (SELECT 1 FROM RDB$DATABASE WHERE 'END;' = '');
DECLARE D SCROLL CURSOR FOR (SELECT 2 FROM RDB$DATABASE);
DECLARE E TYPE OF COLUMN ITEMS.NAME;
DECLARE F D_POS;
DECLARE PROCEDURE SUB (X INTEGER) RETURNS (Y INTEGER) AS DECLARE Z INTEGER; BEGIN Y = X; SUSPEND; END
DECLARE FUNCTION FN (X INTEGER) RETURNS INTEGER;
BEGIN
END""")
    assert [(v.name, v.type, v.not_null) for v in p.locals] == [
        ("A", "INTEGER", False), ("B", "NUMERIC(10, 2)", True), ("Quoted", "VARCHAR(20) CHARACTER SET UTF8", False),
        ("E", "TYPE OF COLUMN ITEMS.NAME", False), ("F", "D_POS", False)]
    assert p.cursors == ["C", "D"]
    assert p.subroutines == ["SUB", "FN"]


def test_column_types():
    assert psql.column_type("INTEGER NOT NULL") == "INTEGER"
    assert psql.column_type("VARCHAR(10) CHARACTER SET UTF8 COLLATE UNICODE_CI") == "VARCHAR(10) CHARACTER SET UTF8"
    assert psql.column_type("D_POS") == "TYPE OF D_POS"
    assert psql.column_type('"my dom" NOT NULL = 5') == 'TYPE OF "my dom"'
    assert psql.column_type("TYPE OF COLUMN T.C") == "TYPE OF COLUMN T.C"
    assert psql.column_type("DOUBLE PRECISION DEFAULT 1") == "DOUBLE PRECISION"
    v = psql.var_from_decl("X", "VARCHAR(5) NOT NULL COLLATE PT_BR DEFAULT 'a'", "input")
    assert (v.type, v.not_null, v.collate) == ("VARCHAR(5)", True, "PT_BR")
    assert v.decl == "VARCHAR(5) NOT NULL COLLATE PT_BR"


def test_lines_are_preserved():
    src = """DECLARE I INTEGER = 0;
BEGIN
  IF (I = 0) THEN I = 1;
  ELSE
    I = 2;
  WHILE (I < 3) DO I = I + 1;
  FOR SELECT 1 FROM RDB$DATABASE INTO :I DO
    IF (I > 0) THEN
      EXIT;
END"""
    out = inst(src)
    lines = out.sql.split("\n")
    assert len(lines) == len(src.split("\n"))
    for a, b in zip(src.split("\n")[1:], lines[1:], strict=True):
        assert a.strip().split(" ")[0] in b
    assert out.sql.startswith("EXECUTE BLOCK RETURNS (FBT$K SMALLINT, FBT$L INTEGER")


def test_if_else_while_for_bodies_are_wrapped():
    src = """DECLARE I INTEGER = 0;
DECLARE S VARCHAR(10);
BEGIN
  IF (I = 0) THEN I = 1; ELSE IF (I = 1) THEN S = 'b'; ELSE S = 'c';
  WHILE (I < 3) DO I = I + 1;
  FOR SELECT 'x' FROM RDB$DATABASE INTO :S DO I = I - 1;
END"""
    sql = squash(inst(src).sql)
    assert "THEN BEGIN I = 1; FBT$K=1;FBT$L=4;FBT$1=I;SUSPEND; END ELSE BEGIN IF (I = 1) THEN BEGIN S = 'b';" in sql
    assert "ELSE BEGIN S = 'c'; FBT$K=1;FBT$L=4;FBT$2=S;SUSPEND; END END" in sql
    assert "DO BEGIN I = I + 1; FBT$K=1;FBT$L=5;FBT$1=I;SUSPEND; END" in sql
    # FOR: a fetch step (with the INTO variables) first, then the body
    assert "INTO :S DO BEGIN FBT$K=5;FBT$L=6;FBT$2=S;SUSPEND; I = I - 1; FBT$K=1;FBT$L=6;FBT$1=I;SUSPEND; END" in sql


def test_only_mentioned_variables_are_copied():
    src = "DECLARE A INTEGER; DECLARE B INTEGER; DECLARE C INTEGER;\nBEGIN\n  A = 1;\n  SELECT 2 FROM RDB$DATABASE INTO :B;\nEND"
    sql = inst(src).sql
    assert "A = 1; FBT$K=1;FBT$L=3;FBT$1=A;SUSPEND;" in sql
    assert "INTO :B; FBT$K=1;FBT$L=4;FBT$2=B;SUSPEND;" in sql
    # entry and end steps copy everything
    assert "FBT$K=0;FBT$L=2;FBT$1=A;FBT$2=B;FBT$3=C;SUSPEND;" in sql
    assert "FBT$K=3;FBT$L=5;FBT$1=A;FBT$2=B;FBT$3=C;SUSPEND; END" in sql


def test_labels_leave_continue_exception_are_traced_before():
    src = """DECLARE I INTEGER = 0;
BEGIN
  L1: WHILE (1 = 1) DO
  BEGIN
    I = I + 1;
    IF (I > 5) THEN LEAVE L1;
    IF (I = 2) THEN CONTINUE;
    IF (I = 4) THEN BREAK;
  END
  EXCEPTION E_NEG 'x' || I;
END"""
    sql = squash(inst(src).sql)
    assert "L1: WHILE (1 = 1) DO BEGIN" in sql
    assert "THEN BEGIN FBT$K=1;FBT$L=6;SUSPEND; LEAVE L1; END" in sql
    assert "THEN BEGIN FBT$K=1;FBT$L=7;SUSPEND; CONTINUE; END" in sql
    assert "THEN BEGIN FBT$K=1;FBT$L=8;SUSPEND; BREAK; END" in sql
    assert "FBT$K=1;FBT$L=10;SUSPEND; EXCEPTION E_NEG 'x' || I;" in sql


def test_handlers_record_the_error():
    src = """DECLARE I INTEGER = 0;
BEGIN
  BEGIN
    I = 1 / 0;
    WHEN GDSCODE arith_except, SQLSTATE '22012' DO I = -1;
    WHEN ANY DO
    BEGIN
      I = -2;
      EXCEPTION;
    END
  END
END"""
    sql = squash(inst(src, with_message=True).sql)
    assert ("DO BEGIN FBT$G=GDSCODE;FBT$S=SQLSTATE;FBT$M=RDB$ERROR(MESSAGE);FBT$K=4;FBT$L=5;FBT$1=I;SUSPEND; "
            "I = -1; FBT$K=1;FBT$L=5;FBT$1=I;SUSPEND; END") in sql
    assert "WHEN ANY DO BEGIN FBT$G=GDSCODE;FBT$S=SQLSTATE;FBT$M=RDB$ERROR(MESSAGE);FBT$K=4;FBT$L=6;" in sql
    assert "FBT$K=1;FBT$L=9;SUSPEND; EXCEPTION; END" in sql
    no_msg = inst(src).sql
    assert "RDB$ERROR" not in no_msg and "FBT$M" not in no_msg


def test_suspend_exit_and_final_rows():
    src = "BEGIN\n  X = 1;\n  SUSPEND;\n  IF (X = 1) THEN EXIT;\nEND"
    out = inst(src, kind="selectable", outputs=[Var("X", "INTEGER", "output")])
    sql = squash(out.sql)
    assert "FBT$K=2;FBT$L=3; SUSPEND;" in sql
    assert "THEN BEGIN FBT$K=3;FBT$L=4;SUSPEND; EXIT; END" in sql
    assert ("output", "X") in out.columns


def test_function_return():
    src = "DECLARE I INTEGER = 2;\nBEGIN\n  IF (I > 1) THEN RETURN I * 2;\n  RETURN 0;\nEND"
    out = inst(src, kind="function", returns="INTEGER")
    sql = squash(out.sql)
    assert "THEN BEGIN FBT$R = I * 2; FBT$K=3;FBT$L=3;FBT$1=I;SUSPEND; EXIT; END" in sql
    assert "FBT$R = 0; FBT$K=3;FBT$L=4;FBT$1=I;SUSPEND; EXIT;" in sql
    assert "FBT$R INTEGER" in sql
    with pytest.raises(PsqlError, match="only allowed in a function"):
        inst(src)
    with pytest.raises(PsqlError, match="not allowed in a function"):
        inst("BEGIN SUSPEND; END", kind="function", returns="INTEGER")


def test_subroutines_are_copied_untouched():
    src = """DECLARE FUNCTION F (X INTEGER) RETURNS INTEGER AS DECLARE Y INTEGER; BEGIN Y = X; RETURN Y; END
DECLARE PROCEDURE P RETURNS (Z INTEGER) AS BEGIN Z = 1; SUSPEND; EXIT; END
BEGIN
  R = F(1);
END"""
    out = inst(src, outputs=[Var("R", "INTEGER", "output")])
    first, second = out.sql.split("\n")[:2]
    assert first.endswith("BEGIN Y = X; RETURN Y; END")
    assert second == "DECLARE PROCEDURE P RETURNS (Z INTEGER) AS BEGIN Z = 1; SUSPEND; EXIT; END"


def test_comments_and_strings_are_untouched():
    src = """BEGIN
  -- IF (1 = 1) THEN EXIT;
  S = 'it''s; END /* x */'; /* BEGIN ; END */
  S = q'[WHILE (1=1) DO;]';
END"""
    out = inst(src, outputs=[Var("S", "VARCHAR(100)", "output")])
    lines = out.sql.split("\n")
    assert lines[1] == "  -- IF (1 = 1) THEN EXIT;"
    assert lines[2].startswith("  S = 'it''s; END /* x */'; FBT$K=1;FBT$L=3;SUSPEND;")
    assert lines[2].endswith("/* BEGIN ; END */")
    assert lines[3].startswith("  S = q'[WHILE (1=1) DO;]';")


def test_case_expressions_and_merge():
    src = """DECLARE V INTEGER;
BEGIN
  V = CASE WHEN V > 1 THEN 1 ELSE 2 END;
  MERGE INTO T USING RDB$DATABASE ON 1 = 0
    WHEN MATCHED THEN UPDATE SET A = 1
    WHEN NOT MATCHED THEN INSERT (A) VALUES (:V);
  IF (CASE WHEN V = 1 THEN TRUE ELSE FALSE END) THEN V = 3;
END"""
    sql = inst(src).sql
    assert "ELSE 2 END; FBT$K=1;FBT$L=3;FBT$1=V;SUSPEND;" in sql
    assert "VALUES (:V); FBT$K=1;FBT$L=4;FBT$1=V;SUSPEND;" in sql
    assert "ELSE FALSE END) THEN BEGIN V = 3;" in sql


def test_for_execute_statement_and_cursor_loops():
    src = """DECLARE N INTEGER;
DECLARE C CURSOR FOR (SELECT 1 FROM RDB$DATABASE);
BEGIN
  FOR EXECUTE STATEMENT ('SELECT 1 FROM RDB$DATABASE WHERE 1 = ?') (1) INTO :N DO N = N + 1;
  FOR SELECT 1 FROM RDB$DATABASE INTO :N AS CURSOR K DO
    UPDATE T SET A = 1 WHERE CURRENT OF K;
  OPEN C;
  FETCH C INTO :N;
  CLOSE C;
END"""
    sql = squash(inst(src).sql)
    assert "(1) INTO :N DO BEGIN FBT$K=5;FBT$L=4;FBT$1=N;SUSPEND; N = N + 1;" in sql
    assert "AS CURSOR K DO BEGIN FBT$K=5;FBT$L=5;FBT$1=N;SUSPEND; UPDATE T SET A = 1 WHERE CURRENT OF K; FBT$K=1;FBT$L=6;SUSPEND; END" in sql
    assert "FETCH C INTO :N; FBT$K=1;FBT$L=8;FBT$1=N;SUSPEND;" in sql


def test_side_effects():
    with pytest.raises(PsqlError) as ei:
        inst("BEGIN\n  IN AUTONOMOUS TRANSACTION DO INSERT INTO T VALUES (1);\nEND")
    assert ei.value.code == "autonomous" and ei.value.line == 2
    with pytest.raises(PsqlError) as ei:
        inst("BEGIN EXECUTE STATEMENT 'X' WITH AUTONOMOUS TRANSACTION; END")
    assert ei.value.code == "autonomous"
    with pytest.raises(PsqlError) as ei:
        inst("BEGIN EXECUTE STATEMENT 'X' ON EXTERNAL 'db' AS USER 'a' PASSWORD 'b'; END")
    assert ei.value.code == "external"
    out = inst("DECLARE I BIGINT;\nBEGIN\n  I = GEN_ID(G, 1);\n  POST_EVENT 'e';\n  EXECUTE STATEMENT 'SELECT 1 FROM RDB$DATABASE' INTO :I;\nEND")
    assert len(out.warnings) == 3
    # inside strings or comments these words are harmless
    assert inst("BEGIN\n  -- IN AUTONOMOUS TRANSACTION\n  S = 'ON EXTERNAL';\nEND",
                outputs=[Var("S", "VARCHAR(20)", "output")]).warnings == []


def test_inputs_become_initialised_variables():
    out = inst("BEGIN\n  Y = X;\nEND", inputs=[psql.var_from_decl("X", "VARCHAR(10) NOT NULL COLLATE UNICODE_CI", "input")],
               outputs=[Var("Y", "VARCHAR(10)", "output")], values={"X": "it's"})
    assert "DECLARE X VARCHAR(10) NOT NULL COLLATE UNICODE_CI = 'it''s';" in out.sql
    out = inst("BEGIN\n  Y = X;\nEND", inputs=[Var("X", "INTEGER", "input")], outputs=[Var("Y", "INTEGER", "output")],
               values={"X": None})
    assert "DECLARE X INTEGER = NULL;" in out.sql
    with pytest.raises(PsqlError, match="unknown input"):
        inst("BEGIN\nEND", inputs=[Var("X", "INTEGER", "input")], values={"Z": "1"})


def test_watch_and_modes():
    p = psql.parse_source("DECLARE A INTEGER; DECLARE B BLOB; DECLARE C VARCHAR(9000);\nBEGIN\n  A = 1; B = 'x'; C = 'y';\nEND")
    tv = psql.default_trace_vars(p, [], {"B", "C"})
    assert [v.column for v in tv] == [None, "FBT$1", "FBT$2"]
    tv[0 + 1].mode, tv[1].col_type = "size", "BIGINT"
    tv[2].mode, tv[2].col_type, tv[2].text_chars = "text", "VARCHAR(1000) CHARACTER SET UTF8", 1000
    out = psql.instrument(p, kind="executable", inputs=[], outputs=[], values={}, tvars=tv)
    assert "FBT$1 BIGINT" in out.sql and "FBT$1=OCTET_LENGTH(B);" in out.sql
    assert "FBT$2=SUBSTRING(C FROM 1 FOR 1000);" in out.sql
    assert "=A;" not in out.sql


def test_catch_errors_wraps_the_body():
    out = inst("BEGIN\n  X = 1;\nEND", outputs=[Var("X", "INTEGER", "output")], with_message=True, catch_errors=True)
    assert out.sql.split("\n")[1].startswith("BEGIN BEGIN FBT$K=0;") is False  # header shares line 1
    assert squash(out.sql).endswith("WHEN ANY DO BEGIN FBT$G=GDSCODE;FBT$S=SQLSTATE;FBT$M=RDB$ERROR(MESSAGE);"
                                    "FBT$K=9;FBT$L=NULL;SUSPEND; END END")
    with pytest.raises(ValueError):
        inst("BEGIN\nEND", catch_errors=True)


def test_execute_block_parsing():
    sql = """EXECUTE BLOCK (A INTEGER = ?, B NUMERIC(10, 2) NOT NULL = ?, "c" VARCHAR(5) = ?)
RETURNS (X INTEGER, Y VARCHAR(10) CHARACTER SET UTF8)
AS
DECLARE I INTEGER;
BEGIN
  X = A;
  SUSPEND;
END;"""
    p = psql.parse_execute_block(sql)
    assert [(v.name, v.type, v.not_null) for v in p.inputs] == [
        ("A", "INTEGER", False), ("B", "NUMERIC(10, 2)", True), ("c", "VARCHAR(5)", False)]
    assert [(v.name, v.type) for v in p.outputs] == [("X", "INTEGER"), ("Y", "VARCHAR(10) CHARACTER SET UTF8")]
    assert p.has_suspend()
    out = psql.instrument(p, kind="selectable", inputs=p.inputs, outputs=p.outputs, values={"A": "1"})
    lines = out.sql.split("\n")
    assert len(lines) == len(sql.rstrip(";").split("\n"))
    assert lines[5].startswith("  X = A; FBT$K=1;FBT$L=6;")
    assert 'DECLARE "c" VARCHAR(5) = NULL;' in lines[0]
    assert psql.is_execute_block("  -- x\nexecute block as begin end")
    with pytest.raises(PsqlError):
        psql.parse_execute_block("SELECT 1 FROM RDB$DATABASE")
