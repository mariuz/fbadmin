"""Helpers for tests that compare or switch the Firebird drivers (firebirdsql / native)."""

from __future__ import annotations

import os

import pytest


def native_status() -> tuple[bool, str]:
    try:
        from fbadmin.native import native_available
    except Exception as exc:  # noqa: BLE001
        return False, str(exc)
    return native_available()


_ok, _why = native_status()
requires_native = pytest.mark.skipif(not _ok, reason=f"native driver unavailable: {_why} "
                                                      "(install firebird-driver, set FBADMIN_FBCLIENT)")


def client_major() -> int | None:
    """Major version of the loaded libfbclient (None without the native driver)."""
    if not _ok:
        return None
    from fbadmin.native import client_major

    return client_major()


def native_suite() -> bool:
    """The whole suite runs with the native driver: FBADMIN_DRIVER=native, or the default (auto)
    with a Firebird 4+ client library."""
    env = os.environ.get("FBADMIN_DRIVER", "").strip().lower()
    if env:
        return env in ("native", "firebird-driver", "fbclient") or (env == "auto" and (client_major() or 0) >= 4)
    return (client_major() or 0) >= 4


def use_driver(monkeypatch, name: str) -> None:
    """Select ``name`` for new connections of the running app and drop its pooled ones."""
    monkeypatch.setenv("FBADMIN_DRIVER", name)
    from fbadmin.api.deps import get_state

    get_state().manager.close_pools()
