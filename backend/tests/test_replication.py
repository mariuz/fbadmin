"""Milestone 0.8 - replication (Firebird 4+), plus the replication.conf generator."""

from __future__ import annotations

import uuid

import firebirdsql
import pytest

from .conftest import DB_DIR, HOST, PASSWORD, PORT, USER, requires_server


def _base(database):
    sid, dbid = database
    return f"/api/servers/{sid}/databases/{dbid}/replication"


def _exec(client, database, sql):
    sid, dbid = database
    r = client.post(f"/api/servers/{sid}/databases/{dbid}/execute", json={"sql": sql})
    assert r.json()["error"] is None, r.json()["error"]


@requires_server
def test_publication_and_tables(client, database):
    st = client.get(_base(database)).json()
    if not st["supported"]:
        assert st["engine_major"] < 4
        r = client.post(_base(database) + "/publication", json={"enabled": True})
        assert r.status_code == 400 and "4.0" in r.json()["detail"]
        return
    assert st["replica_mode"] == "none"
    assert st["publication_enabled"] is False
    assert isinstance(st["replication_sequence"], int)
    _exec(client, database, "CREATE TABLE RA (ID INTEGER NOT NULL PRIMARY KEY); CREATE TABLE RB (ID INTEGER);"
                            "CREATE VIEW RV AS SELECT ID FROM RA;")
    names = [t["name"] for t in client.get(_base(database)).json()["tables"]]
    assert names == ["RA", "RB"]  # views are not publishable

    r = client.post(_base(database) + "/publication", json={"enabled": True}).json()
    assert r["sql"] == "ALTER DATABASE ENABLE PUBLICATION"
    assert r["status"]["publication_enabled"] is True
    assert any("no table" in w for w in r["status"]["warnings"])

    r = client.post(_base(database) + "/tables", json={"action": "include", "tables": ["RA", "RB"]}).json()
    assert r["sql"] == "ALTER DATABASE INCLUDE TABLE RA, RB TO PUBLICATION"
    tables = {t["name"]: t for t in r["status"]["tables"]}
    assert tables["RA"]["published"] and tables["RB"]["published"]
    assert tables["RA"]["has_key"] and not tables["RB"]["has_key"]
    assert any("RB" in w and "key" in w for w in r["status"]["warnings"])

    r = client.post(_base(database) + "/tables", json={"action": "exclude", "tables": ["RB"]}).json()
    assert r["sql"] == "ALTER DATABASE EXCLUDE TABLE RB FROM PUBLICATION"
    assert [t["name"] for t in r["status"]["tables"] if t["published"]] == ["RA"]

    r = client.post(_base(database) + "/tables", json={"action": "include", "all": True}).json()
    assert r["sql"] == "ALTER DATABASE INCLUDE ALL TO PUBLICATION"
    assert r["status"]["auto_enable"] is True

    r = client.post(_base(database) + "/tables", json={"action": "exclude", "all": True}).json()
    assert r["sql"] == "ALTER DATABASE EXCLUDE ALL FROM PUBLICATION"
    assert not any(t["published"] for t in r["status"]["tables"])

    assert client.post(_base(database) + "/tables", json={"action": "drop", "tables": ["RA"]}).status_code == 400
    r = client.post(_base(database) + "/publication", json={"enabled": False}).json()
    assert r["status"]["publication_enabled"] is False
    pubs = r["status"]["publications"]
    assert pubs and pubs[0]["name"] == "RDB$DEFAULT"


@requires_server
def test_replica_mode_and_peer(client, server, database):
    st = client.get(_base(database)).json()
    if not st["supported"]:
        pytest.skip("replication needs Firebird 4+")
    for mode in ("read_only", "read_write", "none"):
        r = client.post(_base(database) + "/replica-mode", json={"mode": mode})
        assert r.status_code == 200, r.text
        assert r.json()["status"]["replica_mode"] == mode
    assert client.post(_base(database) + "/replica-mode", json={"mode": "bogus"}).status_code == 400
    # another attachment blocks the switch (it needs exclusive access)
    srv = next(s for s in client.get("/api/servers").json() if s["id"] == server["id"])
    path = next(d for d in srv["databases"] if d["id"] == database[1])["path"]
    other = firebirdsql.connect(host=HOST, port=PORT, database=path, user=USER, password=PASSWORD)
    try:
        r = client.post(_base(database) + "/replica-mode", json={"mode": "read_only"})
        assert r.status_code == 400 and "exclusive" in r.json()["detail"]
    finally:
        other.close()

    # a second database registered as the "replica" peer
    path = f"{DB_DIR}/fbadmin_test_rep_{uuid.uuid4().hex[:8]}.fdb"
    peer = client.post(f"/api/servers/{server['id']}/databases/create", json={"name": "replica", "path": path}).json()
    try:
        client.post(f"/api/servers/{server['id']}/databases/{peer['id']}/replication/replica-mode",
                    json={"mode": "read_only"}).raise_for_status()
        r = client.get(_base(database), params={"peer_server": server["id"], "peer_db": peer["id"]}).json()
        assert r["peer"]["replica_mode"] == "read_only"
        assert r["peer"]["segments_behind"] >= 0
        client.post(f"/api/servers/{server['id']}/databases/{peer['id']}/replication/replica-mode",
                    json={"mode": "none"}).raise_for_status()
    finally:
        client.post(f"/api/servers/{server['id']}/databases/{peer['id']}/drop")

    r = client.get(_base(database) + "/log")
    assert r.status_code == 200 and isinstance(r.json()["entries"], list)


def test_conf_generator(client):
    r = client.post("/api/replication/conf", json={
        "role": "primary", "database": "/data/main.fdb", "journal_directory": "/data/journal",
        "journal_archive_directory": "/data/archive", "journal_archive_timeout": 60,
        "journal_segment_size": "16M"}).json()
    assert r["errors"] == []
    t = r["text"]
    assert "database = /data/main.fdb\n{" in t
    assert "    journal_directory = /data/journal" in t
    assert "    journal_archive_directory = /data/archive" in t
    assert "    journal_archive_timeout = 60" in t
    assert t.rstrip().endswith("}")
    assert any("ENABLE PUBLICATION" in n for n in r["notes"])

    r = client.post("/api/replication/conf", json={"role": "primary", "database": "main"}).json()
    assert len(r["errors"]) == 2

    r = client.post("/api/replication/conf", json={
        "role": "replica", "database": "C:\\Data\\My Replica.fdb", "journal_source_directory": "/data/archive",
        "verbose_logging": True}).json()
    assert r["errors"] == []
    assert 'database = "C:\\Data\\My Replica.fdb"' in r["text"]
    assert "journal_source_directory = /data/archive" in r["text"]
    assert "verbose_logging = true" in r["text"]

    r = client.post("/api/replication/conf", json={
        "role": "primary_sync", "database": "main", "sync_replica": "SYSDBA:pw@replica:/data/r.fdb"}).json()
    assert "sync_replica = SYSDBA:pw@replica:/data/r.fdb" in r["text"]
    assert client.post("/api/replication/conf", json={"role": "x", "database": "main"}).status_code == 400
    assert client.post("/api/replication/conf", json={"role": "replica", "database": " "}).status_code == 400
