"""Firebird security management: users (Services API and SQL), mappings, roles and system privileges.

Every change returns the SQL (or service action) it ran; ``preview=true`` only returns it.
Changes are written to the audit log.
"""

from __future__ import annotations

import time

from fastapi import APIRouter
from pydantic import BaseModel

from ..metadata import Catalog
from ..security import fbservices as svc_api
from ..security import fbsql
from ..serialize import to_json
from .deps import get_state

router = APIRouter(tags=["firebird security"])

SRV = "/api/servers/{server_id}"
DB = SRV + "/databases/{db_id}/security"


# ------------------------------------------------------------------ helpers
def _run(server_id: str, db_id: str, statements: list[str], preview: bool) -> dict:
    """Execute DDL statements on the metadata connection (each committed), auditing each one."""
    st = get_state()
    if preview:
        return {"sql": statements, "executed": False}
    done = []
    with st.manager.meta(server_id, db_id) as conn:
        cur = conn.cursor()
        for sql in statements:
            t0 = time.perf_counter()
            try:
                cur.execute(sql)
                conn.commit()
            except Exception as exc:
                st.security.audit.record(server_id=server_id, db_id=db_id, statement=sql, success=False,
                                         error=str(exc).strip(), cat="DDL")
                raise
            st.security.audit.record(server_id=server_id, db_id=db_id, statement=sql, success=True, cat="DDL",
                                     elapsed_ms=round((time.perf_counter() - t0) * 1000, 2))
            done.append(sql)
    return {"sql": done, "executed": True}


def _catalog_call(server_id: str, db_id: str, fn):
    with get_state().manager.meta(server_id, db_id) as conn:
        return to_json(fn(Catalog(conn)))


def _engine_major(server_id: str, db_id: str) -> int:
    with get_state().manager.meta(server_id, db_id) as conn:
        return Catalog(conn).engine_major


# ------------------------------------------------------------------ Services API users
class SvcUserIn(BaseModel):
    name: str
    password: str | None = None
    first_name: str | None = None
    middle_name: str | None = None
    last_name: str | None = None
    admin: bool | None = None
    security_db: str | None = None


def _svc_audit(server_id: str, text: str, ok: bool, err: str | None = None) -> None:
    get_state().security.audit.record(server_id=server_id, db_id=None, statement=text, success=ok, error=err,
                                      cat="DDL")


@router.get(SRV + "/fbusers")
def svc_list_users(server_id: str, security_db: str | None = None):
    with get_state().manager.service(server_id) as svc:
        return {"users": svc_api.display_users(svc, security_db=security_db or None)}


def _svc_change(server_id: str, desc: str, fn):
    try:
        with get_state().manager.service(server_id) as svc:
            fn(svc)
    except Exception as exc:
        _svc_audit(server_id, desc, False, str(exc).strip())
        raise
    _svc_audit(server_id, desc, True)
    return {"ok": True, "action": desc}


@router.post(SRV + "/fbusers", status_code=201)
def svc_add_user(server_id: str, body: SvcUserIn):
    name = fbsql.name_input(body.name)
    desc = f"-- Services API: add user {name}" + (" (admin)" if body.admin else "")
    return _svc_change(server_id, desc, lambda svc: svc_api.add_user(
        svc, name, body.password or "", first_name=body.first_name, middle_name=body.middle_name,
        last_name=body.last_name, admin=body.admin, security_db=body.security_db or None))


@router.put(SRV + "/fbusers/{name}")
def svc_modify_user(server_id: str, name: str, body: SvcUserIn):
    desc = f"-- Services API: modify user {name}"
    return _svc_change(server_id, desc, lambda svc: svc_api.modify_user(
        svc, name, password=body.password or None, first_name=body.first_name, middle_name=body.middle_name,
        last_name=body.last_name, admin=body.admin, security_db=body.security_db or None))


@router.delete(SRV + "/fbusers/{name}")
def svc_delete_user(server_id: str, name: str, security_db: str | None = None):
    return _svc_change(server_id, f"-- Services API: delete user {name}",
                       lambda svc: svc_api.delete_user(svc, name, security_db=security_db or None))


# ------------------------------------------------------------------ SQL users
class UserIn(BaseModel):
    name: str = ""  # only used when creating
    password: str | None = None
    first_name: str | None = None
    middle_name: str | None = None
    last_name: str | None = None
    active: bool | None = True
    admin: bool | None = None  # GRANT ADMIN ROLE (security database)
    db_admin: bool | None = None  # GRANT RDB$ADMIN (this database)
    plugin: str | None = None
    tags: dict[str, str | None] | None = None
    drop_tags: list[str] | None = None
    description: str | None = None


@router.get(DB + "/users")
def list_users(server_id: str, db_id: str):
    return _catalog_call(server_id, db_id, fbsql.list_users)


@router.post(DB + "/users", status_code=201)
def create_user(server_id: str, db_id: str, body: UserIn, preview: bool = False):
    name = fbsql.name_input(body.name)
    stmts = [fbsql.user_sql("CREATE", name, password=body.password, first_name=body.first_name,
                            middle_name=body.middle_name, last_name=body.last_name, active=body.active,
                            admin=body.admin or None, plugin=body.plugin, tags=body.tags)]
    if body.description:
        stmts.append(fbsql.comment_user_sql(name, body.description, body.plugin))
    if body.db_admin:
        stmts.append(fbsql.db_admin_sql(name, True))
    return _run(server_id, db_id, stmts, preview)


