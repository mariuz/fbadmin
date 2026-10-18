"""Milestone 0.3: object dialogs — DDL generation (verified on the live server), specs,
drop preview, privileges, comments, renames, SQL SECURITY and the new object types."""

from __future__ import annotations

import uuid

import pytest

from fbadmin.metadata import ddlgen

from .conftest import DB_DIR, requires_server

pytestmark = requires_server


def base(sid, dbid):
    return f"/api/servers/{sid}/databases/{dbid}"


class DB:
    def __init__(self, client, sid, dbid):
        self.c, self.b = client, base(sid, dbid)
        self.engine = client.get(self.b + "/designer/lookups").json()["engine_major"]

    def run(self, sql: str):
        r = self.c.post(self.b + "/execute", json={"sql": sql})
        assert r.status_code == 200, r.text
        res = r.json()
        assert res["error"] is None, (res["error"], sql)
        return res

    def ddl(self, kind, spec, original=None, ok=True):
        r = self.c.post(self.b + "/ddl", json={"kind": kind, "spec": spec, "original": original})
        if ok:
            assert r.status_code == 200, r.text
            return r.json()
        return r

    def spec(self, otype, name):
        r = self.c.get(self.b + f"/designer/{otype}/{name}")
        assert r.status_code == 200, r.text
        return r.json()["spec"]

    def obj(self, otype, name):
        r = self.c.get(self.b + f"/objects/{otype}/{name}")
        assert r.status_code == 200, r.text
        return r.json()

    def apply(self, kind, spec, original=None):
        out = self.ddl(kind, spec, original)
        if out["sql"]:
            self.run(out["sql"])
        return out


@pytest.fixture
def db(client, database):
    return DB(client, *database)


def test_lookups_and_new_object_types(db):
    lk = db.c.get(db.b + "/designer/lookups").json()
    assert lk["engine_major"] in (3, 4, 5)
    assert "UTF8" in lk["charsets"]
    assert any(c["name"] == "UNICODE_CI" for c in lk["collations"])

    types = {t["type"] for t in db.c.get(db.b + "/objects").json()}
    assert {"collations", "charsets", "shadows", "blob_filters"} <= types
    info = db.c.get(db.b + "/info").json()
    assert info["counts"]["charsets"] > 40

    cs = db.obj("charsets", "UTF8")
    assert cs["properties"]["Bytes per character"] == 4
    assert "UNICODE_CI" in cs["properties"]["Collations"]
    assert cs["properties"]["System flag"].startswith("1")
    sys_coll = db.obj("collations", "UNICODE_CI")
    assert sys_coll["properties"]["Character set"] == "UTF8"
    assert db.c.get(db.b + "/objects/blob_filters").status_code == 200
    assert db.c.get(db.b + "/objects/shadows").json() == []


def test_collation_dialog_roundtrip(db):
    spec = {"name": "UTF8_NUM", "charset": "UTF8", "base": "UNICODE", "pad_space": False,
            "case_insensitive": True, "accent_insensitive": False, "attributes": "NUMERIC-SORT=1",
            "comment": "numeric sort"}
    out = db.apply("collation", spec)
    assert out["sql"].startswith("CREATE COLLATION UTF8_NUM FOR UTF8")
    got = db.spec("collations", "UTF8_NUM")
    assert got["case_insensitive"] and not got["accent_insensitive"]
    assert got["attributes"] == "NUMERIC-SORT=1" and got["comment"] == "numeric sort"
    ddl = db.obj("collations", "UTF8_NUM")["ddl"]
    assert "CASE INSENSITIVE" in ddl and "'NUMERIC-SORT=1'" in ddl and "COLL-VERSION" not in ddl
    # the extracted DDL is valid
    db.run("DROP COLLATION UTF8_NUM;\n" + ddl)
    names = {c["name"] for c in db.c.get(db.b + "/objects/collations").json()}
    assert "UTF8_NUM" in names


def test_shadow_read_only(db):
    path = f"{DB_DIR}/fbadmin_shadow_{uuid.uuid4().hex[:8]}.shd"
    db.run(f"CREATE SHADOW 7 MANUAL '{path}'")
    try:
        items = db.c.get(db.b + "/objects/shadows").json()
        assert items and items[0]["name"] == "7" and items[0]["mode"] == "MANUAL"
        sh = db.obj("shadows", "7")
        assert sh["ddl"].startswith("CREATE SHADOW 7 MANUAL")
    finally:
        db.run("DROP SHADOW 7")


TABLE = {
    "name": "T_DESIGN",
    "kind": "persistent",
    "comment": "designer test",
    "columns": [
        {"name": "ID", "data_type": "INTEGER", "identity": "BY DEFAULT", "not_null": True},
        {"name": "CODE", "data_type": "VARCHAR(10)", "not_null": True, "collation": "UNICODE_CI",
         "comment": "short code"},
        {"name": "QTY", "data_type": "INTEGER", "default": "0"},
        {"name": "PRICE", "data_type": "NUMERIC(10,2)"},
        {"name": "TOTAL", "computed": "QTY * PRICE"},
        {"name": "OLD_COL", "data_type": "SMALLINT"},
    ],
    "constraints": [
        {"name": "PK_T_DESIGN", "type": "PRIMARY KEY", "columns": ["ID"]},
        {"name": "UQ_T_DESIGN_CODE", "type": "UNIQUE", "columns": ["CODE"]},
        {"name": "CK_T_DESIGN_QTY", "type": "CHECK", "check": "QTY >= 0"},
    ],
    "indices": [{"name": "IX_T_DESIGN_PRICE", "columns": ["PRICE"], "descending": True}],
}


