"""Exceptions mapped to JSON error responses by ``security.install``."""

from __future__ import annotations


class SecurityError(Exception):
    def __init__(self, status: int, detail: str, code: str, headers: dict[str, str] | None = None):
        super().__init__(detail)
        self.status = status
        self.detail = detail
        self.code = code
        self.headers = headers or {}


class AccessDenied(SecurityError):
    def __init__(self, detail: str = "Not allowed"):
        super().__init__(403, detail, "forbidden")


class LoginRequired(SecurityError):
    def __init__(self, detail: str = "Login required"):
        super().__init__(401, detail, "auth_required")


class MasterPasswordRequired(SecurityError):
    """Saving or using a saved password needs the (locked) vault."""

    def __init__(self, server_id: str | None = None):
        super().__init__(401, "Enter your master password to unlock saved passwords", "master_password_required")
        self.server_id = server_id
