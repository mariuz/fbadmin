"""Optional native driver adapter (``firebird-driver`` on top of ``libfbclient``).

Selected with the server-wide "Database driver" setting (:mod:`fbadmin.drivers`, stored in the
data directory and edited in the UI) or ``FBADMIN_DRIVER=native``; the default is the
pure-Python ``firebirdsql`` wire-protocol driver. The adapter exposes the DB-API subset fbadmin
uses (``cursor``, ``execute``, ``fetchone/fetchall/fetchmany``, ``description``, ``rowcount``,
``commit``, ``rollback``, ``close``) and translates errors into ``firebirdsql`` exceptions, so the
rest of the app and its error handling are unchanged.

Scope (see docs/DRIVERS.md for the per-feature table):

* With ``native`` selected, every connection to a registered server uses it: metadata
  connections, Query Tool sessions (:class:`NativeConnection` implements the hooks
  :mod:`fbadmin.query` needs: ``set_tpb``, ``end_transaction``, ``plan``), database create/drop
  and the Services API (:class:`NativeServices` maps the three wire primitives fbadmin's service
  code uses to ``IService.start`` / ``IService.query``).
* Wire encryption, wire compression, the authentication plugin and the connect timeout of a
  server registration go into the per-connection configuration (``isc_dpb_config`` /
  ``isc_spb_config``: ``WireCrypt``, ``WireCompression``, ``AuthClient``, ``ConnectionTimeout``).
* Features built on ``firebirdsql``'s wire internals stay on it whatever is selected: the event
  monitor (its own auxiliary-port implementation, :mod:`fbadmin.events.wire`).
* **Local (embedded) databases** (:mod:`fbadmin.embedded`) always use this adapter.
* Needs ``pip install firebird-driver`` (or ``pip install firebird-admin[native]``) and a
  ``libfbclient`` (Firebird 3+; a Firebird 5 client for INT128/DECFLOAT/time zones with every
  server). The library comes from the embedded file set when present, else the configured path
  (``FBADMIN_FBCLIENT`` or the setting), else the system search path.
"""

from __future__ import annotations

import ctypes
import decimal
import os
import threading
import warnings
import weakref
from typing import Any

import firebirdsql
from firebirdsql.services import Services

# A second attachment to a database the embedded engine already has open ignores the DPB's
# "Providers = Engine13" (the provider is already chosen); harmless, so not worth a warning.
warnings.filterwarnings("ignore", message="Secondary attachment - config data from DPB ignored")


def driver_name() -> str:
    """The driver selected for registered servers: ``firebirdsql`` or ``native``."""
    from .drivers import selected

    return selected()[0]


def native_enabled() -> bool:
    return driver_name() == "native"


def _load():
    try:
        from firebird import driver
    except ImportError as exc:  # pragma: no cover - depends on optional package
        raise RuntimeError("The native driver needs the 'firebird-driver' package "
                           "(pip install 'firebird-admin[native]')") from exc
    from .drivers import fbclient_setting
    from .embedded import prepare_client

    # With an embedded engine configured its client library serves remote servers too
    # (one libfbclient per process); otherwise FBADMIN_FBCLIENT / the setting, or the search path.
    lib = prepare_client() or fbclient_setting()[0]
    if lib:
        lib = os.path.expanduser(lib)
    if lib and driver.driver_config.fb_client_library.value != lib:
        driver.driver_config.fb_client_library.value = lib
    return driver


_NETWORK_HINTS = ("unable to complete network request", "failed to establish a connection",
                  "connection refused", "no route to host")


def _translate(exc: Exception) -> Exception:
    """firebird.driver errors -> firebirdsql errors (or OSError for network failures).

    The message is formatted like firebirdsql's: one status vector entry per line, without the
    ``-`` that ``fb_interpret`` puts in front of the continuation lines."""
    lines = str(exc).strip().splitlines()
    msg = "\n".join([lines[0], *(ln[1:] if ln.startswith("-") else ln for ln in lines[1:])]) if lines else ""
    if any(h in msg.lower() for h in _NETWORK_HINTS):
        return ConnectionError(msg)
    sqlcode = getattr(exc, "sqlcode", 0) or 0
    gds = set(getattr(exc, "gds_codes", ()) or ())
    # the same classes firebirdsql raises for a server error (fbcore._parse_op_response)
    if gds & _INTEGRITY_CODES:
        cls = firebirdsql.IntegrityError
    elif 335544321 in gds:  # arith_except
        cls = firebirdsql.DataError
    elif gds or sqlcode:
        cls = firebirdsql.OperationalError
    else:  # raised by firebird-driver itself (no status vector)
        cls = getattr(firebirdsql, type(exc).__name__, None)
        if not (isinstance(cls, type) and issubclass(cls, firebirdsql.Error)) or cls is firebirdsql.NotSupportedError:
            cls = firebirdsql.OperationalError
    return cls(msg, gds, sqlcode)


