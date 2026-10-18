"""Property-based DDL round-trips with generated schemas (hypothesis + a live server).

For each generated schema (domains, tables with many column types, defaults, NOT NULL,
collations, identity columns, PK/UNIQUE/CHECK/FK constraints, indices and comments):

1. apply the generated DDL to database A,
2. extract the DDL of every generated object through the API,
3. replay the extracted DDL into database B,
4. extract again from B and require identical text.

Objects of every example get a unique prefix, so the two databases are created once per
test. Raise the number of examples with ``FBADMIN_DDL_EXAMPLES`` (default 12).
"""

from __future__ import annotations

import itertools
import os
import uuid

import pytest

from .conftest import DB_DIR, requires_server

hypothesis = pytest.importorskip("hypothesis")
from hypothesis import HealthCheck, given, settings  # noqa: E402
from hypothesis import strategies as st  # noqa: E402

pytestmark = [pytest.mark.slow, requires_server]

EXAMPLES = int(os.environ.get("FBADMIN_DDL_EXAMPLES", "12"))
_seq = itertools.count(1)

KEY_TYPES = ["SMALLINT", "INTEGER", "BIGINT", "DATE", "VARCHAR(40)", "CHAR(10)", "NUMERIC(9,2)",
             "VARCHAR(30) CHARACTER SET UTF8", "VARCHAR(20) CHARACTER SET WIN1252"]
CHARSETS = ["UTF8", "WIN1252", "ISO8859_1", "ASCII", "NONE", "OCTETS"]
UTF8_COLLATIONS = ["UNICODE", "UNICODE_CI", "UNICODE_CI_AI"]


def numeric_type():
    return st.tuples(st.sampled_from(["NUMERIC", "DECIMAL"]), st.integers(1, 18)).flatmap(
        lambda t: st.integers(0, t[1]).map(lambda s: f"{t[0]}({t[1]},{s})")
    )


def string_type():
    return st.tuples(st.sampled_from(["VARCHAR", "CHAR"]), st.integers(1, 200), st.sampled_from(CHARSETS + [None])
                     ).flatmap(lambda t: (
                         st.sampled_from(UTF8_COLLATIONS + [None]) if t[2] == "UTF8" else st.just(None)
                     ).map(lambda coll: (f"{t[0]}({t[1]})" + (f" CHARACTER SET {t[2]}" if t[2] else ""), coll)))


def data_type(engine: int):
    simple = ["SMALLINT", "INTEGER", "BIGINT", "DOUBLE PRECISION", "FLOAT", "DATE", "TIME", "TIMESTAMP",
              "BOOLEAN", "BLOB SUB_TYPE TEXT", "BLOB SUB_TYPE BINARY", "BLOB SUB_TYPE TEXT CHARACTER SET UTF8"]
    if engine >= 4:
        simple += ["INT128", "DECFLOAT(16)", "DECFLOAT(34)", "TIME WITH TIME ZONE", "TIMESTAMP WITH TIME ZONE",
                   "BINARY(8)", "VARBINARY(20)"]
    return st.one_of(st.sampled_from(simple).map(lambda t: (t, None)), numeric_type().map(lambda t: (t, None)),
                     string_type())


def default_for(t: str):
    """A DEFAULT clause valid for type ``t`` (or None)."""
    u = t.upper()
    if u.startswith(("SMALLINT", "INTEGER", "BIGINT", "INT128")):
        return st.sampled_from([None, "DEFAULT 0", "DEFAULT 42", "DEFAULT -1", "DEFAULT NULL"])
    if u.startswith(("NUMERIC", "DECIMAL")):
        return st.sampled_from([None, "DEFAULT 0", "DEFAULT NULL"])
    if u.startswith(("DOUBLE", "FLOAT", "DECFLOAT")):
        return st.sampled_from([None, "DEFAULT 0", "DEFAULT 1.5"])
    if u.startswith(("VARCHAR", "CHAR")) and "OCTETS" not in u:
        return st.sampled_from([None, "DEFAULT 'x'", "DEFAULT ''", "DEFAULT 'it''s'", "DEFAULT USER"])
    if u == "BOOLEAN":
        return st.sampled_from([None, "DEFAULT TRUE", "DEFAULT FALSE"])
    if u == "TIMESTAMP":
        return st.sampled_from([None, "DEFAULT CURRENT_TIMESTAMP", "DEFAULT 'NOW'"])
    if u == "DATE":
        return st.sampled_from([None, "DEFAULT CURRENT_DATE"])
    return st.just(None)