def test_table_create_and_alter_diff(db):
    out = db.apply("table", TABLE)
    assert out["sql"].startswith("CREATE TABLE T_DESIGN (")
    assert "COMMENT ON COLUMN T_DESIGN.CODE IS 'short code'" in out["sql"]
    orig = db.spec("tables", "T_DESIGN")
    assert [c["name"] for c in orig["columns"]] == ["ID", "CODE", "QTY", "PRICE", "TOTAL", "OLD_COL"]
    cols = {c["name"]: c for c in orig["columns"]}
    assert cols["CODE"]["collation"] == "UNICODE_CI" and cols["QTY"]["default"] == "0"
    assert cols["TOTAL"]["computed"] == "(QTY * PRICE)"
    assert {c["name"] for c in orig["constraints"]} == {"PK_T_DESIGN", "UQ_T_DESIGN_CODE", "CK_T_DESIGN_QTY"}

    # unchanged spec -> empty script
    assert db.ddl("table", orig, orig)["sql"] == ""

    new = __import__("copy").deepcopy(orig)
    nc = {c["name"]: c for c in new["columns"]}
    nc["CODE"]["name"] = "CODE2"  # rename (the UNIQUE constraint follows)
    nc["PRICE"]["data_type"] = "NUMERIC(12,2)"  # type
    nc["PRICE"]["not_null"] = False
    nc["QTY"]["default"] = "1"  # default
    nc["CODE"]["comment"] = "the code"
    new["columns"] = [c for c in new["columns"] if c["name"] != "OLD_COL"]  # drop
    new["columns"].insert(1, {"name": "NOTE", "data_type": "VARCHAR(50)", "default": "'x'"})  # add + reorder
    new["constraints"].append({"name": "CK_T_DESIGN_PRICE", "type": "CHECK", "check": "PRICE > 0"})
    new["indices"][0]["descending"] = False  # changed index -> drop + create
    new["indices"].append({"name": "IX_T_DESIGN_NOTE", "columns": ["NOTE"], "unique": False, "active": False})
    new["comment"] = "changed"
    script = db.ddl("table", new, orig)
    sql = script["sql"]
    assert "ALTER TABLE T_DESIGN DROP OLD_COL;" in sql
    assert "ALTER TABLE T_DESIGN ALTER COLUMN CODE TO CODE2;" in sql
    assert "ALTER TABLE T_DESIGN ALTER COLUMN PRICE TYPE NUMERIC(12,2);" in sql
    assert "ALTER TABLE T_DESIGN ALTER COLUMN QTY SET DEFAULT 1;" in sql
    assert "ALTER TABLE T_DESIGN ADD NOTE VARCHAR(50) DEFAULT 'x';" in sql
    assert "ALTER TABLE T_DESIGN ALTER COLUMN NOTE POSITION 2;" in sql
    assert "DROP INDEX IX_T_DESIGN_PRICE;" in sql and "CREATE INDEX IX_T_DESIGN_PRICE ON T_DESIGN (PRICE);" in sql
    assert "ALTER INDEX IX_T_DESIGN_NOTE INACTIVE;" in sql
    assert "CREATE TABLE" not in sql and "DROP CONSTRAINT CK_T_DESIGN_QTY" not in sql
    db.run(sql)

    after = db.spec("tables", "T_DESIGN")
    assert [c["name"] for c in after["columns"]] == ["ID", "NOTE", "CODE2", "QTY", "PRICE", "TOTAL"]
    ac = {c["name"]: c for c in after["columns"]}
    assert ac["PRICE"]["data_type"] == "NUMERIC(12,2)" and ac["QTY"]["default"] == "1"
    assert ac["CODE2"]["comment"] == "the code" and after["comment"] == "changed"
    assert next(c for c in after["constraints"] if c["type"] == "UNIQUE")["columns"] == ["CODE2"]
    assert {c["name"] for c in after["constraints"]} >= {"CK_T_DESIGN_PRICE", "CK_T_DESIGN_QTY"}
    idx = {i["name"]: i for i in after["indices"]}
    assert not idx["IX_T_DESIGN_PRICE"]["descending"] and not idx["IX_T_DESIGN_NOTE"]["active"]
    # applying the edited spec again is a no-op
    edited = __import__("copy").deepcopy(after)
    assert db.ddl("table", edited, after)["sql"] == ""