# unique/primary/foreign key, not null, check constraint violations (as firebirdsql)
_INTEGRITY_CODES = {335544838, 335544879, 335544880, 335544466, 335544665, 335544347, 335544558}


def _value(v: Any) -> Any:
    # Large blobs come back as stream readers; the app expects plain values.
    if hasattr(v, "read") and hasattr(v, "close") and not isinstance(v, (bytes, str)):
        try:
            return v.read()
        finally:
            v.close()
    return v


_SQL_TEXT = 452
_SQL_INT128 = 32752


def _rstrip(v: Any) -> Any:
    return v.rstrip() if isinstance(v, str) else v


def _int(v: Any) -> Any:
    return int(v) if isinstance(v, decimal.Decimal) and v == v.to_integral_value() else v


def _converters(metas) -> list:
    """Per-column fix-ups so values match firebirdsql's: CHAR without the trailing blanks, INT128
    without a scale as ``int`` (firebird-driver returns a ``Decimal``)."""
    out = []
    for m in metas or ():
        dt = int(m.datatype)
        if dt == _SQL_TEXT:
            out.append(_rstrip)
        elif dt == _SQL_INT128 and not m.scale:
            out.append(_int)
        else:
            out.append(None)
    return out


class _XSQLVar:
    """The attributes of firebirdsql's ``XSQLVAR`` fbadmin reads from a prepared statement."""

    def __init__(self, m) -> None:
        self.sqltype = int(m.datatype) | (1 if m.nullable else 0)
        self.sqlscale = m.scale
        self.sqllen = m.length
        # firebirdsql: the character set id for CHAR/VARCHAR, the subtype for BLOBs
        self.sqlsubtype = m.charset if int(m.datatype) in (_SQL_TEXT, 448) else m.subtype
        self.aliasname = m.alias
        self.fieldname = m.field
        self.relname = m.relation


class _Prepared:
    """What ``firebirdsql.Cursor.prep`` returns, as far as fbadmin uses it: pass it to
    :meth:`NativeCursor.execute` to run it again, read ``.stmt.xsqlda``, ``.stmt.drop()``."""

    def __init__(self, stmt) -> None:
        self.native = stmt

    @property
    def stmt(self) -> _Prepared:  # a property, not an attribute: no reference cycle, freed promptly
        return self

    @property
    def xsqlda(self) -> list[_XSQLVar]:
        return [_XSQLVar(m) for m in (self.native._out_desc or [])]

    def drop(self) -> None:
        try:
            self.native.free()
        except Exception:  # noqa: BLE001
            pass

    close = drop

    def __del__(self) -> None:
        # firebird-driver statements sit in a reference cycle, so without an explicit free() they
        # are freed by the cyclic garbage collector, in whatever thread runs it, possibly while
        # another thread uses the attachment (or after it is gone). Free it here instead, in the
        # thread that drops the last reference (fbadmin code keeps one prep() per loop).
        self.drop()


