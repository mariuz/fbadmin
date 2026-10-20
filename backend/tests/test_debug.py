"""PSQL debugging help (milestone 1.2): trace runs, profiler integration and the test runner."""

from __future__ import annotations

import pytest

from .conftest import requires_server

pytestmark = requires_server

SCHEMA = [
    "CREATE TABLE ITEMS (ID INTEGER NOT NULL PRIMARY KEY, NAME VARCHAR(40), QTY INTEGER, NOTE BLOB SUB_TYPE TEXT)",
    "CREATE TABLE LOG_T (ID INTEGER, MSG VARCHAR(100))",
    "CREATE EXCEPTION E_NEG 'negative value'",
    "CREATE SEQUENCE SEQ_T",
    "CREATE DOMAIN D_POS AS INTEGER CHECK (VALUE >= 0)",
    "INSERT INTO ITEMS VALUES (1, 'apple', 5, 'red fruit')",
    "INSERT INTO ITEMS VALUES (2, 'pear', 0, NULL)",
    "INSERT INTO ITEMS VALUES (3, 'plum; END', 12, 'BEGIN -- not code')",
    # selectable: FOR SELECT, IF/ELSE chain without BEGIN, labels, LEAVE, strings with keywords
    """CREATE PROCEDURE P_LIST (MIN_QTY INTEGER, TAG VARCHAR(10) = 'x')
RETURNS (ID INTEGER, NAME VARCHAR(40), LABEL VARCHAR(60))
AS
DECLARE VARIABLE N INTEGER = 0;
DECLARE VARIABLE Q INTEGER;
BEGIN
  /* a comment with ; BEGIN and END */
  FOR SELECT ID, NAME, QTY FROM ITEMS WHERE QTY >= :MIN_QTY ORDER BY ID INTO :ID, :NAME, :Q DO
  BEGIN
    N = N + 1;
    IF (Q = 0) THEN LABEL = 'none';
    ELSE IF (Q < 10) THEN LABEL = 'few; IF';
    ELSE LABEL = 'many ' || TAG;
    SUSPEND;
  END
  L1: WHILE (N < 10) DO
  BEGIN
    N = N + 1;
    IF (N >= 5) THEN LEAVE L1;
  END
  ID = N; NAME = 'total'; LABEL = NULL;
  SUSPEND;
END""",
    # executable: outputs, WHEN handler, EXIT, user exception, CASE expression
    """CREATE PROCEDURE P_CALC (A INTEGER, B INTEGER)
RETURNS (Q INTEGER, R INTEGER, MSG VARCHAR(100))
AS
DECLARE VARIABLE T INTEGER;
BEGIN
  MSG = CASE WHEN A > B THEN 'a>b' ELSE 'a<=b' END;
  BEGIN
    Q = A / B;
    WHEN GDSCODE arith_except DO
    BEGIN
      Q = NULL;
      MSG = 'division by zero';
      EXIT;
    END
  END
  R = MOD(A, B);
  IF (A < 0) THEN EXCEPTION E_NEG 'A is ' || A;
  T = Q * B + R;
  IF (T <> A) THEN MSG = 'broken';
END""",
    # function with a sub-function, WHILE, RETURN inside IF
    """CREATE FUNCTION F_FACT (N INTEGER) RETURNS BIGINT
AS
DECLARE VARIABLE I INTEGER = 1;
DECLARE VARIABLE ACC BIGINT = 1;
DECLARE FUNCTION TWICE (X BIGINT) RETURNS BIGINT AS BEGIN RETURN X * 2; END
BEGIN
  IF (N < 0) THEN RETURN NULL;
  WHILE (I <= N) DO
  BEGIN
    ACC = ACC * I;
    I = I + 1;
  END
  RETURN TWICE(ACC) / 2;
END""",
    # writes: must be rolled back
    """CREATE PROCEDURE P_WRITE (V INTEGER) RETURNS (CNT INTEGER)
AS
BEGIN
  INSERT INTO LOG_T VALUES (:V, 'written');
  UPDATE ITEMS SET QTY = QTY + :V;
  SELECT COUNT(*) FROM LOG_T INTO :CNT;
END""",
    # cursor, ROW_COUNT, FOR EXECUTE STATEMENT, blob variable
    """CREATE PROCEDURE P_CURSOR RETURNS (TOTAL INTEGER, NOTES INTEGER, ROWS_SEEN INTEGER)
AS
DECLARE VARIABLE Q INTEGER;
DECLARE VARIABLE B BLOB SUB_TYPE TEXT;
DECLARE C CURSOR FOR (SELECT QTY, NOTE FROM ITEMS ORDER BY ID);
BEGIN
  TOTAL = 0; NOTES = 0; ROWS_SEEN = 0;
  OPEN C;
  WHILE (1 = 1) DO
  BEGIN
    FETCH C INTO :Q, :B;
    IF (ROW_COUNT = 0) THEN LEAVE;
    TOTAL = TOTAL + Q;
    IF (B IS NOT NULL) THEN NOTES = NOTES + 1;
  END
  CLOSE C;
  FOR EXECUTE STATEMENT 'SELECT ID FROM ITEMS' INTO :Q DO
    ROWS_SEEN = ROWS_SEEN + 1;
END""",
    # error after some steps
    """CREATE PROCEDURE P_FAIL (X INTEGER) RETURNS (Y INTEGER)
AS
BEGIN
  Y = X + 1;
  Y = Y * 2;
  IF (Y > 10) THEN
    EXCEPTION E_NEG 'too big: ' || Y;
  Y = Y / (X - X);
END""",
    """CREATE PROCEDURE P_AUTO RETURNS (X INTEGER)
AS
BEGIN
  IN AUTONOMOUS TRANSACTION DO INSERT INTO LOG_T VALUES (1, 'auto');
  X = 1;
END""",
    """CREATE PROCEDURE P_SEQ RETURNS (X BIGINT)
AS
BEGIN
  X = NEXT VALUE FOR SEQ_T;
END""",
    """CREATE PROCEDURE P_CALLS_AUTO RETURNS (X INTEGER)
AS
BEGIN
  EXECUTE PROCEDURE P_AUTO RETURNING_VALUES :X;
END""",
    """CREATE PROCEDURE P_DOMAIN (V D_POS) RETURNS (W D_POS)
AS
BEGIN
  W = V * 2;
END""",
]


