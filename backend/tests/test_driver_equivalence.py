"""Cross-driver equivalence: the same queries and API calls through firebirdsql and the native
driver must give identical serialized results (what the browser receives).

Covers the tricky types (INT128, NUMERIC(38), DECFLOAT, TIME/TIMESTAMP WITH TIME ZONE, BLOBs,
UTF8/WIN1252/NONE/OCTETS character sets, extremes, NULLs), parameters (also big ones), the Query
Tool (columns, fetch more, plans, errors) and the metadata endpoints (tree, DDL, data grid).
Firebird 4+ types are skipped on Firebird 3 servers. Needs the native driver (FBADMIN_FBCLIENT).
"""

from __future__ import annotations

import json

import pytest

from .conftest import requires_server
from .drivers_util import client_major, requires_native, use_driver

pytestmark = [requires_server, requires_native]

BASE_COLS = """
  ID INTEGER NOT NULL PRIMARY KEY,
  SI SMALLINT, I INTEGER, BI BIGINT,
  N92 NUMERIC(9,2), N184 NUMERIC(18,4), D152 DECIMAL(15,2),
  F FLOAT, DP DOUBLE PRECISION,
  DT DATE, TM TIME, TS TIMESTAMP, BO BOOLEAN,
  C10 CHAR(10), VU VARCHAR(50) CHARACTER SET UTF8, VW VARCHAR(50) CHARACTER SET WIN1252,
  VN VARCHAR(50) CHARACTER SET NONE, OC CHAR(16) CHARACTER SET OCTETS,
  BT BLOB SUB_TYPE TEXT CHARACTER SET UTF8, BW BLOB SUB_TYPE TEXT CHARACTER SET WIN1252,
  BB BLOB SUB_TYPE BINARY"""
FB4_COLS = """,
  I128 INT128, N38 NUMERIC(38,6), DF16 DECFLOAT(16), DF34 DECFLOAT(34),
  TMZ TIME WITH TIME ZONE, TSZ TIMESTAMP WITH TIME ZONE"""

BASE_ROWS = [
    # typical values
    "1, 12, 123456, 1234567890123, 1234.56, 12345678901234.5678, -987.65, 1.5, 3.141592653589793, "
    "'2024-02-29', '13:14:15.1234', '2024-07-01 10:11:12.3456', TRUE, 'abc', 'héllo ✓ 漢字 😀', "
    "'café € œ', 'plain ascii', x'00112233445566778899AABBCCDDEEFF', 'naïve text blob ✓', "
    "'déjà vu €', x'DEADBEEF'",
    # all NULL
    "2, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, "
    "NULL, NULL, NULL",
    # extremes and empty values
    "3, -32768, 2147483647, -9223372036854775808, -9999999.99, -99999999999999.9999, 0.01, "
    "-3.4e38, 1e-300, '0001-01-01', '00:00:00', '9999-12-31 23:59:59.9999', FALSE, '', '', '', '', "
    "x'00000000000000000000000000000000', '', '', x''",
]
FB4_ROWS = [
    ", 170141183460469231731687303715884105727, 12345678901234567890123456789012.123456, "
    "1234567890.123456, 1.234567890123456789012345678901234E+100, '23:59:59.9999 America/Sao_Paulo', "
    "'2024-02-29 12:00:00.1234 +05:30'",
    ", NULL, NULL, NULL, NULL, NULL, NULL",
    ", -170141183460469231731687303715884105728, -99999999999999999999999999999999.999999, "
    "-9.999999999999999E+384, 1E-6143, '00:00:00 UTC', '0001-01-01 00:00:00 America/New_York'",
]


def _engine_major(client, base) -> int:
    r = client.post(f"{base}/execute", json={"sql": "SELECT RDB$GET_CONTEXT('SYSTEM', 'ENGINE_VERSION') FROM RDB$DATABASE"})
    return int(str(r.json()["results"][0]["rows"][0][0]).split(".")[0])


def _clean(obj):
    """Drop what legitimately differs between runs (timings, attachment ids, session ids)."""
    if isinstance(obj, dict):
        return {k: _clean(v) for k, v in obj.items() if k not in ("elapsed_ms", "attachment_id", "session_id", "stats")}
    if isinstance(obj, list):
        return [_clean(v) for v in obj]
    return obj


