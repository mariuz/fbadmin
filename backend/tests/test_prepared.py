"""Prepared statements in fbadmin's loops (roadmap 2.1): the statement cache, a right ``rowcount`` after a
prepared execute (driver patch 7), and grid edits / data compare scripts that still notice rows changed
by someone else."""

from __future__ import annotations

from fbadmin.prepared import StatementCache

from .conftest import requires_server


class FakeCursor:
    def __init__(self):
        self.prepared, self.calls, self.closed = [], [], []
        self.rowcount = -1

    def prep(self, sql):
        st = type("St", (), {"sql": sql, "close": lambda s: self.closed.append(s.sql)})()
        self.prepared.append(sql)
        return st

    def execute(self, st, params):
        self.calls.append((st.sql, params))
        self.rowcount = 1


def test_cache_prepares_each_text_once():
    cur = FakeCursor()
    with StatementCache(cur, size=2) as c:
        for i in range(5):
            c.execute("UPDATE T SET A = ? WHERE ID = ?", [i, i])
        c.execute("DELETE FROM T WHERE ID = ?", [1])
        c.execute("UPDATE T SET A = ? WHERE ID = ?", [9, 9])  # still cached
        c.execute("INSERT INTO T VALUES (?)", [1])            # evicts DELETE (least recently used)
        assert c.rowcount == 1 and c.executed == 8 and c.prepared == 3
        assert cur.closed == ["DELETE FROM T WHERE ID = ?"]
    assert sorted(cur.closed) == sorted(["DELETE FROM T WHERE ID = ?", "UPDATE T SET A = ? WHERE ID = ?",
                                         "INSERT INTO T VALUES (?)"])
    assert cur.prepared == ["UPDATE T SET A = ? WHERE ID = ?", "DELETE FROM T WHERE ID = ?", "INSERT INTO T VALUES (?)"]
    assert cur.calls[0] == ("UPDATE T SET A = ? WHERE ID = ?", [0, 0])


@requires_server
def test_rowcount_after_prepared_execute(client, database):
    """Both drivers: the count of the statement just executed, prepared or not."""
    sid, dbid = database
    from fbadmin.api.deps import get_state

    with get_state().manager.meta(sid, dbid) as conn:
        cur = conn.cursor()
        cur.execute("CREATE TABLE T (ID INTEGER, V INTEGER)")
        conn.commit()
        for i in range(5):
            cur.execute("INSERT INTO T VALUES (?, 0)", [i % 2])
        cur.execute("UPDATE T SET V = 1 WHERE ID = 0")
        assert cur.rowcount == 3
        with StatementCache(conn.cursor()) as c:
            assert c.execute("UPDATE T SET V = ? WHERE ID = ?", [2, 1]).rowcount == 2
            assert c.execute("UPDATE T SET V = ? WHERE ID = ?", [2, 99]).rowcount == 0
            assert c.execute("DELETE FROM T WHERE ID = ?", [0]).rowcount == 3
            assert c.execute("INSERT INTO T VALUES (?, ?)", [7, 7]).rowcount == 1
            assert c.prepared == 3 and c.executed == 4
        cur.execute("SELECT COUNT(*) FROM T")
        assert cur.fetchone()[0] == 3
        conn.rollback()


@requires_server
def test_grid_save_uses_prepared_statements_and_notices_missing_rows(client, database, monkeypatch):
    sid, dbid = database
    base = f"/api/servers/{sid}/databases/{dbid}"
    r = client.post(base + "/execute", json={"sql": "CREATE TABLE ITEMS (ID INTEGER NOT NULL PRIMARY KEY, NAME VARCHAR(20), QTY INTEGER)"})
    assert r.json()["error"] is None
    from fbadmin import prepared

    seen: list[StatementCache] = []
    orig = prepared.StatementCache.__init__

    def spy(self, *a, **kw):
        orig(self, *a, **kw)
        seen.append(self)

    monkeypatch.setattr(prepared.StatementCache, "__init__", spy)
    inserts = [{"values": {"ID": i, "NAME": f"n{i}", "QTY": i}} for i in range(200)]
    r = client.post(base + "/tables/ITEMS/data", json={"inserts": inserts, "updates": [], "deletes": []})
    assert r.status_code == 200, r.text
    assert r.json() == {"statements": 200, "affected": 200}
    assert seen[-1].prepared == 1 and seen[-1].executed == 200
    updates = [{"key": {"ID": i}, "values": {"QTY": i * 10}} for i in range(100)] + \
              [{"key": {"ID": i}, "values": {"NAME": "x", "QTY": 0}} for i in range(100, 150)]
    deletes = [{"key": {"ID": i}} for i in range(150, 200)]
    r = client.post(base + "/tables/ITEMS/data", json={"inserts": [], "updates": updates, "deletes": deletes})
    assert r.json() == {"statements": 200, "affected": 200}
    assert seen[-1].prepared == 3 and seen[-1].executed == 200

    # a row changed (here: deleted) by someone else: the save fails and nothing is applied
    r = client.post(base + "/tables/ITEMS/data", json={"inserts": [], "deletes": [],
                                                        "updates": [{"key": {"ID": 1}, "values": {"QTY": -1}},
                                                                    {"key": {"ID": 199}, "values": {"QTY": -1}}]})
    assert r.status_code == 400 and "Row not found" in r.json()["detail"]
    s = client.post("/api/query/sessions", json={"server_id": sid, "database_id": dbid}).json()["session_id"]
    res = client.post(f"/api/query/sessions/{s}/execute",
                      json={"sql": "SELECT COUNT(*), SUM(QTY) FROM ITEMS", "autocommit": True}).json()
    assert res["results"][0]["rows"][0] == [150, sum(i * 10 for i in range(100))]  # QTY of ID 1 not -1
    client.delete(f"/api/query/sessions/{s}")