def test_column_not_null_identity_and_fk(db):
    db.apply("table", {"name": "PARENT", "columns": [{"name": "ID", "data_type": "INTEGER", "not_null": True}],
                       "constraints": [{"name": "PK_PARENT", "type": "PRIMARY KEY", "columns": ["ID"]}]})
    db.apply("table", {"name": "CHILD", "columns": [
        {"name": "ID", "data_type": "BIGINT", "identity": "BY DEFAULT", "identity_start": 10, "not_null": True},
        {"name": "PARENT_ID", "data_type": "INTEGER"}]})
    rel = db.c.get(db.b + "/designer/relations/PARENT").json()
    assert rel["columns"] == ["ID"] and rel["unique_keys"][0]["name"] == "PK_PARENT"
    orig = db.spec("tables", "CHILD")
    new = __import__("copy").deepcopy(orig)
    new["columns"][1]["not_null"] = True
    new["constraints"].append({"name": "FK_CHILD_PARENT", "type": "FOREIGN KEY", "columns": ["PARENT_ID"],
                               "ref_table": "PARENT", "ref_columns": ["ID"], "on_delete": "CASCADE",
                               "on_update": "SET NULL"})
    new["columns"][0]["identity_restart"] = 100
    if db.engine >= 4:
        new["columns"][0]["identity_increment"] = 5
        new["columns"][0]["identity"] = "ALWAYS"
    sql = db.ddl("table", new, orig)["sql"]
    assert "ALTER TABLE CHILD ALTER COLUMN PARENT_ID SET NOT NULL;" in sql
    assert "ON UPDATE SET NULL ON DELETE CASCADE" in sql
    db.run(sql)
    after = db.spec("tables", "CHILD")
    fk = next(c for c in after["constraints"] if c["type"] == "FOREIGN KEY")
    assert fk["ref_table"] == "PARENT" and fk["on_delete"] == "CASCADE"
    if db.engine >= 4:
        assert after["columns"][0]["identity"] == "ALWAYS" and after["columns"][0]["identity_increment"] == 5
    else:
        w = db.ddl("table", {**new, "columns": [{**new["columns"][0], "identity": "ALWAYS"}, new["columns"][1]]},
                   orig)["warnings"]
        assert any("Firebird 4" in x for x in w)
    # drop the FK through the designer
    no_fk = __import__("copy").deepcopy(after)
    no_fk["constraints"] = [c for c in no_fk["constraints"] if c["type"] != "FOREIGN KEY"]
    sql = db.ddl("table", no_fk, after)["sql"]
    assert sql == "ALTER TABLE CHILD DROP CONSTRAINT FK_CHILD_PARENT;\n"
    db.run(sql)


def test_unsupported_changes_are_warned(db):
    db.apply("table", {"name": "W", "columns": [{"name": "A", "data_type": "VARCHAR(5)"},
                                                 {"name": "B", "data_type": "INTEGER"}]})
    orig = db.spec("tables", "W")
    new = __import__("copy").deepcopy(orig)
    new["name"] = "W2"
    new["columns"][0]["collation"] = "UNICODE_CI"
    new["columns"][1]["identity"] = "BY DEFAULT"
    out = db.ddl("table", new, orig)
    assert out["sql"] == ""
    assert len(out["warnings"]) == 3


def test_gtt_and_external_and_sql_security_table(db):
    out = db.apply("table", {"name": "GTT1", "kind": "gtt_delete", "columns": [{"name": "A", "data_type": "INT"}]})
    assert "ON COMMIT DELETE ROWS" in out["sql"]
    assert db.spec("tables", "GTT1")["kind"] == "gtt_delete"
    ext = db.ddl("table", {"name": "EXT1", "external_file": "/tmp/ext.dat",
                           "columns": [{"name": "A", "data_type": "CHAR(10)"}]})
    assert ext["sql"].startswith("CREATE TABLE EXT1 EXTERNAL FILE '/tmp/ext.dat' (")
    assert db.ddl("table", {"name": "X", "kind": "gtt_delete", "external_file": "/x",
                            "columns": [{"name": "A", "data_type": "INT"}]}, ok=False).status_code == 400

    spec = {"name": "SECT", "sql_security": "DEFINER", "columns": [{"name": "A", "data_type": "INT"}]}
    out = db.ddl("table", spec)
    if db.engine < 4:
        assert "SQL SECURITY" not in out["sql"] and out["warnings"]
        assert "SQL security" not in db.obj("tables", "GTT1")["properties"]
        return
    db.run(out["sql"])
    t = db.obj("tables", "SECT")
    assert t["properties"]["SQL security"] == "DEFINER"
    assert t["ddl"].rstrip().endswith(") SQL SECURITY DEFINER;")
    orig = db.spec("tables", "SECT")
    sql = db.ddl("table", {**orig, "sql_security": "INVOKER"}, orig)["sql"]
    assert sql == "ALTER TABLE SECT ALTER SQL SECURITY INVOKER;\n"
    db.run(sql)
    sql = db.ddl("table", {**orig, "sql_security": None}, {**orig, "sql_security": "INVOKER"})["sql"]
    assert sql == "ALTER TABLE SECT DROP SQL SECURITY;\n"
    db.run(sql)
    assert db.obj("tables", "SECT")["properties"]["SQL security"] is None


