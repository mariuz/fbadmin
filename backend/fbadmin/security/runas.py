"""Run scheduled jobs as the user who scheduled them (server mode).

In server mode the server registry and the saved Firebird passwords belong to a user, and
saved passwords can only be decrypted with that user's vault key, which is in memory only
while they are signed in. The scheduler thread has no signed-in user, so without help every
scheduled run would fail with "Login required".

When a user creates or changes a schedule, fbadmin issues an internal API token for them
(``security/tokens.py``): limited to the schedule's server registration, without expiry, and
holding a wrapped copy of the user's vault key, exactly like a personal token created from a
signed-in session. The token itself is stored encrypted with the server key (``mfa.server_key``:
``FBADMIN_SECRET_KEY`` or ``<data dir>/secret.key``), never in plain text. When the schedule is
due, the scheduler turns it back into the user's principal, so the run has that user's servers,
passwords and permissions - and stops working when the user is disabled or deleted, or the
token is revoked. Desktop mode needs none of this.
"""

from __future__ import annotations

from typing import Any

from ..config import settings
from .crypto import decrypt, encrypt
from .sessions import Principal, current_principal

AAD = "fbadmin-schedule-run-as"


def _security():
    from ..api.deps import get_state

    sec = get_state().security
    return sec if sec.server_mode else None


def issue(server_id: str | list[str], name: str) -> dict[str, Any] | None:
    """A run-as credential for the current user (limited to the given server registrations), or None
    outside server mode. ``name`` labels the internal token, e.g. "schedule: nightly backup"."""
    sec = _security()
    p = current_principal.get()
    if sec is None or p is None:
        return None
    from ..api.deps import get_state
    from .mfa import server_key

    if p.token_id is not None:
        key = p.token_key  # a token acting for its owner: reuse the key it carries
    else:
        key = get_state().store.user_store(p.user_id).key
    servers = [server_id] if isinstance(server_id, str) else [x for x in server_id if x]
    info, token = sec.tokens.create(p, name[:100], scope="read-write", expires_days=None,
                                    server_ids=servers, vault_key=key, internal=True)
    return {"token_id": info["id"], "secret": encrypt(server_key(settings.data_dir), token, AAD),
            "user": p.username, "vault": key is not None}


def principal(cred: dict[str, Any] | None) -> Principal | None:
    """The scheduling user's principal, or None (desktop mode, revoked token, disabled user)."""
    sec = _security()
    if sec is None or not cred:
        return None
    from .mfa import server_key

    try:
        token = decrypt(server_key(settings.data_dir), cred["secret"], AAD)
    except (KeyError, ValueError):
        return None
    return sec.tokens.authenticate(token, "scheduler")


def revoke(cred: dict[str, Any] | None) -> None:
    sec = _security()
    if sec is None or not cred or cred.get("token_id") is None:
        return
    try:
        sec.tokens.delete(int(cred["token_id"]))
    except KeyError:
        pass


class Unavailable(RuntimeError):
    """The user a background task runs as can no longer act (server mode)."""


class acting_as:  # noqa: N801 - used as a context manager: ``with acting_as(cred): ...``
    """Run the block as the user behind ``cred`` (server mode); a no-op in desktop mode."""

    def __init__(self, cred: dict[str, Any] | None, what: str = "this task"):
        self.cred, self.what, self._tok = cred, what, None

    def __enter__(self) -> Principal | None:
        if _security() is None:
            return None
        p = principal(self.cred)
        if p is None:
            raise Unavailable(f"The user who set up {self.what} is no longer available (disabled, deleted or its "
                              "credential was revoked); save it again to run it as yourself")
        self._tok = current_principal.set(p)
        return p

    def __exit__(self, *exc: object) -> None:
        if self._tok is not None:
            current_principal.reset(self._tok)
