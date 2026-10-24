"""The ``auto`` driver setting (the default): native with a Firebird 4+ client library, else
firebirdsql; the runtime fallback when the client library fails (but never on Firebird errors).

``FBADMIN_TEST_EXPECT_DRIVER=native|firebirdsql`` makes :func:`test_expected_effective_driver`
assert what ``auto`` resolves to on this machine (CI: firebirdsql on the plain runners, which have
no client library; native in the job with the Firebird 5 client).
"""

from __future__ import annotations

import logging
import os

import firebirdsql
import pytest

from .conftest import DB_DIR, HOST, PASSWORD, PORT, USER, requires_server
from .drivers_util import client_major, requires_native

FB5 = {"package": "2.0.3", "available": True, "reason": "", "library": "/opt/fb/lib/libfbclient.so.2",
       "client_version": "Firebird 5.0 (LI-V6.3.4.1812)", "client_major": 5, "restart_required": False,
       "embedded_library": None}


@pytest.fixture(autouse=True)
def _fresh(monkeypatch, tmp_path):
    from fbadmin import config, drivers

    monkeypatch.setattr(config.settings, "data_dir", tmp_path)
    monkeypatch.delenv("FBADMIN_DRIVER", raising=False)
    drivers.reset()
    yield
    drivers.reset()


def fake_native(monkeypatch, **changes) -> list:
    """Make native_status() report FB5 with ``changes``; returns a list counting the probes."""
    from fbadmin import drivers

    calls: list = []

    def status():
        calls.append(1)
        return {**FB5, **changes}

    monkeypatch.setattr(drivers, "native_status", status)
    return calls


# ------------------------------------------------------------------ resolution matrix
def test_resolve_matrix():
    from fbadmin.drivers import resolve

    assert resolve("firebirdsql")[0] == "firebirdsql"
    assert resolve("native")[0] == "native"
    drv, why = resolve("auto", {**FB5, "package": None, "available": False, "library": None})
    assert drv == "firebirdsql" and "firebird-driver is not installed" in why
    drv, why = resolve("auto", {**FB5, "available": False, "library": None, "client_major": None,
                                "reason": "Cannot load the Firebird client library (libfbclient): The location "
                                          "of Firebird Client Library could not be determined."})
    assert drv == "firebirdsql" and why.startswith("No Firebird client library found")
    drv, why = resolve("auto", {**FB5, "available": False, "library": None, "reason": "Client library not found: /x"})
    assert drv == "firebirdsql" and "No Firebird client library found" in why and "/x" in why
    drv, why = resolve("auto", {**FB5, "available": False, "library": None,
                                "reason": "Cannot load the Firebird client library (libfbclient): undefined symbol"})
    assert drv == "firebirdsql" and "undefined symbol" in why
    drv, why = resolve("auto", {**FB5, "client_major": 3, "client_version": "Firebird 3.0"})
    assert drv == "firebirdsql" and why.startswith("The client library is Firebird 3") and "INT128" in why
    drv, why = resolve("auto", {**FB5, "client_major": 4})
    assert drv == "native"
    drv, why = resolve("auto", {**FB5, "icu": False, "icu_error": "Could not find acceptable ICU library"})
    assert drv == "firebirdsql" and "ICU" in why  # time zone names would be read wrongly
    assert resolve("auto", {**FB5, "icu": True})[0] == "native"
    drv, why = resolve("auto", FB5)
    assert drv == "native" and "Firebird 5.0" in why and FB5["library"] in why


def test_default_is_auto_and_cached(monkeypatch):
    from fbadmin import drivers, native

    calls = fake_native(monkeypatch)
    assert drivers.selected() == ("auto", "default")
    assert drivers.effective()[0] == "native" and native.native_enabled()
    for _ in range(5):
        drivers.effective()
    assert len(calls) == 1  # resolved once
    drivers.save(driver="auto")  # a changed setting resolves again
    assert drivers.selected() == ("auto", "settings")
    drivers.effective()
    assert len(calls) == 2