def base(sid, dbid):
    return f"/api/servers/{sid}/databases/{dbid}"


@pytest.fixture
def db(client, database):
    sid, dbid = database
    for sql in SCHEMA:
        r = client.post(base(sid, dbid) + "/execute", json={"sql": sql})
        assert r.status_code == 200, r.text
        assert not r.json()["error"], (sql, r.json()["error"])
    return sid, dbid


def dbg(client, sid, dbid, path, **body):
    r = client.post(base(sid, dbid) + "/debug/" + path, json=body)
    assert r.status_code == 200, r.text
    return r.json()


def trace(client, sid, dbid, otype, name, inputs=None, **kw):
    return dbg(client, sid, dbid, "trace", otype=otype, name=name, inputs=inputs or {}, **kw)


def plain(client, sid, dbid, otype, name, inputs=None):
    return dbg(client, sid, dbid, "run", otype=otype, name=name, inputs=inputs or {})


def values_at(res, step):
    return {v["name"]: step["v"][i] for i, v in enumerate(res["variables"])}


def query(client, sid, dbid, sql):
    r = client.post(base(sid, dbid) + "/execute", json={"sql": sql})
    return r.json()["results"][0]["rows"]


CASES = [
    ("procedures", "P_LIST", {"MIN_QTY": "0", "TAG": "t"}),
    ("procedures", "P_LIST", {"MIN_QTY": "100", "TAG": None}),
    ("procedures", "P_CALC", {"A": "17", "B": "5"}),
    ("procedures", "P_CALC", {"A": "17", "B": "0"}),
    ("functions", "F_FACT", {"N": "10"}),
    ("functions", "F_FACT", {"N": "-1"}),
    ("procedures", "P_WRITE", {"V": "3"}),
    ("procedures", "P_CURSOR", {}),
    ("procedures", "P_DOMAIN", {"V": "4"}),
]


