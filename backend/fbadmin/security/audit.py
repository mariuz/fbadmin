"""Audit log of executed DDL and DML: who, when, what, on which server/database.

Entries are written by the security middleware for the generic execute
endpoints (Query Tool, create/alter dialogs, grid edits, drops, maintenance
actions) and directly by the security API (user, role and mapping changes).
"""

from __future__ import annotations

import getpass
import re
import time
from typing import Any

from .readonly import category
from .sessions import current_client_ip, current_principal

MAX_STATEMENT = 20000
_PASSWORD = re.compile(r"(\bPASSWORD\s+)'(?:[^']|'')*'", re.IGNORECASE)


def redact(sql: str) -> str:
    """Hide passwords of CREATE/ALTER USER statements."""
    return _PASSWORD.sub(r"\1'***'", sql)


def _who() -> str:
    p = current_principal.get()
    if p is not None:
        return p.username
    try:
        return f"desktop:{getpass.getuser()}"
    except Exception:  # noqa: BLE001
        return "desktop"


class AuditLog:
    def __init__(self, db, enabled: bool):
        self.db = db
        self.enabled = enabled

    def _where(self, server_id: str | None, db_id: str | None) -> dict[str, Any]:
        from ..api.deps import get_state

        out: dict[str, Any] = {"server_id": server_id, "db_id": db_id}
        if not server_id:
            return out
        try:
            st = get_state()
            srv = st.store.get(server_id)
            out.update(server_name=srv.name, server_host=f"{srv.host}:{srv.port}")
            if db_id:
                d = st.store.get_database(server_id, db_id)
                out.update(db_name=d.name, db_path=d.path)
        except Exception:  # noqa: BLE001 - unknown ids are still logged
            pass
        return out

    def record(self, *, server_id: str | None, db_id: str | None, statement: str, success: bool,
               error: str | None = None, row_count: int | None = None, elapsed_ms: float | None = None,
               cat: str | None = None) -> None:
        if not self.enabled:
            return
        entry = {
            "ts": time.time(),
            "username": _who(),
            "client_ip": current_client_ip.get(),
            "category": cat or category(statement),
            "statement": redact(statement)[:MAX_STATEMENT],
            "success": int(bool(success)),
            "error": (error or None) and str(error)[:4000],
            "row_count": row_count,
            "elapsed_ms": elapsed_ms,
            **self._where(server_id, db_id),
        }
        try:
            self.db.add_audit(entry)
        except Exception:  # noqa: BLE001 - auditing must never break the request
            pass

    def record_script(self, server_id: str | None, db_id: str | None, result: dict[str, Any]) -> None:
        """Log the non-query statements of a ``run_script`` result."""
        for item in result.get("results") or []:
            kind = item.get("kind")
            sql = item.get("sql") or ""
            cat = category(sql)
            if kind == "ignored" or cat == "QUERY":
                continue
            if kind == "select" and cat not in ("DML", "OTHER"):
                continue
            ok = kind != "error"
            rc = item.get("row_count")
            self.record(server_id=server_id, db_id=db_id, statement=sql, success=ok,
                        error=None if ok else item.get("message"),
                        row_count=rc if isinstance(rc, int) and rc >= 0 else None,
                        elapsed_ms=item.get("elapsed_ms"), cat=cat)
