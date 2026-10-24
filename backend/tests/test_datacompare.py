"""Data compare (milestone 1.2): literal formatting, FK ordering, and compare → sync → compare
round trips against a live Firebird 3/4/5 server."""

from __future__ import annotations

import datetime as dt
import uuid
import zoneinfo
from decimal import Decimal

import pytest

from fbadmin.datacompare import engine
from fbadmin.datacompare.literals import BIN_CHUNK, TEXT_CHUNK, sql_literal

from .conftest import DB_DIR, requires_server


# ------------------------------------------------------------------ unit tests
@pytest.mark.parametrize("value, kind, expected", [
    (None, "", "NULL"),
    (None, "blob", "NULL"),
    (True, "", "TRUE"),
    (False, "", "FALSE"),
    (0, "", "0"),
    (-32768, "", "-32768"),
    (9223372036854775807, "", "9223372036854775807"),
    (-170141183460469231731687303715884105728, "int", "-170141183460469231731687303715884105728"),
    (Decimal("123.4500"), "", "123.4500"),
    (Decimal("-0.0001"), "numeric", "-0.0001"),
    (Decimal("1E+2"), "", "100"),
    (Decimal("12345678901234567890123456789012.345678"), "numeric", "12345678901234567890123456789012.345678"),
    (Decimal("1.5E+300"), "decfloat", "CAST('1.5E+300' AS DECFLOAT(34))"),
    (Decimal("-12.5"), "decfloat", "CAST('-12.5' AS DECFLOAT(34))"),
    (Decimal("NaN"), "decfloat", "CAST('NaN' AS DECFLOAT(34))"),
    (Decimal("-Infinity"), "", "CAST('-Infinity' AS DECFLOAT(34))"),
    (1.5, "", "1.5"),
    (1e-05, "", "1e-05"),
    (1.100000023841858, "", "1.100000023841858"),
    (-1.5e300, "", "-1.5e+300"),
    ("", "", "''"),
    ("O'Brien", "", "'O''Brien'"),
    ("semi; -- not a comment /* */", "", "'semi; -- not a comment /* */'"),
    ("two\nlines", "", "_UTF8 x'74776F0A6C696E6573'"),
    ("tab\tok", "", "'tab\tok'"),
    ("é\r\n", "blob_text", "_UTF8 x'C3A90D0A'"),
    ("ăîșț €", "char", "'ăîșț €'"),
    (b"", "", "x''"),
    (b"\x00\x01\xfe\xff", "", "x'0001FEFF'"),
    (bytearray(b"ab"), "blob", "x'6162'"),
    (dt.date(2024, 2, 29), "", "DATE '2024-02-29'"),
    (dt.date(1, 1, 1), "", "DATE '0001-01-01'"),
    (dt.time(0, 0), "", "TIME '00:00:00'"),
    (dt.time(23, 59, 59, 999900), "", "TIME '23:59:59.9999'"),
    (dt.time(10, 20, 30, 500000), "", "TIME '10:20:30.5000'"),
    (dt.datetime(2024, 3, 1, 10, 20, 30, 123400), "", "TIMESTAMP '2024-03-01 10:20:30.1234'"),
    (dt.datetime(9999, 12, 31, 23, 59, 59), "", "TIMESTAMP '9999-12-31 23:59:59'"),
    (dt.datetime(2024, 3, 1, 10, 20, 30, tzinfo=zoneinfo.ZoneInfo("Europe/Bucharest")), "",
     "TIMESTAMP '2024-03-01 10:20:30 Europe/Bucharest'"),
    (dt.datetime(2024, 3, 1, 10, 20, 30, tzinfo=dt.timezone(dt.timedelta(hours=-3, minutes=-30))), "",
     "TIMESTAMP '2024-03-01 10:20:30 -03:30'"),
    (dt.time(8, 0, tzinfo=dt.timezone(dt.timedelta(hours=5, minutes=45))), "", "TIME '08:00:00 +05:45'"),
    ("2024-03-01 10:20:30.1234 Europe/Bucharest", "timestamp_tz", "TIMESTAMP '2024-03-01 10:20:30.1234 Europe/Bucharest'"),
    ("10:20:30.0000 -03:00", "time_tz", "TIME '10:20:30.0000 -03:00'"),
])
def test_sql_literal(value, kind, expected):
    assert sql_literal(value, kind) == expected


