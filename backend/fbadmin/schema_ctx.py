"""Firebird 6 SQL schemas: the schema a request works in.

The web UI sends ``X-Fbadmin-Schema: <name>`` for requests made in the context of a schema
(a node under Database → Schemas). The middleware stores it in a context variable, and:

* metadata connections run the request with ``SET SEARCH_PATH TO "<schema>", SYSTEM`` so
  unqualified names (data views, scripts, DDL from dialogs) resolve in that schema;
* :class:`fbadmin.metadata.Catalog` restricts schema-scoped system tables to that schema.

On Firebird 3/4/5 (no schemas) the header is ignored.
"""

from __future__ import annotations

import re
from contextvars import ContextVar

current_schema: ContextVar[str | None] = ContextVar("fbadmin_schema", default=None)

_VALID = re.compile(r"^[^\x00-\x1f\"]{1,63}$")

# system tables whose rows belong to a schema (RDB$SCHEMA_NAME); filtered by Catalog
SCHEMA_SCOPED_TABLES = (
    "RDB$RELATIONS", "RDB$RELATION_FIELDS", "RDB$PROCEDURES", "RDB$PROCEDURE_PARAMETERS",
    "RDB$FUNCTIONS", "RDB$FUNCTION_ARGUMENTS", "RDB$PACKAGES", "RDB$TRIGGERS", "RDB$TRIGGER_MESSAGES",
    "RDB$GENERATORS", "RDB$EXCEPTIONS", "RDB$INDICES", "RDB$INDEX_SEGMENTS", "RDB$RELATION_CONSTRAINTS",
    "RDB$REF_CONSTRAINTS", "RDB$CHECK_CONSTRAINTS", "RDB$VIEW_RELATIONS",
)

_KEYWORDS = (
    "WHERE|JOIN|LEFT|RIGHT|INNER|OUTER|FULL|CROSS|NATURAL|ON|ORDER|GROUP|UNION|INTO|HAVING|PLAN|ROWS|"
    "FETCH|OFFSET|FOR|WITH|AND|OR"
)
_REF = re.compile(
    r"\b(FROM|JOIN)\s+(" + "|".join(re.escape(t) for t in SCHEMA_SCOPED_TABLES) + r")\b(?!\s*\.)"
    r"(\s+(?:AS\s+)?(?!(?:" + _KEYWORDS + r")\b)([A-Za-z_][A-Za-z0-9_$]*))?",
    re.IGNORECASE,
)


def valid_schema(name: str | None) -> str | None:
    if name is None or name == "":
        return None
    if not _VALID.match(name):
        raise ValueError("Invalid schema name")
    return name


def scope_sql(sql: str, schema: str) -> str:
    """Restrict schema-scoped system tables in ``sql`` to ``schema`` (derived tables keep aliases)."""
    lit = schema.replace("'", "''")

    def repl(m: re.Match) -> str:
        table = m.group(2).upper()
        alias = m.group(4) or table
        return f"{m.group(1)} (SELECT * FROM {table} WHERE RDB$SCHEMA_NAME = '{lit}') {alias}"

    return _REF.sub(repl, sql)


def _literal(v) -> str:
    if v is None:
        return "NULL"
    if isinstance(v, bool):
        return "TRUE" if v else "FALSE"
    if isinstance(v, (int, float)):
        return repr(v)
    if isinstance(v, (bytes, bytearray)):
        return "x'" + bytes(v).hex() + "'"
    return "'" + str(v).replace("'", "''") + "'"


def inline_params(sql: str, params: list) -> str:
    """Replace ``?`` markers (outside strings/comments) with SQL literals of ``params``."""
    from .sqlsplit import _scan_params

    out, last, i = [], 0, 0
    for start, end, name in _scan_params(sql):
        if name is not None:
            continue  # :name markers are not used in metadata queries
        out.append(sql[last:start])
        out.append(_literal(params[i]))
        i += 1
        last = end
    if i != len(params):
        raise ValueError("parameter count mismatch")
    out.append(sql[last:])
    return "".join(out)
