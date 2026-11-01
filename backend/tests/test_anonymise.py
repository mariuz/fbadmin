"""Anonymised copies (roadmap 2.3): masking functions, suggestions, rules along foreign keys, and a real copy -
scratch database on the source server, masking with triggers off and foreign keys recreated, leak check,
restore on the target, deterministic refresh."""

from __future__ import annotations

import datetime as dt
import decimal
import time
import uuid
from pathlib import Path

import firebirdsql
import pytest

from fbadmin import anonymise as A

from .conftest import DB_DIR, HOST, PASSWORD, PORT, USER, requires_server

S = b"k" * 32


def m(strategy, kind="text", length=None, unique=False, **kw):
    return A.masker(A.Rule(strategy=strategy, **kw), kind, length, S, unique)


def test_kinds_and_lengths():
    assert [A.kind_of(t) for t in ("VARCHAR(60)", "CHAR(5)", "BIGINT", "NUMERIC(10,2)", "DOUBLE PRECISION", "DATE",
                                   "TIMESTAMP WITH TIME ZONE", "TIME", "BOOLEAN", "BLOB SUB_TYPE TEXT",
                                   "BLOB SUB_TYPE BINARY", "VARBINARY(16)", "INT128", "DECFLOAT(34)")] == \
        ["text", "text", "int", "decimal", "float", "date", "timestamp", "time", "bool", "text_blob", "blob",
         "binary", "int", "float"]
    assert A.length_of("VARCHAR(60)") == 60 and A.length_of("CHAR(5)") == 5 and A.length_of("INTEGER") is None


def test_maskers_are_deterministic_and_fit():
    email = m("email", length=30)
    a, b = email("ann@corp.com"), email("bob@corp.com")
    assert a == email("ann@corp.com") and a != b and a.endswith("@example.invalid") and len(a) <= 30
    assert email(None) is None
    # the same strategy gives the same value in another column (foreign keys keep matching)
    assert m("email", length=60)("ann@corp.com") == "user" + a[4:14] + "@example.invalid"
    # a different secret gives other values
    assert A.masker(A.Rule(strategy="email"), "text", None, b"other", False)("ann@corp.com") != a
    name = m("name", length=40)
    assert name("Alice Real") == name("Alice Real") and " " in name("Alice Real")
    assert m("name", unique=True)("Alice Real").count("-") == 1
    assert len(m("last_name", length=3)("Jones")) <= 3
    assert m("phone")("+40 (721) 555-123")[:4] == "+" + m("phone")("+40 (721) 555-123")[1:4]
    ph = m("phone")("+40 (721) 555-123")
    assert [c.isdigit() for c in ph] == [c.isdigit() for c in "+40 (721) 555-123"] and ph != "+40 (721) 555-123"
    assert m("redact", keep=4)("RO49 AAAA 1B31 0075 9384 0000") == "XX99 XXXX 9X99 9999 9999 0000"
    assert m("redact", keep=0)("ab-12") == "XX-99"
    assert m("hash", "int")(42) == m("hash", "int")(42) and 1 <= m("hash", "int")(42) <= 2_000_000_000
    h = m("hash", length=8)("secret value")
    assert len(h) <= 8 and h == m("hash", length=8)("secret value")
    assert m("null")("x") is None and m("fixed", value="n/a")("x") == "n/a" and m("fixed", value="n/a")(None) is None
    w = m("words")("one two three")
    assert len(w.split()) == 3 and w == m("words")("one two three")
    noisy = m("number_noise", "decimal", percent=10)(decimal.Decimal("1000.00"))
    assert decimal.Decimal("900") <= noisy <= decimal.Decimal("1100") and noisy.as_tuple().exponent == -2
    assert isinstance(m("number_noise", "int", percent=50)(10), int)
    born = dt.date(1980, 5, 17)
    shifted = m("date_shift", "date", days=30)(born)
    assert shifted != born and abs((shifted - born).days) <= 30 and shifted == m("date_shift", "date", days=30)(born)


