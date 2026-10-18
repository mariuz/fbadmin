"""Read-only servers ("production safety") and statement classification.

Two layers protect a server registered with ``read_only``:

1. Every connection fbadmin opens to it uses read-only transactions
   (``connections.connect`` / ``QuerySession.force_read_only``), so Firebird
   itself rejects INSERT/UPDATE/DELETE, DDL, GRANT, writes inside procedures and
   EXECUTE BLOCK ("attempted update during read-only transaction").
2. Requests are checked before they reach Firebird (``middleware``): scripts may
   only contain queries, and endpoints that change data, metadata or the server
   (grid edits, drops, create/restore database, user management...) are refused
   with HTTP 403 ``{"code": "read_only"}``.
"""

from __future__ import annotations

import re

from ..sqlsplit import split_sql, strip_comments

# isql commands the script runner ignores (see query._IGNORED) and session settings.
_READ_HEADS = re.compile(
    r"^(SELECT|WITH|EXECUTE\s+PROCEDURE|EXECUTE\s+BLOCK|COMMIT|ROLLBACK|SHOW|SET\s+(TERM|SQL\s+DIALECT|NAMES|STATS|"
    r"PLAN|PLANONLY|ECHO|LIST|COUNT|BAIL|HEADING|WIDTH|BLOB|TIME|WARNINGS|WNG|ROWCOUNT|EXPLAIN|PER_TABLE_STATS|"
    r"MAXROWS|KEEP_TRAN_PARAMS|LOCAL_TIMEOUT|AUTODDL|BIND|DECFLOAT|TIME\s+ZONE|ROLE|TRUSTED\s+ROLE|"
    r"STATEMENT\s+TIMEOUT|SESSION\s+IDLE\s+TIMEOUT))\b",
    re.IGNORECASE,
)
_DDL = re.compile(r"^(CREATE|ALTER|DROP|RECREATE|DECLARE|COMMENT|GRANT|REVOKE|SET\s+(GENERATOR|STATISTICS))\b",
                  re.IGNORECASE)
_DML = re.compile(r"^(INSERT|UPDATE|DELETE|MERGE|UPSERT|EXECUTE)\b", re.IGNORECASE)
_TX = re.compile(r"^(COMMIT|ROLLBACK|SET\s+TRANSACTION|SAVEPOINT|RELEASE\s+SAVEPOINT)\b", re.IGNORECASE)


def head(sql: str) -> str:
    return strip_comments(sql).strip().lstrip("(").strip()


def is_read_statement(sql: str) -> bool:
    return bool(_READ_HEADS.match(head(sql)))


def category(sql: str) -> str:
    """DDL, DML, TX, QUERY or OTHER - used by the audit log."""
    h = head(sql)
    if re.match(r"^(SELECT|WITH)\b", h, re.IGNORECASE):
        return "QUERY"
    if _TX.match(h):
        return "TX"
    if _DDL.match(h):
        return "DDL"
    if _DML.match(h):
        return "DML"
    return "OTHER"


def write_statements(script: str) -> list[str]:
    """Statements of ``script`` that are not allowed on a read-only server."""
    return [s.sql for s in split_sql(script) if not is_read_statement(s.sql)]


# --- endpoint rules for read-only servers -------------------------------------------------
# Last path segment of POST requests that only read (anything else changing state is refused).
READ_ACTIONS = {
    "connect", "disconnect", "statistics", "explain", "plan", "search", "compare", "diff", "export",
    "preview", "params", "fetch", "cancel", "completion", "describe", "script", "ddl", "backup",
}

_SERVER = re.compile(r"^/api/servers/([^/]+)(/.*)?$")


def blocked_request(method: str, path: str) -> tuple[str | None, str]:
    """For a request on ``/api/servers/{id}/...`` return (server_id, reason) if it must be refused
    on a read-only server. (None, "") when the request is allowed or not server-specific.
    ``execute`` requests are allowed here and checked statement by statement by the caller."""
    m = _SERVER.match(path)
    if not m or method in ("GET", "HEAD", "OPTIONS"):
        return None, ""
    sid, rest = m.group(1), (m.group(2) or "")
    parts = [p for p in rest.split("/") if p]
    # registration changes only touch fbadmin's own registry
    if not parts and method in ("PUT", "DELETE"):
        return None, ""
    if parts == ["databases"] and method == "POST":
        return None, ""  # register an existing database
    if len(parts) == 2 and parts[0] == "databases" and method in ("PUT", "DELETE"):
        return None, ""  # edit/unregister a database entry
    if parts and parts[-1] == "execute":
        return None, ""
    if method == "DELETE" and len(parts) >= 3 and parts[-2] == "statements" and "monitor" in parts:
        return None, ""  # cancelling a running statement does not change data
    if method == "POST" and parts and parts[-1] in READ_ACTIONS:
        return None, ""
    return sid, f"Server is registered as read-only: {method} {rest or '/'} is not allowed"
