from fbadmin.sqlsplit import split_sql, strip_comments


def sqls(script):
    return [s.sql for s in split_sql(script)]


def test_simple():
    assert sqls("select 1 from rdb$database; select 2 from rdb$database;") == [
        "select 1 from rdb$database",
        "select 2 from rdb$database",
    ]


def test_no_trailing_terminator():
    assert sqls("select 1 from rdb$database") == ["select 1 from rdb$database"]


def test_strings_and_comments():
    s = "insert into t values ('a;b'); -- c;d\n/* e;f */ select '''' from rdb$database;"
    assert sqls(s) == ["insert into t values ('a;b')", "-- c;d\n/* e;f */ select '''' from rdb$database"]


def test_quoted_identifier():
    assert sqls('select "a;b" from t; select 1 from t') == ['select "a;b" from t', "select 1 from t"]


def test_q_string():
    assert sqls("select q'{it's;}' from rdb$database; select 1 from rdb$database") == [
        "select q'{it's;}' from rdb$database",
        "select 1 from rdb$database",
    ]


def test_set_term():
    script = """
SET TERM ^ ;
CREATE PROCEDURE P AS BEGIN EXIT; END^
SET TERM ; ^
SELECT 1 FROM RDB$DATABASE;
"""
    assert sqls(script) == ["CREATE PROCEDURE P AS BEGIN EXIT; END", "SELECT 1 FROM RDB$DATABASE"]


def test_psql_without_set_term():
    script = """
create or alter procedure p (a int) returns (b int) as
declare x int;
begin
  x = case when a > 0 then 1 else 0 end;
  if (x = 1) then begin b = a; suspend; end
end;
select * from p(1);
execute block as begin end;
"""
    out = sqls(script)
    assert len(out) == 3
    assert out[0].startswith("create or alter procedure") and out[0].endswith("end")
    assert out[1] == "select * from p(1)"
    assert out[2] == "execute block as begin end"


def test_package_header():
    script = "create package pk as begin function f returns int; procedure p; end; select 1 from rdb$database;"
    out = sqls(script)
    assert out == ["create package pk as begin function f returns int; procedure p; end", "select 1 from rdb$database"]


def test_line_numbers():
    stmts = split_sql("select 1 from rdb$database;\n\n\nselect 2 from rdb$database;")
    assert [s.line for s in stmts] == [1, 4]


def test_empty_statements_skipped():
    assert sqls(";;  ; select 1 from rdb$database;;") == ["select 1 from rdb$database"]


def test_strip_comments():
    assert strip_comments("-- x\n/* y */  select 1") == "select 1"


def test_declare_section_before_begin():
    script = (
        "create procedure p returns (x int) as\ndeclare variable n int = 0;\ndeclare c cursor for (select 1 from t);\n"
        "begin n = 1; end;\nalter trigger t inactive;\ncreate function f returns int external name 'm!f' engine udr;"
    )
    out = sqls(script)
    assert len(out) == 3, out
    assert out[1] == "alter trigger t inactive"


def test_start_column_and_offset():
    script = "select 1 from rdb$database;  select 2\nfrom rdb$database;\n  -- note\n  select 3 from rdb$database"
    st = split_sql(script)
    assert [(s.line, s.col) for s in st] == [(1, 1), (1, 30), (3, 3)]
    assert script[st[1].offset:].startswith("select 2")
    assert st[2].sql.startswith("-- note")


def test_bind_params():
    from fbadmin.sqlsplit import bind_params, script_params

    sql, names = bind_params("select * from t where a = :a and b = ? and c = ':x' and d = :a -- :y", [0])
    assert sql == "select * from t where a = ? and b = ? and c = ':x' and d = ? -- :y"
    assert names == ["A", "$1", "A"]
    # PSQL modules use :vars internally
    assert bind_params("execute block returns (x int) as begin x = :x; end", [0])[1] == []
    assert bind_params("create procedure p as begin select 1 from t into :v; end", [0])[1] == []
    assert script_params("select :b, ? from t; update t set x = :b, y = ?;") == ["B", "$1", "$2"]
    assert script_params('select :"lower" from t') == ["lower"]
    assert script_params("select q'{:not}' from t") == []