def test_sql_security_routines_roundtrip(client, server, db):
    if db.engine < 4:
        pytest.skip("SQL SECURITY needs Firebird 4")
    db.run("CREATE TABLE ST (A INT)")
    db.apply("trigger", {"name": "ST_BI", "relation": "ST", "event": "BEFORE INSERT", "position": 0,
                         "sql_security": "DEFINER", "body": "BEGIN NEW.A = 1; END"})
    db.apply("procedure", {"name": "SP_SEC", "inputs": [{"name": "X", "type": "INTEGER"}],
                           "outputs": [{"name": "Y", "type": "INTEGER"}], "sql_security": "INVOKER",
                           "body": "BEGIN Y = X; SUSPEND; END"})
    db.apply("function", {"name": "F_SEC", "inputs": [{"name": "X", "type": "INTEGER"}], "returns": "INTEGER",
                          "deterministic": True, "sql_security": "DEFINER", "body": "BEGIN RETURN X; END"})
    db.apply("package", {"name": "PK_SEC", "sql_security": "DEFINER",
                         "header": "BEGIN FUNCTION F(X INT) RETURNS INT; END",
                         "body": "BEGIN FUNCTION F(X INT) RETURNS INT AS BEGIN RETURN X; END END"})
    ddl = {}
    for otype, name, want in (("triggers", "ST_BI", "DEFINER"), ("procedures", "SP_SEC", "INVOKER"),
                              ("functions", "F_SEC", "DEFINER"), ("packages", "PK_SEC", "DEFINER")):
        o = db.obj(otype, name)
        assert o["properties"]["SQL security"] == want, otype
        assert f"SQL SECURITY {want}" in o["ddl"], otype
        assert db.spec(otype, name)["sql_security"] == want
        ddl[(otype, name)] = o["ddl"]
    # replay the extracted DDL into a fresh database and compare
    sid = db.b.split("/")[3]
    r = client.post(f"/api/servers/{sid}/databases/create",
                    json={"name": "sec", "path": f"{DB_DIR}/fbadmin_sec_{uuid.uuid4().hex[:8]}.fdb"})
    dst = r.json()["id"]
    try:
        other = DB(client, sid, dst)
        other.run("CREATE TABLE ST (A INT);\n" + "\n".join(ddl.values()))
        for (otype, name), text in ddl.items():
            assert other.obj(otype, name)["ddl"] == text
    finally:
        client.post(f"/api/servers/{sid}/databases/{dst}/drop")


def test_index_dialog(db):
    db.run("CREATE TABLE IT (A INT, B INT)")
    spec = {"name": "IX_IT", "table": "IT", "columns": ["A", "B"], "unique": True, "descending": True,
            "comment": "idx"}
    out = db.apply("index", spec)
    assert out["sql"].startswith("CREATE UNIQUE DESCENDING INDEX IX_IT ON IT (A, B);")
    orig = db.spec("indices", "IX_IT")
    assert orig["unique"] and orig["descending"] and orig["comment"] == "idx"
    sql = db.ddl("index", {**orig, "active": False}, orig)["sql"]
    assert sql == "ALTER INDEX IX_IT INACTIVE;\n"
    db.run(sql)
    assert not db.spec("indices", "IX_IT")["active"]
    sql = db.ddl("index", {**orig, "recompute_selectivity": True}, orig)["sql"]
    assert sql == "SET STATISTICS INDEX IX_IT;\n"
    db.apply("index", {"name": "IX_IT_EXPR", "table": "IT", "expression": "A + B"})
    assert db.spec("indices", "IX_IT_EXPR")["expression"] == "(A + B)"
    partial = {"name": "IX_IT_P", "table": "IT", "columns": ["A"], "condition": "A > 0"}
    out = db.ddl("index", partial)
    if db.engine >= 5:
        assert out["sql"] == "CREATE INDEX IX_IT_P ON IT (A) WHERE A > 0;\n"
        db.run(out["sql"])
        assert db.spec("indices", "IX_IT_P")["condition"] == "A > 0"
    else:
        assert "WHERE" not in out["sql"] and out["warnings"]


def test_view_editor(db):
    db.run("CREATE TABLE VT (ID INT, NAME VARCHAR(20))")
    spec = {"name": "V_VT", "columns": ["VID", "VNAME"], "select": "SELECT ID, NAME FROM VT WHERE ID > 0",
            "check_option": True, "comment": "a view"}
    out = db.apply("view", spec)
    assert out["sql"].startswith("CREATE OR ALTER VIEW V_VT (VID, VNAME)\nAS\nSELECT")
    assert "WITH CHECK OPTION;" in out["sql"]
    got = db.spec("views", "V_VT")
    assert got["columns"] == ["VID", "VNAME"] and got["check_option"] and got["comment"] == "a view"
    assert "CHECK OPTION" not in got["select"]
    db.apply("view", {**got, "check_option": False, "select": got["select"] + " AND ID < 10"}, got)
    got2 = db.spec("views", "V_VT")
    assert not got2["check_option"] and "ID < 10" in got2["select"]