@pytest.mark.parametrize("otype,name,inputs", CASES)
def test_trace_returns_the_same_outputs_as_the_routine(client, db, otype, name, inputs):
    sid, dbid = db
    t = trace(client, sid, dbid, otype, name, inputs)
    assert t["compiled"], t
    assert t["error"] is None, t["error"]
    real = plain(client, sid, dbid, otype, name, inputs)
    assert real["error"] is None
    assert t["rows"] == real["rows"], (t["rows"], real["rows"])
    assert t["steps"][0]["k"] == "entry"
    assert t["steps"][-1]["k"] == "end"


def test_trace_steps_and_lines(client, db):
    sid, dbid = db
    t = trace(client, sid, dbid, "procedures", "P_LIST", {"MIN_QTY": "0", "TAG": "t"})
    info = client.get(base(sid, dbid) + "/debug/routine", params={"otype": "procedures", "name": "P_LIST"}).json()
    src = info["source"].split("\n")

    def line_of(text):
        return next(i + 1 for i, s in enumerate(src) if text in s)

    rows = [s for s in t["steps"] if s["k"] == "row"]
    assert len(rows) == 4 and [r["row"] for r in rows] == [1, 2, 3, 4]
    assert all(r["line"] == line_of("    SUSPEND;") for r in rows[:3])
    fetches = [s for s in t["steps"] if s["k"] == "fetch"]
    assert len(fetches) == 3 and fetches[0]["line"] == line_of("FOR SELECT")
    assert values_at(t, fetches[1])["Q"] == 0
    # the ELSE IF chain: the 'few' branch was taken for apple (qty 5)
    lbl = [s for s in t["steps"] if s["line"] == line_of("ELSE IF (Q < 10)")]
    assert values_at(t, lbl[0])["LABEL"] == "few; IF"
    # LEAVE L1 was traced before leaving the loop, once
    leave = [s for s in t["steps"] if s["line"] == line_of("LEAVE L1")]
    assert len(leave) == 1 and values_at(t, leave[0])["N"] == 5
    assert t["rows"][-1] == [5, "total", None]
    # inputs are traced, outputs too
    assert values_at(t, t["steps"][0])["TAG"] == "t"
    assert "changed" in t["steps"][1]


def test_trace_handler_exit_and_function(client, db):
    sid, dbid = db
    t = trace(client, sid, dbid, "procedures", "P_CALC", {"A": "7", "B": "0"})
    h = [s for s in t["steps"] if s["k"] == "handler"]
    assert len(h) == 1 and h[0]["caught"]["gdscode"] == 335544321
    assert t["rows"] == [[None, None, "division by zero"]]
    f = trace(client, sid, dbid, "functions", "F_FACT", {"N": "5"})
    assert f["result"] == 120 and f["rows"] == [[120]]
    acc = [values_at(f, s)["ACC"] for s in f["steps"] if s["k"] == "step"]
    assert 24 in acc and 120 in acc


def test_trace_error_is_mapped_to_the_source_line(client, db):
    sid, dbid = db
    info = client.get(base(sid, dbid) + "/debug/routine", params={"otype": "procedures", "name": "P_FAIL"}).json()
    src = info["source"].split("\n")
    t = trace(client, sid, dbid, "procedures", "P_FAIL", {"X": "9"})
    err = t["error"]
    assert err and "too big: 20" in err["message"]
    assert src[err["last_line"] - 1].strip().startswith("EXCEPTION")
    t = trace(client, sid, dbid, "procedures", "P_FAIL", {"X": "1"})
    assert "divide" in t["error"]["message"].lower() or "arithmetic" in t["error"]["message"].lower()
    assert "Y = Y / (X - X)" in src[t["error"]["line"] - 1]
    assert "At source line" in t["error"]["message"]
    y = [values_at(t, s)["Y"] for s in t["steps"]]
    assert y[-1] == 4