@pytest.fixture
def eq_db(client, database):
    sid, dbid = database
    base = f"/api/servers/{sid}/databases/{dbid}"
    major = _engine_major(client, base)
    if major >= 4 and (client_major() or 0) < 4:
        pytest.skip("Firebird 3 client library with a Firebird 4+ server: the native driver gets INT128, "
                    "DECFLOAT and time zones as legacy types (SET BIND), so results differ by design")
    fb4 = major >= 4
    cols = BASE_COLS + (FB4_COLS if fb4 else "")
    if major >= 6:
        # Firebird 6 (snapshot) sends WIN1252 text blobs untransliterated to firebirdsql, which then
        # fails to decode them as UTF8; the native client reads them fine. Known pure-driver gap.
        cols = cols.replace("BW BLOB SUB_TYPE TEXT CHARACTER SET WIN1252", "BW BLOB SUB_TYPE TEXT CHARACTER SET UTF8")
    script = [f"CREATE TABLE EQ ({cols})", "COMMIT"]
    big_bin = "x'" + ("0123456789ABCDEF" * 1000) + "'"  # 8 KB, doubled below to 256 KB (many segments)
    for i, row in enumerate(BASE_ROWS):
        if i == 0:
            row = row.replace("x'DEADBEEF'", big_bin)
        values = row + (FB4_ROWS[i] if fb4 else "")
        script.append(f"INSERT INTO EQ VALUES ({values})")
    script += ["UPDATE EQ SET BB = BB || BB WHERE ID = 1"] * 5
    script.append("COMMIT")
    r = client.post(f"{base}/execute", json={"sql": ";\n".join(script) + ";"})
    assert r.status_code == 200 and not r.json()["error"], r.text
    return sid, dbid, base, fb4