def test_auto_without_package_library_or_with_fb3_client(monkeypatch):
    from fbadmin import drivers

    fake_native(monkeypatch, package=None, available=False, library=None, client_major=None)
    assert drivers.effective() == ("firebirdsql", "firebird-driver is not installed.")
    drivers.reset()
    fake_native(monkeypatch, available=False, library=None, client_major=None,
                reason="Cannot load the Firebird client library (libfbclient): The location of Firebird "
                       "Client Library could not be determined.")
    assert drivers.effective()[0] == "firebirdsql"
    drivers.reset()
    fake_native(monkeypatch, client_major=3, client_version="Firebird 3.0")
    drv, why = drivers.effective()
    assert drv == "firebirdsql" and "Firebird 3" in why


def test_environment_and_stored_setting(monkeypatch):
    from fbadmin import drivers

    fake_native(monkeypatch, client_major=3)  # auto would pick firebirdsql
    drivers.save(driver="native")
    assert drivers.selected() == ("native", "settings") and drivers.effective()[0] == "native"
    monkeypatch.setenv("FBADMIN_DRIVER", "auto")
    assert drivers.selected() == ("auto", "environment") and drivers.effective()[0] == "firebirdsql"
    monkeypatch.setenv("FBADMIN_DRIVER", "firebirdsql")
    assert drivers.effective()[0] == "firebirdsql"
    monkeypatch.setenv("FBADMIN_DRIVER", "native")
    assert drivers.effective()[0] == "native"
    monkeypatch.setenv("FBADMIN_DRIVER", "Automatic")
    assert drivers.selected() == ("auto", "environment")
    assert drivers.normalize("") == "auto" and drivers.normalize("pure") == "firebirdsql"
    with pytest.raises(ValueError):
        drivers.normalize("odbc")


def test_api_auto_and_explicit_native_unavailable(client, monkeypatch):
    from fbadmin import drivers

    monkeypatch.delenv("FBADMIN_FBCLIENT", raising=False)
    fake_native(monkeypatch, available=False, library=None, client_major=None, reason="Client library not found: /x")
    drivers.reset()
    st = client.get("/api/driver").json()
    assert st["driver"] == "auto" and st["source"] == "default" and st["effective"] == "firebirdsql"
    assert "No Firebird client library found" in st["reason"] and st["fallback"] is None
    assert st["drivers"] == ["auto", "native", "firebirdsql"]
    r = client.put("/api/driver", json={"driver": "native"})  # explicit native is refused when unavailable
    assert r.status_code == 400 and "cannot be used" in r.json()["detail"]
    assert client.get("/api/driver").json()["stored"] == {}
    r = client.put("/api/driver", json={"driver": "firebirdsql"})
    assert r.status_code == 200 and r.json()["effective"] == "firebirdsql"
    r = client.put("/api/driver", json={"driver": "auto"})  # always allowed
    assert r.status_code == 200 and r.json()["driver"] == "auto" and r.json()["source"] == "settings"


# ------------------------------------------------------------------ runtime fallback
def _srv():
    from fbadmin.store import ServerEntry

    return ServerEntry(id="s", name="s", host=HOST, port=PORT, user=USER)


def test_client_failure_classification():
    from fbadmin.native import _client_failure

    class FbError(Exception):
        def __init__(self, msg, gds):
            super().__init__(msg)
            self.gds_codes = gds

    assert _client_failure(OSError("libfbclient.so.2: cannot open shared object file"))
    assert _client_failure(FbError("Error loading plugin ChaCha", (335545004, 335545005)))
    assert _client_failure(FbError("No providers loaded", (335545044,)))
    assert not _client_failure(FbError("Your user name and password are not defined", (335544472,)))
    assert not _client_failure(FbError("I/O error during open", (335544344, 335544734)))
    assert not _client_failure(ValueError("a bug"))