def test_routine_dialogs_roundtrip(db):
    db.run("CREATE TABLE RT (ID INT)")
    proc = {"name": "SP_X", "inputs": [{"name": "A", "type": "INTEGER", "comment": "input a"},
                                       {"name": "B", "type": "VARCHAR(10) = 'x'"}],
            "outputs": [{"name": "R", "type": "INTEGER"}],
            "body": "DECLARE V INT;\nBEGIN\n  V = A;\n  R = V;\n  SUSPEND;\nEND", "comment": "proc"}
    out = db.apply("procedure", proc)
    assert "COMMENT ON PARAMETER SP_X.A IS 'input a'" in out["sql"]
    got = db.spec("procedures", "SP_X")
    assert got["inputs"][0]["comment"] == "input a" and got["body"].startswith("DECLARE V INT")
    assert db.ddl("procedure", got, got)["sql"].startswith("CREATE OR ALTER PROCEDURE SP_X")
    db.apply("procedure", {**got, "body": got["body"].replace("V = A", "V = A + 1")}, got)
    assert "A + 1" in db.spec("procedures", "SP_X")["body"]

    fn = {"name": "F_X", "inputs": [{"name": "A", "type": "INTEGER"}], "returns": "INTEGER",
          "deterministic": True, "body": "BEGIN RETURN A * 2; END"}
    db.apply("function", fn)
    got = db.spec("functions", "F_X")
    assert got["deterministic"] and got["returns"] == "INTEGER"
    trg = {"name": "RT_BI", "relation": "RT", "event": "BEFORE INSERT OR UPDATE", "position": 3, "active": False,
           "body": "BEGIN NEW.ID = COALESCE(NEW.ID, 0); END"}
    db.apply("trigger", trg)
    got = db.spec("triggers", "RT_BI")
    assert got["event"] == "BEFORE INSERT OR UPDATE" and got["position"] == 3 and not got["active"]
    db.apply("trigger", {"name": "DB_CONN", "event": "ON CONNECT", "body": "BEGIN END"})
    assert db.spec("triggers", "DB_CONN")["trigger_kind"] == "database"
    pkg = {"name": "PK_X", "header": "BEGIN\n  FUNCTION ONE RETURNS INT;\nEND",
           "body": "BEGIN\n  FUNCTION ONE RETURNS INT AS BEGIN RETURN 1; END\nEND"}
    db.apply("package", pkg)
    got = db.spec("packages", "PK_X")
    assert "FUNCTION ONE" in got["header"] and "RETURN 1" in got["body"]
    # UDR (external) routines: DDL only, the module may not be installed
    udr = db.ddl("procedure", {"name": "UDR_P", "kind": "external", "inputs": [{"name": "A", "type": "INT"}],
                               "external": {"name": "udrcpp_example!gen_rows", "engine": "UDR"}})
    assert udr["sql"] == ("CREATE OR ALTER PROCEDURE UDR_P (\n    A INT)\n"
                          "EXTERNAL NAME 'udrcpp_example!gen_rows' ENGINE UDR;\n")
    assert db.ddl("procedure", {"name": "P", "body": ""}, ok=False).status_code == 400


def test_sequence_domain_exception_dialogs(db):
    db.apply("sequence", {"name": "SEQ_D", "start": 10, "increment": 2, "comment": "s"})
    s = db.spec("sequences", "SEQ_D")
    assert s["start"] == 10 and s["increment"] == 2 and s["comment"] == "s"
    sql = db.ddl("sequence", {**s, "increment": 3, "set_value": 500}, s)["sql"]
    assert sql == "ALTER SEQUENCE SEQ_D INCREMENT BY 3;\nSET GENERATOR SEQ_D TO 500;\n"
    db.run(sql)
    s2 = db.spec("sequences", "SEQ_D")
    assert s2["increment"] == 3 and s2["current_value"] == 500
    assert db.ddl("sequence", {**s2, "name": "SEQ_NEW"}, s2)["warnings"]

    db.apply("domain", {"name": "D_POS", "data_type": "INTEGER", "default": "1", "not_null": True,
                        "check": "VALUE > 0", "comment": "positive"})
    d = db.spec("domains", "D_POS")
    assert d["default"] == "1" and d["not_null"] and d["check"] == "(VALUE > 0)"
    new = {**d, "name": "D_POSITIVE", "data_type": "BIGINT", "default": None, "not_null": False,
           "check": "VALUE >= 0"}
    sql = db.ddl("domain", new, d)["sql"]
    assert sql.splitlines()[0] == "ALTER DOMAIN D_POS TO D_POSITIVE;"
    assert "ALTER DOMAIN D_POSITIVE DROP CONSTRAINT;" in sql and "ADD CONSTRAINT CHECK (VALUE >= 0)" in sql
    db.run(sql)
    d2 = db.spec("domains", "D_POSITIVE")
    assert d2["data_type"] == "BIGINT" and not d2["not_null"] and d2["default"] is None

    db.apply("exception", {"name": "E_X", "message": "bad @1", "comment": "c"})
    e = db.spec("exceptions", "E_X")
    assert e["message"] == "bad @1"
    sql = db.ddl("exception", {**e, "message": "worse @1"}, e)["sql"]
    assert sql == "CREATE OR ALTER EXCEPTION E_X 'worse @1';\n"


