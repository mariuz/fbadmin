"""Upgrade assistant (milestone 1.2): keyword lists, tokenizer, checks against crafted databases on the
source version under test, report rendering, and the cross-version trial restore."""

from __future__ import annotations

import os
import socket
import uuid

import firebirdsql
import pytest

from fbadmin.upgrade import keywords as K
from fbadmin.upgrade import plan

from .conftest import DB_DIR, HOST, PASSWORD, PORT, USER, requires_server
from .test_maintenance import base, execute, major, wait_job

# ------------------------------------------------------------------ unit tests


def test_keyword_lists():
    new4 = K.new_reserved("3.0", "4.0")
    assert {"WINDOW", "LOCAL", "UNBOUNDED", "BINARY", "VARBINARY", "INT128", "DECFLOAT", "WITHOUT"} <= new4
    assert "WINDOW" not in K.reserved("3.0") and "WINDOW" in K.reserved("5.0")
    assert K.new_reserved("4.0", "5.0") == {"RDB$RESET_CONTEXT"}
    assert {"TRUNCATE", "CALL", "LTRIM", "GREATEST", "LISTAGG", "CURRENT_SCHEMA"} <= K.new_reserved("5.0", "6.0")
    for v in K.VERSIONS:  # never shrinks: what was reserved stays reserved
        assert {"SELECT", "TABLE", "WHERE"} <= K.reserved(v)
    assert K.reserved("3.0") <= K.reserved("4.0") <= K.reserved("5.0") <= K.reserved("6.0")
    assert K.version_key("5.0.1") == "5.0" and K.version_key(4) == "4.0"
    with pytest.raises(ValueError):
        K.version_key("2.5")
    assert K.qi("TRUNCATE") == '"TRUNCATE"' and K.qi("ORDERS") == "ORDERS" and K.qi("Mixed") == '"Mixed"'


def test_tokenizer_skips_strings_comments_and_quoted_names():
    src = ("BEGIN -- window in a comment\n"
           "  /* local\n unbounded */ x = 'window ''local''' || q'{window}' || \"WINDOW\";\n"
           "  y = :Local + t.window;\n"
           "END")
    ws = [w for w in K.words(src)]
    uppers = [w.upper for w in ws]
    assert "WINDOW" in uppers and uppers.count("WINDOW") == 1  # only t.window
    w = next(w for w in ws if w.upper == "WINDOW")
    assert (w.line, w.before, w.after) == (4, ".", ";")
    loc = next(w for w in ws if w.upper == "LOCAL")
    assert (loc.text, loc.line, loc.before) == ("Local", 4, ":")
    assert "UNBOUNDED" not in uppers
    assert [w.upper for w in K.words("SELECT RDB$GET_CONTEXT('SYSTEM', 'X') FROM RDB$DATABASE")][1:2] == ["RDB$GET_CONTEXT"]


def _report(findings):
    return {
        "database": {"name": "shop", "path": "/data/shop.fdb", "host": "db1", "port": 3050},
        "source": {"version": "3.0", "engine_version": "3.0.12", "ods": "12.0", "dialect": 3, "page_size": 4096,
                   "size_bytes": 5 << 20, "tables": ["ORDERS", "WINDOW"], "counts": {"tables": 2}},
        "target": {"version": "6.0", "ods": "14.0"},
        "findings": findings, "counts": {"blocker": 1, "warning": 0, "info": 0}, "generated_at": "now",
    }