def test_writes_are_rolled_back(client, db):
    sid, dbid = db
    before = query(client, sid, dbid, "SELECT SUM(QTY) FROM ITEMS")
    t = trace(client, sid, dbid, "procedures", "P_WRITE", {"V": "10"})
    assert t["rows"] == [[1]] and t["rolled_back"]
    assert query(client, sid, dbid, "SELECT COUNT(*) FROM LOG_T") == [[0]]
    assert query(client, sid, dbid, "SELECT SUM(QTY) FROM ITEMS") == before
    plain(client, sid, dbid, "procedures", "P_WRITE", {"V": "10"})
    assert query(client, sid, dbid, "SELECT COUNT(*) FROM LOG_T") == [[0]]


def test_blob_variables_are_shown_as_size(client, db):
    sid, dbid = db
    t = trace(client, sid, dbid, "procedures", "P_CURSOR")
    bvals = [values_at(t, s)["B"] for s in t["steps"]]
    assert "<BLOB, 9 bytes>" in bvals
    assert t["rows"] == [[17, 2, 3]]


def test_refused_and_warnings(client, db):
    sid, dbid = db
    info = client.get(base(sid, dbid) + "/debug/routine", params={"otype": "procedures", "name": "P_AUTO"}).json()
    assert not info["traceable"] and "AUTONOMOUS" in info["reason"]
    r = client.post(base(sid, dbid) + "/debug/trace", json={"otype": "procedures", "name": "P_AUTO", "inputs": {}})
    assert r.status_code == 400 and "AUTONOMOUS" in r.json()["detail"]
    info = client.get(base(sid, dbid) + "/debug/routine", params={"otype": "procedures", "name": "P_SEQ"}).json()
    assert info["traceable"] and any("sequence" in w for w in info["warnings"])
    info = client.get(base(sid, dbid) + "/debug/routine",
                      params={"otype": "procedures", "name": "P_CALLS_AUTO"}).json()
    assert info["traceable"] and any("P_AUTO" in w and "AUTONOMOUS" in w for w in info["warnings"])
    r = client.post(base(sid, dbid) + "/debug/trace",
                    json={"otype": "procedures", "name": "P_LIST", "inputs": {"NOPE": "1"}})
    assert r.status_code == 400


def test_max_steps_stops_the_run(client, db):
    sid, dbid = db
    t = trace(client, sid, dbid, "functions", "F_FACT", {"N": "1000"}, max_steps=20)
    assert t["stopped"] and len(t["steps"]) == 20 and t["result"] is None


def test_watch_limits_traced_variables(client, db):
    sid, dbid = db
    t = trace(client, sid, dbid, "procedures", "P_LIST", {"MIN_QTY": "0"}, watch=["N"])
    names = [v["name"] for v in t["variables"]]
    assert names == ["N", "ID", "NAME", "LABEL"]
    assert "FBT$2" not in t["instrumented_sql"]


def test_execute_block(client, db):
    sid, dbid = db
    sql = """EXECUTE BLOCK (X INTEGER = ?)
RETURNS (Y INTEGER, S VARCHAR(20))
AS
DECLARE I INTEGER = 0;
BEGIN
  WHILE (I < X) DO
  BEGIN
    I = I + 1;
    Y = I * I;
    S = 'sq ' || I;
    SUSPEND;
  END
END"""
    info = dbg(client, sid, dbid, "parse", sql=sql)
    assert info["traceable"] and [p["name"] for p in info["inputs"]] == ["X"]
    assert info["kind"] == "selectable"
    t = dbg(client, sid, dbid, "trace", otype="block", sql=sql, inputs={"X": "3"})
    assert t["error"] is None, t
    assert t["rows"] == [[1, "sq 1"], [4, "sq 2"], [9, "sq 3"]]
    assert [s["line"] for s in t["steps"] if s["k"] == "row"] == [11, 11, 11]
    real = dbg(client, sid, dbid, "run", otype="block", sql=sql, inputs={"X": "3"})
    assert real["rows"] == t["rows"]
    bad = dbg(client, sid, dbid, "parse", sql="EXECUTE BLOCK AS BEGIN IF (1=1) THEN END")
    assert not bad["traceable"] and bad["reason"]


