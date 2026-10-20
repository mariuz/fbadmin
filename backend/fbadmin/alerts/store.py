"""Alert channels and rules, persisted in ``<data dir>/alerts.json`` (file mode 0600).

Secret settings (SMTP password, webhook URLs, signing secrets) are never returned by the API: they
are masked as ``********``, and sending the mask back on update keeps the stored value. Any setting
may be written as ``env:NAME`` to read it from the environment when it is used, so secrets can stay
out of the file (Docker, Kubernetes).
"""

from __future__ import annotations

import json
import os
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field

from ..config import settings

MASK = "********"

ChannelKind = Literal["email", "slack", "webhook"]

#: settings per channel kind: (name, required, secret)
CHANNEL_FIELDS: dict[str, list[tuple[str, bool, bool]]] = {
    "email": [("host", True, False), ("port", False, False), ("security", False, False),
              ("username", False, False), ("password", False, True), ("from_addr", True, False),
              ("to", True, False)],
    "slack": [("webhook_url", True, True)],
    "webhook": [("url", True, True), ("secret", False, True)],
}

#: event types: (id, kind, default threshold, description)
#: kind "event" is raised by fbadmin itself; "condition" is checked periodically on a database.
EVENTS: list[dict[str, Any]] = [
    {"id": "schedule.failed", "kind": "event", "threshold": None,
     "label": "A scheduled job failed (backup, sweep, validation, verification or upload)"},
    {"id": "backup.verify_failed", "kind": "event", "threshold": None,
     "label": "A backup could not be restored in the test restore"},
    {"id": "validation.errors", "kind": "event", "threshold": None,
     "label": "Online validation found errors"},
    {"id": "upload.failed", "kind": "event", "threshold": None,
     "label": "A backup could not be uploaded to cloud storage"},
    {"id": "oat_gap", "kind": "condition", "threshold": 10000, "unit": "transactions",
     "label": "Next transaction - OAT gap above the threshold (garbage collection is held back)"},
    {"id": "long_transaction", "kind": "condition", "threshold": 3600, "unit": "seconds",
     "label": "A transaction has been running longer than the threshold"},
    {"id": "replication_lag", "kind": "condition", "threshold": 5, "unit": "segments",
     "label": "The replica is more journal segments behind the primary than the threshold"},
    {"id": "database.unreachable", "kind": "condition", "threshold": None,
     "label": "fbadmin cannot connect to the database"},
]
EVENT_IDS = {e["id"] for e in EVENTS}
CONDITIONS = {e["id"] for e in EVENTS if e["kind"] == "condition"}


def _id() -> str:
    return uuid.uuid4().hex[:10]


class Channel(BaseModel):
    id: str = Field(default_factory=_id)
    name: str
    kind: ChannelKind
    enabled: bool = True
    settings: dict[str, Any] = Field(default_factory=dict)
    created_at: float = Field(default_factory=time.time)


class Rule(BaseModel):
    id: str = Field(default_factory=_id)
    name: str
    event: str
    enabled: bool = True
    server_id: str | None = None      # None: any server (events only)
    database_id: str | None = None    # None: any database of the server (events only)
    threshold: float | None = None    # conditions; None uses the event's default
    peer_server_id: str | None = None  # replication_lag: the other side
    peer_database_id: str | None = None
    channels: list[str] = Field(default_factory=list)
    cooldown_minutes: int = 60        # do not repeat the same alert more often than this
    notify_resolved: bool = True      # send a message when a condition clears
    # server mode: condition checks run as the admin who saved the rule (security/runas.py)
    run_as: dict | None = None
    created_at: float = Field(default_factory=time.time)


def resolve_value(value: Any) -> Any:
    """``env:NAME`` -> the environment variable's value."""
    if isinstance(value, str) and value.startswith("env:"):
        name = value[4:].strip()
        if name not in os.environ:
            raise ValueError(f"Environment variable {name} is not set")
        return os.environ[name]
    return value


def _validate_channel(c: Channel) -> None:
    for name, required, _secret in CHANNEL_FIELDS[c.kind]:
        if required and not str(c.settings.get(name) or "").strip():
            raise ValueError(f"{c.kind} channel: '{name}' is required")
    if c.kind == "email":
        sec = c.settings.get("security") or "starttls"
        if sec not in ("starttls", "ssl", "none"):
            raise ValueError("email channel: security must be starttls, ssl or none")
        port = c.settings.get("port")
        if port not in (None, "") and not str(port).isdigit():
            raise ValueError("email channel: port must be a number")
    for key in ("webhook_url", "url"):
        v = str(c.settings.get(key) or "")
        if v and not v.startswith("env:") and not v.startswith(("https://", "http://")):
            raise ValueError(f"{c.kind} channel: '{key}' must be an http(s) URL or env:NAME")