@router.put(DB + "/users/{name}")
def alter_user(server_id: str, db_id: str, name: str, body: UserIn, preview: bool = False):
    stmts = []
    try:
        stmts.append(fbsql.user_sql("ALTER", name, password=body.password, first_name=body.first_name,
                                    middle_name=body.middle_name, last_name=body.last_name, active=body.active,
                                    admin=body.admin, plugin=body.plugin, tags=body.tags,
                                    drop_tags=body.drop_tags))
    except ValueError:
        if body.description is None and body.db_admin is None:
            raise
    if body.description is not None:
        stmts.append(fbsql.comment_user_sql(name, body.description, body.plugin))
    if body.db_admin is not None:
        stmts.append(fbsql.db_admin_sql(name, body.db_admin))
    return _run(server_id, db_id, stmts, preview)


@router.delete(DB + "/users/{name}")
def drop_user(server_id: str, db_id: str, name: str, plugin: str | None = None, preview: bool = False):
    return _run(server_id, db_id, [fbsql.drop_user_sql(name, plugin)], preview)


# ------------------------------------------------------------------ mappings
class MappingIn(BaseModel):
    name: str
    is_global: bool = False
    using: str = "plugin"
    plugin: str | None = None
    database: str | None = None
    from_type: str = "USER"
    from_name: str | None = None
    to_type: str = "USER"
    to_name: str | None = None
    description: str | None = None
    action: str = "CREATE"  # CREATE | ALTER | CREATE OR ALTER


@router.get(DB + "/mappings")
def list_mappings(server_id: str, db_id: str):
    return _catalog_call(server_id, db_id, fbsql.list_mappings)


def _mapping_stmts(server_id: str, db_id: str, body: MappingIn, name: str) -> list[str]:
    from_name = body.from_name.strip() if body.from_name else None
    stmts = [fbsql.mapping_sql(body.action.upper(), name, is_global=body.is_global, using=body.using,
                               plugin=body.plugin, database=body.database or None, from_type=body.from_type,
                               from_name=fbsql.name_input(from_name) if from_name and from_name != "*" else None,
                               to_type=body.to_type,
                               to_name=fbsql.name_input(body.to_name) if body.to_name else None)]
    if body.description:
        if _engine_major(server_id, db_id) < 5:
            raise ValueError("Comments on mappings need Firebird 5 or newer")
        stmts.append(fbsql.comment_mapping_sql(name, body.description, body.is_global))
    return stmts


@router.post(DB + "/mappings", status_code=201)
def create_mapping(server_id: str, db_id: str, body: MappingIn, preview: bool = False):
    return _run(server_id, db_id, _mapping_stmts(server_id, db_id, body, fbsql.name_input(body.name)), preview)


@router.put(DB + "/mappings/{name}")
def alter_mapping(server_id: str, db_id: str, name: str, body: MappingIn, preview: bool = False):
    body.action = "ALTER"
    return _run(server_id, db_id, _mapping_stmts(server_id, db_id, body, name), preview)


@router.delete(DB + "/mappings/{name}")
def drop_mapping(server_id: str, db_id: str, name: str, is_global: bool = False, preview: bool = False):
    return _run(server_id, db_id, [fbsql.drop_mapping_sql(name, is_global)], preview)


# ------------------------------------------------------------------ roles / system privileges
class RoleIn(BaseModel):
    name: str | None = None
    privileges: list[str] = []
    description: str | None = None


class GrantIn(BaseModel):
    user: str
    grant: bool = True
    default: bool = False
    admin_option: bool = False


@router.get(DB + "/roles")
def list_roles(server_id: str, db_id: str):
    return _catalog_call(server_id, db_id, fbsql.list_roles)


@router.post(DB + "/roles", status_code=201)
def create_role(server_id: str, db_id: str, body: RoleIn, preview: bool = False):
    name = fbsql.name_input(body.name or "")
    stmts = [fbsql.role_sql("CREATE", name, body.privileges, _engine_major(server_id, db_id))]
    if body.description:
        stmts.append(f"COMMENT ON ROLE {fbsql.quote_ident(name)} IS {fbsql.sql_string(body.description)}")
    return _run(server_id, db_id, stmts, preview)


@router.put(DB + "/roles/{name}")
def alter_role(server_id: str, db_id: str, name: str, body: RoleIn, preview: bool = False):
    return _run(server_id, db_id, [fbsql.role_sql("ALTER", name, body.privileges,
                                                  _engine_major(server_id, db_id))], preview)


@router.post(DB + "/roles/{name}/members")
def grant_role(server_id: str, db_id: str, name: str, body: GrantIn, preview: bool = False):
    user = fbsql.name_input(body.user)
    return _run(server_id, db_id, [fbsql.grant_role_sql(name, user, body.grant, body.default, body.admin_option)],
                preview)
