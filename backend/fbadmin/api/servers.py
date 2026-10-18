"""Server and database registration, server info, maintenance jobs."""

from __future__ import annotations

from fastapi import APIRouter
from pydantic import BaseModel

from ..connections import connect
from ..metadata.types import quote_ident
from ..store import DatabaseEntry, ServerEntry
from .deps import get_state

router = APIRouter(prefix="/api/servers", tags=["servers"])


class ServerIn(BaseModel):
    name: str
    host: str = "localhost"
    port: int = 3050
    user: str = "SYSDBA"
    password: str | None = None
    save_password: bool = True
    role: str | None = None
    charset: str = "UTF8"
    wire_crypt: bool = True
    wire_compress: bool = False
    auth_plugin: str | None = None
    read_only: bool = False
    shared: bool = False
    group: str = "Servers"


class ServerUpdate(BaseModel):
    name: str | None = None
    host: str | None = None
    port: int | None = None
    user: str | None = None
    password: str | None = None
    save_password: bool | None = None
    role: str | None = None
    charset: str | None = None
    wire_crypt: bool | None = None
    wire_compress: bool | None = None
    auth_plugin: str | None = None
    read_only: bool | None = None
    shared: bool | None = None
    group: str | None = None


class ConnectIn(BaseModel):
    password: str | None = None


class DatabaseIn(BaseModel):
    name: str
    path: str


class CreateDatabaseIn(BaseModel):
    name: str
    path: str
    page_size: int = 16384
    charset: str = "UTF8"


class BackupIn(BaseModel):
    database_id: str
    backup_file: str
    metadata_only: bool = False
    garbage_collect: bool = True
    transportable: bool = True


class RestoreIn(BaseModel):
    backup_file: str
    database_path: str
    replace: bool = False
    page_size: int | None = None
    deactivate_indexes: bool = False
    register_as: str | None = None


def _server_view(srv: ServerEntry) -> dict:
    st = get_state()
    data = srv.public()
    data["connected"] = any(st.manager.is_connected(srv.id, d.id) for d in srv.databases)
    for d in data["databases"]:
        d["connected"] = st.manager.is_connected(srv.id, d["id"])
    data["password_known"] = bool(st.store.password_for(srv.id))
    return data


@router.get("")
def list_servers():
    return [_server_view(s) for s in get_state().store.list()]


@router.post("", status_code=201)
def add_server(body: ServerIn):
    srv = get_state().store.add(ServerEntry(**body.model_dump()))
    return _server_view(srv)


@router.get("/{server_id}")
def get_server(server_id: str):
    return _server_view(get_state().store.get(server_id))


@router.put("/{server_id}")
def update_server(server_id: str, body: ServerUpdate):
    st = get_state()
    st.manager.disconnect(server_id)
    srv = st.store.update(server_id, body.model_dump(exclude_unset=True))
    return _server_view(srv)


@router.delete("/{server_id}", status_code=204)
def delete_server(server_id: str):
    st = get_state()
    st.manager.disconnect(server_id)
    st.store.remove(server_id)


@router.post("/{server_id}/connect")
def connect_server(server_id: str, body: ConnectIn):
    """Verify credentials via the Services API and remember the password for this session."""
    st = get_state()
    if body.password:
        st.store.remember_password(server_id, body.password)
    with st.manager.service(server_id) as svc:
        return {
            "server_version": svc.getServerVersion(),
            "architecture": svc.getArchitecture(),
        }


@router.post("/{server_id}/disconnect")
def disconnect_server(server_id: str):
    get_state().manager.disconnect(server_id)
    return {"ok": True}


@router.get("/{server_id}/info")
def server_info(server_id: str):
    with get_state().manager.service(server_id) as svc:
        info = {
            "server_version": svc.getServerVersion(),
            "architecture": svc.getArchitecture(),
            "service_manager_version": svc.getServiceManagerVersion(),
        }
        for key, fn in (
            ("home_directory", svc.getHomeDir),
            ("security_database", svc.getSecurityDatabasePath),
            ("lock_directory", svc.getLockFileDir),
            ("message_directory", svc.getMessageFileDir),
        ):
            try:
                info[key] = fn()
            except Exception:
                info[key] = None
        try:
            count, names = svc._getSvrDbInfo()
            info["attachment_count"] = count
            info["attached_databases"] = names
        except Exception:
            info["attachment_count"] = None
            info["attached_databases"] = []
        return info


@router.get("/{server_id}/log")
def server_log(server_id: str):
    with get_state().manager.service(server_id) as svc:
        return {"log": svc.getLog()}


# ------------------------------------------------------------- databases
@router.post("/{server_id}/databases", status_code=201)
def add_database(server_id: str, body: DatabaseIn):
    return get_state().store.add_database(server_id, DatabaseEntry(**body.model_dump())).model_dump()


