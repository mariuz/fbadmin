"""Firebird security objects through SQL: users (plugins, tags, admin role), mappings,
roles with system privileges. Works on Firebird 3, 4 and 5; system privileges need 4+.
"""

from __future__ import annotations

import re
from typing import Any

from ..metadata.types import quote_ident, sql_string

# RDB$ROLES.RDB$SYSTEM_PRIVILEGES: bit N (little-endian) = privilege with id N (src/jrd/SystemPrivileges.h).
SYSTEM_PRIVILEGES = [
    None,  # 0: NULL_PRIVILEGE
    "USER_MANAGEMENT", "READ_RAW_PAGES", "CREATE_USER_TYPES", "USE_NBACKUP_UTILITY", "CHANGE_SHUTDOWN_MODE",
    "TRACE_ANY_ATTACHMENT", "MONITOR_ANY_ATTACHMENT", "ACCESS_SHUTDOWN_DATABASE", "CREATE_DATABASE",
    "DROP_DATABASE", "USE_GBAK_UTILITY", "USE_GSTAT_UTILITY", "USE_GFIX_UTILITY", "IGNORE_DB_TRIGGERS",
    "CHANGE_HEADER_SETTINGS", "SELECT_ANY_OBJECT_IN_DATABASE", "ACCESS_ANY_OBJECT_IN_DATABASE",
    "MODIFY_ANY_OBJECT_IN_DATABASE", "CHANGE_MAPPING_RULES", "USE_GRANTED_BY_CLAUSE",
    "GRANT_REVOKE_ON_ANY_OBJECT", "GRANT_REVOKE_ANY_DDL_RIGHT", "CREATE_PRIVILEGED_ROLES", "GET_DBCRYPT_INFO",
    "MODIFY_EXT_CONN_POOL", "REPLICATE_INTO_DATABASE",
    "PROFILE_ANY_ATTACHMENT",  # Firebird 5
]
_FB5_ONLY = {"PROFILE_ANY_ATTACHMENT"}

_PLAIN_WORD = re.compile(r"^[A-Za-z][A-Za-z0-9_$]*$")
MAP_USING = ("plugin", "any_plugin", "serverwide", "mapping", "all")


def privileges_for(engine_major: int) -> list[str]:
    if engine_major < 4:
        return []
    return [p for p in SYSTEM_PRIVILEGES if p and (engine_major >= 5 or p not in _FB5_ONLY)]


def decode_privileges(raw: Any, engine_major: int = 5) -> list[str]:
    """Bitmask (bytes, or hex text from HEX_ENCODE) -> privilege names known to that Firebird version."""
    if raw is None:
        return []
    if isinstance(raw, str):
        raw = bytes.fromhex(raw.strip())
    mask = int.from_bytes(bytes(raw), "little")
    known = set(privileges_for(engine_major))
    return [name for i, name in enumerate(SYSTEM_PRIVILEGES) if name in known and mask >> i & 1]


def encode_privileges(names: list[str]) -> bytes:
    mask = 0
    for n in names:
        mask |= 1 << SYSTEM_PRIVILEGES.index(n)
    return mask.to_bytes(8, "little")


def name_input(name: str) -> str:
    """A name as typed by the user: "Quoted" keeps its case, anything else is upper-cased."""
    n = (name or "").strip()
    if not n:
        raise ValueError("A name is required")
    if len(n) >= 2 and n[0] == n[-1] == '"':
        return n[1:-1].replace('""', '"')
    return n.upper()


def _word(value: str, what: str) -> str:
    if not _PLAIN_WORD.match(value or ""):
        raise ValueError(f"Invalid {what}: {value!r}")
    return value