def test_plan_and_rendering():
    f = {"id": "legacy_udfs", "category": "Removed or changed features", "severity": "blocker", "title": "Legacy UDFs",
         "explanation": "x < y & z", "fix": "Replace", "refs": ["https://example.org/rn"],
         "objects": [{"type": "function", "name": "LTRIM", "parent": None, "link": None, "detail": "ib_udf!IB_UDF_ltrim",
                      "fix": "Replace calls with TRIM(LEADING FROM s)"}]}
    rep = _report([f])
    rep["plan"] = plan.build_plan(rep)
    titles = [s["title"] for s in rep["plan"]]
    assert "Back up with the Firebird 3.0 gbak" in titles and "Restore with the Firebird 6.0 gbak" in titles
    restore = next(s for s in rep["plan"] if s["title"].startswith("Restore"))
    assert "-page_size 8192" in restore["commands"][0] and "-par 4" in restore["commands"][0]
    assert any("stdout |" in c for c in restore["commands"])
    settings = next(s for s in rep["plan"] if s["table"])["table"]["rows"]
    assert any(r[0] == "UdfAccess" for r in settings) and any(r[0] == "DataTypeCompatibility" for r in settings)
    check = next(s for s in rep["plan"] if s["title"] == "Check the restored database")
    assert "\"WINDOW\"" in check["commands"][-1] and "udf_replace.sql" in " ".join(check["text"])
    md = plan.to_markdown(rep)
    assert "# Upgrade report: shop → Firebird 6.0" in md and "[Blocker] Legacy UDFs" in md
    assert "| 6.0 | 14.0 |" in md and "gbak -b -g -v" in md
    html = plan.to_html(rep)
    assert html.startswith("<!doctype html>") and "x &lt; y &amp; z" in html and "<pre>" in html


# ------------------------------------------------------------- live servers
srv_mark = requires_server


def ubase(sid, dbid):
    return base(sid, dbid) + "/upgrade"


def check(client, sid, dbid, target):
    r = client.post(ubase(sid, dbid) + "/check", json={"target": target})
    assert r.status_code == 200, r.text
    return r.json()


def by_id(rep):
    return {f["id"]: f for f in rep["findings"]}


def names(f):
    return {o["name"] for o in f["objects"]}


FB3_SCHEMA = """
CREATE DOMAIN VARBINARY AS VARCHAR(20);
CREATE TABLE WINDOW (ID INTEGER NOT NULL PRIMARY KEY, LOCAL VARCHAR(10), UNBOUNDED VARBINARY,
  TS TIMESTAMP, NOTE VARCHAR(20) CHARACTER SET UNICODE_FSS);
CREATE TABLE ORDERS (ID INTEGER NOT NULL PRIMARY KEY, AMOUNT NUMERIC(18,2));
CREATE SEQUENCE PUBLICATION;
CREATE EXCEPTION RESETTING 'reset';
CREATE VIEW V_WIN AS SELECT ID, LOCAL FROM WINDOW;
CREATE TRIGGER WINDOW_BI FOR WINDOW BEFORE INSERT AS BEGIN NEW.TS = CURRENT_TIMESTAMP; END;
CREATE PROCEDURE P_WIN (WITHOUT INTEGER) RETURNS (BINARY INTEGER) AS
BEGIN
  -- WINDOW in a comment is fine
  BINARY = :WITHOUT + CHAR_LENGTH(RDB$GET_CONTEXT('SYSTEM', 'ENGINE_VERSION'));
  SUSPEND;
END;
CREATE PROCEDURE P_OK RETURNS (N INTEGER) AS BEGIN SELECT COUNT(*) FROM ORDERS INTO :N; SUSPEND; END;
CREATE FUNCTION BASE64_ENCODE (X VARCHAR(10)) RETURNS VARCHAR(20) AS BEGIN RETURN X; END;
DECLARE EXTERNAL FUNCTION STRLEN CSTRING(255) RETURNS INTEGER BY VALUE ENTRY_POINT 'IB_UDF_strlen' MODULE_NAME 'ib_udf';
CREATE PROCEDURE P_SYS RETURNS (N INTEGER) AS BEGIN SELECT COUNT(*) FROM RDB$RELATIONS INTO :N; SUSPEND; END;
CREATE TRIGGER ON_CONN ACTIVE ON CONNECT AS BEGIN END;
"""