def test_test_runner(client, db):
    sid, dbid = db
    url = base(sid, dbid) + "/debug/tests"
    cases = [
        {"otype": "procedures", "routine": "P_CALC", "name": "17/5", "inputs": {"A": "17", "B": "5"},
         "expect": {"rows": [["3", "2", "a>b"]]}},
        {"otype": "procedures", "routine": "P_CALC", "name": "wrong", "inputs": {"A": "17", "B": "5"},
         "expect": {"rows": [["4", "2", "a>b"]]}},
        {"otype": "procedures", "routine": "P_CALC", "name": "negative", "inputs": {"A": "-1", "B": "5"},
         "expect": {"error": "A is -1"}},
        {"otype": "functions", "routine": "F_FACT", "name": "5!", "inputs": {"N": "5"}, "expect": {"rows": [[120]]}},
        {"otype": "procedures", "routine": "P_LIST", "name": "list", "inputs": {"MIN_QTY": "10", "TAG": "z"},
         "expect": {"rows": [[3, "plum; END", "many z"], [5, "total", None]]}},
        {"otype": "procedures", "routine": "P_WRITE", "name": "write", "inputs": {"V": "1"}, "expect": {"rows": [[1]]}},
    ]
    ids = []
    for c in cases:
        r = client.post(url, json=c)
        assert r.status_code == 201, r.text
        ids.append(r.json()["id"])
    assert len(client.get(url, params={"otype": "procedures", "routine": "P_CALC"}).json()) == 3
    res = dbg(client, sid, dbid, "tests/run")
    by = {r["name"]: r for r in res["results"]}
    assert by["17/5"]["status"] == "pass"
    assert by["wrong"]["status"] == "fail" and "expected '4', got 3" in by["wrong"]["diffs"][0]
    assert by["negative"]["status"] == "pass"
    assert by["5!"]["status"] == "pass"
    assert by["list"]["status"] == "pass", by["list"]
    assert by["write"]["status"] == "pass"
    assert res["summary"] == {"pass": 5, "fail": 1, "error": 0}
    # a second run still passes: the writes of the first one were rolled back
    res = dbg(client, sid, dbid, "tests/run", ids=[ids[5]])
    assert res["results"][0]["status"] == "pass"
    # update, persistence, delete
    r = client.put(f"{url}/{ids[1]}", json={"expect": {"rows": [["3", "2", "a>b"]]}})
    assert r.status_code == 200
    res = dbg(client, sid, dbid, "tests/run", otype="procedures", routine="P_CALC")
    assert res["summary"]["pass"] == 3
    listed = client.get(url).json()
    assert {t["last_status"] for t in listed} == {"pass"}
    assert client.delete(f"{url}/{ids[0]}").status_code == 204
    assert len(client.get(url).json()) == 5


def test_profile(client, db):
    sid, dbid = db
    p = dbg(client, sid, dbid, "profile", otype="procedures", name="P_LIST", inputs={"MIN_QTY": "0"})
    major = int(client.get(base(sid, dbid) + "/debug/routine",
                           params={"otype": "procedures", "name": "P_LIST"}).json()["engine_version"].split(".")[0])
    if major < 5:
        assert not p["available"] and "Firebird 5" in p["reason"]
        return
    assert p["available"], p
    assert p["run"]["error"] is None and len(p["run"]["rows"]) == 4
    info = client.get(base(sid, dbid) + "/debug/routine", params={"otype": "procedures", "name": "P_LIST"}).json()
    src = info["source"].split("\n")
    by_line = {ln["line"]: ln for ln in p["lines"]}
    n_line = next(i + 1 for i, s in enumerate(src) if "N = N + 1;" in s)
    assert by_line[n_line]["count"] == 3
    # writes of a profiled run are rolled back too
    dbg(client, sid, dbid, "profile", otype="procedures", name="P_WRITE", inputs={"V": "1"})
    assert query(client, sid, dbid, "SELECT COUNT(*) FROM LOG_T") == [[0]]


