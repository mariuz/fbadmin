"""LDAP authentication (``pip install firebird-admin[ldap]`` - uses ldap3).

Two modes:

* **Simple bind**: ``user_dn_template = "uid={username},ou=people,dc=example,dc=org"``
* **Search + bind**: bind with ``bind_dn``/``bind_password`` (or anonymously), find
  the user with ``search_filter`` under ``base_dn``, then bind as that DN.

Members of ``admin_group`` become fbadmin administrators.
"""

from __future__ import annotations

from typing import Any

from .settings import LdapSettings

# Tests replace these to use ldap3's MOCK_SYNC strategy.
server_factory = None
connection_kwargs: dict[str, Any] = {}


def _ldap3():
    try:
        import ldap3
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError("LDAP authentication needs the ldap3 package: pip install 'firebird-admin[ldap]'") from exc
    return ldap3


def _server(cfg: LdapSettings):
    if server_factory is not None:
        return server_factory(cfg)
    ldap3 = _ldap3()
    return ldap3.Server(cfg.url, use_ssl=cfg.url.lower().startswith("ldaps://"), connect_timeout=cfg.timeout,
                        get_info=ldap3.NONE)


def _connect(server, user: str | None, password: str | None, cfg: LdapSettings):
    ldap3 = _ldap3()
    conn = ldap3.Connection(server, user=user, password=password, receive_timeout=cfg.timeout,
                            raise_exceptions=False, **connection_kwargs)
    if cfg.start_tls and not connection_kwargs:
        conn.open()
        conn.start_tls()
    return conn


def authenticate(cfg: LdapSettings, username: str, password: str) -> dict[str, Any] | None:
    """Return ``{"username", "email", "admin"}`` when the credentials are valid, else None."""
    if not cfg.url or not username or not password:  # an empty password would be an anonymous bind
        return None
    ldap3 = _ldap3()
    from ldap3.utils.conv import escape_filter_chars
    from ldap3.utils.dn import escape_rdn

    server = _server(cfg)
    if cfg.user_dn_template:
        dn = cfg.user_dn_template.format(username=escape_rdn(username))
    else:
        if not cfg.base_dn:
            raise RuntimeError("LDAP: set user_dn_template, or base_dn for search + bind")
        svc = _connect(server, cfg.bind_dn or None, cfg.bind_password or None, cfg)
        if not svc.bind():
            raise RuntimeError("LDAP: the search bind failed (check bind_dn / bind_password)")
        svc.search(cfg.base_dn, cfg.search_filter.format(username=escape_filter_chars(username)),
                   search_scope=ldap3.SUBTREE, attributes=[cfg.email_attribute])
        entries = list(svc.entries)
        svc.unbind()
        if len(entries) != 1:
            return None
        dn = entries[0].entry_dn

    conn = _connect(server, dn, password, cfg)
    if not conn.bind():
        return None
    email = None
    admin = False
    try:
        conn.search(dn, "(objectClass=*)", search_scope=ldap3.BASE, attributes=[cfg.email_attribute])
        if conn.entries:
            vals = conn.entries[0].entry_attributes_as_dict.get(cfg.email_attribute) or []
            email = str(vals[0]) if vals else None
        if cfg.admin_group:
            conn.search(cfg.admin_group, f"({cfg.group_member_attribute}={escape_filter_chars(dn)})",
                        search_scope=ldap3.BASE, attributes=["cn"])
            admin = bool(conn.entries)
    finally:
        conn.unbind()
    return {"username": username, "email": email, "admin": admin}