# ------------------------------------------------------------------ users
def user_sql(action: str, name: str, *, password: str | None = None, first_name: str | None = None,
             middle_name: str | None = None, last_name: str | None = None, active: bool | None = None,
             admin: bool | None = None, plugin: str | None = None, tags: dict[str, str | None] | None = None,
             drop_tags: list[str] | None = None) -> str:
    """CREATE / ALTER / CREATE OR ALTER USER. ``admin`` grants/revokes the security database admin role."""
    if action not in ("CREATE", "ALTER", "CREATE OR ALTER"):
        raise ValueError(f"Unknown action {action!r}")
    sql = f"{action} USER {quote_ident(name)}"
    if action == "ALTER":
        sql += " SET"
    if password:
        sql += f" PASSWORD {sql_string(password)}"
    elif action == "CREATE":
        raise ValueError("A password is required for a new user")
    for kw, val in (("FIRSTNAME", first_name), ("MIDDLENAME", middle_name), ("LASTNAME", last_name)):
        if val is not None:
            sql += f" {kw} {sql_string(val)}"
    if active is not None:
        sql += " ACTIVE" if active else " INACTIVE"
    if admin is not None:  # Firebird 3 wants this before USING PLUGIN / TAGS
        sql += " GRANT ADMIN ROLE" if admin else " REVOKE ADMIN ROLE"
    if plugin:
        sql += f" USING PLUGIN {_word(plugin, 'plugin name')}"
    parts = [f"{_word(k, 'tag name')} = {sql_string('' if v is None else str(v))}" for k, v in (tags or {}).items()]
    parts += [f"DROP {_word(k, 'tag name')}" for k in (drop_tags or [])]
    if parts:
        sql += f" TAGS ({', '.join(parts)})"
    if sql.endswith(" SET"):
        raise ValueError("Nothing to change")
    return sql


def drop_user_sql(name: str, plugin: str | None = None) -> str:
    sql = f"DROP USER {quote_ident(name)}"
    if plugin:
        sql += f" USING PLUGIN {_word(plugin, 'plugin name')}"
    return sql


def comment_user_sql(name: str, comment: str | None, plugin: str | None = None) -> str:
    sql = f"COMMENT ON USER {quote_ident(name)}"
    if plugin:
        sql += f" USING PLUGIN {_word(plugin, 'plugin name')}"
    return sql + f" IS {sql_string(comment or None)}"


def db_admin_sql(name: str, grant: bool) -> str:
    return f"GRANT RDB$ADMIN TO USER {quote_ident(name)}" if grant else \
        f"REVOKE RDB$ADMIN FROM USER {quote_ident(name)}"


def list_users(cat) -> dict[str, Any]:
    users = cat.rows(
        "SELECT TRIM(u.SEC$USER_NAME) AS user_name, u.SEC$FIRST_NAME AS first_name, "
        "u.SEC$MIDDLE_NAME AS middle_name, u.SEC$LAST_NAME AS last_name, u.SEC$ACTIVE AS is_active, "
        "u.SEC$ADMIN AS is_admin, u.SEC$DESCRIPTION AS description, TRIM(u.SEC$PLUGIN) AS plugin "
        "FROM SEC$USERS u ORDER BY u.SEC$USER_NAME, u.SEC$PLUGIN"
    )
    tags: dict[tuple[str, str], dict[str, str]] = {}
    for r in cat.rows("SELECT TRIM(SEC$USER_NAME) AS user_name, TRIM(SEC$KEY) AS tag_key, SEC$VALUE AS tag_value, "
                      "TRIM(SEC$PLUGIN) AS plugin FROM SEC$USER_ATTRIBUTES"):
        tags.setdefault((r["user_name"], r["plugin"]), {})[r["tag_key"]] = r["tag_value"]
    db_admins = {r["u"] for r in cat.rows(
        "SELECT DISTINCT TRIM(RDB$USER) AS u FROM RDB$USER_PRIVILEGES "
        "WHERE RDB$RELATION_NAME = 'RDB$ADMIN' AND RDB$PRIVILEGE = 'M' AND RDB$USER_TYPE = 8")}
    out = []
    for u in users:
        out.append({
            "name": u["user_name"],
            "first_name": (u["first_name"] or "").strip(),
            "middle_name": (u["middle_name"] or "").strip(),
            "last_name": (u["last_name"] or "").strip(),
            "active": bool(u["is_active"]),
            "admin": bool(u["is_admin"]),
            "db_admin": u["user_name"] in db_admins,
            "plugin": u["plugin"],
            "description": u["description"],
            "tags": tags.get((u["user_name"], u["plugin"]), {}),
        })
    sec_db = cat.one("SELECT TRIM(MON$SEC_DATABASE) AS sec_db FROM MON$DATABASE") or {}
    plugins = sorted({u["plugin"] for u in out if u["plugin"]} | {"Srp"})
    return {"users": out, "plugins": plugins, "security_database": sec_db.get("sec_db"),
            "current_user": (cat.one("SELECT TRIM(CURRENT_USER) AS u FROM RDB$DATABASE") or {}).get("u")}


