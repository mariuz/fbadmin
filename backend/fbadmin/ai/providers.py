from __future__ import annotations

import os


class AIUnavailable(Exception):
    """The assistant is not configured or the provider failed."""


class AIProvider:
    """Interface for assistant backends."""

    name = "base"

    def complete(self, system: str, prompt: str) -> str:  # pragma: no cover - interface
        raise NotImplementedError

    def describe(self) -> dict:
        return {"provider": self.name}


class AnthropicProvider(AIProvider):
    """Claude via the official Anthropic SDK (``pip install fbadmin[ai]``).

    Credentials come from the SDK's normal resolution (ANTHROPIC_API_KEY, ANTHROPIC_AUTH_TOKEN,
    an ``ant auth login`` profile, ...). Model: FBADMIN_AI_MODEL (default claude-opus-5-5).
    """

    name = "anthropic"

    def __init__(self) -> None:
        try:
            import anthropic
        except ImportError as exc:
            raise AIUnavailable("The 'anthropic' package is not installed: pip install 'fbadmin[ai]'") from exc
        self._anthropic = anthropic
        self.model = os.environ.get("FBADMIN_AI_MODEL", "claude-opus-5-5")
        self.effort = os.environ.get("FBADMIN_AI_EFFORT", "high")
        self.client = anthropic.Anthropic()

    def describe(self) -> dict:
        return {"provider": self.name, "model": self.model}

    def complete(self, system: str, prompt: str) -> str:
        a = self._anthropic
        try:
            # Server-side fallback: if a safety classifier declines, the API re-runs the
            # request on a fallback model inside the same call.
            with self.client.beta.messages.stream(
                model=self.model,
                max_tokens=16000,
                system=system,
                output_config={"effort": self.effort},
                betas=["server-side-fallback-2026-07-01"],
                fallbacks="default",
                messages=[{"role": "user", "content": prompt}],
            ) as stream:
                msg = stream.get_final_message()
        except a.AuthenticationError as exc:
            raise AIUnavailable("Anthropic credentials are missing or invalid (set ANTHROPIC_API_KEY)") from exc
        except a.RateLimitError as exc:
            raise AIUnavailable("Rate limited by the Anthropic API; try again shortly") from exc
        except a.APIStatusError as exc:
            raise AIUnavailable(f"Anthropic API error {exc.status_code}: {exc.message}") from exc
        except a.APIConnectionError as exc:
            raise AIUnavailable("Cannot reach the Anthropic API (network)") from exc
        if msg.stop_reason == "refusal":
            raise AIUnavailable("The model declined this request")
        text = "".join(b.text for b in msg.content if b.type == "text").strip()
        if msg.stop_reason == "max_tokens":
            text += "\n\n(answer truncated)"
        return text
