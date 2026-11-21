"""Unused index finder (roadmap 2.5): index names from both plan formats, the statuses (unused, used, constraint,
inactive), the drop script, the table writes and how good the evidence is."""

from __future__ import annotations

from fbadmin.monitoring import history
from fbadmin.monitoring import regressions as R
from fbadmin.monitoring import unused_indices as U

from .conftest import requires_server
from .test_maintenance import execute


def test_indices_in_plan():
    explained = ('Select Expression\n    -> Filter\n        -> Table "ORDERS" Access By ID\n'
                 '            -> Bitmap\n                -> Index "IX_ORDERS_DAY" Range Scan (full match)\n'
                 '    -> Index "PUBLIC"."IX_Mixed" Unique Scan')
    assert U.indices_in_plan(explained) == {"IX_ORDERS_DAY", "IX_Mixed"}
    assert U.indices_in_plan("PLAN JOIN (O INDEX (IX_A, IX_B), C ORDER PK_C)") == {"IX_A", "IX_B", "PK_C"}
    assert U.indices_in_plan("PLAN (T NATURAL)") == set() and U.indices_in_plan(None) == set()


@requires_server
def test_unused_indices(client, database):
    sid, dbid = database
    for sql in (
        "CREATE TABLE CUST (ID INTEGER NOT NULL PRIMARY KEY, EMAIL VARCHAR(80) UNIQUE, NAME VARCHAR(40))",
        "CREATE TABLE ORD (ID INTEGER NOT NULL PRIMARY KEY, CUST INTEGER REFERENCES CUST (ID), ODAY DATE, NOTE VARCHAR(20))",
        "CREATE INDEX IX_ORD_DAY ON ORD (ODAY)",
        "CREATE INDEX IX_ORD_NOTE ON ORD (NOTE)",
        "CREATE DESCENDING INDEX IX_CUST_NAME ON CUST (NAME)",
        "CREATE INDEX IX_OFF ON ORD (NOTE, ODAY)",
        "ALTER INDEX IX_OFF INACTIVE",
        "INSERT INTO CUST VALUES (1, 'a@b', 'A')",
        "INSERT INTO ORD VALUES (1, 1, CURRENT_DATE, 'x')",
        "INSERT INTO ORD VALUES (2, 1, CURRENT_DATE, 'y')",
    ):
        execute(client, sid, dbid, sql)
    base = f"/api/servers/{sid}/databases/{dbid}/monitoring"
    r = client.get(base + "/unused-indices").json()
    assert r["quality"] == "none" and r["evidence"]["plans"] == 0  # no history: nothing to go on
    client.put(base + "/history/tracking", json={"enabled": True})
    try:
        store = history.get_store()
        R.record(store, sid, dbid, "SELECT * FROM ORD WHERE ODAY = CURRENT_DATE", 1.2, source="trace",
                 plan='Select Expression\n    -> Filter\n        -> Table "ORD" Access By ID\n'
                      '            -> Bitmap\n                -> Index "IX_ORD_DAY" Range Scan (full match)')
        R.record(store, sid, dbid, "SELECT * FROM CUST ORDER BY NAME DESC", 0.9, source="sampler",
                 plan="PLAN (CUST ORDER IX_CUST_NAME)")
        r = client.get(base + "/unused-indices", params={"days": 30}).json()
        by = {i["name"]: i for i in r["indices"]}
        assert by["IX_ORD_DAY"]["status"] == "used" and by["IX_ORD_DAY"]["last_used"]
        assert by["IX_CUST_NAME"]["status"] == "used"
        drop = "DROP INDEX PUBLIC.IX_ORD_NOTE;" if by["IX_ORD_NOTE"]["schema"] else "DROP INDEX IX_ORD_NOTE;"  # Firebird 6
        assert by["IX_ORD_NOTE"]["status"] == "unused" and by["IX_ORD_NOTE"]["drop_sql"] == drop
        assert by["IX_ORD_NOTE"]["columns"] == ["NOTE"] and by["IX_ORD_NOTE"]["table"] == "ORD"
        assert by["IX_OFF"]["status"] == "inactive" and by["IX_OFF"]["drop_sql"] is None
        constraint = [i for i in r["indices"] if i["status"] == "constraint"]
        assert {i["constraint"] for i in constraint} == {"PRIMARY KEY", "UNIQUE", "FOREIGN KEY"}
        assert r["indices"][0]["name"] == "IX_ORD_NOTE"  # candidates first
        assert r["script"] == drop and r["counts"]["unused"] == 1
        assert r["evidence"]["plans"] == 2 and r["evidence"]["sources"] == {"trace": 1, "sampler": 1}
        assert r["quality"] == "little" and r["evidence"]["trace"] is False
        if r["writes_known"]:
            assert by["IX_ORD_NOTE"]["table_writes"] >= 2  # the two inserts into ORD
    finally:
        client.delete(base + "/history")
        client.put(base + "/history/tracking", json={"enabled": False})