class NativeCursor:
    def __init__(self, conn: NativeConnection) -> None:
        self._owner = conn
        self._cur = conn._con.cursor()
        self._cur.stream_blob_threshold = 64 * 1024 * 1024
        self._has_rows = False
        self._conv: list = []

    def prepare(self, sql: str):
        """A prepared statement (compile errors are raised here) to pass to :meth:`execute`."""
        try:
            stmt = self._cur.prepare(sql)
        except self._owner._errors as exc:
            raise _translate(exc) from exc
        self._owner._prepared.add(stmt)  # freed before the attachment is closed
        return stmt

    def prep(self, sql: str) -> _Prepared:
        """Like ``firebirdsql.Cursor.prep``: prepare once, execute many times."""
        return _Prepared(self.prepare(sql))

    def execute(self, sql, params: list | tuple | None = None):
        if isinstance(sql, _Prepared):
            sql = sql.native
        try:
            self._cur.execute(sql, list(params) if params else None)
        except self._owner._errors as exc:
            raise _translate(exc) from exc
        self._has_rows = self._cur.description is not None
        metas = getattr(getattr(self._cur, "_stmt", None), "_out_desc", None) if self._has_rows else None
        conv = _converters(metas)
        self._conv = conv if any(conv) else []
        return self

    def _row(self, r):
        if r is None:
            return None
        if not self._conv:
            return tuple(_value(v) for v in r)
        return tuple(_value(v) if f is None else f(_value(v)) for v, f in zip(r, self._conv, strict=False))

    @property
    def description(self):
        """DB-API description with Firebird SQL type codes as ``type_code`` (like firebirdsql),
        not Python types (firebird-driver's default)."""
        if not self._has_rows:
            return None
        desc = self._cur.description
        metas = getattr(getattr(self._cur, "_stmt", None), "_out_desc", None)
        if not metas or len(metas) != len(desc):
            return desc
        return tuple((d[0], int(m.datatype) | (1 if m.nullable else 0), *d[2:]) for d, m in zip(desc, metas, strict=True))

    @property
    def rowcount(self) -> int:
        return self._cur.rowcount

    def fetchone(self):
        try:
            return self._row(self._cur.fetchone()) if self._has_rows else None
        except self._owner._errors as exc:
            raise _translate(exc) from exc

    def fetchmany(self, size: int | None = None):
        try:
            rows = self._cur.fetchmany(size) if size else self._cur.fetchmany()
        except self._owner._errors as exc:
            raise _translate(exc) from exc
        return [self._row(r) for r in rows]

    def fetchall(self):
        if not self._has_rows:
            return []
        try:
            return [self._row(r) for r in self._cur.fetchall()]
        except self._owner._errors as exc:
            raise _translate(exc) from exc

    def __iter__(self):
        while (r := self.fetchone()) is not None:
            yield r

    def close(self) -> None:
        try:
            self._cur.close()
        except Exception:  # noqa: BLE001
            pass

    def __del__(self) -> None:
        # release the statement firebird-driver prepared for execute(str) now, in this thread
        # (see _Prepared.__del__); fbadmin code often lets cursors go without close()
        self.close()


class NativeConnection:
    """DB-API-ish wrapper around ``firebird.driver.Connection``."""

    def __init__(self, con, errors: tuple[type[Exception], ...]) -> None:
        self._con = con
        self._errors = errors
        # statements prepared through NativeCursor.prepare/prep: freed on close(), because
        # firebird-driver frees a forgotten one on garbage collection, after the attachment is gone
        self._prepared: weakref.WeakSet = weakref.WeakSet()

    def _free_prepared(self) -> None:
        for stmt in list(self._prepared):
            try:
                stmt.free()
            except Exception:  # noqa: BLE001
                pass
        self._prepared.clear()

    def cursor(self) -> NativeCursor:
        return NativeCursor(self)

    def _active(self) -> bool:
        return self._con.main_transaction.is_active()

    def commit(self) -> None:
        if not self._active():  # firebirdsql allows commit without a transaction; so do we
            return
        try:
            self._con.commit()
        except self._errors as exc:
            raise _translate(exc) from exc

    def rollback(self) -> None:
        if not self._active():
            return
        try:
            self._con.rollback()
        except self._errors as exc:
            raise _translate(exc) from exc

    def close(self) -> None:
        self._free_prepared()
        try:
            self._con.close()
        except self._errors as exc:
            raise _translate(exc) from exc

    def drop_database(self) -> None:
        self._free_prepared()
        try:
            self._con.drop_database()
        except self._errors as exc:
            raise _translate(exc) from exc

    # --- hooks used by fbadmin.query (Query Tool sessions on local databases) ---------
    def set_tpb(self, tpb: bytes) -> None:
        """Use the raw transaction parameter block ``tpb`` for transactions started from now on."""
        self._con.main_transaction.default_tpb = bytes(tpb)

    def end_transaction(self, commit: bool) -> None:
        if commit:
            self.commit()
        else:
            self.rollback()

    def plan(self, sql: str, detailed: bool = False) -> str | None:
        """Execution plan of ``sql`` (prepared, not executed)."""
        cur = self._con.cursor()
        try:
            stmt = cur.prepare(sql)
            try:
                text = stmt.detailed_plan if detailed else stmt.plan
            finally:
                stmt.free()
        except self._errors as exc:
            raise _translate(exc) from exc
        finally:
            cur.close()
        return (text or "").strip() or None


_db_configs: dict[tuple, str] = {}
_srv_configs: dict[tuple, str] = {}
_cfg_lock = threading.Lock()


def _embedded_config(driver, database: str, page_size: int | None = None, db_charset: str | None = None) -> str:
    """Name of a firebird-driver database configuration that opens ``database`` with the
    embedded engine only (``Providers = Engine13``): a local registration must never fall
    back to the Loopback provider and silently reach a server on localhost."""
    key = ("embedded", database, page_size, db_charset)
    with _cfg_lock:
        name = _db_configs.get(key)
        if name is None:
            name = f"fbadmin-embedded-{len(_db_configs) + 1}"
            cfg = driver.driver_config.register_database(name)
            cfg.database.value = database
            cfg.config.value = "Providers = Engine13"
            if page_size:
                cfg.page_size.value = page_size
            if db_charset:
                cfg.db_charset.value = db_charset
            _db_configs[key] = name
    return name