# ------------------------------------------------------------------ mappings
def mapping_sql(action: str, name: str, *, is_global: bool = False, using: str = "plugin",
                plugin: str | None = None, database: str | None = None, from_type: str = "USER",
                from_name: str | None = None, to_type: str = "USER", to_name: str | None = None) -> str:
    if action not in ("CREATE", "ALTER", "CREATE OR ALTER"):
        raise ValueError(f"Unknown action {action!r}")
    if using not in MAP_USING:
        raise ValueError(f"'using' must be one of {', '.join(MAP_USING)}")
    if to_type.upper() not in ("USER", "ROLE"):
        raise ValueError("Map to USER or ROLE")
    sql = f"{action} {'GLOBAL ' if is_global else ''}MAPPING {quote_ident(name)} USING "
    if using == "plugin":
        if not plugin:
            raise ValueError("Choose the authentication plugin")
        sql += f"PLUGIN {_word(plugin, 'plugin name')}"
    elif using in ("any_plugin", "serverwide"):
        sql += "ANY PLUGIN"
    elif using == "mapping":
        sql += "MAPPING"
    else:
        sql += "*"
    if using == "serverwide":
        sql += " SERVERWIDE"
    elif database:
        sql += f" IN {quote_ident(database)}"
    ft = _word((from_type or "USER").strip(), "source type")
    sql += f" FROM ANY {ft}" if not from_name or from_name == "*" else f" FROM {ft} {quote_ident(from_name)}"
    sql += f" TO {to_type.upper()}"
    if to_name:
        sql += f" {quote_ident(to_name)}"
    elif to_type.upper() == "ROLE":
        raise ValueError("A role mapping needs the target role")
    return sql


def drop_mapping_sql(name: str, is_global: bool = False) -> str:
    return f"DROP {'GLOBAL ' if is_global else ''}MAPPING {quote_ident(name)}"


def comment_mapping_sql(name: str, comment: str | None, is_global: bool = False) -> str:
    return f"COMMENT ON {'GLOBAL ' if is_global else ''}MAPPING {quote_ident(name)} IS {sql_string(comment or None)}"


_USING = {"P": "plugin", "S": "serverwide", "M": "mapping", "*": "all"}


def _mapping_row(r: dict[str, Any], prefix: str, is_global: bool) -> dict[str, Any]:
    g = {k.split("$", 1)[1].lower() if "$" in k else k: v for k, v in r.items()}
    using = _USING.get((g.get("map_using") or "").strip(), (g.get("map_using") or "").strip())
    plugin = (g.get("map_plugin") or "").strip() or None
    if using == "plugin" and not plugin:
        using = "any_plugin"
    to_type = g.get("map_to_type")
    return {
        "name": (g.get("map_name") or "").strip(),
        "global": is_global,
        "using": using,
        "plugin": plugin,
        "database": (g.get("map_db") or "").strip() or None,
        "from_type": (g.get("map_from_type") or "").strip(),
        "from_name": (g.get("map_from") or "").strip(),
        "to_type": "ROLE" if to_type == 1 else "USER",
        "to_name": (g.get("map_to") or "").strip() or None,
        "system": bool(g.get("system_flag")),
        "description": g.get("description"),
    }