def _validate_rule(r: Rule, channels: dict[str, Channel]) -> None:
    if r.event not in EVENT_IDS:
        raise ValueError(f"Unknown event {r.event!r}")
    if r.event in CONDITIONS and not (r.server_id and r.database_id):
        raise ValueError("A condition rule needs a server and a database")
    if r.database_id and not r.server_id:
        raise ValueError("A database needs its server")
    if r.event == "replication_lag" and not (r.peer_server_id and r.peer_database_id):
        raise ValueError("Replication lag needs the other side (peer server and database)")
    for cid in r.channels:
        if cid not in channels:
            raise ValueError(f"Unknown channel {cid!r}")
    if r.cooldown_minutes < 0:
        raise ValueError("Cooldown must be 0 or more minutes")


class AlertStore:
    def __init__(self, data_dir: Path | None = None):
        self.dir = Path(data_dir or settings.data_dir)
        self.file = self.dir / "alerts.json"
        self._lock = threading.RLock()
        self.channels: dict[str, Channel] = {}
        self.rules: dict[str, Rule] = {}
        if self.file.exists():
            data = json.loads(self.file.read_text())
            self.channels = {c["id"]: Channel(**c) for c in data.get("channels", [])}
            self.rules = {r["id"]: Rule(**r) for r in data.get("rules", [])}

    def _save(self) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        tmp = self.file.with_suffix(".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump({"channels": [c.model_dump() for c in self.channels.values()],
                       "rules": [r.model_dump() for r in self.rules.values()]}, f, indent=1)
        os.replace(tmp, self.file)

    # ------------------------------------------------------------- channels
    def view_channel(self, c: Channel) -> dict:
        d = c.model_dump()
        s = dict(d["settings"])
        for name, _req, secret in CHANNEL_FIELDS[c.kind]:
            if secret:
                has = bool(s.get(name))
                s[name] = MASK if has else ""
                d[f"has_{name}"] = has
        d["settings"] = s
        return d

    def list_channels(self) -> list[dict]:
        with self._lock:
            return [self.view_channel(c) for c in sorted(self.channels.values(), key=lambda c: c.created_at)]

    def get_channel(self, cid: str) -> Channel:
        try:
            return self.channels[cid]
        except KeyError:
            raise KeyError(f"Unknown channel {cid!r}") from None

    def add_channel(self, c: Channel) -> Channel:
        _validate_channel(c)
        with self._lock:
            self.channels[c.id] = c
            self._save()
        return c

    def update_channel(self, cid: str, changes: dict) -> Channel:
        with self._lock:
            cur = self.get_channel(cid)
            data = cur.model_dump()
            if "settings" in changes and changes["settings"] is not None:
                new_settings = dict(changes["settings"])
                kind = changes.get("kind") or cur.kind
                for name, _req, secret in CHANNEL_FIELDS[kind]:
                    if secret and new_settings.get(name) in (MASK, None) and kind == cur.kind:
                        new_settings[name] = cur.settings.get(name, "")
                data["settings"] = new_settings
            for k in ("name", "kind", "enabled"):
                if changes.get(k) is not None:
                    data[k] = changes[k]
            new = Channel(**{**data, "id": cid})
            _validate_channel(new)
            self.channels[cid] = new
            self._save()
            return new

    def remove_channel(self, cid: str) -> None:
        with self._lock:
            self.get_channel(cid)
            del self.channels[cid]
            for r in self.rules.values():
                if cid in r.channels:
                    r.channels = [x for x in r.channels if x != cid]
            self._save()

    # ---------------------------------------------------------------- rules
    def list_rules(self) -> list[dict]:
        with self._lock:
            return [r.model_dump() for r in sorted(self.rules.values(), key=lambda r: r.created_at)]

    def get_rule(self, rid: str) -> Rule:
        try:
            return self.rules[rid]
        except KeyError:
            raise KeyError(f"Unknown rule {rid!r}") from None

    def add_rule(self, r: Rule) -> Rule:
        with self._lock:
            _validate_rule(r, self.channels)
            self.rules[r.id] = r
            self._save()
        return r

    def update_rule(self, rid: str, changes: dict) -> Rule:
        with self._lock:
            cur = self.get_rule(rid)
            new = Rule(**{**cur.model_dump(), **changes, "id": rid})
            _validate_rule(new, self.channels)
            self.rules[rid] = new
            self._save()
            return new

    def remove_rule(self, rid: str) -> None:
        with self._lock:
            self.get_rule(rid)
            del self.rules[rid]
            self._save()


_store: AlertStore | None = None
_store_lock = threading.Lock()


def get_store() -> AlertStore:
    global _store
    with _store_lock:
        if _store is None or _store.dir != Path(settings.data_dir):
            _store = AlertStore()
        return _store
