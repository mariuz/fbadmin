"""Optional native driver adapter (``firebird-driver`` on top of ``libfbclient``).

Enabled with ``FBADMIN_DRIVER=native``; the default is the pure-Python ``firebirdsql``
wire-protocol driver. The adapter exposes the small DB-API subset fbadmin's metadata code
uses (``cursor``, ``execute``, ``fetchone/fetchall/fetchmany``, ``description``,
``rowcount``, ``commit``, ``rollback``, ``close``) and translates errors into
``firebirdsql`` exceptions, so the rest of the app and its error handling are unchanged.

Scope and limitations (see docs/DEVELOPMENT.md):

* For registered servers only **metadata connections** (object explorer, DDL, data grid,
  dashboards, monitor) use it. Query Tool sessions, database create/drop and the Services
  API (backup, restore, gstat, log) keep using ``firebirdsql``, because they depend on its
  transaction parameter blocks and wire-level statement info.
* **Local (embedded) databases** (see :mod:`fbadmin.embedded`) use it for *every*
  connection, including Query Tool sessions: :class:`NativeConnection` implements the few
  hooks :mod:`fbadmin.query` needs (``set_tpb``, ``end_transaction``, ``plan``), and the
  Services API goes through :class:`fbadmin.embedded.EmbeddedServices`.
* ``wire_crypt`` follows the client library's ``firebird.conf`` (``WireCrypt``), not the
  per-server setting.
* Needs ``pip install firebird-driver`` (or ``pip install firebird-admin[native]``) and a
  ``libfbclient`` (Firebird 3+). Point ``FBADMIN_FBCLIENT`` at the library file if it is
  not on the default search path.
"""

from __future__ import annotations

import os
import warnings
from typing import Any

import firebirdsql

# A second attachment to a database the embedded engine already has open ignores the DPB's
# "Providers = Engine13" (the provider is already chosen); harmless, so not worth a warning.
warnings.filterwarnings("ignore", message="Secondary attachment - config data from DPB ignored")


def driver_name() -> str:
    return os.environ.get("FBADMIN_DRIVER", "firebirdsql").strip().lower()


def native_enabled() -> bool:
    return driver_name() in ("native", "firebird-driver", "fbclient")


def _load():
    try:
        from firebird import driver
    except ImportError as exc:  # pragma: no cover - depends on optional package
        raise RuntimeError("The native driver needs the 'firebird-driver' package "
                           "(pip install 'firebird-admin[native]')") from exc
    from .embedded import prepare_client

    # With an embedded engine configured its client library serves remote servers too
    # (one libfbclient per process); otherwise FBADMIN_FBCLIENT or the default search path.
    lib = prepare_client() or os.environ.get("FBADMIN_FBCLIENT")
    if lib and driver.driver_config.fb_client_library.value != lib:
        driver.driver_config.fb_client_library.value = lib
    return driver


_NETWORK_HINTS = ("unable to complete network request", "failed to establish a connection",
                  "connection refused", "no route to host")


def _translate(exc: Exception) -> Exception:
    """firebird.driver errors -> firebirdsql errors (or OSError for network failures)."""
    msg = str(exc).strip()
    if any(h in msg.lower() for h in _NETWORK_HINTS):
        return ConnectionError(msg)
    sqlcode = getattr(exc, "sqlcode", 0) or 0
    gds = set(getattr(exc, "gds_codes", ()) or ())
    name = type(exc).__name__
    cls = getattr(firebirdsql, name, None)
    if not (isinstance(cls, type) and issubclass(cls, firebirdsql.Error)) or cls is firebirdsql.NotSupportedError:
        cls = firebirdsql.OperationalError
    return cls(msg, gds, sqlcode)


def _value(v: Any) -> Any:
    # Large blobs come back as stream readers; the app expects plain values.
    if hasattr(v, "read") and hasattr(v, "close") and not isinstance(v, (bytes, str)):
        try:
            return v.read()
        finally:
            v.close()
    return v


def _row(r):
    return None if r is None else tuple(_value(v) for v in r)


class NativeCursor:
    def __init__(self, conn: NativeConnection) -> None:
        self._owner = conn
        self._cur = conn._con.cursor()
        self._cur.stream_blob_threshold = 64 * 1024 * 1024
        self._has_rows = False

    def execute(self, sql: str, params: list | tuple | None = None):
        try:
            self._cur.execute(sql, list(params) if params else None)
        except self._owner._errors as exc:
            raise _translate(exc) from exc
        self._has_rows = self._cur.description is not None
        return self

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
            return _row(self._cur.fetchone()) if self._has_rows else None
        except self._owner._errors as exc:
            raise _translate(exc) from exc

    def fetchmany(self, size: int | None = None):
        try:
            rows = self._cur.fetchmany(size) if size else self._cur.fetchmany()
        except self._owner._errors as exc:
            raise _translate(exc) from exc
        return [_row(r) for r in rows]

    def fetchall(self):
        if not self._has_rows:
            return []
        try:
            return [_row(r) for r in self._cur.fetchall()]
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


class NativeConnection:
    """DB-API-ish wrapper around ``firebird.driver.Connection``."""

    def __init__(self, con, errors: tuple[type[Exception], ...]) -> None:
        self._con = con
        self._errors = errors

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
        try:
            self._con.close()
        except self._errors as exc:
            raise _translate(exc) from exc

    def drop_database(self) -> None:
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


def _embedded_config(driver, database: str, page_size: int | None = None, db_charset: str | None = None) -> str:
    """Name of a firebird-driver database configuration that opens ``database`` with the
    embedded engine only (``Providers = Engine13``): a local registration must never fall
    back to the Loopback provider and silently reach a server on localhost."""
    key = (database, page_size, db_charset)
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


def connect_native(*, host: str, port: int, database: str, user: str, password: str,
                   role: str | None = None, charset: str = "UTF8", embedded: bool = False,
                   create: bool = False, page_size: int | None = None, **_ignored) -> NativeConnection:
    """Attach (or with ``create`` create) a database. ``embedded``: a local file opened in-process."""
    driver = _load()
    errors = (driver.Error,)
    if embedded:
        dsn = _embedded_config(driver, database, page_size, charset) if create else _embedded_config(driver, database)
    else:
        dsn = f"{host}/{port}:{database}" if host else database
    try:
        if create:
            con = driver.create_database(dsn, user=user, password=password or None, role=role or None,
                                         charset=charset)
        else:
            con = driver.connect(dsn, user=user, password=password or None, role=role or None, charset=charset)
        # Match firebirdsql's default (read committed) so new objects and fresh MON$ data show up.
        con.main_transaction.default_tpb = driver.tpb(driver.Isolation.READ_COMMITTED_RECORD_VERSION)
    except errors as exc:
        raise _translate(exc) from exc
    return NativeConnection(con, errors)


def native_available() -> tuple[bool, str]:
    """(True, "") when the package and client library load, else (False, reason)."""
    try:
        driver = _load()
        driver.load_api()
    except Exception as exc:  # noqa: BLE001
        return False, str(exc)
    return True, ""