def test_fallback_on_client_library_failure(monkeypatch, caplog):
    """auto + a client-library failure -> firebirdsql for that connection, a warning logged once,
    the reason in the driver status. Firebird errors and explicit native: no fallback."""
    from fbadmin import connections, drivers, native

    fake_native(monkeypatch)
    pure = object()
    attempts: list = []

    def failing_native(**kw):
        attempts.append(kw)
        raise native.NativeClientError("Firebird client library error (connect): libfbclient: boom")

    monkeypatch.setattr(connections, "connect_native", failing_native)
    monkeypatch.setattr(connections.firebirdsql, "connect", lambda **kw: pure)
    with caplog.at_level(logging.WARNING, logger="fbadmin.drivers"):
        assert connections.connect(_srv(), "/db.fdb", "pw") is pure
        assert connections.connect(_srv(), "/db.fdb", "pw") is pure
    assert len(attempts) == 2  # per connection: not a load failure, so native is tried again
    warnings = [r for r in caplog.records if "using firebirdsql" in r.getMessage()]
    assert len(warnings) == 1 and "boom" in warnings[0].getMessage()
    assert "Native driver failed, using firebirdsql" in drivers.fallback_reason()
    assert drivers.status()["fallback"] == drivers.fallback_reason()

    # a library that does not load at all: auto stops trying it
    def unloadable(**kw):
        attempts.append(kw)
        raise native.NativeClientError("Cannot load the Firebird client library", load_failure=True)

    monkeypatch.setattr(connections, "connect_native", unloadable)
    assert connections.connect(_srv(), "/db.fdb", "pw") is pure
    n = len(attempts)
    assert drivers.effective()[0] == "firebirdsql"
    assert connections.connect(_srv(), "/db.fdb", "pw") is pure and len(attempts) == n

    # explicit native: reported, no fallback
    drivers.save(driver="native")
    with pytest.raises(native.NativeClientError):
        connections.connect(_srv(), "/db.fdb", "pw")
    # driver= forced by the caller: no fallback either
    drivers.save(driver="auto")
    with pytest.raises(native.NativeClientError):
        connections.connect(_srv(), "/db.fdb", "pw", driver="native")


def test_no_fallback_on_firebird_error(monkeypatch):
    from fbadmin import connections, drivers

    fake_native(monkeypatch)

    def wrong_password(**kw):
        raise firebirdsql.OperationalError("Your user name and password are not defined", {335544472}, -902)

    def must_not_run(**kw):
        raise AssertionError("fell back to firebirdsql on a Firebird error")

    monkeypatch.setattr(connections, "connect_native", wrong_password)
    monkeypatch.setattr(connections.firebirdsql, "connect", must_not_run)
    with pytest.raises(firebirdsql.OperationalError, match="password"):
        connections.connect(_srv(), "/db.fdb", "pw")
    assert drivers.fallback_reason() is None

    # an ordinary bug in our code is not a client-library failure either
    monkeypatch.setattr(connections, "connect_native", lambda **kw: (_ for _ in ()).throw(TypeError("bug")))
    with pytest.raises(TypeError):
        connections.connect(_srv(), "/db.fdb", "pw")
    assert drivers.fallback_reason() is None


def test_services_fallback(monkeypatch):
    from fbadmin import connections, drivers, native
    from fbadmin.store import ServerStore

    fake_native(monkeypatch)
    pure = type("Svc", (), {"close": lambda self: None})()

    def failing(**kw):
        raise native.NativeClientError("Firebird client library error (service manager): boom")

    monkeypatch.setattr(connections, "connect_services", failing)
    monkeypatch.setattr(connections.services, "connect", lambda **kw: pure)
    store = ServerStore.__new__(ServerStore)
    mgr = connections.ConnectionManager.__new__(connections.ConnectionManager)
    mgr.store = store
    monkeypatch.setattr(store, "get", lambda sid: _srv(), raising=False)
    monkeypatch.setattr(mgr, "_password", lambda sid: "pw")
    with mgr.service("s") as svc:
        assert svc is pure
    assert drivers.fallback_reason()


