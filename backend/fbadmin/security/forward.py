"""Audit log forwarding (roadmap 2.2): send every audit entry to syslog or a webhook.

The audit log in fbadmin's SQLite database stays the source: each destination has a cursor (the id of the last
entry it accepted), and a background thread sends what is after it, in order. A destination that is down
gets the entries later, with a growing pause between attempts (up to 5 minutes); nothing is lost while it
is down or while fbadmin is stopped. Only one fbadmin process sharing a data directory forwards (a file lock).

* **syslog**: RFC 5424 messages over UDP, TCP or TLS (TCP and TLS use octet counting, RFC 6587). The
  structured data carries who, what and where; the message is the entry as JSON.
* **webhook**: POST of a JSON batch ``{"source": "fbadmin", "host": ..., "entries": [...]}``; with a secret,
  ``X-Fbadmin-Signature: sha256=<HMAC of the body>`` as for alert webhooks, and an optional bearer token.

Destinations are created by administrators (``<data dir>/audit_forwarding.json``, secrets masked in the
API) or from the environment: FBADMIN_AUDIT_SYSLOG=udp://host:514 (tcp://, tls://), FBADMIN_AUDIT_WEBHOOK=URL
with FBADMIN_AUDIT_WEBHOOK_SECRET and FBADMIN_AUDIT_WEBHOOK_TOKEN.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import socket
import ssl
import threading
import time
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, Field, field_validator

from ..config import settings

log = logging.getLogger("fbadmin.audit.forward")
MASK = "********"
SECRETS = ("secret", "token")
BATCH = 200
MAX_BACKOFF = 300.0
TIMEOUT = 10.0
FACILITIES = {"kern": 0, "user": 1, "auth": 4, "authpriv": 10, "local0": 16, "local1": 17, "local2": 18,
              "local3": 19, "local4": 20, "local5": 21, "local6": 22, "local7": 23}
CATEGORIES = ("DDL", "DML", "TX", "ACTION", "SECURITY", "OTHER", "QUERY")
SD_ID = "fbadmin@32473"  # 32473: the example enterprise number of RFC 5424 (no number is registered for fbadmin)


class Destination(BaseModel):
    id: str = Field(default_factory=lambda: uuid.uuid4().hex[:10])
    name: str
    kind: Literal["syslog", "webhook"]
    enabled: bool = True
    # syslog
    host: str | None = None
    port: int | None = Field(default=None, ge=1, le=65535)
    protocol: Literal["udp", "tcp", "tls"] = "udp"
    facility: str = "local0"
    tls_verify: bool = True
    ca_file: str | None = None
    # webhook
    url: str | None = None
    secret: str | None = None              # HMAC key (or env:NAME)
    token: str | None = None               # bearer token (or env:NAME)
    # what to send
    categories: list[str] = Field(default_factory=list)  # empty: everything
    failures_only: bool = False
    created_at: float = Field(default_factory=time.time)
    source: Literal["stored", "environment"] = "stored"

    @field_validator("facility")
    @classmethod
    def _fac(cls, v: str) -> str:
        if v not in FACILITIES:
            raise ValueError(f"Facility is one of {', '.join(FACILITIES)}")
        return v

    @field_validator("categories")
    @classmethod
    def _cats(cls, v: list[str]) -> list[str]:
        bad = [c for c in v if c not in CATEGORIES]
        if bad:
            raise ValueError(f"Unknown categories {bad}: use {', '.join(CATEGORIES)}")
        return v

    @field_validator("host", "url", "ca_file", "name")
    @classmethod
    def _line(cls, v: str | None) -> str | None:
        if v is not None and any(c in v for c in "\r\n\0"):
            raise ValueError("A single line")
        return v.strip() if isinstance(v, str) else v

    def check(self) -> None:
        if self.kind == "syslog":
            if not self.host:
                raise ValueError("The syslog host is required")
        else:
            u = urlsplit(self.url or "")
            if u.scheme not in ("http", "https") or not u.netloc:
                raise ValueError("The webhook URL must be http:// or https://")


def resolve(v: str | None) -> str | None:
    if v and v.startswith("env:"):
        name = v[4:].strip()
        if name not in os.environ:
            raise ValueError(f"Environment variable {name} is not set")
        return os.environ[name]
    return v or None


# ------------------------------------------------------------------ formats
def entry(row: dict[str, Any]) -> dict[str, Any]:
    """An audit row as sent: with an ISO time and only the fields that are set."""
    out = {"id": row["id"], "time": datetime.fromtimestamp(row["ts"], timezone.utc).isoformat(timespec="milliseconds"),
           "ts": row["ts"], "user": row.get("username"), "client_ip": row.get("client_ip"),
           "token": row.get("token"), "category": row.get("category"), "statement": row.get("statement"),
           "success": bool(row.get("success")), "error": row.get("error"), "row_count": row.get("row_count"),
           "elapsed_ms": row.get("elapsed_ms"), "server": row.get("server_name"), "server_host": row.get("server_host"),
           "server_id": row.get("server_id"), "database": row.get("db_name"), "database_path": row.get("db_path"),
           "database_id": row.get("db_id")}
    return {k: v for k, v in out.items() if v is not None}


def _sd(v: Any) -> str:
    return str(v).replace("\\", "\\\\").replace('"', '\\"').replace("]", "\\]")


def syslog_message(e: dict[str, Any], facility: str, hostname: str | None = None, max_len: int = 8192) -> bytes:
    """An RFC 5424 message: ``<PRI>1 TIMESTAMP HOST fbadmin PID AUDIT [fbadmin@32473 ...] {json}``."""
    severity = 5 if e.get("success", True) else 4  # notice, warning
    pri = FACILITIES[facility] * 8 + severity
    host = (hostname or socket.gethostname() or "-").split(".")[0][:255] or "-"
    params = " ".join(f'{k}="{_sd(e[k])}"' for k in ("id", "user", "category", "success", "server", "database",
                                                     "client_ip", "token") if k in e)
    head = f"<{pri}>1 {e['time']} {host} fbadmin {os.getpid()} AUDIT [{SD_ID} {params}] "
    body = json.dumps(e, ensure_ascii=False, default=str)
    msg = (head + body).encode("utf-8")
    if len(msg) > max_len:  # keep one entry in one datagram: shorten the statement
        cut = dict(e)
        room = max(200, len(e.get("statement") or "") - (len(msg) - max_len) - 32)
        cut["statement"] = (e.get("statement") or "")[:room] + " …"
        cut["truncated"] = True
        msg = (head + json.dumps(cut, ensure_ascii=False, default=str)).encode("utf-8")[:max_len]
    return msg


def webhook_body(entries: list[dict[str, Any]]) -> bytes:
    return json.dumps({"source": "fbadmin", "host": socket.gethostname(), "entries": entries},
                      ensure_ascii=False, default=str).encode("utf-8")


def signature(secret: str, body: bytes) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


# ------------------------------------------------------------------ transports
def send_syslog(d: Destination, entries: list[dict[str, Any]]) -> str:
    port = d.port or (6514 if d.protocol == "tls" else 514)
    msgs = [syslog_message(e, d.facility, max_len=8192 if d.protocol == "udp" else 65536) for e in entries]
    if d.protocol == "udp":
        with socket.socket(socket.AF_INET6 if ":" in (d.host or "") else socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.settimeout(TIMEOUT)
            for m in msgs:
                s.sendto(m, (d.host, port))
        return f"{len(msgs)} message(s) to udp://{d.host}:{port}"
    raw = socket.create_connection((d.host, port), timeout=TIMEOUT)
    try:
        conn: socket.socket = raw
        if d.protocol == "tls":
            ctx = ssl.create_default_context(cafile=d.ca_file or None)
            if not d.tls_verify:
                ctx.check_hostname = False
                ctx.verify_mode = ssl.CERT_NONE
            conn = ctx.wrap_socket(raw, server_hostname=d.host)
        conn.sendall(b"".join(str(len(m)).encode() + b" " + m for m in msgs))  # RFC 6587 octet counting
        if d.protocol == "tls":
            try:
                conn.unwrap()
            except (OSError, ssl.SSLError):
                pass
    finally:
        raw.close()
    return f"{len(msgs)} message(s) to {d.protocol}://{d.host}:{port}"


def send_webhook(d: Destination, entries: list[dict[str, Any]]) -> str:
    body = webhook_body(entries)
    headers = {"Content-Type": "application/json", "User-Agent": "fbadmin-audit"}
    secret, token = resolve(d.secret), resolve(d.token)
    if secret:
        headers["X-Fbadmin-Signature"] = signature(secret, body)
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(d.url or "", data=body, method="POST", headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:  # noqa: S310 - administrator's URL
            return f"{len(entries)} entr{'y' if len(entries) == 1 else 'ies'}: HTTP {r.status}"
    except urllib.error.HTTPError as e:
        detail = e.read(300).decode("utf-8", "replace").strip()
        raise RuntimeError(f"HTTP {e.code}{': ' + detail if detail else ''}") from None
    except urllib.error.URLError as e:
        raise RuntimeError(f"Cannot reach {d.url}: {e.reason}") from None


def send(d: Destination, entries: list[dict[str, Any]]) -> str:
    return (send_syslog if d.kind == "syslog" else send_webhook)(d, entries)


def wanted(d: Destination, row: dict[str, Any]) -> bool:
    if d.failures_only and row.get("success"):
        return False
    return not d.categories or row.get("category") in d.categories


# ------------------------------------------------------------------ destinations and their cursors
def env_destinations() -> list[Destination]:
    out = []
    sl = os.environ.get("FBADMIN_AUDIT_SYSLOG", "").strip()
    if sl:
        u = urlsplit(sl if "://" in sl else "udp://" + sl)
        if u.scheme not in ("udp", "tcp", "tls") or not u.hostname:
            raise ValueError("FBADMIN_AUDIT_SYSLOG is udp://host:514, tcp://host:514 or tls://host:6514")
        q = dict(p.split("=", 1) for p in u.query.split("&") if "=" in p)
        out.append(Destination(id="env-syslog", name="FBADMIN_AUDIT_SYSLOG", kind="syslog", host=u.hostname,
                               port=u.port, protocol=u.scheme, facility=q.get("facility", "local0"),
                               tls_verify=q.get("verify", "1") not in ("0", "false", "no"), source="environment"))
    wh = os.environ.get("FBADMIN_AUDIT_WEBHOOK", "").strip()
    if wh:
        out.append(Destination(id="env-webhook", name="FBADMIN_AUDIT_WEBHOOK", kind="webhook", url=wh,
                               secret="env:FBADMIN_AUDIT_WEBHOOK_SECRET" if os.environ.get("FBADMIN_AUDIT_WEBHOOK_SECRET") else None,
                               token="env:FBADMIN_AUDIT_WEBHOOK_TOKEN" if os.environ.get("FBADMIN_AUDIT_WEBHOOK_TOKEN") else None,
                               source="environment"))
    for d in out:
        d.check()
    return out


class Store:
    """Destinations (stored ones) and the state of every destination (cursor, last result)."""

    def __init__(self, data_dir: Path | None = None):
        self.file = Path(data_dir or settings.data_dir) / "audit_forwarding.json"
        self._lock = threading.RLock()

    def _load(self) -> dict[str, Any]:
        if not self.file.exists():
            return {"destinations": [], "state": {}}
        return json.loads(self.file.read_text())

    def _save(self, d: dict[str, Any]) -> None:
        self.file.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.file.with_suffix(".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump(d, f, indent=1)
        os.replace(tmp, self.file)

    def destinations(self) -> list[Destination]:
        with self._lock:
            stored = [Destination(**x) for x in self._load()["destinations"]]
        try:
            env = env_destinations()
        except ValueError as e:
            log.error("%s", e)
            env = []
        return env + stored

    def get(self, did: str) -> Destination:
        for d in self.destinations():
            if d.id == did:
                return d
        raise KeyError(f"Unknown audit destination {did!r}")

    def add(self, d: Destination, cursor: int) -> Destination:
        d.check()
        with self._lock:
            data = self._load()
            data["destinations"].append(d.model_dump())
            data["state"][d.id] = {"cursor": cursor}
            self._save(data)
        return d

    def update(self, did: str, changes: dict[str, Any]) -> Destination:
        with self._lock:
            data = self._load()
            for i, x in enumerate(data["destinations"]):
                if x["id"] == did:
                    for k in SECRETS:
                        if changes.get(k) == MASK:
                            changes.pop(k)
                    new = Destination(**{**x, **changes, "id": did, "source": "stored"})
                    new.check()
                    data["destinations"][i] = new.model_dump()
                    self._save(data)
                    return new
        raise KeyError(f"Unknown audit destination {did!r} (destinations from the environment can't be changed)")

    def remove(self, did: str) -> None:
        with self._lock:
            data = self._load()
            before = len(data["destinations"])
            data["destinations"] = [x for x in data["destinations"] if x["id"] != did]
            if len(data["destinations"]) == before:
                raise KeyError(f"Unknown audit destination {did!r}")
            data["state"].pop(did, None)
            self._save(data)

    def state(self, did: str) -> dict[str, Any]:
        with self._lock:
            return dict(self._load()["state"].get(did) or {})

    def set_state(self, did: str, **changes: Any) -> None:
        with self._lock:
            data = self._load()
            data["state"][did] = {**(data["state"].get(did) or {}), **changes}
            self._save(data)


def view(d: Destination, state: dict[str, Any], newest: int) -> dict[str, Any]:
    out = d.model_dump()
    for k in SECRETS:
        out[f"has_{k}"] = bool(out.get(k))
        if out.get(k) and not str(out[k]).startswith("env:"):
            out[k] = MASK
    cursor = state.get("cursor")
    out.update(cursor=cursor, backlog=max(0, newest - cursor) if cursor is not None else None,
               last_ok_at=state.get("last_ok_at"), last_error=state.get("last_error"),
               last_error_at=state.get("last_error_at"), sent=state.get("sent", 0), last_result=state.get("last_result"))
    return out


# ------------------------------------------------------------------ the forwarder
class Forwarder:
    def __init__(self, db, store: Store, interval: float = 5.0):
        self.db, self.store, self.interval = db, store, interval
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._retry_at: dict[str, float] = {}
        self._backoff: dict[str, float] = {}
        self._lockfile = None
        self._run = threading.Lock()

    def notify(self) -> None:
        self._wake.set()

    def _leader(self) -> bool:
        """Only one process per data directory forwards (workers share the cursors)."""
        if self._lockfile is not None:
            return True
        try:
            import fcntl
        except ImportError:  # Windows: one process
            self._lockfile = True
            return True
        path = self.store.file.with_suffix(".lock")
        path.parent.mkdir(parents=True, exist_ok=True)
        f = open(path, "a+")  # noqa: SIM115 - kept open while this process forwards
        try:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            f.close()
            return False
        self._lockfile = f
        return True

    def run_once(self, now: float | None = None) -> dict[str, str]:
        """Send what each destination hasn't got yet (one batch per destination); returns the results."""
        with self._run:  # the thread and an explicit call never send the same entries twice
            return self._run_once(now or time.time())

    def _run_once(self, now: float) -> dict[str, str]:
        results: dict[str, str] = {}
        if not self._leader():
            return results
        newest = self.db.max_audit_id()
        for d in self.store.destinations():
            if not d.enabled:
                continue
            st = self.store.state(d.id)
            if "cursor" not in st:  # a destination from the environment, seen for the first time: from now on
                self.store.set_state(d.id, cursor=newest)
                continue
            if self._retry_at.get(d.id, 0) > now:
                continue
            rows = self.db.audit_after(st["cursor"], BATCH)
            if not rows:
                continue
            todo = [entry(r) for r in rows if wanted(d, r)]
            try:
                res = send(d, todo) if todo else "filtered"
            except Exception as e:  # noqa: BLE001 - the destination is down: try again later
                wait = min(MAX_BACKOFF, max(self.interval, self._backoff.get(d.id, self.interval / 2) * 2))
                self._backoff[d.id] = wait
                self._retry_at[d.id] = now + wait
                msg = str(e).strip() or e.__class__.__name__
                self.store.set_state(d.id, last_error=msg, last_error_at=now)
                results[d.id] = f"error: {msg}"
                log.warning("audit forwarding to %s failed (retry in %.0f s): %s", d.name, wait, msg)
                continue
            self._backoff.pop(d.id, None)
            self._retry_at.pop(d.id, None)
            self.store.set_state(d.id, cursor=rows[-1]["id"], last_ok_at=now, last_error=None, last_result=res,
                                 sent=st.get("sent", 0) + len(todo))
            results[d.id] = res
            if len(rows) == BATCH:
                self._wake.set()  # more waiting
        return results

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return

        def loop() -> None:
            while not self._stop.is_set():
                try:
                    self.run_once()
                except Exception:  # noqa: BLE001
                    log.exception("audit forwarding")
                self._wake.wait(self.interval)
                self._wake.clear()

        self._thread = threading.Thread(target=loop, name="fbadmin-audit-forward", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()

    def retry_now(self, did: str) -> None:
        self._retry_at.pop(did, None)
        self._backoff.pop(did, None)
        self._wake.set()