def auth_client_list(plugin: str) -> str:
    """``AuthClient`` for a registration's authentication plugin. firebirdsql treats the plugin as
    the one to try first and still accepts Srp when the server only offers that (a Firebird 3
    server without Srp256); libfbclient tries exactly the listed ones, so Srp follows Srp256."""
    return "Srp256, Srp" if plugin == "Srp256" else plugin


def client_config(*, wire_crypt: bool | None = None, wire_compress: bool | None = None,
                  auth_plugin_name: str | None = None, timeout: int | None = None) -> str:
    """firebird.conf-style per-connection settings (``isc_dpb_config`` / ``isc_spb_config``)
    that mirror firebirdsql's connection keywords."""
    lines = []
    if wire_crypt is not None:  # firebirdsql: True = encrypt when the server can, False = never
        lines.append(f"WireCrypt = {'Enabled' if wire_crypt else 'Disabled'}")
    if wire_compress is not None:
        lines.append(f"WireCompression = {'true' if wire_compress else 'false'}")
    if auth_plugin_name:
        lines.append(f"AuthClient = {auth_client_list(auth_plugin_name)}")
    if timeout:
        lines.append(f"ConnectionTimeout = {int(timeout)}")
    return "\n".join(lines)


def _host_port(host: str, port: int | None) -> str:
    h = host.strip()
    if ":" in h and not h.startswith("["):  # IPv6 literal
        h = f"[{h}]"
    return f"{h}/{port}" if port else h


def _remote_config(driver, host: str, port: int | None, database: str, config: str,
                   page_size: int | None = None, db_charset: str | None = None) -> str:
    """A registered database configuration for a remote database: firebird-driver takes the
    per-connection ``config`` string only from a configuration."""
    key = ("remote", host, port, database, config, page_size, db_charset)
    with _cfg_lock:
        name = _db_configs.get(key)
        if name is None:
            name = f"fbadmin-remote-{len(_db_configs) + 1}"
            cfg = driver.driver_config.register_database(name)
            cfg.dsn.value = f"{_host_port(host, port)}:{database}"
            if config:
                cfg.config.value = config
            if page_size:
                cfg.page_size.value = page_size
            if db_charset:
                cfg.db_charset.value = db_charset
            _db_configs[key] = name
    return name


def connect_native(*, host: str, port: int, database: str, user: str, password: str,
                   role: str | None = None, charset: str = "UTF8", embedded: bool = False,
                   create: bool = False, page_size: int | None = None, wire_crypt: bool | None = None,
                   wire_compress: bool | None = None, auth_plugin_name: str | None = None,
                   timeout: int | None = None, **_ignored) -> NativeConnection:
    """Attach (or with ``create`` create) a database. ``embedded``: a local file opened in-process.

    The keyword arguments match :func:`firebirdsql.connect` / ``create_database``; the wire
    options become the connection's client configuration (see :func:`client_config`)."""
    driver = _load()
    errors = (driver.Error,)
    if embedded:
        dsn = _embedded_config(driver, database, page_size, charset) if create else _embedded_config(driver, database)
    elif host:
        conf = client_config(wire_crypt=wire_crypt, wire_compress=wire_compress,
                             auth_plugin_name=auth_plugin_name, timeout=timeout)
        dsn = _remote_config(driver, host, port, database, conf, page_size if create else None,
                             charset if create else None)
    else:
        dsn = database
    try:
        if create:
            con = driver.create_database(dsn, user=user, password=password or None, role=role or None,
                                         charset=charset)
        else:
            con = driver.connect(dsn, user=user, password=password or None, role=role or None, charset=charset)
        # Match firebirdsql's default (read committed) so new objects and fresh MON$ data show up.
        con.main_transaction.default_tpb = driver.tpb(driver.Isolation.READ_COMMITTED_RECORD_VERSION)
        if not embedded and client_major() < 4:
            _legacy_bind(con)
    except errors as exc:
        raise _translate(exc) from exc
    return NativeConnection(con, errors)


def client_version(util=None) -> tuple[int, int]:
    """(major, minor) of the loaded libfbclient. firebird-driver 2.0 returns the packed integer
    ``major << 8 | minor`` from ``get_client_version()``, 2.1 returns the tuple."""
    if util is None:
        from firebird.driver import get_api

        util = get_api().util
    v = util.get_client_version()
    if isinstance(v, tuple):
        return int(v[0]), int(v[1]) if len(v) > 1 else 0
    v = int(v)
    return v >> 8, v & 0xFF