# ------------------------------------------------------------------ against a server
@requires_native
@requires_server
def test_real_fallback_and_firebird_errors(client, server, monkeypatch):
    """With a Firebird 4+ client library auto picks native; a simulated OSError from the client
    library falls back to firebirdsql, a wrong password is reported and does not."""
    if (client_major() or 0) < 4:
        pytest.skip("needs a Firebird 4+ client library (auto picks firebirdsql with an older one)")
    import firebird.driver as fd

    from fbadmin.api.deps import get_state
    from fbadmin.native import NativeConnection

    st = client.get("/api/driver").json()
    assert st["driver"] == "auto" and st["effective"] == "native", st
    path = f"{DB_DIR}/fbadmin_auto_{os.getpid()}.fdb"
    r = client.post(f"/api/servers/{server['id']}/databases/create", json={"name": "a", "path": path})
    assert r.status_code == 201, r.text
    dbid = r.json()["id"]
    try:
        mgr = get_state().manager
        with mgr.meta(server["id"], dbid) as conn:
            assert isinstance(conn, NativeConnection)
        mgr.close_pools()

        def broken(*a, **kw):
            raise OSError("libfbclient.so.2: simulated client library failure")

        monkeypatch.setattr(fd, "connect", broken)
        with mgr.meta(server["id"], dbid) as conn:
            assert isinstance(conn, firebirdsql.Connection) and not isinstance(conn, NativeConnection)
            cur = conn.cursor()
            cur.execute("SELECT 1 FROM RDB$DATABASE")
            assert cur.fetchone()[0] == 1
        st = client.get("/api/driver").json()
        assert "simulated client library failure" in (st["fallback"] or "")
        monkeypatch.undo()
        mgr.close_pools()
        from fbadmin import drivers

        drivers.reset()

        # a Firebird error (wrong password) through the native driver: reported, no fallback
        from fbadmin.connections import connect
        from fbadmin.store import ServerEntry

        bad = ServerEntry(id="x", name="x", host=HOST, port=PORT, user=USER)
        with pytest.raises(firebirdsql.OperationalError) as ei:
            connect(bad, path, PASSWORD + "-wrong")
        assert not isinstance(ei.value, firebirdsql.InterfaceError)
        assert drivers.fallback_reason() is None
    finally:
        client.post(f"/api/servers/{server['id']}/databases/{dbid}/drop")


@requires_native
@requires_server
def test_native_negotiates_chacha(client, database, monkeypatch):
    """With the ChaCha plugin next to the client library (the file sets fbadmin ships), the native
    driver negotiates ChaCha64 with Firebird 4+ servers, like a normal Firebird client."""
    from pathlib import Path

    from fbadmin import drivers
    from fbadmin.api.deps import get_state

    from .drivers_util import use_driver

    lib = drivers.native_status()["library"] or ""
    plugins = Path(lib).resolve().parent.parent / "plugins"
    if not any((plugins / n).is_file() for n in ("libChaCha.so", "chacha.dll", "libChaCha.dylib")):
        pytest.skip(f"no ChaCha plugin in {plugins}")
    use_driver(monkeypatch, "native")
    sid, dbid = database
    with get_state().manager.meta(sid, dbid) as conn:
        cur = conn.cursor()
        cur.execute("SELECT RDB$GET_CONTEXT('SYSTEM', 'ENGINE_VERSION') FROM RDB$DATABASE")
        if int(str(cur.fetchone()[0]).split(".")[0]) < 4:
            pytest.skip("Firebird 3 offers only Arc4 (and has no WIRE_CRYPT_PLUGIN context variable)")
        cur.execute("SELECT RDB$GET_CONTEXT('SYSTEM', 'WIRE_CRYPT_PLUGIN') FROM RDB$DATABASE")
        assert cur.fetchone()[0] == "ChaCha64"


def test_expected_effective_driver():
    """CI: what auto resolves to on this runner is intentional (FBADMIN_TEST_EXPECT_DRIVER)."""
    want = os.environ.get("FBADMIN_TEST_EXPECT_DRIVER", "").strip()
    if not want:
        pytest.skip("FBADMIN_TEST_EXPECT_DRIVER not set")
    from fbadmin import drivers

    drv, why = drivers.effective()  # FBADMIN_DRIVER is unset here (the _fresh fixture)
    assert drivers.selected() == ("auto", "default")
    assert drv == want, why