def _snapshot(client, sid, dbid, base, fb4, row_id: int) -> dict:
    out: dict = {}
    r = client.post("/api/query/sessions", json={"server_id": sid, "database_id": dbid})
    assert r.status_code == 201, r.text
    qs = r.json()["session_id"]

    def run(name, sql, **kw):
        r = client.post(f"/api/query/sessions/{qs}/execute", json={"sql": sql, "keep_cursor": False, **kw})
        assert r.status_code == 200, r.text
        out[name] = _clean(r.json())
        return out[name]

    run("select_all", "SELECT * FROM EQ ORDER BY ID")
    run("expressions", "SELECT CAST(1 AS NUMERIC(18,4)) / 3, 1e0 / 3, CAST(-7 AS BIGINT) / 2, 'x' || NULL, "
        "CAST('2000-01-01 00:00:00.0001' AS TIMESTAMP), CHAR_LENGTH('漢字'), OCTET_LENGTH('漢字') FROM RDB$DATABASE")
    if fb4:
        run("expressions_fb4", "SELECT CAST(2 AS INT128) * 85070591730234615865843651857942052863, "
            "CAST(1 AS DECFLOAT(34)) / 3, CAST(1 AS DECFLOAT(16)) / 7, CAST('NaN' AS DECFLOAT(34)), "
            "CAST('-Infinity' AS DECFLOAT(16)), CAST(-0.0 AS DECFLOAT(34)), "
            "CAST(10 AS NUMERIC(38,10)) / 3, CAST('10:00 Europe/London' AS TIME WITH TIME ZONE), "
            "CAST('2024-10-27 01:30 Europe/London' AS TIMESTAMP WITH TIME ZONE), "
            "CAST('2024-01-01 00:00:00 -03:30' AS TIMESTAMP WITH TIME ZONE) FROM RDB$DATABASE")
    # parameters: insert through the driver's parameter binding, read back
    params = {"id": row_id, "si": -5, "bi": 9007199254740993, "n": "-12345.6789", "dp": 2.5e-10,
              "vu": "παράμετρος ✓ 😀", "vw": "Ærø €", "bt": "ü" * 100000, "ts": "2023-12-31 23:59:59.9999",
              "dt": "2020-02-29", "tm": "01:02:03.0400", "bo": True}
    if _engine_major(client, base) >= 6:
        # Firebird 6 (snapshot) stores a firebirdsql parameter for a WIN1252 column without
        # transliterating it from UTF8 (mojibake); the native client is right. Known pure-driver gap.
        params["vw"] = "Aero"
    cols = "ID, SI, BI, N184, DP, VU, VW, BT, TS, DT, TM, BO"
    marks = ":id, :si, :bi, :n, :dp, :vu, :vw, :bt, :ts, :dt, :tm, :bo"
    if fb4:
        params.update({"i128": "-170141183460469231731687303715884105728", "n38": "1.000001",
                       "df": "-1.5E-300", "tsz": "2024-03-31 02:30:00 Europe/Berlin"})
        cols += ", I128, N38, DF34, TSZ"
        marks += ", :i128, :n38, :df, :tsz"
    params = {k.upper(): v for k, v in params.items()}  # parameter names are case-insensitive (upper)
    ins = run("insert_params", f"INSERT INTO EQ ({cols}) VALUES ({marks})", params=params)
    assert not ins["error"], ins
    sel = run("select_params", f"SELECT {cols.replace('ID, ', '', 1)} FROM EQ WHERE ID = :id", params={"ID": row_id})
    assert not sel["error"], sel
    run("delete_params", "DELETE FROM EQ WHERE ID = ?", params={"$1": row_id})
    # fetch more (open cursor), errors, plans
    gen = ("WITH RECURSIVE R(N) AS (SELECT 1 FROM RDB$DATABASE UNION ALL SELECT N + 1 FROM R WHERE N < 25) "
           "SELECT N, CAST(N AS VARCHAR(10)) || '✓' AS S FROM R")
    r = client.post(f"/api/query/sessions/{qs}/execute", json={"sql": gen, "max_rows": 5, "keep_cursor": True})
    out["cursor_first"] = _clean(r.json())
    out["cursor_more"] = _clean(client.post(f"/api/query/sessions/{qs}/fetch", json={"rows": 7}).json())
    out["cursor_rest"] = _clean(client.post(f"/api/query/sessions/{qs}/fetch", json={"rows": 100}).json())
    run("error_unknown_table", "SELECT * FROM NO_SUCH_TABLE")
    run("error_syntax", "SELECT FROM WHERE")
    run("error_constraint", "INSERT INTO EQ (ID) VALUES (1)")
    out["explain"] = client.post(f"/api/query/sessions/{qs}/explain",
                                 json={"sql": "SELECT * FROM EQ WHERE ID = 1"}).json()
    client.delete(f"/api/query/sessions/{qs}")
    # metadata endpoints (pooled metadata connections)
    out["tables"] = client.get(f"{base}/objects/tables").json()
    detail = client.get(f"{base}/objects/tables/EQ").json()
    out["ddl"] = detail.get("ddl")
    out["data"] = _clean(client.get(f"{base}/tables/EQ/data").json())
    out["count"] = client.get(f"{base}/tables/EQ/count").json()
    return out


def _diff(a, b, path: str = "") -> list[str]:
    """Paths of the differing leaves, with both values."""
    if isinstance(a, dict) and isinstance(b, dict):
        return [m for k in sorted(set(a) | set(b), key=str) for m in _diff(a.get(k), b.get(k), f"{path}.{k}")]
    if isinstance(a, list) and isinstance(b, list) and len(a) == len(b):
        return [m for i, (x, y) in enumerate(zip(a, b, strict=True)) for m in _diff(x, y, f"{path}[{i}]")]
    if a == b:
        return []
    return [f"{path}:\n  firebirdsql: {json.dumps(a, ensure_ascii=False)[:500]}\n"
            f"  native:      {json.dumps(b, ensure_ascii=False)[:500]}"]


def test_same_results_with_both_drivers(client, eq_db, monkeypatch):
    sid, dbid, base, fb4 = eq_db
    use_driver(monkeypatch, "firebirdsql")
    pure = _snapshot(client, sid, dbid, base, fb4, 100)
    use_driver(monkeypatch, "native")
    from fbadmin.api.deps import get_state
    from fbadmin.native import NativeConnection

    with get_state().manager.meta(sid, dbid) as conn:
        assert isinstance(conn, NativeConnection)
    native = _snapshot(client, sid, dbid, base, fb4, 100)
    for k in ("select_all", "expressions", "cursor_first"):
        assert not pure[k]["error"], (k, pure[k]["error"])
    assert pure["select_all"]["results"][0]["row_count"] == 3
    problems = _diff(pure, native)
    assert not problems, "drivers differ:\n" + "\n".join(problems)