# More PSQL shapes: each is traced and must give the same outputs (or error) as the routine itself.
SHAPES = [
    ("P_NESTED", {"N": "6"}, """CREATE PROCEDURE P_NESTED (N INTEGER) RETURNS (I INTEGER, J INTEGER, K VARCHAR(20))
AS
BEGIN
  I = 0;
  OUTER_L: WHILE (I < N) DO
  BEGIN
    I = I + 1;
    IF (MOD(I, 2) = 0) THEN CONTINUE;
    J = 0;
    INNER_L: WHILE (1 = 1) DO
      IF (J >= I) THEN LEAVE INNER_L; ELSE IF (J > 3) THEN LEAVE OUTER_L; ELSE J = J + 1;
    K = IIF(J > 2, 'big', 'small');
    SUSPEND;
  END
END"""),
    ("P_CURSOR_UPD", {}, """CREATE PROCEDURE P_CURSOR_UPD RETURNS (ID INTEGER, QTY INTEGER)
AS
BEGIN
  FOR SELECT ID, QTY FROM ITEMS ORDER BY ID FOR UPDATE INTO :ID, :QTY AS CURSOR CUR DO
  BEGIN
    UPDATE ITEMS SET QTY = QTY * 10 WHERE CURRENT OF CUR;
    SELECT QTY FROM ITEMS WHERE ID = :ID INTO :QTY;
    SUSPEND;
  END
END"""),
    ("P_SUBPROC", {"X": "4"}, """CREATE PROCEDURE P_SUBPROC (X INTEGER) RETURNS (Y INTEGER, Z INTEGER)
AS
DECLARE PROCEDURE INNER_P (A INTEGER) RETURNS (B INTEGER)
AS
DECLARE C INTEGER = 1;
BEGIN
  WHILE (C <= A) DO
  BEGIN
    B = C * 10;
    C = C + 1;
    SUSPEND;
  END
END
DECLARE FUNCTION INNER_F (A INTEGER) RETURNS INTEGER
AS
BEGIN
  IF (A > 2) THEN RETURN A; ELSE RETURN -A;
END
BEGIN
  FOR SELECT B FROM INNER_P(:X) INTO :Y DO
  BEGIN
    Z = INNER_F(Y / 10);
    SUSPEND;
  END
END"""),
    ("P_HANDLERS", {"V": "3"}, """CREATE PROCEDURE P_HANDLERS (V INTEGER) RETURNS (STAGE VARCHAR(30), CODE INTEGER)
AS
DECLARE X INTEGER;
BEGIN
  BEGIN
    BEGIN
      IF (V > 2) THEN EXCEPTION E_NEG;
      STAGE = 'not reached';
      WHEN SQLCODE -803, GDSCODE lock_conflict DO STAGE = 'wrong handler';
    END
    WHEN EXCEPTION E_NEG DO
    BEGIN
      STAGE = 'inner caught';
      SUSPEND;
      EXCEPTION;
    END
  END
  WHEN ANY DO
  BEGIN
    STAGE = 'outer caught';
    CODE = GDSCODE;
    SUSPEND;
  END
END"""),
    ("P_STRINGS", {"S": "it's; END"}, """CREATE PROCEDURE P_STRINGS (S VARCHAR(50)) RETURNS (R VARCHAR(200), L INTEGER)
AS
DECLARE T VARCHAR(100) = 'BEGIN; -- not a comment';
BEGIN
  -- IF (1 = 1) THEN EXIT;
  /* WHILE (1=1) DO BEGIN END */
  R = S || ' | ' || T || ' | ' || q'{a 'quoted' ; END}' || ' | "x"';
  IF (R CONTAINING 'END') THEN R = R || ' /* has END */';
  L = CHAR_LENGTH(R);
  IF (CASE WHEN L > 10 THEN 1 ELSE 0 END = 1) THEN L = -L;
END"""),
    ("P_SEL_EXIT", {}, """CREATE PROCEDURE P_SEL_EXIT RETURNS (ID INTEGER)
AS
BEGIN
  FOR SELECT ID FROM ITEMS ORDER BY ID INTO :ID DO
  BEGIN
    IF (ID = 3) THEN EXIT;
    SUSPEND;
  END
  ID = 99;
  SUSPEND;
END"""),
    ("P_FOR_ES", {"T": "ITEMS"}, """CREATE PROCEDURE P_FOR_ES (T VARCHAR(31)) RETURNS (N VARCHAR(40), Q INTEGER)
AS
BEGIN
  FOR EXECUTE STATEMENT ('SELECT NAME, QTY FROM ' || T || ' WHERE QTY > ? ORDER BY ID') (0)
    INTO :N, :Q
  DO
    IF (Q > 10) THEN SUSPEND; ELSE BEGIN N = UPPER(N); SUSPEND; END
END"""),
    ("P_WHEN_SUSPEND", {}, """CREATE PROCEDURE P_WHEN_SUSPEND RETURNS (MSG VARCHAR(40))
AS
DECLARE I INTEGER = 0;
BEGIN
  WHILE (I < 3) DO
  BEGIN
    I = I + 1;
    BEGIN
      IF (I = 2) THEN I = I / 0;
      MSG = 'ok ' || I;
      SUSPEND;
      WHEN ANY DO
      BEGIN
        MSG = 'failed at ' || I;
        SUSPEND;
      END
    END
  END
END"""),
    ("P_EXEC_EXIT", {"A": "5"}, """CREATE PROCEDURE P_EXEC_EXIT (A INTEGER) RETURNS (R INTEGER)
AS
BEGIN
  R = A;
  IF (A > 3) THEN
  BEGIN
    R = R * 100;
    EXIT;
  END
  R = -1;
END"""),
    ("P_UPDATE_RC", {}, """CREATE PROCEDURE P_UPDATE_RC RETURNS (RC INTEGER, ID INTEGER)
AS
BEGIN
  UPDATE ITEMS SET QTY = QTY WHERE QTY > 1;
  RC = ROW_COUNT;
  INSERT INTO LOG_T (ID, MSG) VALUES (1, 'x') RETURNING ID INTO :ID;
  EXECUTE PROCEDURE P_DOMAIN(2) RETURNING_VALUES :ID;
END"""),
    ("P_NOT_NULL_OUT", {"A": "2"}, """CREATE PROCEDURE P_NOT_NULL_OUT (A INTEGER NOT NULL) RETURNS (R INTEGER NOT NULL, D D_POS)
AS
DECLARE T D_POS;
BEGIN
  T = A;
  R = T + 1;
  D = R;
END"""),
]


@pytest.mark.parametrize("name,inputs,ddl", SHAPES, ids=[s[0] for s in SHAPES])
def test_psql_shapes(client, db, name, inputs, ddl):
    sid, dbid = db
    r = client.post(base(sid, dbid) + "/execute", json={"sql": ddl})
    assert not r.json()["error"], r.json()["error"]
    t = trace(client, sid, dbid, "procedures", name, inputs)
    assert t["compiled"], t["error"]
    real = plain(client, sid, dbid, "procedures", name, inputs)
    assert t["rows"] == real["rows"], (t["rows"], real["rows"], t["instrumented_sql"])
    assert (t["error"] is None) == (real["error"] is None), (t["error"], real["error"])
