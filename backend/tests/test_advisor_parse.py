"""Unit tests of the index advisor's SQL/plan parsing (no server needed)."""

from fbadmin.monitoring import advisor
from fbadmin.monitoring.metrics import render


def test_table_refs():
    refs = advisor.table_refs('SELECT * FROM ORDERS o JOIN "Cust" AS c ON c.ID = o.CUST_ID LEFT JOIN ITEMS ON 1=1')
    assert refs == {"ORDERS": "ORDERS", "O": "ORDERS", "Cust": "Cust", "C": "Cust", "ITEMS": "ITEMS"}
    assert advisor.table_refs("UPDATE T SET A = 1 WHERE B = 2") == {"T": "T"}


def test_predicates():
    p = advisor.predicates(
        "SELECT * FROM T t JOIN U u ON u.TID = t.ID WHERE t.A = 'x' AND t.B > 5 AND t.C LIKE 'ab%' "
        "AND t.D LIKE '%z' AND UPPER(t.E) = 'Q' AND t.F <> 1 AND 7 = t.G ORDER BY t.H")
    got = {(x["qualifier"], x["column"], x["kind"]) for x in p}
    assert ("U", "TID", "eq") in got and ("T", "ID", "eq") in got
    assert ("T", "A", "eq") in got
    assert ("T", "B", "range") in got
    assert ("T", "C", "range") in got
    assert ("T", "D", "unsargable") in got
    assert ("T", "E", "function") in got
    assert ("T", "F", "unsargable") in got
    assert ("T", "G", "eq") in got
    assert not any(x["column"] == "H" for x in p)
    # string literals do not confuse the parser
    p = advisor.predicates("SELECT * FROM T WHERE A = 'B = 1 WHERE C = 2'")
    assert [x["column"] for x in p] == ["A"]


def test_full_scans():
    plan = ('Select Expression\n    -> Filter\n        -> Table "ORD" as "O" Full Scan\n'
            '    -> Table "CUST" Access By ID')
    assert advisor.full_scans(plan, {}) == [("ORD", "O")]
    assert advisor.full_scans("PLAN JOIN (O NATURAL, C INDEX (PK_C))", {"O": "ORD", "C": "CUST"}) == [("ORD", "O")]
    assert advisor.full_scans(None, {}) == []


def test_metrics_render():
    text = render([({"server": 'a"b', "host": "h:1", "database": "d"}, {"attachments": 3, "page_reads": 10}, 0.01),
                   ({"server": "x", "host": "h:2", "database": "e"}, None, 0.5)])
    assert 'fbadmin_database_up{server="a\\"b",host="h:1",database="d"} 1' in text
    assert 'fbadmin_database_up{server="x",host="h:2",database="e"} 0' in text
    assert 'firebird_attachments{server="a\\"b",host="h:1",database="d"} 3' in text
    assert 'firebird_page_reads_total{server="a\\"b",host="h:1",database="d"} 10' in text
    assert text.endswith("\n")
