"""Opt-in AI assistant: explain plans, suggest indices, convert SQL from other dialects.

Disabled unless ``FBADMIN_AI_PROVIDER`` is set. Only SQL text, plans and table DDL are
sent to the provider — never table data. Providers are pluggable: register a class with
``register_provider`` or via the ``fbadmin.ai_providers`` entry point group.
"""

from __future__ import annotations

import os
from importlib.metadata import entry_points

from .providers import AIProvider, AnthropicProvider, AIUnavailable

_PROVIDERS: dict[str, type[AIProvider]] = {"anthropic": AnthropicProvider}


def register_provider(name: str, cls: type[AIProvider]) -> None:
    _PROVIDERS[name] = cls


def _load_entry_points() -> None:
    try:
        for ep in entry_points(group="fbadmin.ai_providers"):
            _PROVIDERS.setdefault(ep.name, ep.load())
    except Exception:  # noqa: BLE001 - a broken plugin must not break the app
        pass


def get_provider() -> AIProvider | None:
    name = os.environ.get("FBADMIN_AI_PROVIDER", "").strip().lower()
    if not name or name == "none":
        return None
    _load_entry_points()
    cls = _PROVIDERS.get(name)
    if cls is None:
        raise AIUnavailable(f"Unknown AI provider {name!r}; available: {', '.join(sorted(_PROVIDERS))}")
    return cls()


__all__ = ["AIProvider", "AIUnavailable", "AnthropicProvider", "get_provider", "register_provider"]