def test_suggestions():
    assert A.suggest("EMAIL_ADDRESS", "text", True) == {"strategy": "email"}
    assert A.suggest("FIRST_NAME", "text", False) == {"strategy": "first_name"}
    assert A.suggest("CUSTOMER_NAME", "text", False) == {"strategy": "name"}
    assert A.suggest("DATE_OF_BIRTH", "date", False) == {"strategy": "date_shift", "days": 180}
    assert A.suggest("IBAN", "text", False) == {"strategy": "redact", "keep": 4}
    assert A.suggest("PASSWORD_HASH", "text", False) == {"strategy": "null"}
    assert A.suggest("PASSWORD_HASH", "text", True) == {"strategy": "hash"}  # NOT NULL: cannot be emptied
    assert A.suggest("SALARY", "decimal", False) == {"strategy": "number_noise", "percent": 15}
    assert A.suggest("AMOUNT", "decimal", False) is None and A.suggest("NAME", "int", False) is None


SCHEMA = {"tables": [
    {"name": "CUSTOMERS", "columns": [
        {"name": "ID", "kind": "int", "data_type": "INTEGER", "not_null": True, "key": True, "linked": []},
        {"name": "EMAIL", "kind": "text", "data_type": "VARCHAR(60)", "not_null": True, "key": True,
         "linked": ["ORDERS.CUSTOMER_EMAIL"]},
        {"name": "NOTE", "kind": "text_blob", "data_type": "BLOB SUB_TYPE TEXT", "not_null": False, "key": False, "linked": []}]},
    {"name": "ORDERS", "columns": [
        {"name": "CUSTOMER_EMAIL", "kind": "text", "data_type": "VARCHAR(60)", "not_null": False, "key": False,
         "linked": ["CUSTOMERS.EMAIL"]}]},
]}


def test_rules_follow_foreign_keys():
    eff = A.effective_rules({"CUSTOMERS": {"EMAIL": {"strategy": "email"}, "ID": {"strategy": "keep"}}}, SCHEMA)
    assert eff == {"CUSTOMERS": {"EMAIL": A.Rule(strategy="email")}, "ORDERS": {"CUSTOMER_EMAIL": A.Rule(strategy="email")}}
    # set on the child, applied to the parent too
    eff = A.effective_rules({"ORDERS": {"CUSTOMER_EMAIL": {"strategy": "hash"}}}, SCHEMA)
    assert eff["CUSTOMERS"]["EMAIL"].strategy == "hash"
    for rules, msg in [
        ({"CUSTOMERS": {"EMAIL": {"strategy": "null"}}}, "NOT NULL"),
        ({"CUSTOMERS": {"EMAIL": {"strategy": "shuffle"}}}, "cannot be shuffled"),
        ({"CUSTOMERS": {"NOTE": {"strategy": "email"}}}, "does not fit"),
        ({"CUSTOMERS": {"EMAIL": {"strategy": "email"}}, "ORDERS": {"CUSTOMER_EMAIL": {"strategy": "hash"}}},
         "different rules"),
        ({"CUSTOMERS": {"NOPE": {"strategy": "null"}}}, "no such column"),
    ]:
        with pytest.raises(ValueError, match=msg):
            A.effective_rules(rules, SCHEMA)


# ============================================================ real server
def _con(path):
    return firebirdsql.connect(host=HOST, port=PORT, database=path, user=USER, password=PASSWORD)


def _wait(client, job_id, timeout=240):
    for _ in range(int(timeout / 0.25)):
        j = client.get(f"/api/jobs/{job_id}").json()
        if j["status"] != "running":
            return j
        time.sleep(0.25)
    raise AssertionError("job did not end")