def test_long_literals_are_chunked():
    text = "é'" * TEXT_CHUNK  # 2 chunks of TEXT_CHUNK characters
    lit = sql_literal(text, "blob_text")
    assert lit.startswith("CAST('") and " AS BLOB SUB_TYPE TEXT) || '" in lit
    assert lit.count(" || ") == 1
    lit = sql_literal("é\n" * TEXT_CHUNK, "blob_text")  # control characters: UTF-8 hex chunks
    assert lit.startswith("CAST(_UTF8 x'") and " AS BLOB SUB_TYPE TEXT CHARACTER SET UTF8) || _UTF8 x'" in lit
    # a long VARCHAR is concatenated without the BLOB cast
    assert not sql_literal("x" * (TEXT_CHUNK + 1), "").startswith("CAST")
    data = bytes(range(256)) * (BIN_CHUNK // 256 + 50)
    lit = sql_literal(data, "blob")
    assert lit.startswith("CAST(x'") and " AS BLOB SUB_TYPE BINARY) || x'" in lit
    hexed = "".join(p.split("x'")[1].split("'")[0] for p in lit.split(" || "))
    assert bytes.fromhex(hexed) == data


def test_literal_rejects_non_finite_float():
    with pytest.raises(ValueError):
        sql_literal(float("nan"))
    with pytest.raises(ValueError):
        sql_literal(object())


def test_fk_order_parents_first_and_cycles():
    deps = {"ORDER_LINE": {"ORDERS", "PRODUCT"}, "ORDERS": {"CUSTOMER"}, "PRODUCT": {"CATEGORY"}}
    order = engine.fk_order(["ORDER_LINE", "PRODUCT", "ORDERS", "CUSTOMER", "CATEGORY", "LOOSE"], deps)
    for child, parents in deps.items():
        for p in parents:
            assert order.index(p) < order.index(child)
    assert set(order) == {"ORDER_LINE", "PRODUCT", "ORDERS", "CUSTOMER", "CATEGORY", "LOOSE"}
    # dependencies from the second database are merged; parents outside the list are ignored
    order = engine.fk_order(["A", "B", "C"], {"A": {"B"}}, {"B": {"C", "OTHER"}})
    assert order == ["C", "B", "A"]
    # a cycle does not lose tables
    assert sorted(engine.fk_order(["X", "Y"], {"X": {"Y"}, "Y": {"X"}})) == ["X", "Y"]


def test_same_values():
    assert engine.same(None, None)
    assert not engine.same(None, 0)
    assert not engine.same("", None)
    assert engine.same(Decimal("1.50"), Decimal("1.5"))
    assert engine.same(float("nan"), float("nan"))
    assert engine.same(Decimal("NaN"), Decimal("NaN"))
    assert not engine.same(True, 1)
    assert not engine.same("1", 1)
    assert engine.same(b"\x00", b"\x00")


def test_external_sort_and_order_check():
    rows = [((i * 7919 % 1000, "k"), [i]) for i in range(1000)]
    out = list(engine.external_sort(iter(rows), run_rows=64))  # many spilled runs
    assert [k for k, _ in out] == sorted(k for k, _ in rows)
    assert list(engine._checked(iter(out), "source")) == out
    with pytest.raises(engine.OutOfOrder):
        list(engine._checked(iter([((2,), []), ((1,), [])]), "source"))
    with pytest.raises(engine.TableError, match="Duplicate key"):
        list(engine._checked(iter([((1,), []), ((1,), [])]), "target"))


def test_statement_file_roundtrip(tmp_path):
    f = engine.StmtFile(tmp_path / "s.bin")
    f.add("DELETE FROM \"x'y\" WHERE ID = 1")
    big = b"\x01" * 5000
    f.add("INSERT INTO T (A, B, C) VALUES (", (1, ""), ", ", ("a;\nb", "blob_text"), ", ", (big, "blob"), ")")
    f.close()
    a, b = list(f)
    assert a.sql == a.param_sql == "DELETE FROM \"x'y\" WHERE ID = 1" and a.params == []
    # short values are literals; long BLOBs are parameters when applied, literals in the script
    assert b.param_sql == "INSERT INTO T (A, B, C) VALUES (1, _UTF8 x'613B0A62', ?)"
    assert b.params == [big]
    assert b.sql == "INSERT INTO T (A, B, C) VALUES (1, _UTF8 x'613B0A62', x'" + "01" * 5000 + "')"
    f.reset()
    assert f.count == 0


def test_preview_shortens_values():
    assert engine.preview("x" * 500).endswith("… (500 characters)")
    assert engine.preview(b"\x01" * 100).endswith("… (100 bytes)")
    assert engine.preview(Decimal("1.5")) == "1.5"


# ------------------------------------------------------------------ integration
COMMON = """
CREATE TABLE PARENT (ID INTEGER NOT NULL PRIMARY KEY, NAME VARCHAR(40));
CREATE TABLE CHILD (ID INTEGER NOT NULL PRIMARY KEY, PARENT_ID INTEGER REFERENCES PARENT (ID), NOTE VARCHAR(20));
CREATE TABLE TYPES (ID INTEGER NOT NULL PRIMARY KEY, SI SMALLINT, BI BIGINT, NUM NUMERIC(18,4), DCM DECIMAL(9,2),
  FL FLOAT, DBL DOUBLE PRECISION, D DATE, TM TIME, TS TIMESTAMP, B BOOLEAN, CH CHAR(10), VC VARCHAR(100),
  W VARCHAR(20) CHARACTER SET WIN1252, OCT CHAR(4) CHARACTER SET OCTETS, BT BLOB SUB_TYPE TEXT,
  BB BLOB SUB_TYPE BINARY);
CREATE TABLE COMPOSITE (A INTEGER NOT NULL, B VARCHAR(10) NOT NULL, V INTEGER, PRIMARY KEY (A, B));
CREATE TABLE UQ (CODE VARCHAR(10) UNIQUE, V INTEGER);
CREATE TABLE NOKEY (X INTEGER);
CREATE TABLE CI (NAME VARCHAR(20) CHARACTER SET UTF8 NOT NULL PRIMARY KEY COLLATE UNICODE_CI, V INTEGER);
CREATE TABLE "Mixed Case" ("Id" INTEGER NOT NULL PRIMARY KEY, "Value" VARCHAR(10));
COMMIT;
"""
SOURCE_ONLY_DDL = "CREATE TABLE COLS (ID INTEGER NOT NULL PRIMARY KEY, SAME_COL INTEGER, SRC_ONLY INTEGER);\n" \
                  "CREATE TABLE ONLY_SRC (ID INTEGER NOT NULL PRIMARY KEY);\nCOMMIT;"
TARGET_ONLY_DDL = "CREATE TABLE COLS (ID INTEGER NOT NULL PRIMARY KEY, SAME_COL INTEGER, TGT_ONLY INTEGER DEFAULT 7 NOT NULL);\n" \
                  "COMMIT;"
FB4_DDL = """
CREATE TABLE T4 (ID INT128 NOT NULL PRIMARY KEY, N38 NUMERIC(38,6), DF DECFLOAT(34), DF16 DECFLOAT(16),
  TSTZ TIMESTAMP WITH TIME ZONE, TTZ TIME WITH TIME ZONE, BIN BINARY(4), VB VARBINARY(10),
  GA INTEGER GENERATED ALWAYS AS IDENTITY);
COMMIT;
"""

BIG_TEXT = ("Ünïcödé 'quoted' ; -- not a comment\n" * 3000)  # ~110 KB, several literal chunks
BIG_BIN = bytes(range(256)) * 300  # 76.8 KB

SHARED_ROWS = """
INSERT INTO PARENT VALUES (1, 'one');
INSERT INTO PARENT VALUES (2, 'two');
INSERT INTO PARENT VALUES (3, 'three');
INSERT INTO TYPES VALUES (1, 1, 2, 3.5, 4.25, 1.5, 2.5, DATE '2024-01-02', TIME '10:11:12.3456',
  TIMESTAMP '2024-01-02 10:11:12.3456', TRUE, 'abc', 'text', 'café', x'01020304', 'blob text', x'DEADBEEF');
INSERT INTO COMPOSITE VALUES (1, 'a', 1);
INSERT INTO COMPOSITE VALUES (1, 'b', 2);
INSERT INTO UQ VALUES ('same', 1);
INSERT INTO UQ VALUES (NULL, 5);
INSERT INTO NOKEY VALUES (1);
INSERT INTO "Mixed Case" VALUES (1, 'same');
INSERT INTO CI VALUES ('Same', 0);
COMMIT;
"""
SOURCE_ROWS = """
INSERT INTO PARENT VALUES (4, 'four');
INSERT INTO CHILD VALUES (10, 4, 'new child');
INSERT INTO CHILD VALUES (11, 1, 'moved');
INSERT INTO TYPES VALUES (2, -1, 9223372036854775807, -0.0001, NULL, 3.25, -1e300, DATE '0001-01-01', TIME '00:00:00',
  TIMESTAMP '9999-12-31 23:59:59.9999', FALSE, 'x', NULL, 'œuvre €', x'00000000', NULL, NULL);
INSERT INTO TYPES (ID, VC, BT, BB) VALUES (3, 'O''Brien; -- x', ?, ?);
INSERT INTO COMPOSITE VALUES (2, 'a', 3);
UPDATE COMPOSITE SET V = 20 WHERE A = 1 AND B = 'b';
INSERT INTO UQ VALUES ('diff', 1);
INSERT INTO UQ VALUES ('src', 1);
INSERT INTO CI VALUES ('a', 1);
INSERT INTO CI VALUES ('B', 2);
INSERT INTO CI VALUES ('c', 3);
INSERT INTO CI VALUES ('é', 4);
INSERT INTO COLS VALUES (1, 1, 100);
INSERT INTO COLS VALUES (2, 2, 200);
INSERT INTO "Mixed Case" VALUES (2, 'it''s');
COMMIT;
"""
TARGET_ROWS = """
INSERT INTO PARENT VALUES (5, 'five');
INSERT INTO CHILD VALUES (11, 2, 'moved');
INSERT INTO CHILD VALUES (12, 5, 'orphan soon');
INSERT INTO TYPES VALUES (2, -1, 1, 0, 7.5, 3.25, -1e300, DATE '0001-01-02', TIME '00:00:00',
  TIMESTAMP '9999-12-31 23:59:59.9999', NULL, 'x', 'was null', 'oeuvre', x'00000001', 'old', x'00');
INSERT INTO TYPES (ID) VALUES (4);
INSERT INTO COMPOSITE VALUES (3, 'z', 9);
INSERT INTO UQ VALUES ('diff', 2);
INSERT INTO UQ VALUES (NULL, 6);
INSERT INTO CI VALUES ('d', 1);
INSERT INTO CI VALUES ('E', 2);
INSERT INTO COLS VALUES (1, 1, 1);
INSERT INTO COLS VALUES (2, 3, 1);
INSERT INTO "Mixed Case" VALUES (3, 'gone');
COMMIT;
"""
FB4_SOURCE = """
INSERT INTO T4 (ID, N38, DF, DF16, TSTZ, TTZ, BIN, VB) VALUES (170141183460469231731687303715884105727,
  12345678901234567890123456789012.345678, 1.5E+300, -12.5, TIMESTAMP '2024-03-01 10:20:30.1234 Europe/Bucharest',
  TIME '10:20:30.5 America/New_York', x'01020300', x'0A');
INSERT INTO T4 (ID, N38, DF, DF16, TSTZ, TTZ, BIN, VB) VALUES (-170141183460469231731687303715884105728,
  -1.5, CAST('NaN' AS DECFLOAT(34)), 1, TIMESTAMP '2024-03-01 10:20:30 +05:30', TIME '10:20:30 -03:00', x'01', x'');
INSERT INTO T4 (ID, N38) VALUES (5, 1);
COMMIT;
"""
FB4_TARGET = """
INSERT INTO T4 (ID, N38, DF, DF16, TSTZ, TTZ, BIN, VB) VALUES (170141183460469231731687303715884105727,
  12345678901234567890123456789012.345677, 1.5E+300, -12.5, TIMESTAMP '2024-03-01 10:20:30.1234 Europe/Lisbon',
  TIME '10:20:30.5 America/New_York', x'01020300', x'0B');
INSERT INTO T4 (ID, N38) VALUES (6, 1);
COMMIT;
"""


def base(sid, dbid):
    return f"/api/servers/{sid}/databases/{dbid}"


def execute(client, sid, dbid, sql):
    r = client.post(base(sid, dbid) + "/execute", json={"sql": sql})
    assert r.status_code == 200, r.text
    assert r.json()["error"] is None, r.json()["error"]
    return r.json()


def wait_job(client, job, timeout=120):
    import time

    end = time.time() + timeout
    while time.time() < end:
        j = client.get(f"/api/jobs/{job['id']}").json()
        if j["status"] != "running":
            return j
        time.sleep(0.2)
    raise AssertionError("job did not finish")


def engine_major(client, sid, dbid):
    r = execute(client, sid, dbid, "SELECT RDB$GET_CONTEXT('SYSTEM', 'ENGINE_VERSION') FROM RDB$DATABASE")
    return int(r["results"][0]["rows"][0][0].split(".")[0])


@pytest.fixture
def pair(client, server):
    sid = server["id"]
    ids = []
    for tag in ("src", "tgt"):
        path = f"{DB_DIR}/fbadmin_dc_{tag}_{uuid.uuid4().hex[:8]}.fdb"
        r = client.post(f"/api/servers/{sid}/databases/create", json={"name": f"dc_{tag}", "path": path})
        assert r.status_code == 201, r.text
        ids.append(r.json()["id"])
    yield sid, ids[0], ids[1]
    for dbid in ids:
        client.post(base(sid, dbid) + "/drop")


def insert_blob_row(client, sid, dbid):
    """Row 3 of TYPES with a large text and binary BLOB (via the driver, not a literal)."""
    from fbadmin.api.schema import open_db

    conn = open_db(sid, dbid)
    try:
        conn.cursor().execute("INSERT INTO TYPES (ID, VC, BT, BB) VALUES (3, 'O''Brien; -- x', ?, ?)", [BIG_TEXT, BIG_BIN])
        conn.commit()
    finally:
        conn.close()


@pytest.fixture
def populated(client, pair):
    sid, src, tgt = pair
    fb4 = engine_major(client, sid, src) >= 4
    for dbid, ddl, rows, rows4 in ((src, SOURCE_ONLY_DDL, SOURCE_ROWS, FB4_SOURCE), (tgt, TARGET_ONLY_DDL, TARGET_ROWS, FB4_TARGET)):
        execute(client, sid, dbid, COMMON)
        execute(client, sid, dbid, ddl)
        if fb4:
            execute(client, sid, dbid, FB4_DDL)
        execute(client, sid, dbid, SHARED_ROWS)
        execute(client, sid, dbid, "\n".join(line for line in rows.splitlines() if "?" not in line))
        if fb4:
            execute(client, sid, dbid, rows4)
    insert_blob_row(client, sid, src)
    return sid, src, tgt, fb4


def run_compare(client, sid, src, tgt, tables=None, **opts):
    if tables is None:
        listing = client.post("/api/datacompare/tables", json={"source": {"server_id": sid, "db_id": src},
                                                               "target": {"server_id": sid, "db_id": tgt}})
        assert listing.status_code == 200, listing.text
        tables = [{"name": t["name"]} for t in listing.json()["tables"] if t["comparable"]]
    r = client.post("/api/datacompare/compare", json={"source": {"server_id": sid, "db_id": src},
                                                      "target": {"server_id": sid, "db_id": tgt},
                                                      "tables": tables, **opts})
    assert r.status_code == 202, r.text
    j = wait_job(client, r.json())
    assert j["status"] == "finished", "\n".join(j["log"])
    res = client.get(f"/api/datacompare/jobs/{j['id']}").json()
    errors = {t["name"]: t["error"] for t in res["tables"] if t["status"] != "done"}
    assert not errors, errors
    return j["id"], res, {t["name"]: t for t in res["tables"]}


@requires_server
def test_table_listing(client, populated):
    sid, src, tgt, fb4 = populated
    r = client.post("/api/datacompare/tables", json={"source": {"server_id": sid, "db_id": src},
                                                     "target": {"server_id": sid, "db_id": tgt},
                                                     "ignore_columns": ["NOTE"]})
    assert r.status_code == 200, r.text
    t = {x["name"]: x for x in r.json()["tables"]}
    assert t["NOKEY"]["comparable"] is False and "No primary key" in t["NOKEY"]["reason"]
    assert t["ONLY_SRC"]["comparable"] is False and t["ONLY_SRC"]["reason"] == "Only in the source database"
    assert t["UQ"]["key"] == ["CODE"] and t["UQ"]["key_type"] == "UNIQUE"
    assert t["COMPOSITE"]["key"] == ["A", "B"] and t["COMPOSITE"]["key_type"] == "PRIMARY KEY"
    assert t["COLS"]["source_only"] == ["SRC_ONLY"] and t["COLS"]["target_only"] == ["TGT_ONLY"]
    assert t["COLS"]["columns"] == ["ID", "SAME_COL"]
    assert t["CHILD"]["ignored"] == ["NOTE"]
    assert ("T4" in t) == fb4
    # the same database on both sides is refused
    r = client.post("/api/datacompare/tables", json={"source": {"server_id": sid, "db_id": src},
                                                     "target": {"server_id": sid, "db_id": src}})
    assert r.status_code == 400


@requires_server
@pytest.mark.parametrize("how", ["apply", "script"])
def test_compare_sync_and_compare_again(client, populated, how):
    sid, src, tgt, fb4 = populated
    job_id, res, t = run_compare(client, sid, src, tgt, max_differences=50)
    assert {s["name"] for s in res["skipped"]} == set()
    assert (t["PARENT"]["only_source"], t["PARENT"]["only_target"], t["PARENT"]["different"], t["PARENT"]["identical"]) == (1, 1, 0, 3)
    assert (t["CHILD"]["only_source"], t["CHILD"]["only_target"], t["CHILD"]["different"]) == (1, 1, 1)
    assert t["CHILD"]["changed_columns"] == {"PARENT_ID": 1}
    assert (t["TYPES"]["only_source"], t["TYPES"]["only_target"], t["TYPES"]["different"], t["TYPES"]["identical"]) == (1, 1, 1, 1)
    assert set(t["TYPES"]["changed_columns"]) == {"BI", "NUM", "DCM", "D", "B", "VC", "W", "OCT", "BT", "BB"}
    assert (t["COMPOSITE"]["only_source"], t["COMPOSITE"]["only_target"], t["COMPOSITE"]["different"]) == (1, 1, 1)
    assert (t["UQ"]["only_source"], t["UQ"]["different"], t["UQ"]["identical"], t["UQ"]["null_keys"]) == (1, 1, 1, 3)
    assert (t["COLS"]["different"], t["COLS"]["identical"]) == (1, 1)
    assert t["Mixed Case"]["only_source"] == 1 and t["Mixed Case"]["only_target"] == 1
    # case-insensitive collation: the server's ORDER BY disagrees with Python's ordering -> sorted in fbadmin
    assert t["CI"]["ordering"] == "python"
    assert (t["CI"]["only_source"], t["CI"]["only_target"], t["CI"]["identical"]) == (4, 2, 1)
    assert t["PARENT"]["ordering"] == "server"
    if fb4:
        assert (t["T4"]["only_source"], t["T4"]["only_target"], t["T4"]["different"]) == (2, 1, 1)
        assert set(t["T4"]["changed_columns"]) == {"N38", "TSTZ", "VB"}

    # sample rows with the changed columns
    s = client.get(f"/api/datacompare/jobs/{job_id}/samples", params={"table": "CHILD"}).json()
    assert s["columns"] == ["ID", "PARENT_ID", "NOTE"]
    assert s["update"] == [{"source": [11, 1, "moved"], "target": [11, 2, "moved"], "changed": ["PARENT_ID"]}]
    assert s["insert"][0]["source"][0] == 10 and s["delete"][0]["target"][0] == 12
    s = client.get(f"/api/datacompare/jobs/{job_id}/samples", params={"table": "TYPES"}).json()
    big = s["insert"][0]["source"][s["columns"].index("BT")]
    assert big.endswith("characters)")

    script = client.get(f"/api/datacompare/jobs/{job_id}/script")
    assert script.status_code == 200
    text = script.text
    assert "attachment" in script.headers["content-disposition"]
    # parents inserted before children, children deleted before parents
    assert text.index("\nINSERT INTO PARENT ") < text.index("\nINSERT INTO CHILD ")
    assert text.index("\nDELETE FROM CHILD ") < text.index("\nDELETE FROM PARENT ")
    assert text.index("\nINSERT INTO") < text.index("\nUPDATE ") < text.index("\nDELETE FROM")
    assert 'INSERT INTO "Mixed Case" ("Id", "Value") VALUES (2, \'it\'\'s\')' in text
    assert text.rstrip().endswith("COMMIT;")

    if how == "apply":
        r = client.post(base(sid, tgt) + f"/datacompare/{job_id}/apply", json={"confirm": False})
        assert r.status_code == 400
        r = client.post(base(sid, tgt) + f"/datacompare/{job_id}/apply", json={"confirm": True})
        assert r.status_code == 202, r.text
        j = wait_job(client, r.json())
        assert j["status"] == "finished", "\n".join(j["log"])
        assert client.get(f"/api/datacompare/jobs/{job_id}").json()["applied"] is True
        r = client.post(base(sid, tgt) + f"/datacompare/{job_id}/apply", json={"confirm": True})
        assert r.status_code == 400  # not twice
    else:  # the downloaded script runs in fbadmin's script runner (Query Tool)
        execute(client, sid, tgt, text)

    _, res2, t2 = run_compare(client, sid, src, tgt)
    for name, st in t2.items():
        assert (st["only_source"], st["only_target"], st["different"]) == (0, 0, 0), (name, st)
    assert res2["statements"] == 0
    # the big BLOBs arrived intact
    rows = execute(client, sid, tgt, "SELECT OCTET_LENGTH(BB), CHAR_LENGTH(BT) FROM TYPES WHERE ID = 3")
    assert rows["results"][0]["rows"][0] == [len(BIG_BIN), len(BIG_TEXT)]
    # columns that exist only in the target kept their default
    rows = execute(client, sid, tgt, "SELECT TGT_ONLY FROM COLS ORDER BY ID")
    assert rows["results"][0]["rows"] == [[1], [1]]


@requires_server
def test_options_filter_directions_and_ignore(client, populated):
    sid, src, tgt, _ = populated
    job_id, res, t = run_compare(
        client, sid, src, tgt,
        tables=[{"name": "PARENT", "where": "ID <> 4"}, {"name": "CHILD", "ignore_columns": ["PARENT_ID"]},
                {"name": "NOKEY"}],
        inserts=False, updates=True, deletes=True, max_differences=0)
    assert res["skipped"] == [{"name": "NOKEY", "reason": "No primary key or unique constraint on columns present in both databases"}]
    assert (t["PARENT"]["only_source"], t["PARENT"]["only_target"]) == (0, 1)
    assert t["CHILD"]["different"] == 0 and t["CHILD"]["ignored"] == ["PARENT_ID"]
    text = client.get(f"/api/datacompare/jobs/{job_id}/script").text
    assert "INSERT INTO" not in text and "DELETE FROM PARENT WHERE ID = 5" in text
    s = client.get(f"/api/datacompare/jobs/{job_id}/samples", params={"table": "PARENT"}).json()
    assert s["delete"] == []  # max_differences=0 keeps no samples


@requires_server
def test_apply_rolls_back_when_target_changed(client, populated):
    sid, src, tgt, _ = populated
    job_id, _, _ = run_compare(client, sid, src, tgt, tables=[{"name": "PARENT"}, {"name": "CHILD"}])
    execute(client, sid, tgt, "DELETE FROM CHILD WHERE ID = 12; COMMIT;")  # a statement will find no row
    r = client.post(base(sid, tgt) + f"/datacompare/{job_id}/apply", json={"confirm": True})
    j = wait_job(client, r.json())
    assert j["status"] == "failed" and "changed no row" in j["error"]
    # nothing was applied: PARENT 4 was not inserted
    rows = execute(client, sid, tgt, "SELECT COUNT(*) FROM PARENT WHERE ID = 4")
    assert rows["results"][0]["rows"] == [[0]]


@requires_server
def test_apply_refused_on_read_only_target(client, populated):
    from .conftest import HOST, PASSWORD, PORT, USER

    sid, src, tgt, _ = populated
    path = client.get(f"/api/servers/{sid}").json()["databases"]
    tpath = next(d["path"] for d in path if d["id"] == tgt)
    r = client.post("/api/servers", json={"name": "ro", "host": HOST, "port": PORT, "user": USER,
                                          "password": PASSWORD, "read_only": True})
    assert r.status_code == 201, r.text
    ro = r.json()["id"]
    rdb = client.post(f"/api/servers/{ro}/databases", json={"name": "tgt", "path": tpath}).json()["id"]
    r = client.post("/api/datacompare/compare", json={"source": {"server_id": sid, "db_id": src},
                                                      "target": {"server_id": ro, "db_id": rdb},
                                                      "tables": [{"name": "PARENT"}]})
    assert r.status_code == 202, r.text
    j = wait_job(client, r.json())
    assert j["status"] == "finished", "\n".join(j["log"])  # comparing only reads
    r = client.post(base(ro, rdb) + f"/datacompare/{j['id']}/apply", json={"confirm": True})
    assert r.status_code == 403
    assert r.json()["code"] == "read_only"
    # the comparison belongs to its target
    r = client.post(base(sid, tgt) + f"/datacompare/{j['id']}/apply", json={"confirm": True})
    assert r.status_code == 400


# ------------------------------------------------------------------ 2.1: ordering, collations, saved runs
def test_parents_first_orders_self_references():
    # rows: ((own key, parent key),) for one foreign key; row 0 references row 2, row 1 references row 0
    rows = [(((1,), (3,)),), (((2,), (1,)),), (((3,), (None,)),), (((4,), (99,)),)]
    order, cycles = engine.parents_first(rows)
    assert cycles == 0
    assert order.index(2) < order.index(0) < order.index(1)
    assert sorted(order) == [0, 1, 2, 3]
    # no row references another row of the list: nothing to reorder
    assert engine.parents_first([(((1,), (7,)),), (((2,), (None,)),)]) == (None, 0)
    # a cycle keeps every row and is reported
    order, cycles = engine.parents_first([(((1,), (2,)),), (((2,), (1,)),)])
    assert sorted(order) == [0, 1] and cycles == 1


def test_statement_file_reorder(tmp_path):
    f = engine.StmtFile(tmp_path / "s.bin")
    pos = [f.add(f"INSERT INTO T VALUES ({i})") for i in range(4)]
    f.reorder([pos[2], pos[0], pos[3], pos[1]])
    assert [s.sql for s in f] == [f"INSERT INTO T VALUES ({i})" for i in (2, 0, 3, 1)]
    assert f.count == 4


def test_key_normalizer():
    norm = engine.key_normalizer(ci=True, ai=True, pad=True)
    assert norm("Ação  ") == norm("acao") == "acao"
    assert norm(5) == 5
    assert engine.key_normalizer(ci=False, ai=False, pad=True)("Ab ") == "Ab"
    assert engine.key_normalizer(ci=True, ai=False, pad=False)("Straße") == "strasse"


def test_checked_explains_keys_equal_only_in_the_target():
    rows = [(("abc",), ["ABC", 1]), (("abc",), ["abc", 2])]
    with pytest.raises(engine.TableError, match="different in the source database but equal in the target"):
        list(engine._checked(iter(rows), "source", 1, True))


SELF_DDL = """
CREATE TABLE NODE (ID INTEGER NOT NULL PRIMARY KEY, PARENT_ID INTEGER, NAME VARCHAR(20),
  CONSTRAINT FK_NODE_PARENT FOREIGN KEY (PARENT_ID) REFERENCES NODE (ID));
"""


@requires_server
def test_self_referencing_rows_are_inserted_parents_first_and_deleted_children_first(client, pair):
    sid, src, tgt = pair
    for dbid in (src, tgt):
        execute(client, sid, dbid, SELF_DDL)
    # in key order, each child comes before its parent: 1 -> 2 -> 3 (3 is the root)
    execute(client, sid, src, "INSERT INTO NODE VALUES (3, NULL, 'root'); INSERT INTO NODE VALUES (2, 3, 'mid');"
                              "INSERT INTO NODE VALUES (1, 2, 'leaf'); COMMIT;")
    # rows only in the target: 10 -> 11 -> 12, deleted children first
    execute(client, sid, tgt, "INSERT INTO NODE VALUES (12, NULL, 'r'); INSERT INTO NODE VALUES (11, 12, 'm');"
                              "INSERT INTO NODE VALUES (10, 11, 'l'); COMMIT;")
    listing = client.post("/api/datacompare/tables", json={"source": {"server_id": sid, "db_id": src},
                                                           "target": {"server_id": sid, "db_id": tgt}}).json()
    node = next(t for t in listing["tables"] if t["name"] == "NODE")
    assert node["self_references"] == [{"name": "FK_NODE_PARENT", "columns": ["PARENT_ID"], "ref_columns": ["ID"]}]
    job_id, _, t = run_compare(client, sid, src, tgt, tables=[{"name": "NODE"}])
    assert t["NODE"]["only_source"] == 3 and t["NODE"]["only_target"] == 3
    text = client.get(f"/api/datacompare/jobs/{job_id}/script").text
    ins = [text.index(f"VALUES ({i}, ") for i in (3, 2, 1)]
    assert ins == sorted(ins), text
    dels = [text.index(f"WHERE ID = {i}") for i in (10, 11, 12)]
    assert dels == sorted(dels), text
    r = client.post(base(sid, tgt) + f"/datacompare/{job_id}/apply", json={"confirm": True})
    j = wait_job(client, r.json())
    assert j["status"] == "finished", j["error"]
    _, res, _ = run_compare(client, sid, src, tgt, tables=[{"name": "NODE"}])
    assert res["totals"] == {"only_source": 0, "only_target": 0, "different": 0, "identical": 3}


@requires_server
def test_keys_differing_only_by_case_follow_the_target_collation(client, pair):
    sid, src, tgt = pair
    execute(client, sid, src, "CREATE TABLE K (CODE VARCHAR(10) NOT NULL PRIMARY KEY, V INTEGER);")
    execute(client, sid, tgt, "CREATE TABLE K (CODE VARCHAR(10) CHARACTER SET UTF8 NOT NULL PRIMARY KEY "
                              "COLLATE UNICODE_CI_AI, V INTEGER);")
    execute(client, sid, src, "INSERT INTO K VALUES ('abc', 1); INSERT INTO K VALUES ('Café', 2);"
                              "INSERT INTO K VALUES ('same', 3); INSERT INTO K VALUES ('new', 4); COMMIT;")
    execute(client, sid, tgt, "INSERT INTO K VALUES ('ABC', 1); INSERT INTO K VALUES ('CAFE', 2);"
                              "INSERT INTO K VALUES ('same', 3); INSERT INTO K VALUES ('gone', 5); COMMIT;")
    listing = client.post("/api/datacompare/tables", json={"source": {"server_id": sid, "db_id": src},
                                                           "target": {"server_id": sid, "db_id": tgt}}).json()
    k = next(t for t in listing["tables"] if t["name"] == "K")
    assert k["key_matching"] == [{"column": "CODE", "case": True, "accents": True}]
    job_id, res, t = run_compare(client, sid, src, tgt, tables=[{"name": "K"}])
    # 'abc'/'ABC' and 'Café'/'CAFE' are the same rows: their keys are updated, not inserted and deleted
    assert (t["K"]["only_source"], t["K"]["only_target"], t["K"]["different"], t["K"]["identical"]) == (1, 1, 2, 1)
    assert t["K"]["changed_columns"] == {"CODE": 2}
    text = client.get(f"/api/datacompare/jobs/{job_id}/script").text
    assert "UPDATE K SET CODE = 'abc' WHERE CODE = 'ABC'" in text, text
    r = client.post(base(sid, tgt) + f"/datacompare/{job_id}/apply", json={"confirm": True})
    j = wait_job(client, r.json())
    assert j["status"] == "finished", j["error"]
    rows = execute(client, sid, tgt, "SELECT CODE FROM K ORDER BY V")["results"][0]["rows"]
    assert [r[0] for r in rows] == ["abc", "Café", "same", "new"]
    # two source keys that the target cannot tell apart: a clear error instead of a failing script
    execute(client, sid, src, "INSERT INTO K VALUES ('ABC', 9); COMMIT;")
    r = client.post("/api/datacompare/compare", json={"source": {"server_id": sid, "db_id": src},
                                                      "target": {"server_id": sid, "db_id": tgt},
                                                      "tables": [{"name": "K"}]})
    j = wait_job(client, r.json())
    res = client.get(f"/api/datacompare/jobs/{j['id']}").json()
    assert res["tables"][0]["status"] == "error"
    assert "equal in the target" in res["tables"][0]["error"]


@requires_server
def test_comparisons_are_kept_across_restarts(client, populated):
    sid, src, tgt, _ = populated
    job_id, res, _ = run_compare(client, sid, src, tgt, tables=[{"name": "PARENT"}, {"name": "CHILD"}])
    script = client.get(f"/api/datacompare/jobs/{job_id}/script").text
    runs = client.get("/api/datacompare/runs").json()
    assert runs[0]["job_id"] == job_id and runs[0]["status"] == "finished"
    assert runs[0]["source"] == [sid, src] and runs[0]["target"] == [sid, tgt]
    # a restart: the job manager forgets the job, the comparison is read back from the data directory
    from fbadmin.api import deps

    deps.reset_state()  # servers are read back from the data directory too
    engine.runs = engine.Runs()
    again = client.get(f"/api/datacompare/jobs/{job_id}").json()
    assert again["status"] == "finished" and again["totals"] == res["totals"]
    assert again["source"] == {"server_id": sid, "db_id": src}
    assert client.get(f"/api/datacompare/jobs/{job_id}/script").text == script
    assert client.get(f"/api/datacompare/jobs/{job_id}/log").json()["log"][-1].startswith("Done:")
    assert [r["job_id"] for r in client.get("/api/datacompare/runs").json()] == [job_id]
    assert client.delete(f"/api/datacompare/runs/{job_id}").status_code == 204
    assert client.get(f"/api/datacompare/jobs/{job_id}").status_code == 404
    assert client.get("/api/datacompare/runs").json() == []


def test_interrupted_comparison_is_marked_failed_when_loaded(tmp_path, monkeypatch):
    from fbadmin import config

    monkeypatch.setattr(config.settings, "data_dir", tmp_path)
    run = engine.Run(source=("s", "a"), target=("s", "b"), id="j1")
    f = engine.StmtFile(run.dir / "0_insert.bin")
    f.add("INSERT INTO T VALUES (1)")
    f.close()
    run.segments = [("T", "INSERT", f)]
    run.save()  # saved while still running, then fbadmin stopped
    runs = engine.Runs()
    loaded = runs.get("j1")
    assert loaded.status == "failed" and "stopped" in loaded.error
    assert loaded.statement_count() == 0