def client_major() -> int:
    """Major version of the loaded libfbclient (3 for a Firebird 3 client)."""
    return client_version()[0]


def _legacy_bind(con) -> None:
    """A Firebird 3 client cannot decode INT128, DECFLOAT and the time zone types ("Data type
    unknown"): on a Firebird 4+ server, ask for them as BIGINT / DOUBLE PRECISION / without time
    zone (``SET BIND ... TO LEGACY``), like isql of Firebird 3 would need. A Firebird 4+ client
    library gets the real types."""
    if con.info.engine_version < 4:
        return
    cur = con.cursor()
    try:
        for t in ("INT128", "DECFLOAT", "TIME ZONE"):
            cur.execute(f"SET BIND OF {t} TO LEGACY")
    finally:
        cur.close()


def native_available() -> tuple[bool, str]:
    """(True, "") when the package and client library load, else (False, reason)."""
    try:
        driver = _load()
        driver.load_api()
    except Exception as exc:  # noqa: BLE001
        return False, str(exc)
    return True, ""


# ----------------------------------------------------------------------------- services
class NativeServices(Services):
    """``firebirdsql.services.Services`` running on firebird-driver's service manager.

    fbadmin's maintenance, security and trace code (and firebirdsql's own service helpers)
    talk to a service through three wire operations: ``_op_service_start``,
    ``_op_service_info`` and ``_op_response``. Here they map to ``IService.start`` /
    ``IService.query`` of the native API, which take exactly the same parameter and info
    buffers, so backup/restore (also streamed), gstat, validation, sweep, properties, nbackup,
    users, trace and the server log run unchanged.
    """

    def __init__(self, server, errors: tuple[type[Exception], ...]):  # noqa: D107 - no socket
        self._server = server
        self._svc = server._svc
        self._errors = errors
        self._pending: tuple | None = None
        self.charset = "UTF8"
        self.sock = None
        self.db_handle = 0
        self.svc_handle = 0
        self.timeout = None

    def _call(self, fn, *args) -> None:
        try:
            fn(*args)
        except self._errors as exc:
            raise _translate(exc) from exc

    def _check_action(self, param: bytes) -> None:
        """Hook for subclasses that refuse some service actions (local databases)."""

    def _op_service_start(self, param) -> None:
        param = bytes(param)
        self._check_action(param)
        self._call(self._svc.start, param)
        self._pending = (0, 0, b"")

    def _op_service_info(self, param, item, buffer_length: int = 512) -> None:
        buf = ctypes.create_string_buffer(max(16, min(int(buffer_length), 65535)))
        self._call(self._svc.query, bytes(param) or None, bytes(item), buf)
        self._pending = (0, 0, buf.raw)

    def _op_response(self):
        if self._pending is None:
            raise firebirdsql.OperationalError("No pending service response")
        r, self._pending = self._pending, None
        return r

    def close(self) -> None:
        try:
            self._server.close()
        except Exception:  # noqa: BLE001
            pass


def _server_config(driver, host: str, port: int | None, config: str, auth_plugin_name: str | None) -> str:
    key = (host, port, config, auth_plugin_name)
    with _cfg_lock:
        name = _srv_configs.get(key)
        if name is None:
            name = f"fbadmin-server-{len(_srv_configs) + 1}"
            cfg = driver.driver_config.register_server(name)
            cfg.host.value = _host_port(host, None)
            if port:
                cfg.port.value = str(port)
            if config:
                cfg.config.value = config
            if auth_plugin_name:
                cfg.auth_plugin_list.value = auth_client_list(auth_plugin_name)
            _srv_configs[key] = name
    return name


def connect_services(*, host: str, port: int | None, user: str, password: str,
                     wire_crypt: bool | None = None, wire_compress: bool | None = None,
                     auth_plugin_name: str | None = None, timeout: int | None = None,
                     **_ignored) -> NativeServices:
    """Attach to a remote server's service manager (same keywords as ``firebirdsql.services.connect``)."""
    driver = _load()
    conf = client_config(wire_crypt=wire_crypt, wire_compress=wire_compress,
                         auth_plugin_name=auth_plugin_name, timeout=timeout)
    name = _server_config(driver, host, port, conf, auth_plugin_name)
    try:
        srv = driver.connect_server(name, user=user, password=password or None)
    except driver.Error as exc:
        raise _translate(exc) from exc
    return NativeServices(srv, (driver.Error,))