def test_role_dialog_members_and_system_privileges(db):
    privs = ["USER_MANAGEMENT", "MONITOR_ANY_ATTACHMENT"] if db.engine >= 4 else []
    db.apply("role", {"name": "R_DLG", "system_privileges": privs,
                      "members": [{"name": "FBADMIN_T_U1", "type": "USER", "admin": True}], "comment": "r"})
    r = db.spec("roles", "R_DLG")
    assert r["members"] == [{"name": "FBADMIN_T_U1", "type": "USER", "admin": True}]
    assert r["comment"] == "r"
    if db.engine >= 4:
        assert r["system_privileges"] == privs
    new = {**r, "members": [{"name": "FBADMIN_T_U1", "type": "USER", "admin": False},
                            {"name": "FBADMIN_T_U2", "type": "USER", "admin": False}],
           "system_privileges": ["USER_MANAGEMENT"] if db.engine >= 4 else None}
    sql = db.ddl("role", new, r)["sql"]
    assert "REVOKE ADMIN OPTION FOR R_DLG FROM USER FBADMIN_T_U1;" in sql
    assert "GRANT R_DLG TO USER FBADMIN_T_U2;" in sql
    db.run(sql)
    r2 = db.spec("roles", "R_DLG")
    assert sorted(m["name"] for m in r2["members"]) == ["FBADMIN_T_U1", "FBADMIN_T_U2"]
    if db.engine >= 4:
        assert r2["system_privileges"] == ["USER_MANAGEMENT"]


def test_privileges_editor(db):
    db.run("CREATE TABLE PT (A INT, B INT, C INT);\nCREATE ROLE R_PT;\nCREATE SEQUENCE S_PT;\n"
           "CREATE PROCEDURE P_PT AS BEGIN END")
    cur = db.c.get(db.b + "/privileges", params={"object": "PT"}).json()
    assert cur["columns"] == ["A", "B", "C"]
    owner_rows = cur["grants"]
    assert all(g["grantee"] == "SYSDBA" for g in owner_rows)
    want = owner_rows + [
        {"grantee": "FBADMIN_T_JOE", "grantee_type": "USER", "privilege": "SELECT", "object": "PT",
         "object_type": "tables", "grant_option": True},
        {"grantee": "FBADMIN_T_JOE", "grantee_type": "USER", "privilege": "UPDATE", "object": "PT",
         "object_type": "tables", "column": "B"},
        {"grantee": "FBADMIN_T_JOE", "grantee_type": "USER", "privilege": "UPDATE", "object": "PT",
         "object_type": "tables", "column": "A"},
        {"grantee": "R_PT", "grantee_type": "ROLE", "privilege": "INSERT", "object": "PT", "object_type": "tables"},
        {"grantee": "P_PT", "grantee_type": "PROCEDURE", "privilege": "DELETE", "object": "PT",
         "object_type": "tables"},
        {"grantee": "PUBLIC", "grantee_type": "USER", "privilege": "REFERENCES", "object": "PT",
         "object_type": "tables", "column": "C"},
    ]
    sql = db.ddl("grants", {"grants": want}, {"grants": owner_rows})["sql"]
    assert "GRANT SELECT ON TABLE PT TO USER FBADMIN_T_JOE WITH GRANT OPTION;" in sql
    assert "GRANT UPDATE (A, B) ON TABLE PT TO USER FBADMIN_T_JOE;" in sql
    assert "GRANT REFERENCES (C) ON TABLE PT TO PUBLIC;" in sql
    assert "GRANT DELETE ON TABLE PT TO PROCEDURE P_PT;" in sql
    db.run(sql)
    got = db.c.get(db.b + "/privileges", params={"object": "PT"}).json()["grants"]
    keyset = {(g["grantee"], g["privilege"], g.get("column"), g["grant_option"]) for g in got}
    assert ("FBADMIN_T_JOE", "SELECT", None, True) in keyset
    assert ("FBADMIN_T_JOE", "UPDATE", "A", False) in keyset
    assert ("PUBLIC", "REFERENCES", "C", False) in keyset
    assert ("R_PT", "INSERT", None, False) in keyset

    # round trip: the same set produces no statements
    assert db.ddl("grants", {"grants": got}, {"grants": got})["sql"] == ""
    # revoke grant option + revoke a column privilege
    edited = []
    for g in got:
        if g["grantee"] == "FBADMIN_T_JOE" and g["privilege"] == "SELECT":
            edited.append({**g, "grant_option": False})
        elif g["grantee"] == "FBADMIN_T_JOE" and g.get("column") == "A":
            continue
        else:
            edited.append(g)
    sql = db.ddl("grants", {"grants": edited}, {"grants": got})["sql"]
    assert "REVOKE UPDATE (A) ON TABLE PT FROM USER FBADMIN_T_JOE;" in sql
    assert "REVOKE GRANT OPTION FOR SELECT ON TABLE PT FROM USER FBADMIN_T_JOE;" in sql
    db.run(sql)

    # per grantee view + other object kinds
    more = [
        {"grantee": "FBADMIN_T_JOE", "grantee_type": "USER", "privilege": "USAGE", "object": "S_PT",
         "object_type": "sequences"},
        {"grantee": "FBADMIN_T_JOE", "grantee_type": "USER", "privilege": "EXECUTE", "object": "P_PT",
         "object_type": "procedures"},
        {"grantee": "FBADMIN_T_JOE", "grantee_type": "USER", "privilege": "MEMBER", "object": "R_PT",
         "object_type": "roles", "grant_option": True},
    ]
    sql = db.ddl("grants", {"grants": more}, {"grants": []})["sql"]
    assert "GRANT USAGE ON SEQUENCE S_PT TO USER FBADMIN_T_JOE;" in sql
    assert "GRANT R_PT TO USER FBADMIN_T_JOE WITH ADMIN OPTION;" in sql
    db.run(sql)
    mine = db.c.get(db.b + "/privileges", params={"grantee": "FBADMIN_T_JOE"}).json()["grants"]
    kinds = {(g["object_type"], g["privilege"]) for g in mine}
    assert {("sequences", "USAGE"), ("procedures", "EXECUTE"), ("roles", "MEMBER"), ("tables", "SELECT")} <= kinds
    ge = db.c.get(db.b + "/privileges/grantees").json()
    assert "PUBLIC" in ge["users"] and "R_PT" in ge["roles"] and "P_PT" in ge["procedures"]
    objs = db.c.get(db.b + "/privileges/objects").json()
    assert "PT" in objs["tables"] and "S_PT" in objs["sequences"]
    bad = db.ddl("grants", {"grants": [{"grantee": "X", "privilege": "SELECT", "object": "PT",
                                        "object_type": "tables", "column": "A"}]}, ok=False)
    assert bad.status_code == 400