@requires_server
def test_anonymised_copy(client, database):
    sid, dbid = database
    base = f"/api/servers/{sid}/databases/{dbid}"
    src_path = client.get(f"/api/servers/{sid}").json()["databases"][0]["path"]
    con = _con(src_path)
    cur = con.cursor()
    for ddl in (
        "CREATE TABLE CUSTOMERS (ID INTEGER NOT NULL PRIMARY KEY, EMAIL VARCHAR(60) NOT NULL UNIQUE, "
        "CUSTOMER_NAME VARCHAR(40), PHONE VARCHAR(20), BIRTH_DATE DATE, SALARY NUMERIC(10,2), NOTES BLOB SUB_TYPE TEXT, "
        "PASSWORD VARCHAR(40), TOUCHED VARCHAR(20))",
        "CREATE TABLE ORDERS (ID INTEGER NOT NULL PRIMARY KEY, CUSTOMER_EMAIL VARCHAR(60) REFERENCES CUSTOMERS(EMAIL), "
        "AMOUNT INTEGER)",
        # a trigger that must not run while masking (it would mark the rows)
        "CREATE TRIGGER CUSTOMERS_BU FOR CUSTOMERS BEFORE UPDATE AS BEGIN NEW.TOUCHED = 'trigger'; END",
    ):
        cur.execute(ddl)
        con.commit()
    cur.execute("EXECUTE BLOCK AS DECLARE I INTEGER = 0; BEGIN WHILE (I < 120) DO BEGIN "
                "INSERT INTO CUSTOMERS VALUES (:I, 'person' || :I || '@corp.example', 'Real Person ' || :I, "
                "'+40 721 555 ' || :I, DATEADD(:I DAY TO DATE '1980-01-01'), 1000 + :I, 'likes cats', 'pw' || :I, NULL); "
                "INSERT INTO ORDERS VALUES (:I, 'person' || MOD(:I, 40) || '@corp.example', :I); "
                "I = I + 1; END END")
    con.commit()
    con.close()

    cols = client.get(base + "/anonymise/columns").json()
    cust = {c["name"]: c for c in next(t for t in cols["tables"] if t["name"] == "CUSTOMERS")["columns"]}
    assert cust["EMAIL"]["suggested"] == {"strategy": "email"} and cust["EMAIL"]["linked"] == ["ORDERS.CUSTOMER_EMAIL"]
    assert cust["CUSTOMER_NAME"]["suggested"]["strategy"] == "name" and cust["PASSWORD"]["suggested"]["strategy"] == "null"
    assert cust["BIRTH_DATE"]["suggested"]["strategy"] == "date_shift" and cust["EMAIL"]["unique"]

    rules = {"CUSTOMERS": {n: c["suggested"] for n, c in cust.items() if c["suggested"]}}
    target = f"{DB_DIR}/fbadmin_anon_copy_{uuid.uuid4().hex[:6]}.fdb"
    r = client.post(base + "/anonymise/profiles", json={"name": "dev copy", "rules": rules,
                                                         "target": {"server_id": sid, "path": target,
                                                                    "register_as": "dev copy"}})
    assert r.status_code == 201, r.text
    prof = r.json()
    assert "secret" not in prof and prof["rule_count"] == len(rules["CUSTOMERS"])
    chk = client.post(base + f"/anonymise/profiles/{prof['id']}/check").json()
    assert {"table": "ORDERS", "column": "CUSTOMER_EMAIL", "strategy": "email", "linked": True} in chk["columns"]

    pv = client.post(base + "/anonymise/preview", json={"table": "CUSTOMERS", "rules": rules,
                                                        "profile_id": prof["id"], "limit": 3}).json()
    names = [c["name"] for c in pv["columns"]]
    e = names.index("EMAIL")
    assert pv["original"][0][e] == "person0@corp.example" and pv["masked"][0][e].endswith("@example.invalid")

    # the target cannot be the source
    bad = client.post(base + "/anonymise/profiles", json={"name": "bad", "rules": rules,
                                                          "target": {"server_id": sid, "path": src_path}}).json()
    assert client.post(base + f"/anonymise/profiles/{bad['id']}/run").status_code == 400

    job = client.post(base + f"/anonymise/profiles/{prof['id']}/run").json()
    j = _wait(client, job["id"])
    log = "\n".join(client.get(f"/api/jobs/{job['id']}", params={"log": "true"}).json().get("log") or [])
    if j["status"] == "failed" and "***unknown***" in (j.get("error") or "") and cols["engine_version"].startswith("6"):
        pytest.xfail("Firebird 6 snapshot: a dropped foreign key is still enforced (docs/FIREBIRD6.md)")
    assert j["status"] == "finished", (j, log)
    last = client.get(base + "/anonymise").json()["profiles"][0]["last_run"]
    assert last["ok"] and last["leaks"] == [] and last["foreign_keys"] == 1, last
    assert last["tables"]["ORDERS"]["rows"] == 120 and last["tables"]["CUSTOMERS"]["columns"]["EMAIL"] == "email"
    copy_id = last["database_id"]
    assert any(d["id"] == copy_id and d["name"] == "dev copy" for d in client.get(f"/api/servers/{sid}").json()["databases"])

    c2 = _con(target)
    try:
        q = c2.cursor()
        q.execute("SELECT COUNT(*), COUNT(DISTINCT EMAIL) FROM CUSTOMERS")
        assert q.fetchone() == (120, 120)
        q.execute("SELECT COUNT(*) FROM CUSTOMERS WHERE EMAIL LIKE '%corp.example' OR CUSTOMER_NAME LIKE 'Real%' "
                  "OR PASSWORD IS NOT NULL OR TOUCHED IS NOT NULL OR NOTES = 'likes cats'")
        assert q.fetchone()[0] == 0
        # foreign keys still match: every order's customer exists, and the constraint is back
        q.execute("SELECT COUNT(*) FROM ORDERS O WHERE NOT EXISTS (SELECT 1 FROM CUSTOMERS C WHERE C.EMAIL = O.CUSTOMER_EMAIL)")
        assert q.fetchone()[0] == 0
        q.execute("SELECT COUNT(*) FROM RDB$RELATION_CONSTRAINTS WHERE RDB$RELATION_NAME = 'ORDERS' "
                  "AND RDB$CONSTRAINT_TYPE = 'FOREIGN KEY'")
        assert q.fetchone()[0] == 1
        q.execute("SELECT COALESCE(RDB$TRIGGER_INACTIVE, 0) FROM RDB$TRIGGERS WHERE RDB$TRIGGER_NAME = 'CUSTOMERS_BU'")
        assert q.fetchone()[0] == 0  # switched on again
        q.execute("SELECT SALARY, BIRTH_DATE, PHONE FROM CUSTOMERS WHERE ID = 7")
        sal, born, phone = q.fetchone()
        assert sal != decimal.Decimal("1007.00") and decimal.Decimal("850") <= sal <= decimal.Decimal("1160")
        assert born != dt.date(1980, 1, 8) and phone.startswith("+") and phone != "+40 721 555 7"
        q.execute("SELECT EMAIL FROM CUSTOMERS ORDER BY ID")
        first_emails = [r[0] for r in q.fetchall()]
        c2.commit()
    finally:
        c2.close()

    # the source is untouched, the scratch copy and the local files are gone
    c3 = _con(src_path)
    q = c3.cursor()
    q.execute("SELECT EMAIL, TOUCHED FROM CUSTOMERS WHERE ID = 0")
    assert q.fetchone() == ("person0@corp.example", None)
    c3.close()
    from fbadmin.config import settings

    assert list((Path(settings.data_dir) / "anonymise" / "work").iterdir()) == []
    assert "Dropped the scratch copy" in log and "unmasked backup file was deleted" in log

    # a refresh replaces the copy with the same pseudonyms
    p2 = client.put(base + f"/anonymise/profiles/{prof['id']}", json={
        "name": "dev copy", "rules": rules, "target": {"server_id": sid, "path": target, "replace": True}})
    assert p2.status_code == 200, p2.text
    j = _wait(client, client.post(base + f"/anonymise/profiles/{prof['id']}/run").json()["id"])
    assert j["status"] == "finished", j
    c2 = _con(target)
    q = c2.cursor()
    q.execute("SELECT EMAIL FROM CUSTOMERS ORDER BY ID")
    assert [r[0] for r in q.fetchall()] == first_emails
    c2.close()

    # on the source's timeline
    tl = client.get(base + "/timeline", params={"category": "backup"}).json()["events"]
    assert any(e["title"].startswith("Anonymised copy to ") and e["status"] == "ok" for e in tl)

    client.post(f"/api/servers/{sid}/databases/{copy_id}/drop")
    assert client.delete(base + f"/anonymise/profiles/{prof['id']}").status_code == 204
    assert client.get(base + "/anonymise").json()["profiles"][0]["name"] == "bad"
