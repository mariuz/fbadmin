"""Very large schemas (milestone 2.1): object counts without listing, autocompletion that sends table
names only and fetches columns on demand, and a load test with thousands of tables."""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from .conftest import requires_server

HERE = Path(__file__).parent
TABLES = 1500


def _base(sid, dbid):
    return f"/api/servers/{sid}/databases/{dbid}"


def _run(client, sid, dbid, sql):
    r = client.post(_base(sid, dbid) + "/execute", json={"sql": sql})
    assert r.status_code == 200, r.text
    assert r.json()["error"] is None, r.json()["error"]


@requires_server
def test_counts_equal_the_lists(client, database):
    from fbadmin.api.deps import get_state
    from fbadmin.metadata import Catalog

    sid, dbid = database
    _run(client, sid, dbid, (HERE / "complex_schema.sql").read_text())
    with get_state().manager.meta(sid, dbid) as conn:
        cat = Catalog(conn)
        counts = cat.object_counts()
        for otype, n in counts.items():
            assert n == len(cat.list_objects(otype)), otype
    assert counts["tables"] > 0 and counts["procedures"] > 0 and counts["triggers"] > 0
    assert client.get(_base(sid, dbid) + "/info").json()["counts"] == counts


@requires_server
def test_completion_sends_names_only_for_huge_schemas(client, database, monkeypatch):
    from fbadmin.api import databases

    sid, dbid = database
    _run(client, sid, dbid, "CREATE TABLE A1 (ID INTEGER, NAME VARCHAR(10)); CREATE TABLE \"b x\" (\"Q\" INTEGER);")
    full = client.get(_base(sid, dbid) + "/completion").json()
    assert full["lazy_columns"] is False and full["relations"]["A1"] == ["ID", "NAME"]
    monkeypatch.setattr(databases, "COMPLETION_COLUMNS", 3)  # pretend the schema is huge
    lazy = client.get(_base(sid, dbid) + "/completion").json()
    assert lazy["lazy_columns"] is True
    assert lazy["relations"]["A1"] == [] and "RDB$RELATIONS" in lazy["relations"]
    r = client.post(_base(sid, dbid) + "/completion/columns", json={"relations": ["A1", "b x", "NOPE"]})
    assert r.json()["relations"] == {"A1": ["ID", "NAME"], "b x": ["Q"], "NOPE": []}


@pytest.fixture(scope="module")
def big_schema():
    """A database with TABLES tables (created once for this module)."""
    import uuid

    import firebirdsql

    from .conftest import DB_DIR, HOST, PASSWORD, PORT, USER

    path = f"{DB_DIR}/fbadmin_large_{uuid.uuid4().hex[:8]}.fdb"
    conn = firebirdsql.create_database(host=HOST, port=PORT, database=path, user=USER, password=PASSWORD,
                                       charset="UTF8", page_size=16384)
    cur = conn.cursor()
    for i in range(TABLES):
        cur.execute(f"CREATE TABLE T{i:05d} (ID INTEGER NOT NULL PRIMARY KEY, NAME VARCHAR(40), "
                    "AMOUNT NUMERIC(12,2), CREATED TIMESTAMP)")
        if i % 250 == 249:
            conn.commit()
    conn.commit()
    conn.close()
    yield path
    try:
        firebirdsql.connect(host=HOST, port=PORT, database=path, user=USER, password=PASSWORD).drop_database()
    except Exception:  # noqa: BLE001
        pass


@requires_server
def test_load_thousands_of_tables(client, server, big_schema, monkeypatch):
    """Opening the database (counts), a folder, an object, completion and search stay quick."""
    from fbadmin.api import databases

    sid = server["id"]
    dbid = client.post(f"/api/servers/{sid}/databases", json={"name": "large", "path": big_schema}).json()["id"]
    b = _base(sid, dbid)
    client.post(b + "/connect")

    def timed(method, path, **kw):
        t0 = time.time()
        r = client.request(method, b + path, **kw)
        assert r.status_code == 200, r.text
        return r.json(), time.time() - t0

    info, dt = timed("GET", "/info")
    assert info["counts"]["tables"] == TABLES and info["counts"]["indices"] == TABLES
    assert dt < 3, f"counts took {dt:.1f}s"  # listing every type took seconds before 2.1
    tables, dt = timed("GET", "/objects/tables")
    assert len(tables) == TABLES and tables[-1]["name"] == f"T{TABLES - 1:05d}"
    obj, dt = timed("GET", "/objects/tables/T01234")
    assert [c["name"] for c in obj["columns"]] == ["ID", "NAME", "AMOUNT", "CREATED"] and dt < 3
    monkeypatch.setattr(databases, "COMPLETION_COLUMNS", 1000)  # 6000 columns: names only
    comp, dt = timed("GET", "/completion")
    assert comp["lazy_columns"] is True and len([n for n in comp["relations"] if n.startswith("T")]) == TABLES
    cols, dt = timed("POST", "/completion/columns", json={"relations": ["T00007", "T01499"]})
    assert cols["relations"]["T01499"] == ["ID", "NAME", "AMOUNT", "CREATED"] and dt < 3
    found, dt = timed("GET", "/schema/search", params={"q": "T0149"})
    assert {f"T0149{i}" for i in range(10)} <= {x["name"] for x in found["items"]}