def list_mappings(cat) -> dict[str, Any]:
    local = [_mapping_row(r, "RDB$", False) for r in cat.rows("SELECT * FROM RDB$AUTH_MAPPING ORDER BY RDB$MAP_NAME")]
    try:
        glob = [_mapping_row(r, "SEC$", True) for r in cat.rows(
            "SELECT * FROM SEC$GLOBAL_AUTH_MAPPING ORDER BY SEC$MAP_NAME")]
        glob_error = None
    except Exception as exc:  # noqa: BLE001 - e.g. no access to the security database
        glob, glob_error = [], str(exc).strip()
    return {"local": local, "global": glob, "global_error": glob_error}


# ------------------------------------------------------------------ roles / system privileges
def role_sql(action: str, name: str, privileges: list[str] | None, engine_major: int) -> str:
    valid = privileges_for(engine_major)
    for p in privileges or []:
        if p not in valid:
            raise ValueError(f"Unknown system privilege {p!r} for Firebird {engine_major}")
    if action == "CREATE":
        sql = f"CREATE ROLE {quote_ident(name)}"
        if privileges:
            sql += " SET SYSTEM PRIVILEGES TO " + ", ".join(privileges)
        return sql
    if action == "ALTER":
        if engine_major < 4:
            raise ValueError("System privileges need Firebird 4 or newer")
        if privileges:
            return f"ALTER ROLE {quote_ident(name)} SET SYSTEM PRIVILEGES TO " + ", ".join(privileges)
        return f"ALTER ROLE {quote_ident(name)} DROP SYSTEM PRIVILEGES"
    raise ValueError(f"Unknown action {action!r}")


def grant_role_sql(role: str, user: str, grant: bool, default: bool = False, admin_option: bool = False) -> str:
    if grant:
        sql = f"GRANT {'DEFAULT ' if default else ''}{quote_ident(role)} TO USER {quote_ident(user)}"
        return sql + (" WITH ADMIN OPTION" if admin_option else "")
    return f"REVOKE {quote_ident(role)} FROM USER {quote_ident(user)}"


def list_roles(cat) -> dict[str, Any]:
    major = cat.engine_major
    priv_col = ", HEX_ENCODE(RDB$SYSTEM_PRIVILEGES) AS privs" if major >= 4 else ""
    roles = cat.rows(f"SELECT TRIM(RDB$ROLE_NAME) AS role_name, TRIM(RDB$OWNER_NAME) AS owner_name, "
                     f"RDB$DESCRIPTION AS description, COALESCE(RDB$SYSTEM_FLAG, 0) AS sys{priv_col} "
                     f"FROM RDB$ROLES ORDER BY RDB$ROLE_NAME")
    members: dict[str, list[dict[str, Any]]] = {}
    for r in cat.rows("SELECT TRIM(RDB$RELATION_NAME) AS role_name, TRIM(RDB$USER) AS grantee, "
                      "RDB$GRANT_OPTION AS admin_option FROM RDB$USER_PRIVILEGES "
                      "WHERE RDB$PRIVILEGE = 'M' AND RDB$USER_TYPE = 8 ORDER BY RDB$USER"):
        members.setdefault(r["role_name"], []).append({"user": r["grantee"], "admin_option": bool(r["admin_option"])})
    out = [{
        "name": r["role_name"],
        "owner": r["owner_name"],
        "description": r["description"],
        "system": bool(r["sys"]),
        "privileges": decode_privileges(r.get("privs"), major) if major >= 4 else [],
        "members": members.get(r["role_name"], []),
    } for r in roles]
    return {"supported": major >= 4, "engine_major": major, "privileges": privileges_for(major), "roles": out}