def test_comments_editor(db):
    db.run("CREATE TABLE CT (A INT, B INT);\n"
           "CREATE PROCEDURE CP (X INT) RETURNS (Y INT) AS BEGIN Y = X; SUSPEND; END;\n"
           "CREATE SEQUENCE CS;")
    spec = db.c.get(db.b + "/designer/comments/tables/CT").json()
    assert [i["name"] for i in spec["items"]] == ["A", "B"]
    new = {**spec, "comment": "tbl", "items": [{**spec["items"][0], "comment": "col a"}, spec["items"][1]]}
    sql = db.ddl("comments", new, spec)["sql"]
    assert sql == "COMMENT ON TABLE CT IS 'tbl';\nCOMMENT ON COLUMN CT.A IS 'col a';\n"
    db.run(sql)
    assert db.c.get(db.b + "/designer/comments/tables/CT").json()["items"][0]["comment"] == "col a"
    # clearing a comment sets it to NULL
    sql = db.ddl("comments", {**new, "comment": ""}, new)["sql"]
    assert sql == "COMMENT ON TABLE CT IS NULL;\n"
    pspec = db.c.get(db.b + "/designer/comments/procedures/CP").json()
    assert [i["name"] for i in pspec["items"]] == ["X", "Y"]
    sql = db.ddl("comments", {**pspec, "items": [{"name": "Y", "comment": "out"}]}, pspec)["sql"]
    assert sql == "COMMENT ON PARAMETER CP.Y IS 'out';\n"
    db.run(sql)
    for otype, name in (("sequences", "CS"), ("charsets", "UTF8"), ("collations", "UNICODE_CI")):
        s = db.c.get(db.b + f"/designer/comments/{otype}/{name}").json()
        db.apply("comments", {**s, "comment": f"c {otype}"}, s)
        assert db.obj(otype, name)["properties"]["Comment"] == f"c {otype}"
    assert ddlgen.comment_on("database", "", "db") == "COMMENT ON DATABASE IS 'db'"


def test_rename(db):
    db.run("CREATE DOMAIN D_R AS INT;\nCREATE TABLE RN (A D_R, B INT);\nCREATE INDEX IX_RN ON RN (B)")
    db.apply("rename", {"otype": "columns", "table": "RN", "name": "A", "new_name": "AA"})
    assert [c["name"] for c in db.spec("tables", "RN")["columns"]] == ["AA", "B"]
    db.apply("rename", {"otype": "domains", "name": "D_R", "new_name": "D_R2"})
    assert db.spec("tables", "RN")["columns"][0]["domain"] == "D_R2"
    idx = db.spec("indices", "IX_RN")
    out = db.apply("rename", {"otype": "indices", "name": "IX_RN", "new_name": "IX_RN2", "index": idx})
    assert out["sql"] == "DROP INDEX IX_RN;\nCREATE INDEX IX_RN2 ON RN (B);\n"
    assert db.spec("indices", "IX_RN2")["columns"] == ["B"]
    for otype in ("sequences", "tables", "views", "procedures"):
        r = db.ddl("rename", {"otype": otype, "name": "X", "new_name": "Y"}, ok=False)
        assert r.status_code == 400 and "rename" in r.json()["detail"].lower()


