"""Firebird user management through the Services API (what ``gsec`` does).

The ``firebirdsql`` driver has no helpers for the user actions, so the service
parameter blocks are built here. Works on Firebird 3, 4 and 5 against the
default security database or, with ``security_db``, the security database
used by a given database (``isc_spb_dbname``).

The Services API does not know about authentication plugins or tags; use the
SQL variant (``CREATE USER ... USING PLUGIN ... TAGS (...)``) for those.
"""

from __future__ import annotations

from typing import Any

from firebirdsql.utils import bytes_to_int, int_to_bytes

ACTION_ADD_USER = 4
ACTION_DELETE_USER = 5
ACTION_MODIFY_USER = 6
ACTION_DISPLAY_USER_ADM = 29
INFO_GET_USERS = 68
INFO_TRUNCATED = 2
INFO_END = 1

SPB_SEC_USERID = 5
SPB_SEC_GROUPID = 6
SPB_SEC_USERNAME = 7
SPB_SEC_PASSWORD = 8
SPB_SEC_GROUPNAME = 9
SPB_SEC_FIRSTNAME = 10
SPB_SEC_MIDDLENAME = 11
SPB_SEC_LASTNAME = 12
SPB_SEC_ADMIN = 13
SPB_DBNAME = 106

_STRINGS = {
    SPB_SEC_USERNAME: "user_name",
    SPB_SEC_FIRSTNAME: "first_name",
    SPB_SEC_MIDDLENAME: "middle_name",
    SPB_SEC_LASTNAME: "last_name",
    SPB_SEC_GROUPNAME: "group_name",
}
_INTS = {SPB_SEC_USERID: "user_id", SPB_SEC_GROUPID: "group_id", SPB_SEC_ADMIN: "admin"}


def _str(svc, tag: int, value: str) -> bytes:
    b = svc.str_to_bytes(value)
    return bytes([tag]) + int_to_bytes(len(b), 2) + b


def _int(tag: int, value: int) -> bytes:
    return bytes([tag]) + int_to_bytes(int(value), 4)


def _run(svc, spb: bytes) -> None:
    svc._op_service_start(spb)
    (h, _oid, _buf) = svc._op_response()
    svc.svc_handle = h


def parse_users(svc, data: bytes) -> list[dict[str, Any]]:
    """Decode the ``isc_info_svc_get_users`` cluster (without its 3-byte header)."""
    users: list[dict[str, Any]] = []
    cur: dict[str, Any] | None = None
    i = 0
    while i < len(data):
        tag = data[i]
        if tag in (INFO_END, INFO_TRUNCATED, 0):
            break
        if tag in _STRINGS:
            ln = bytes_to_int(data[i + 1 : i + 3])
            val = svc.bytes_to_str(data[i + 3 : i + 3 + ln])
            i += 3 + ln
            if tag == SPB_SEC_USERNAME:
                cur = {"user_name": val, "first_name": "", "middle_name": "", "last_name": "", "admin": False}
                users.append(cur)
            elif cur is not None:
                cur[_STRINGS[tag]] = val
        elif tag in _INTS:
            val = bytes_to_int(data[i + 1 : i + 5])
            i += 5
            if cur is not None:
                cur[_INTS[tag]] = bool(val) if tag == SPB_SEC_ADMIN else val
        else:
            raise ValueError(f"Unexpected tag {tag} in user list")
    return users


def display_users(svc, user: str | None = None, security_db: str | None = None) -> list[dict[str, Any]]:
    spb = bytes([ACTION_DISPLAY_USER_ADM])
    if security_db:
        spb += _str(svc, SPB_DBNAME, security_db)
    if user:
        spb += _str(svc, SPB_SEC_USERNAME, user)
    _run(svc, spb)
    raw = b""
    while True:
        svc._op_service_info(bytes([]), bytes([INFO_GET_USERS]), buffer_length=65535)
        (_h, _oid, buf) = svc._op_response()
        if not buf or buf[0] != INFO_GET_USERS:
            break
        ln = bytes_to_int(buf[1:3])
        chunk = buf[3 : 3 + ln]
        raw += chunk
        if ln == 0 or (3 + ln < len(buf) and buf[3 + ln] != INFO_TRUNCATED):
            break
    return parse_users(svc, raw)


def _user_spb(svc, action: int, name: str, password: str | None, first_name: str | None,
              middle_name: str | None, last_name: str | None, admin: bool | None,
              security_db: str | None) -> bytes:
    spb = bytes([action]) + _str(svc, SPB_SEC_USERNAME, name)
    if security_db:
        spb += _str(svc, SPB_DBNAME, security_db)
    if password:
        spb += _str(svc, SPB_SEC_PASSWORD, password)
    for tag, val in ((SPB_SEC_FIRSTNAME, first_name), (SPB_SEC_MIDDLENAME, middle_name),
                     (SPB_SEC_LASTNAME, last_name)):
        if val is not None:
            spb += _str(svc, tag, val)
    if admin is not None:
        spb += _int(SPB_SEC_ADMIN, 1 if admin else 0)
    return spb


def add_user(svc, name: str, password: str, *, first_name: str | None = None, middle_name: str | None = None,
             last_name: str | None = None, admin: bool | None = None, security_db: str | None = None) -> None:
    if not password:
        raise ValueError("A password is required for a new user")
    _run(svc, _user_spb(svc, ACTION_ADD_USER, name, password, first_name, middle_name, last_name, admin,
                        security_db))


def modify_user(svc, name: str, *, password: str | None = None, first_name: str | None = None,
                middle_name: str | None = None, last_name: str | None = None, admin: bool | None = None,
                security_db: str | None = None) -> None:
    _run(svc, _user_spb(svc, ACTION_MODIFY_USER, name, password, first_name, middle_name, last_name, admin,
                        security_db))


def delete_user(svc, name: str, security_db: str | None = None) -> None:
    spb = bytes([ACTION_DELETE_USER]) + _str(svc, SPB_SEC_USERNAME, name)
    if security_db:
        spb += _str(svc, SPB_DBNAME, security_db)
    _run(svc, spb)