@router.post("/{server_id}/databases/create", status_code=201)
def create_database(server_id: str, body: CreateDatabaseIn):
    st = get_state()
    srv = st.store.get(server_id)
    pw = st.store.password_for(server_id)
    if not pw:
        from ..connections import PasswordRequired

        raise PasswordRequired(server_id)
    srv_cs = srv.model_copy(update={"charset": body.charset})
    conn = connect(srv_cs, body.path, pw, create=True, page_size=body.page_size)
    conn.close()
    return st.store.add_database(server_id, DatabaseEntry(name=body.name, path=body.path)).model_dump()


@router.put("/{server_id}/databases/{db_id}")
def update_database(server_id: str, db_id: str, body: DatabaseIn):
    st = get_state()
    st.manager.disconnect(server_id, db_id)
    return st.store.update_database(server_id, db_id, body.model_dump()).model_dump()


@router.delete("/{server_id}/databases/{db_id}", status_code=204)
def remove_database(server_id: str, db_id: str):
    st = get_state()
    st.manager.disconnect(server_id, db_id)
    st.store.remove_database(server_id, db_id)


@router.post("/{server_id}/databases/{db_id}/disconnect")
def disconnect_database(server_id: str, db_id: str):
    get_state().manager.disconnect(server_id, db_id)
    return {"ok": True}


@router.post("/{server_id}/databases/{db_id}/drop", status_code=204)
def drop_database(server_id: str, db_id: str):
    """Physically drop the database file (DROP DATABASE) and unregister it."""
    st = get_state()
    st.manager.disconnect(server_id, db_id)
    srv = st.store.get(server_id)
    db = st.store.get_database(server_id, db_id)
    conn = connect(srv, db.path, st.store.password_for(server_id) or "")
    conn.drop_database()
    st.store.remove_database(server_id, db_id)


# ------------------------------------------------------------ maintenance
@router.post("/{server_id}/backup", status_code=202)
def backup(server_id: str, body: BackupIn):
    st = get_state()
    db = st.store.get_database(server_id, body.database_id)

    def run(log):
        log(f"Backing up {db.path} to {body.backup_file}")
        with st.manager.service(server_id) as svc:
            svc.backup_database(
                db.path, body.backup_file, transportable=body.transportable, metadataOnly=body.metadata_only,
                garbageCollect=body.garbage_collect, callback=log,
            )
        log("Backup completed")

    return st.jobs.start("backup", f"Backup {db.name}", server_id, run).summary()


@router.post("/{server_id}/restore", status_code=202)
def restore(server_id: str, body: RestoreIn):
    st = get_state()

    def run(log):
        log(f"Restoring {body.backup_file} to {body.database_path}")
        with st.manager.service(server_id) as svc:
            svc.restore_database(
                body.backup_file, body.database_path, replace=body.replace, create=not body.replace,
                deactivateIndexes=body.deactivate_indexes, pageSize=body.page_size, callback=log,
            )
        log("Restore completed")
        if body.register_as:
            st.store.add_database(server_id, DatabaseEntry(name=body.register_as, path=body.database_path))
            log(f"Registered database as {body.register_as!r}")

    return st.jobs.start("restore", f"Restore {body.backup_file}", server_id, run).summary()


@router.post("/{server_id}/databases/{db_id}/sweep", status_code=202)
def sweep(server_id: str, db_id: str):
    st = get_state()
    db = st.store.get_database(server_id, db_id)

    def run(log):
        log(f"Sweeping {db.path}")
        with st.manager.service(server_id) as svc:
            svc.sweep(db.path, callback=log)
        log("Sweep completed")

    return st.jobs.start("sweep", f"Sweep {db.name}", server_id, run).summary()


@router.post("/{server_id}/databases/{db_id}/statistics", status_code=202)
def statistics(server_id: str, db_id: str, header_only: bool = False):
    st = get_state()
    db = st.store.get_database(server_id, db_id)

    def run(log):
        with st.manager.service(server_id) as svc:
            if header_only:
                out = svc.getStatistics(db.path, showOnlyDatabaseHeaderPages=True,
                                        showUserDataPages=False, showUserIndexPages=False)
            else:
                out = svc.getStatistics(db.path)
        log(out)

    return st.jobs.start("statistics", f"Statistics {db.name}", server_id, run).summary()


@router.post("/{server_id}/databases/{db_id}/recompute-selectivity", status_code=202)
def recompute_selectivity(server_id: str, db_id: str):
    """SET STATISTICS INDEX for every user index."""
    st = get_state()
    db = st.store.get_database(server_id, db_id)

    def run(log):
        with st.manager.meta(server_id, db_id) as conn:
            cur = conn.cursor()
            cur.execute(
                "SELECT TRIM(i.RDB$INDEX_NAME) FROM RDB$INDICES i JOIN RDB$RELATIONS r "
                "ON r.RDB$RELATION_NAME = i.RDB$RELATION_NAME WHERE COALESCE(r.RDB$SYSTEM_FLAG, 0) = 0"
            )
            names = [r[0] for r in cur.fetchall()]
            for n in names:
                cur.execute(f"SET STATISTICS INDEX {quote_ident(n)}")
                log(f"Recomputed {n}")
        log(f"Done: {len(names)} indices")

    return st.jobs.start("selectivity", f"Recompute index statistics {db.name}", server_id, run).summary()
