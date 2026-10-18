"""The optional native driver adapter (FBADMIN_DRIVER=native).

Skipped unless ``firebird-driver`` is installed and a ``libfbclient`` can be loaded
(set FBADMIN_FBCLIENT=/path/to/libfbclient.so). The whole suite can also be run with
FBADMIN_DRIVER=native to exercise every metadata endpoint through the adapter.
"""

from __future__ import annotations

import pytest

from .conftest import requires_server


def _available() -> tuple[bool, str]:
    try:
        from fbadmin.native import native_available
    except Exception as exc:  # noqa: BLE001
        return False, str(exc)
    return native_available()


_ok, _why = _available()
requires_native = pytest.mark.skipif(not _ok, reason=f"native driver unavailable: {_why}")


def test_driver_switch(monkeypatch):
    from fbadmin import native

    monkeypatch.delenv("FBADMIN_DRIVER", raising=False)
    assert not native.native_enabled()
    monkeypatch.setenv("FBADMIN_DRIVER", "native")
    assert native.native_enabled()


def test_error_translation():
    import firebirdsql

    from fbadmin.native import _translate

    class ProgrammingError(Exception):
        sqlcode = -204

    e = _translate(ProgrammingError("Table unknown"))
    assert isinstance(e, firebirdsql.ProgrammingError) and e.sql_code == -204
    assert isinstance(_translate(Exception("Unable to complete network request to host")), OSError)


@requires_native
@requires_server
def test_native_metadata_endpoints(client, database, monkeypatch):
    monkeypatch.setenv("FBADMIN_DRIVER", "native")
    from fbadmin.api.deps import get_state
    from fbadmin.native import NativeConnection

    sid, dbid = database
    base = f"/api/servers/{sid}/databases/{dbid}"
    r = client.post(f"{base}/execute", json={"sql": "CREATE TABLE NT (ID INTEGER NOT NULL PRIMARY KEY, "
                                                   "NAME VARCHAR(20), N NUMERIC(10,2), B BLOB SUB_TYPE TEXT)"})
    assert r.status_code == 200, r.text
    with get_state().manager.meta(sid, dbid) as conn:
        assert isinstance(conn, NativeConnection)
        cur = conn.cursor()
        cur.execute("INSERT INTO NT VALUES (1, 'a', 1.25, 'blob text')")
        assert cur.rowcount == 1
    names = [o["name"] for o in client.get(f"{base}/objects/tables").json()]
    assert "NT" in names
    ddl = client.get(f"{base}/objects/tables/NT").json()["ddl"]
    assert "CREATE TABLE NT" in ddl and "NUMERIC(10, 2)" in ddl.replace("NUMERIC(10,2)", "NUMERIC(10, 2)")
    data = client.get(f"{base}/tables/NT/data").json()
    assert data["rows"][0][1] == "a" and data["rows"][0][3] == "blob text"
    assert data["columns"][2]["type"].startswith(("NUMERIC", "INTEGER"))
    # Firebird errors keep the API's error shape
    r = client.get(f"{base}/tables/NO_SUCH_TABLE/count")
    assert r.status_code == 400 and r.json()["code"] == "firebird_error"
