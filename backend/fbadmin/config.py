"""Runtime configuration, read from environment variables."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


def _default_data_dir() -> Path:
    env = os.environ.get("FBADMIN_DATA_DIR")
    if env:
        return Path(env)
    return Path.home() / ".fbadmin"


@dataclass
class Settings:
    data_dir: Path = field(default_factory=_default_data_dir)
    host: str = field(default_factory=lambda: os.environ.get("FBADMIN_HOST", "127.0.0.1"))
    port: int = field(default_factory=lambda: int(os.environ.get("FBADMIN_PORT", "5050")))
    # Max rows a single query returns before the result is marked as truncated.
    max_rows: int = field(default_factory=lambda: int(os.environ.get("FBADMIN_MAX_ROWS", "1000")))
    # Idle query-tool sessions are closed after this many seconds.
    session_idle_timeout: int = field(
        default_factory=lambda: int(os.environ.get("FBADMIN_SESSION_IDLE_TIMEOUT", "3600"))
    )

    @property
    def servers_file(self) -> Path:
        return self.data_dir / "servers.json"


settings = Settings()