@srv_mark
def test_fb3_source_report(client, server, database):
    sid, dbid = database
    if major(client, sid) != 3:
        pytest.skip("Firebird 3 source checks")
    execute(client, sid, dbid, FB3_SCHEMA)
    rep = check(client, sid, dbid, "4.0")
    assert rep["source"]["ods"] == "12.0" and rep["target"]["ods"] == "13.0"
    f = by_id(rep)
    rn = f["reserved_names"]
    assert rn["severity"] == "blocker"
    assert {"WINDOW", "LOCAL", "UNBOUNDED", "VARBINARY", "PUBLICATION", "RESETTING", "WITHOUT", "BINARY"} <= names(rn)
    col = next(o for o in rn["objects"] if o["name"] == "LOCAL")
    assert col["type"] == "column" and col["parent"] == "WINDOW" and col["link"] == {"otype": "tables", "name": "WINDOW"}
    assert col["fix"] == 'ALTER TABLE "WINDOW" ALTER COLUMN "LOCAL" TO LOCAL_;'
    dom = next(o for o in rn["objects"] if o["name"] == "VARBINARY")
    assert dom["fix"] == 'ALTER DOMAIN "VARBINARY" TO VARBINARY_;'
    src = f["reserved_in_source"]
    hits = {(o["type"], o["name"]): o["detail"] for o in src["objects"]}
    assert hits[("view", "V_WIN")].startswith("LOCAL (line 1); WINDOW (line 1): SELECT ID, LOCAL FROM WINDOW")
    assert hits[("procedure", "P_WIN")].startswith("BINARY (line 3); WITHOUT (line 3): BINARY = :WITHOUT")
    assert ("trigger", "WINDOW_BI") not in hits  # NEW.TS = CURRENT_TIMESTAMP has no reserved word
    assert not any("RDB$GET_CONTEXT (line" in d for d in hits.values())  # built-in call stays valid
    assert not any(n == "P_OK" for _, n in hits)
    udf = f["legacy_udfs"]
    assert udf["severity"] == "blocker" and names(udf) == {"STRLEN"}
    assert "CHAR_LENGTH" in udf["objects"][0]["fix"]
    assert names(f["current_timestamp"]) == {"WINDOW_BI"}
    assert names(f["shadowed_functions"]) == {"BASE64_ENCODE"} and f["shadowed_functions"]["severity"] == "info"
    assert "P_SYS" in names(f["system_tables"])
    assert names(f["database_triggers"]) == {"ON_CONN"}
    assert {"NOTE"} <= names(f["unicode_fss"])
    for fid in ("ods", "time_zone_types", "read_consistency", "srp256", "sql_security", "sequence_start"):
        assert fid in f, fid
    assert "fb6_schemas" not in f and "page_size_fb6" not in f
    assert rep["counts"]["blocker"] >= 2
    assert any(s["title"].startswith("Restore with the Firebird 4.0") for s in rep["plan"])

    rep6 = check(client, sid, dbid, "6.0")
    f6 = by_id(rep6)
    assert "fb6_schemas" in f6 and "WINDOW" in names(f6["reserved_names"])
    assert f6["system_tables"]["severity"] == "warning"

    # downloads
    r = client.post("/api/upgrade/render", json={"report": rep, "format": "md"})
    assert r.status_code == 200 and r.json()["filename"].endswith("_upgrade_fb4.md")
    assert "Names that are reserved words in Firebird 4.0" in r.json()["content"]
    r = client.post("/api/upgrade/render", json={"report": rep, "format": "html"})
    assert r.status_code == 200 and r.json()["content"].startswith("<!doctype html>")
    assert client.post("/api/upgrade/render", json={"report": {}, "format": "md"}).status_code >= 400


@srv_mark
def test_target_must_be_newer(client, server, database):
    sid, dbid = database
    m = major(client, sid)
    r = client.post(ubase(sid, dbid) + "/check", json={"target": f"{m}.0"})
    assert r.status_code == 400 and "newer" in r.text
    assert client.post(ubase(sid, dbid) + "/check", json={"target": "2.5"}).status_code == 400