def test_rename_column_used_by_constraints(db):
    db.apply("table", {"name": "RC", "columns": [{"name": "A", "data_type": "INT", "not_null": True},
                                                  {"name": "B", "data_type": "INT"}],
                       "constraints": [{"name": "PK_RC", "type": "PRIMARY KEY", "columns": ["A"]},
                                       {"name": "CK_RC", "type": "CHECK", "check": "B > A"}],
                       "indices": [{"name": "IX_RC_B", "columns": ["B"]}]})
    orig = db.spec("tables", "RC")
    new = __import__("copy").deepcopy(orig)
    new["columns"][0]["name"] = "AA"
    new["columns"][1]["name"] = "BB"
    sql = db.ddl("table", new, orig)["sql"]
    lines = sql.splitlines()
    assert lines.index("ALTER TABLE RC DROP CONSTRAINT PK_RC;") < lines.index("ALTER TABLE RC ALTER COLUMN A TO AA;")
    assert "ALTER TABLE RC ADD CONSTRAINT CK_RC CHECK (BB > AA);" in lines
    assert "DROP INDEX IX_RC_B;" not in lines  # Firebird renames index segments itself
    db.run(sql)
    after = db.spec("tables", "RC")
    assert {c["name"]: c["columns"] for c in after["constraints"]}["PK_RC"] == ["AA"]
    assert after["indices"][0]["columns"] == ["BB"]


def test_drop_preview(db):
    db.run("CREATE DOMAIN D_DP AS INT;\n"
           "CREATE TABLE DP (ID INT NOT NULL PRIMARY KEY, V D_DP CHECK (V > 0), W COMPUTED BY (V * 2));\n"
           "CREATE INDEX IX_DP_EXPR ON DP COMPUTED BY (V + 1);\n"
           "CREATE TABLE DP_CHILD (ID INT, P INT REFERENCES DP (ID), "
           "  CHECK (P IS NULL OR EXISTS (SELECT 1 FROM DP WHERE DP.V = P)));\n"
           "CREATE VIEW V_DP AS SELECT ID, V FROM DP WHERE V > 1 WITH CHECK OPTION;\n"
           "CREATE VIEW V_DP2 AS SELECT ID FROM V_DP;\n"
           "CREATE PROCEDURE P_DP AS DECLARE X INT; BEGIN SELECT COUNT(*) FROM V_DP2 INTO X; END")
    p = db.c.get(db.b + "/objects/tables/DP/drop-preview").json()
    assert p["sql"] == "DROP TABLE DP;\n"
    deps = {(d["name"], d["type"], d["depth"]) for d in p["dependents"]}
    assert ("V_DP", "views", 1) in deps and ("V_DP2", "views", 2) in deps and ("P_DP", "procedures", 3) in deps
    # the table's own CHECK trigger, computed column and expression index are not listed; the CHECK of
    # another table is reported as that table
    names = {d["name"] for d in p["dependents"]}
    assert names == {"V_DP", "V_DP2", "P_DP", "DP_CHILD"}, p["dependents"]
    assert next(d for d in p["dependents"] if d["name"] == "DP_CHILD")["detail"] == "CHECK constraint of DP_CHILD"
    assert p["foreign_keys"][0]["relation"] == "DP_CHILD"
    p = db.c.get(db.b + "/objects/domains/D_DP/drop-preview").json()
    assert {"relation": "DP", "name": "V"} in p["columns"]
    p = db.c.get(db.b + "/objects/tables/DP/drop-preview", params={"column": "V"}).json()
    assert p["sql"] == "ALTER TABLE DP DROP V;\n"
    assert any(d["name"] == "V_DP" for d in p["dependents"])
    p = db.c.get(db.b + "/objects/tables/DP/drop-preview", params={"column": "ID"}).json()
    assert p["constraints"]
    p = db.c.get(db.b + "/objects/procedures/P_DP/drop-preview").json()
    assert p["dependents"] == [] and p["sql"] == "DROP PROCEDURE P_DP;\n"


def test_ddlgen_unit():
    """Pure generator checks that do not need a server."""
    assert ddlgen.paren("(A) + (B)") == "((A) + (B))"
    assert ddlgen.paren("(A + B)") == "(A + B)"
    c = {"name": "x", "data_type": "INT", "default": "DEFAULT 5", "not_null": True}
    assert ddlgen.column_def(c, 5) == '"x" INT DEFAULT 5 NOT NULL'
    assert ddlgen.constraint_def({"type": "CHECK", "check": "CHECK (A > 0)"}) == "CHECK (A > 0)"
    sc = ddlgen.generate("table", {"name": "T", "columns": [{"name": "ID", "data_type": "INT",
                                                             "identity": "ALWAYS", "identity_increment": 2}]},
                         None, engine=3)
    assert "GENERATED BY DEFAULT AS IDENTITY" in sc.sql and len(sc.warnings) == 2
    with pytest.raises(ddlgen.DDLError):
        ddlgen.generate("nope", {})