comment_text = st.text(alphabet=st.characters(min_codepoint=32, max_codepoint=0x24F, blacklist_categories=("Cs",)),
                       min_size=1, max_size=40)


def sql_str(s: str) -> str:
    return "'" + s.replace("'", "''") + "'"


@st.composite
def schema(draw, engine: int, prefix: str):
    """Returns (ddl_script, [(otype, name), ...]) in creation order."""
    stmts: list[str] = []
    objects: list[tuple[str, str]] = []
    domains: list[tuple[str, str]] = []  # (name, base type)
    for d in range(draw(st.integers(0, 2))):
        name = f"{prefix}_D{d}"
        t, coll = draw(data_type(engine))
        s = f"CREATE DOMAIN {name} AS {t}"
        dflt = draw(default_for(t))
        if dflt:
            s += f" {dflt}"
        if dflt != "DEFAULT NULL" and draw(st.booleans()):
            s += " NOT NULL"
        if t.startswith(("SMALLINT", "INTEGER", "BIGINT")) and draw(st.booleans()):
            s += " CHECK (VALUE >= 0)"
        if coll:
            s += f" COLLATE {coll}"
        stmts.append(s + ";")
        objects.append(("domains", name))
        domains.append((name, t))

    int_pks: list[str] = []  # tables whose PK is a single INTEGER column "ID"
    for ti in range(draw(st.integers(1, 3))):
        tname = draw(st.sampled_from([f"{prefix}_T{ti}", f'"{prefix.lower()}_t {ti}"']))
        cols: list[str] = []
        after: list[str] = []
        pk_int = draw(st.booleans())
        if pk_int:
            ident = draw(st.sampled_from(["", " GENERATED BY DEFAULT AS IDENTITY",
                                          " GENERATED BY DEFAULT AS IDENTITY (START WITH 100)"]))
            cols.append(f"ID INTEGER{ident} NOT NULL")
        else:
            cols.append(f"ID {draw(st.sampled_from(KEY_TYPES))} NOT NULL")
        key_cols = []
        for ci in range(draw(st.integers(1, 6))):
            cname = draw(st.sampled_from([f"C{ci}", f'"c {ci}"', f'"Mixed{ci}"']))
            if domains and draw(st.integers(0, 3)) == 0:
                dom, base = draw(st.sampled_from(domains))
                col = f"{cname} {dom}"
                if draw(st.booleans()) and "NOT NULL" not in col:
                    col += " NOT NULL"
            else:
                t, coll = draw(data_type(engine))
                col = f"{cname} {t}"
                dflt = draw(default_for(t))
                if dflt:
                    col += f" {dflt}"
                if dflt != "DEFAULT NULL" and draw(st.integers(0, 3)) == 0:
                    col += " NOT NULL"
                if coll:
                    col += f" COLLATE {coll}"
                if t in KEY_TYPES or t.startswith(("SMALLINT", "INTEGER", "BIGINT")):
                    key_cols.append((cname, t))
                if t.startswith(("SMALLINT", "INTEGER", "BIGINT", "NUMERIC", "DECIMAL")) and draw(st.booleans()):
                    after.append(f"ALTER TABLE {tname} ADD CONSTRAINT {prefix}_CK{ti}_{ci} CHECK ({cname} >= 0);")
            cols.append(col)
        if draw(st.booleans()):
            cols.append("CALC COMPUTED BY (ID || '-')")
        if int_pks and draw(st.booleans()):
            ref = draw(st.sampled_from(int_pks))
            cols.append("REF_ID INTEGER")
            rule = draw(st.sampled_from(["", " ON DELETE CASCADE", " ON DELETE SET NULL",
                                         " ON UPDATE CASCADE ON DELETE NO ACTION"]))
            named = draw(st.booleans())
            fk = f"CONSTRAINT {prefix}_FK{ti} " if named else ""
            after.append(f"ALTER TABLE {tname} ADD {fk}FOREIGN KEY (REF_ID) REFERENCES {ref} (ID){rule};")
        pk_named = draw(st.booleans())
        cols.append((f"CONSTRAINT {prefix}_PK{ti} " if pk_named else "") + "PRIMARY KEY (ID)")
        if key_cols and draw(st.booleans()):
            ucol = draw(st.sampled_from(key_cols))[0]
            cols.append(f"CONSTRAINT {prefix}_UQ{ti} UNIQUE ({ucol})")
        stmts.append(f"CREATE TABLE {tname} (\n  " + ",\n  ".join(cols) + "\n);")
        stmts.extend(after)
        for ii, (kc, _t) in enumerate(key_cols[: draw(st.integers(0, 2))]):
            kind = draw(st.sampled_from(["", "UNIQUE ", "DESCENDING ", "ASC "]))
            stmts.append(f"CREATE {kind}INDEX {prefix}_IX{ti}_{ii} ON {tname} ({kc});")
        if draw(st.booleans()):
            stmts.append(f"CREATE INDEX {prefix}_IXE{ti} ON {tname} COMPUTED BY (ID || '');")
        if draw(st.booleans()):
            stmts.append(f"COMMENT ON TABLE {tname} IS {sql_str(draw(comment_text))};")
        if draw(st.booleans()):
            stmts.append(f"COMMENT ON COLUMN {tname}.ID IS {sql_str(draw(comment_text))};")
        objects.append(("tables", tname[1:-1] if tname.startswith('"') else tname))
        if pk_int:
            int_pks.append(tname)
    return "\n".join(stmts), objects