@srv_mark
def test_fb4_source_report(client, server, database):
    sid, dbid = database
    if major(client, sid) != 4:
        pytest.skip("Firebird 4 source checks")
    execute(client, sid, dbid, """
CREATE TABLE CALL (ID INTEGER, GREATEST INTEGER);
CREATE PROCEDURE P_T RETURNS (N INTEGER) AS BEGIN SELECT MAX(GREATEST) FROM CALL INTO :N; SUSPEND; END;
CREATE FUNCTION UNICODE_VAL (X VARCHAR(10)) RETURNS INTEGER AS BEGIN RETURN 1; END;
DECLARE EXTERNAL FUNCTION RTRIM CSTRING(255) RETURNS CSTRING(255) FREE_IT ENTRY_POINT 'IB_UDF_rtrim' MODULE_NAME 'ib_udf';
""")
    f = by_id(check(client, sid, dbid, "5.0"))
    assert "reserved_names" not in f  # nothing new is reserved in 5.0 except RDB$RESET_CONTEXT
    assert "ODS 13.1" in f["ods"]["explanation"] and "upgrades it to ODS 13.1" in f["ods"]["explanation"]
    assert f["legacy_udfs"]["severity"] == "warning"  # already governed by UdfAccess on Firebird 4
    assert names(f["shadowed_functions"]) == {"UNICODE_VAL"}
    assert "time_zone_types" not in f and "read_consistency" not in f
    f6 = by_id(check(client, sid, dbid, "6.0"))
    assert {"CALL", "GREATEST", "RTRIM"} <= names(f6["reserved_names"])
    assert "P_T" in names(f6["reserved_in_source"])
    assert "upgrades it to ODS 13.1" not in f6["ods"]["explanation"]


@srv_mark
def test_fb5_source_report(client, server):
    sid = server["id"]
    if major(client, sid) != 5:
        pytest.skip("Firebird 5 source checks")
    path = f"{DB_DIR}/fbadmin_upg_{uuid.uuid4().hex[:8]}.fdb"
    firebirdsql.create_database(host=HOST, port=PORT, user=USER, password=PASSWORD, database=path,
                                page_size=4096).close()
    r = client.post(f"/api/servers/{sid}/databases", json={"name": "fb5src", "path": path})
    assert r.status_code == 201, r.text
    dbid = r.json()["id"]
    try:
        execute(client, sid, dbid, """
CREATE TABLE LISTAGG (ID INTEGER, "SYSTEM" INTEGER, TRUNCATE VARCHAR(5));
CREATE TABLE SYSTEM (ID INTEGER);
CREATE VIEW V_L AS SELECT TRUNCATE FROM LISTAGG;
""")
        f = by_id(check(client, sid, dbid, "6.0"))
        assert {"LISTAGG", "TRUNCATE"} <= names(f["reserved_names"])
        assert "V_L" in names(f["reserved_in_source"])
        assert f["page_size_fb6"]["severity"] == "warning"
        assert "fb6_schemas" in f and "SYSTEM" in names(f["fb6_schema_names"])
        assert "ODS 14.0" in f["ods"]["title"]
    finally:
        client.post(f"/api/servers/{sid}/databases/{dbid}/drop")


@srv_mark
def test_dialect1_and_external_table(client, server, database):
    sid, dbid = database
    m = major(client, sid)
    if m >= 6:
        pytest.skip("no newer target than Firebird 6")
    execute(client, sid, dbid, "CREATE TABLE EXT_T EXTERNAL FILE '/tmp/fbadmin_upgrade_ext.txt' (A CHAR(10));")
    r = client.put(base(sid, dbid) + "/maintenance/properties", json={"dialect": 1})
    assert r.status_code == 200, r.text
    f = by_id(check(client, sid, dbid, "6.0"))
    assert "dialect1" in f and f["dialect1"]["severity"] == "warning"
    assert names(f["external_tables"]) == {"EXT_T"}
    assert f["external_tables"]["objects"][0]["detail"] == "/tmp/fbadmin_upgrade_ext.txt"


# --------------------------------------------------------------- trial restore
NEWER = {3053: 3050, 3054: 3050, 3050: 3056}


def _up(port):
    try:
        with socket.create_connection((HOST, port), timeout=1):
            return True
    except OSError:
        return False


