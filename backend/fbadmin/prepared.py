"""Run many statements, preparing each distinct SQL text once.

Grid edits and data compare scripts run the same few statement shapes many times (one UPDATE per
table and set of changed columns, one INSERT, one DELETE...). Executing SQL text prepares it on the
server every time; a prepared statement is executed straight away, which saves a round trip per
statement (about half the time on a network). Works with both drivers (``cursor.prep``; the native
adapter emulates it). ``rowcount`` is right after a prepared execute (see ``driver_patches`` 7).
"""

from __future__ import annotations

from collections import OrderedDict
from typing import Any


class StatementCache:
    """``execute(sql, params)`` with the statements prepared once per cursor (least recently used ones
    are freed beyond ``size``). Use it as a context manager so the statements are freed at the end."""

    def __init__(self, cursor, size: int = 64):
        self.cursor = cursor
        self.size = size
        self._stmts: OrderedDict[str, Any] = OrderedDict()
        self.prepared = 0
        self.executed = 0

    def execute(self, sql: str, params: list | tuple | None = None):
        stmt = self._stmts.get(sql)
        if stmt is None:
            stmt = self.cursor.prep(sql)
            self.prepared += 1
            self._stmts[sql] = stmt
            if len(self._stmts) > self.size:
                _free(self._stmts.popitem(last=False)[1])
        else:
            self._stmts.move_to_end(sql)
        self.cursor.execute(stmt, list(params or []))
        self.executed += 1
        return self.cursor

    @property
    def rowcount(self) -> int:
        return self.cursor.rowcount

    def close(self) -> None:
        while self._stmts:
            _free(self._stmts.popitem()[1])

    def __enter__(self) -> StatementCache:
        return self

    def __exit__(self, *exc) -> None:
        self.close()


def _free(stmt) -> None:
    try:
        stmt.close()
    except Exception:  # noqa: BLE001 - the attachment may be gone
        pass
