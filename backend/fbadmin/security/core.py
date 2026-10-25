"""The security service: users, logins, sessions and master-password vaults."""

from __future__ import annotations

import json
import os
import shutil
import time
from typing import Any

from . import ldap as ldap_backend
from .audit import AuditLog
from .crypto import (
    WrongPassword,
    dummy_verify,
    hash_password,
    vault_create,
    vault_unlock,
    vault_wrap,
    verify_password,
)
from .db import SecurityDB
from .errors import AccessDenied, LoginRequired, SecurityError
from .oidc import OidcClient
from .sessions import Principal, RateLimiter, Session, SessionStore, current_principal
from .settings import SecuritySettings

MIN_PASSWORD = 8
INTERNAL = "internal"


class Security:
    def __init__(self, settings: SecuritySettings, state: Any):
        self.settings = settings
        self.state = state
        self.db = SecurityDB(settings.db_file)
        self.sessions = SessionStore(settings.session_lifetime, settings.session_idle)
        self.login_limiter = RateLimiter(*settings.login_rate)
        self.api_limiter = RateLimiter(*settings.api_rate)
        self.audit = AuditLog(self.db, settings.audit)
        self.oidc = OidcClient()
        from .access import AccessControl  # finer permissions, 2FA and API tokens (milestone 2.0)
        from .mfa import MfaService
        from .tokens import TokenService

        self.access = AccessControl(self)
        self.mfa = MfaService(self)
        self.tokens = TokenService(self)

    @property
    def server_mode(self) -> bool:
        return self.settings.server_mode

    def close(self) -> None:
        self.db.close()

    # ------------------------------------------------------------ users
    def needs_setup(self) -> bool:
        return self.server_mode and self.db.user_count() == 0

    def bootstrap(self) -> None:
        """Create the initial admin from FBADMIN_ADMIN_EMAIL / FBADMIN_ADMIN_PASSWORD. Safe when several
        processes (workers, replicas on one volume) start at once: only the first one creates it."""
        s = self.settings
        if self.server_mode and s.admin_username and s.admin_password and self.db.user_count() == 0:
            self.create_first_admin(s.admin_username, email=s.admin_email or None, password=s.admin_password)

    def create_first_admin(self, username: str, *, email: str | None, password: str) -> dict[str, Any] | None:
        """The first administrator (bootstrap or the setup page); None if a user exists already."""
        self.check_password_strength(password)
        vault, _ = vault_create(password)
        user = self.db.create_first_user(username, email=email, password_hash=hash_password(password), role="admin",
                                         auth_source=INTERNAL, vault=vault)
        if user is not None:
            self._migrate_desktop_servers(user["id"])
        return user

    @staticmethod
    def check_password_strength(password: str) -> None:
        if len(password or "") < MIN_PASSWORD:
            raise SecurityError(400, f"The password must have at least {MIN_PASSWORD} characters", "weak_password")

    def create_user(self, username: str, *, email: str | None, password: str | None, role: str = "user",
                    auth_source: str = INTERNAL, active: bool = True) -> dict[str, Any]:
        first = self.db.user_count() == 0
        vault = None
        pw_hash = None
        if auth_source == INTERNAL:
            self.check_password_strength(password or "")
            pw_hash = hash_password(password or "")
            vault, _ = vault_create(password or "")
        user = self.db.create_user(username, email=email, password_hash=pw_hash, role=role,
                                   auth_source=auth_source, active=active, vault=vault)
        if first and role == "admin":
            self._migrate_desktop_servers(user["id"])
        return user

    def _migrate_desktop_servers(self, user_id: int) -> None:
        """Server mode on an existing data dir: the first admin inherits the desktop servers.json."""
        legacy = self.settings.data_dir / "servers.json"
        target = self.state.store.user_path(user_id)
        if legacy.exists() and not target.exists():
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(legacy), str(target))
            os.chmod(target, 0o600)
            self.state.store.drop_user(user_id)  # reload from the moved file

    def update_user(self, user_id: int, changes: dict[str, Any], by: Principal) -> dict[str, Any]:
        user = self.db.get_user(user_id)
        if user is None:
            raise KeyError(f"Unknown user {user_id}")
        demote = changes.get("role") == "user" or changes.get("active") is False
        if demote and user["role"] == "admin" and self.db.admin_count(exclude=user_id) == 0:
            raise SecurityError(400, "At least one active administrator is required", "last_admin")
        if user_id == by.user_id and demote:
            raise SecurityError(400, "You cannot demote or disable yourself", "self_demote")
        upd = {k: v for k, v in changes.items() if k in ("email", "role", "active") and v is not None}
        if changes.get("password"):
            if user["auth_source"] != INTERNAL:
                raise SecurityError(400, "Only internal users have a password in fbadmin", "not_internal")
            self.check_password_strength(changes["password"])
            upd["password_hash"] = hash_password(changes["password"])
            # The old vault key cannot be recovered without the old password: start a new vault.
            self.state.store.user_store(user_id).forget_encrypted()
            upd["vault"] = vault_create(changes["password"])[0]
        out = self.db.update_user(user_id, **upd)
        if "role" in upd:
            self.sessions.update_role(user_id, upd["role"])
        if upd.get("active") is False or "password_hash" in upd:
            self.logout_user(user_id)
        return out

    def delete_user(self, user_id: int, by: Principal) -> None:
        user = self.db.get_user(user_id)
        if user is None:
            raise KeyError(f"Unknown user {user_id}")
        if user_id == by.user_id:
            raise SecurityError(400, "You cannot delete yourself", "self_delete")
        if user["role"] == "admin" and self.db.admin_count(exclude=user_id) == 0:
            raise SecurityError(400, "At least one active administrator is required", "last_admin")
        self.logout_user(user_id)
        self.tokens.forget_user(user_id)
        self.access.forget_user(user_id)
        self.mfa.forget_user(user_id)
        self.db.delete_user(user_id)
        folder = self.state.store.user_path(user_id).parent
        self.state.store.drop_user(user_id)
        shutil.rmtree(folder, ignore_errors=True)

    def logout_user(self, user_id: int) -> None:
        self.sessions.remove_user(user_id)
        self._after_last_session(user_id)

    def _after_last_session(self, user_id: int) -> None:
        if not self.sessions.user_has_sessions(user_id):
            self.state.store.user_store(user_id).lock()
            self.state.manager.close_user(user_id)

    # ------------------------------------------------------------ login
    def _rate_keys(self, username: str, ip: str | None) -> list[str]:
        return [f"ip:{ip or '?'}", f"user:{username.strip().lower()}"]

    def check_login_rate(self, username: str, ip: str | None) -> None:
        wait = max(self.login_limiter.blocked(k) for k in self._rate_keys(username, ip))
        if wait:
            raise SecurityError(429, f"Too many failed logins - try again in {int(wait)} s", "rate_limited",
                                {"Retry-After": str(int(wait))})

    def _login_failed(self, username: str, ip: str | None) -> None:
        for k in self._rate_keys(username, ip):
            self.login_limiter.hit(k)
        raise SecurityError(401, "Invalid user name or password", "invalid_credentials")

    def login_password(self, username: str, password: str, ip: str | None) -> Principal:
        return self.finish_login(self.authenticate_password(username, password, ip), password)

    def authenticate_password(self, username: str, password: str, ip: str | None) -> dict[str, Any]:
        """Check the password (internal or LDAP); returns the user without opening a session."""
        username = (username or "").strip()
        self.check_login_rate(username, ip)
        sources = self.settings.auth_sources
        user = self.db.find_user(username) if username else None
        ok = False
        if user and user["auth_source"] == INTERNAL:
            ok = INTERNAL in sources and verify_password(user["password_hash"], password)
        elif (user is None or user["auth_source"] == "ldap") and "ldap" in sources and password:
            try:
                info = ldap_backend.authenticate(self.settings.ldap, username, password)
            except Exception as exc:  # noqa: BLE001 - directory down / misconfigured
                raise SecurityError(502, f"LDAP error: {exc}", "ldap_error") from exc
            if info:
                user = self._external_user(info, "ldap", sync_role=bool(self.settings.ldap.admin_group))
                ok = True
        else:
            dummy_verify()
        if not ok or user is None:
            self._login_failed(username, ip)
        assert user is not None
        if not user["active"]:
            raise SecurityError(403, "This account is disabled", "account_disabled")
        self.login_limiter.reset(self._rate_keys(username, ip)[1])
        return user

    def finish_login(self, user: dict[str, Any], password: str) -> Principal:
        """Password (and second factor, if any) accepted: open the user's vault, record the login."""
        p = Principal(user_id=user["id"], username=user["username"], role=user["role"],
                      auth_source=user["auth_source"])
        self._open_vault_with_login_password(user, password)
        self.db.update_user(user["id"], last_login=time.time())
        return p

    def _external_user(self, info: dict[str, Any], source: str, sync_role: bool) -> dict[str, Any]:
        user = self.db.find_user(info["username"])
        if user is None:
            if not self.settings.auto_create_users:
                raise SecurityError(403, "No fbadmin account for this user - ask an administrator",
                                    "no_account")
            user = self.create_user(info["username"], email=info.get("email"), password=None,
                                    role="admin" if info.get("admin") else "user", auth_source=source)
        elif user["auth_source"] != source:
            raise SecurityError(403, f"User {user['username']!r} signs in with {user['auth_source']}",
                                "wrong_auth_source")
        changes: dict[str, Any] = {}
        if info.get("email") and info["email"] != user.get("email"):
            changes["email"] = info["email"]
        if info.get("admin") and user["role"] != "admin":
            changes["role"] = "admin"
        elif sync_role and not info.get("admin") and user["role"] == "admin" \
                and self.db.admin_count(exclude=user["id"]) > 0:
            changes["role"] = "user"
        if changes:
            user = self.db.update_user(user["id"], **changes)
        return user

    def login_external(self, info: dict[str, Any], source: str) -> Principal:
        user = self._external_user(info, source, sync_role=False)
        if not user["active"]:
            raise SecurityError(403, "This account is disabled", "account_disabled")
        self.db.update_user(user["id"], last_login=time.time())
        return Principal(user_id=user["id"], username=user["username"], role=user["role"], auth_source=source)

    def _open_vault_with_login_password(self, user: dict[str, Any], password: str) -> None:
        store = self.state.store.user_store(user["id"])
        if user.get("vault"):
            try:
                store.unlock(vault_unlock(user["vault"], password))
            except WrongPassword:
                pass  # e.g. LDAP password changed: the user unlocks with the previous one or resets
        elif user["auth_source"] in (INTERNAL, "ldap"):
            record, dek = vault_create(password)
            self.db.update_user(user["id"], vault=record)
            store.enable_encryption(dek)

    def change_password(self, p: Principal, old: str, new: str) -> None:
        user = self.db.get_user(p.user_id)
        if user is None or user["auth_source"] != INTERNAL:
            raise SecurityError(400, "Only internal users can change their password here", "not_internal")
        if not verify_password(user["password_hash"], old):
            raise SecurityError(400, "The current password is wrong", "invalid_credentials")
        self.check_password_strength(new)
        upd: dict[str, Any] = {"password_hash": hash_password(new)}
        if user.get("vault"):
            try:
                upd["vault"] = vault_wrap(vault_unlock(user["vault"], old), new)
            except WrongPassword:
                upd["vault"] = vault_create(new)[0]
                self.state.store.user_store(p.user_id).forget_encrypted()
        self.db.update_user(p.user_id, **upd)

    # ------------------------------------------------------------ sessions
    def start_session(self, p: Principal) -> Session:
        return self.sessions.create(p)

    def end_session(self, sid: str | None) -> None:
        s = self.sessions.remove(sid) if sid else None
        if s:
            self._after_last_session(s.principal.user_id)

    def purge_sessions(self) -> None:
        for s in self.sessions.purge():
            self._after_last_session(s.principal.user_id)

    # ------------------------------------------------------------ vault
    def _principal(self) -> Principal:
        p = current_principal.get()
        if p is None:
            raise LoginRequired()
        return p

    def _store(self):
        if self.server_mode:
            return self.state.store.user_store(self._principal().user_id)
        return self.state.store

    def _vault_record(self) -> dict[str, Any] | None:
        if self.server_mode:
            user = self.db.get_user(self._principal().user_id)
            return user.get("vault") if user else None
        f = self.settings.master_file
        return json.loads(f.read_text("utf-8")) if f.exists() else None

    def _save_vault_record(self, record: dict[str, Any] | None) -> None:
        if self.server_mode:
            self.db.update_user(self._principal().user_id, vault=record)
            return
        f = self.settings.master_file
        if record is None:
            f.unlink(missing_ok=True)
            return
        f.parent.mkdir(parents=True, exist_ok=True)
        tmp = f.with_suffix(".tmp")
        tmp.write_text(json.dumps(record, indent=2), "utf-8")
        os.chmod(tmp, 0o600)
        tmp.replace(f)

    def vault_status(self) -> dict[str, Any]:
        store = self._store()
        record = self._vault_record()
        login_bound = False
        if self.server_mode:
            login_bound = self._principal().auth_source in (INTERNAL, "ldap")
        return {
            "enabled": record is not None,
            "unlocked": record is not None and store.key is not None,
            "login_bound": login_bound,
            "plaintext_passwords": store.plaintext_count(),
        }

    def vault_setup(self, password: str) -> dict[str, Any]:
        if self._vault_record() is not None:
            raise SecurityError(400, "A master password is already set", "vault_exists")
        self.check_password_strength(password)
        record, dek = vault_create(password)
        self._save_vault_record(record)
        migrated = self._store().enable_encryption(dek)
        return {**self.vault_status(), "migrated": migrated}

    def vault_unlock(self, password: str) -> dict[str, Any]:
        record = self._vault_record()
        if record is None:
            raise SecurityError(400, "No master password is set", "no_vault")
        try:
            dek = vault_unlock(record, password)
        except WrongPassword:
            raise SecurityError(400, "Wrong master password", "wrong_master_password") from None
        self._store().unlock(dek)
        return self.vault_status()

    def vault_lock(self) -> dict[str, Any]:
        self._store().lock()
        return self.vault_status()

    def vault_change(self, old: str, new: str) -> dict[str, Any]:
        if self.server_mode and self._principal().auth_source == INTERNAL:
            raise SecurityError(400, "Your saved passwords are protected by your login password - "
                                     "change that instead", "login_bound")
        record = self._vault_record()
        if record is None:
            raise SecurityError(400, "No master password is set", "no_vault")
        try:
            dek = vault_unlock(record, old)
        except WrongPassword:
            raise SecurityError(400, "Wrong master password", "wrong_master_password") from None
        self.check_password_strength(new)
        self._save_vault_record(vault_wrap(dek, new))
        self._store().unlock(dek)
        return self.vault_status()

    def vault_disable(self, password: str) -> dict[str, Any]:
        if self.server_mode:
            raise SecurityError(400, "In server mode saved passwords are always encrypted", "server_mode")
        self.vault_unlock(password)
        self._store().disable_encryption()
        self._save_vault_record(None)
        return self.vault_status()

    def vault_reset(self) -> dict[str, Any]:
        """Forgotten master password: forget every encrypted password and remove the vault."""
        dropped = self._store().forget_encrypted()
        self._save_vault_record(None)
        if self.server_mode:
            self._store().encrypted = True  # a new vault is created at next login / setup
        return {**self.vault_status(), "dropped": dropped}

    def needs_unlock(self, server_id: str) -> bool:
        try:
            return bool(self.state.store.needs_unlock(server_id))
        except Exception:  # noqa: BLE001
            return False


def require_admin() -> Principal | None:
    """Admin check for admin endpoints. In desktop mode the local user is the admin (returns None)."""
    p = current_principal.get()
    from ..api.deps import get_state

    if not get_state().security.server_mode:
        return None
    if p is None:
        raise LoginRequired()
    if not p.is_admin:
        raise AccessDenied("Administrators only")
    return p