@srv_mark
def test_trial_restore_cross_version(client, server, database):
    """FB3 -> FB5, FB4 -> FB5 and FB5 -> FB6: back up on the source server, restore on the target, recompile."""
    sid, dbid = database
    # FBADMIN_TEST_UPGRADE_PORT: a newer server (CI runs one next to each matrix server)
    tport = int(os.environ.get("FBADMIN_TEST_UPGRADE_PORT") or 0) or NEWER.get(PORT)
    if not tport or not _up(tport):
        pytest.skip(f"no newer test server for port {PORT}")
    m = major(client, sid)
    # reserved in the target, not in the source; Firebird 5 reserves nothing new that a table could be named
    word = {3: "WINDOW", 4: "WORDS", 5: "LISTAGG"}[m]
    execute(client, sid, dbid, f"""
CREATE TABLE {word} (ID INTEGER NOT NULL PRIMARY KEY, V VARCHAR(10));
CREATE TABLE ITEMS (ID INTEGER NOT NULL PRIMARY KEY, NAME VARCHAR(20));
CREATE VIEW V_BAD AS SELECT ID FROM {word};
CREATE VIEW V_GOOD AS SELECT ID, NAME FROM ITEMS;
CREATE PROCEDURE P_BAD RETURNS (N INTEGER) AS BEGIN SELECT COUNT(*) FROM {word} INTO :N; SUSPEND; END;
CREATE PROCEDURE P_GOOD RETURNS (N INTEGER) AS BEGIN SELECT COUNT(*) FROM ITEMS INTO :N; SUSPEND; END;
CREATE TRIGGER ITEMS_BI FOR ITEMS BEFORE INSERT AS BEGIN NEW.NAME = UPPER(NEW.NAME); END;
INSERT INTO ITEMS VALUES (1, 'a');
INSERT INTO {word} VALUES (1, 'x');
""")
    r = client.post("/api/servers", json={"name": "target", "host": HOST, "port": tport, "user": USER,
                                          "password": PASSWORD})
    assert r.status_code == 201, r.text
    tid = r.json()["id"]
    scratch = f"{DB_DIR}/fbadmin_upgrade_t{uuid.uuid4().hex[:8]}.fdb"
    r = client.post(ubase(sid, dbid) + "/trial", json={"target_server_id": tid, "scratch_path": scratch, "keep": True})
    assert r.status_code == 202, r.text
    job = wait_job(client, r.json(), timeout=300)
    assert job["status"] == "finished", "\n".join(client.get(f"/api/jobs/{job['id']}").json()["log"])
    res = client.get(f"/api/upgrade/jobs/{job['id']}/result").json()
    rep = res["report"]
    try:
        tr = rep["trial"]
        tmajor = major(client, tid)
        assert rep["target"]["version"] == f"{tmajor}.0"
        assert tr["restored"]["ods"] == {5: "13.1", 6: "14.0"}[tmajor]
        if tmajor == 6:
            assert tr["restored"]["page_size"] >= 8192
        assert tr["restored"]["counts"]["tables"] == 2
        failed = {f["name"] for f in tr["recompile_failures"]}
        f = by_id(rep)
        if m == 4:
            assert not failed and "trial_recompile" not in f
        else:
            assert failed == {"V_BAD", "P_BAD"}, tr["recompile_failures"]
            assert f["trial_recompile"]["severity"] == "blocker"
            assert "Token unknown" in f["trial_recompile"]["objects"][0]["detail"]
        assert "trial_counts" not in f and "trial_ok" in f
        assert tr["config"] and tr["config"]["UdfAccess"]["value"] == "None"
        assert tr["kept"] and tr["registered_as"]
        # the restored copy still holds the data and still runs its compiled code
        tdb = next(d for d in client.get("/api/servers").json() if d["id"] == tid)["databases"][0]
        res = execute(client, tid, tdb["id"], f'SELECT COUNT(*) FROM "{word}"')["results"][0]
        assert res["rows"][0][0] == 1
        res = execute(client, tid, tdb["id"], "SELECT N FROM P_BAD")["results"][0]
        assert res["rows"][0][0] == 1
        md = client.post("/api/upgrade/render", json={"report": rep, "format": "md"}).json()["content"]
        assert "Trial restore on the target server" in md
    finally:
        try:
            c = firebirdsql.connect(host=HOST, port=tport, user=USER, password=PASSWORD, database=scratch)
            c.drop_database()
        except Exception:  # noqa: BLE001
            pass


@srv_mark
def test_trial_rejects_older_target(client, server, database):
    sid, dbid = database
    r = client.post(ubase(sid, dbid) + "/trial", json={"target_server_id": server["id"]})
    assert r.status_code == 400 and "newer" in r.text