def _base(sid, dbid):
    return f"/api/servers/{sid}/databases/{dbid}"


def _engine(client, sid, dbid) -> int:
    r = client.post(_base(sid, dbid) + "/execute",
                    json={"sql": "SELECT RDB$GET_CONTEXT('SYSTEM', 'ENGINE_VERSION') FROM RDB$DATABASE"}).json()
    return int(str(r["results"][0]["rows"][0][0]).split(".")[0])


def test_generated_schema_round_trip(client, database):
    sid, src = database
    r = client.post(f"/api/servers/{sid}/databases/create",
                    json={"name": "copy", "path": f"{DB_DIR}/fbadmin_prop_{uuid.uuid4().hex[:8]}.fdb"})
    assert r.status_code == 201, r.text
    dst = r.json()["id"]
    engine = _engine(client, sid, src)

    @settings(max_examples=EXAMPLES, deadline=None, database=None,
              suppress_health_check=[HealthCheck.too_slow, HealthCheck.data_too_large])
    @given(st.data())
    def check(data):
        prefix = f"H{next(_seq)}"
        script, objects = data.draw(schema(engine, prefix))
        res = client.post(_base(sid, src) + "/execute", json={"sql": script}).json()
        assert res["error"] is None, f"generated DDL failed: {res['error']}\n{script}"
        extracted = []
        for otype, name in objects:
            r = client.get(_base(sid, src) + f"/objects/{otype}/{name}")
            assert r.status_code == 200, f"{otype} {name}: {r.text}"
            extracted.append(r.json()["ddl"])
        replay = "\n".join(extracted)
        res = client.post(_base(sid, dst) + "/execute", json={"sql": replay}).json()
        assert res["error"] is None, f"extracted DDL failed: {res['error']}\n--- generated:\n{script}\n--- extracted:\n{replay}"
        for (otype, name), text in zip(objects, extracted, strict=True):
            again = client.get(_base(sid, dst) + f"/objects/{otype}/{name}").json()["ddl"]
            assert again == text, f"{otype} {name} differs:\n{text}\n---\n{again}\n--- generated:\n{script}"

    try:
        check()
    finally:
        client.post(f"/api/servers/{sid}/databases/{dst}/drop")
