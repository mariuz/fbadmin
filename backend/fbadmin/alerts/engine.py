"""Alert engine: match events to rules, apply cool-downs, deliver in the background, keep a history,
and poll condition rules (OAT gap, long transactions, replication lag, reachability)."""

from __future__ import annotations

import json
import logging
import os
import queue
import threading
import time
import uuid
from pathlib import Path
from typing import Any

from ..config import settings
from . import channels as ch
from .store import CONDITIONS, EVENTS, AlertStore, Rule, get_store

log = logging.getLogger("fbadmin.alerts")
HISTORY_KEEP = 500
_DEFAULT_THRESHOLD = {e["id"]: e["threshold"] for e in EVENTS}


def threshold(rule: Rule) -> float | None:
    return rule.threshold if rule.threshold is not None else _DEFAULT_THRESHOLD.get(rule.event)


def _names(server_id: str | None, database_id: str | None) -> tuple[str | None, str | None]:
    from ..api.deps import get_state

    srv = db = None
    try:
        if server_id:
            srv = get_state().store.get(server_id).name
        if server_id and database_id:
            db = get_state().store.get_database(server_id, database_id).name
    except Exception:  # noqa: BLE001 - an unregistered server/database only loses its display name
        pass
    return srv, db


class Engine:
    def __init__(self, data_dir: Path | None = None):
        self.dir = Path(data_dir or settings.data_dir)
        self.history_file = self.dir / "alert_history.json"
        self._lock = threading.RLock()
        self.history: list[dict] = []
        if self.history_file.exists():
            try:
                self.history = json.loads(self.history_file.read_text()).get("history", [])
            except ValueError:
                self.history = []
        self._last_sent: dict[tuple[str, str], float] = {}
        self._firing: dict[tuple[str, str], dict] = {}
        self.status: dict[str, dict] = {}  # condition rule id -> last check result
        self._queue: queue.Queue[dict | None] = queue.Queue()
        self._worker: threading.Thread | None = None
        self._checker: threading.Thread | None = None
        self._stop = threading.Event()

    @property
    def store(self) -> AlertStore:
        return get_store()

    # -------------------------------------------------------------- history
    def _save_history(self) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        tmp = self.history_file.with_suffix(".tmp")
        tmp.write_text(json.dumps({"history": self.history[-HISTORY_KEEP:]}, default=str))
        os.replace(tmp, self.history_file)

    def recent(self, limit: int = 100) -> list[dict]:
        with self._lock:
            return list(reversed(self.history[-limit:]))

    def clear_history(self) -> None:
        with self._lock:
            self.history = []
            self._save_history()

    # ------------------------------------------------------------- matching
    @staticmethod
    def matches(rule: Rule, event: str, server_id: str | None, database_id: str | None) -> bool:
        if not rule.enabled or rule.event != event:
            return False
        if rule.server_id and rule.server_id != server_id:
            return False
        return not (rule.database_id and rule.database_id != database_id)

    def emit(self, event: str, level: str, title: str, message: str, *, server_id: str | None = None,
             database_id: str | None = None, key: str | None = None, details: dict | None = None) -> list[dict]:
        """Raise an event; every matching rule sends it unless the rule is cooling down."""
        rules = [r for r in self.store.rules.values() if self.matches(r, event, server_id, database_id)]
        return [a for r in rules if (a := self._fire(r, level, title, message, server_id, database_id,
                                                      key or f"{event}:{server_id}:{database_id}", details))]

    def _fire(self, rule: Rule, level: str, title: str, message: str, server_id: str | None,
              database_id: str | None, key: str, details: dict | None, *, force: bool = False) -> dict | None:
        now = time.time()
        k = (rule.id, key)
        with self._lock:
            last = self._last_sent.get(k)
            if not force and last is not None and now - last < rule.cooldown_minutes * 60:
                return None
            self._last_sent[k] = now
        srv, db = _names(server_id, database_id)
        alert = {
            "id": uuid.uuid4().hex[:12], "event": rule.event, "level": level, "title": title, "message": message,
            "server_id": server_id, "database_id": database_id, "server": srv, "database": db,
            "time": now, "time_text": time.strftime("%Y-%m-%d %H:%M:%S %Z", time.localtime(now)),
            "rule": rule.name, "rule_id": rule.id, "key": key, "details": details or {}, "deliveries": [],
        }
        with self._lock:
            self.history.append(alert)
            self._save_history()
        self._queue.put({"alert": alert, "channels": list(rule.channels)})
        self._ensure_worker()
        return alert

    # --------------------------------------------------------- conditions
    def set_condition(self, rule: Rule, bad: bool, level: str, title: str, message: str, value: Any = None,
                      details: dict | None = None) -> None:
        """Record a condition check; alert when it turns (or stays) bad, and once when it clears."""
        key = f"{rule.event}:{rule.server_id}:{rule.database_id}"
        k = (rule.id, key)
        with self._lock:
            self.status[rule.id] = {"checked_at": time.time(), "state": "firing" if bad else "ok", "value": value,
                                    "message": message if bad else None, "error": None}
            was_firing = k in self._firing
        if bad:
            alert = self._fire(rule, level, title, message, rule.server_id, rule.database_id, key, details)
            with self._lock:
                self._firing[k] = alert or self._firing.get(k) or {"time": time.time()}
        elif was_firing:
            with self._lock:
                self._firing.pop(k, None)
                self._last_sent.pop(k, None)
            if rule.notify_resolved:
                self._fire(rule, "resolved", f"Resolved: {title}", message, rule.server_id, rule.database_id, key,
                           details, force=True)

    def check_error(self, rule: Rule, error: str) -> None:
        with self._lock:
            self.status[rule.id] = {"checked_at": time.time(), "state": "error", "value": None, "message": None,
                                    "error": error}

    def check_all(self) -> None:
        from . import checks

        from ..security.runas import Unavailable, acting_as

        for rule in list(self.store.rules.values()):
            if rule.enabled and rule.event in CONDITIONS:
                try:  # server mode: the checker thread has no signed-in user; act as the rule's author
                    with acting_as(rule.run_as, "this alert rule"):
                        checks.run(self, rule)
                except Unavailable as exc:
                    self.check_error(rule, str(exc))

    # ------------------------------------------------------------- delivery
    def _deliver(self, item: dict) -> None:
        alert = item["alert"]
        for cid in item["channels"]:
            c = self.store.channels.get(cid)
            if c is None or not c.enabled:
                continue
            rec: dict[str, Any] = {"channel_id": cid, "channel": c.name, "kind": c.kind, "at": time.time()}
            try:
                rec["result"] = ch.send(c, alert)
                rec["ok"] = True
            except Exception as exc:  # noqa: BLE001
                rec["ok"] = False
                rec["error"] = str(exc) or exc.__class__.__name__
                log.warning("alert %s via %s failed: %s", alert["event"], c.name, rec["error"])
            with self._lock:
                alert["deliveries"].append(rec)
                self._save_history()

    def _ensure_worker(self) -> None:
        if self._worker and self._worker.is_alive():
            return

        def loop() -> None:
            while True:
                item = self._queue.get()
                try:
                    if item is None:
                        return
                    self._deliver(item)
                finally:
                    self._queue.task_done()

        self._worker = threading.Thread(target=loop, name="fbadmin-alerts", daemon=True)
        self._worker.start()

    def flush(self, timeout: float = 30) -> None:
        """Wait until queued alerts are delivered (tests, shutdown)."""
        end = time.time() + timeout
        while self._queue.unfinished_tasks and time.time() < end:
            time.sleep(0.02)

    def send_test(self, channel_id: str) -> dict:
        c = self.store.get_channel(channel_id)
        now = time.time()
        alert = {"id": "test", "event": "test", "level": "info", "title": "Test alert from fbadmin",
                 "message": f"This is a test of the alert channel \"{c.name}\". If you can read it, delivery works.",
                 "server": None, "database": None, "time": now,
                 "time_text": time.strftime("%Y-%m-%d %H:%M:%S %Z", time.localtime(now)), "rule": None,
                 "details": {}}
        return {"ok": True, "result": ch.send(c, alert)}

    # ---------------------------------------------------------------- threads
    def start(self, interval: float = 60) -> None:
        self._ensure_worker()
        if self._checker and self._checker.is_alive():
            return

        def loop() -> None:
            while not self._stop.wait(interval):
                try:
                    self.check_all()
                except Exception:  # noqa: BLE001
                    log.exception("alert checks failed")

        self._checker = threading.Thread(target=loop, name="fbadmin-alert-checks", daemon=True)
        self._checker.start()

    def stop(self) -> None:
        self._stop.set()
        self._queue.put(None)


_engine: Engine | None = None
_engine_lock = threading.Lock()


def get_engine() -> Engine:
    global _engine
    with _engine_lock:
        if _engine is None or _engine.dir != Path(settings.data_dir):
            _engine = Engine()
        return _engine


def emit(event: str, level: str, title: str, message: str, **kw: Any) -> list[dict]:
    """Raise an event; never raises (alerting must not break the caller)."""
    try:
        return get_engine().emit(event, level, title, message, **kw)
    except Exception:  # noqa: BLE001
        log.exception("could not raise alert %s", event)
        return []


def startup() -> Engine:
    global _engine
    with _engine_lock:
        if _engine is not None:
            _engine.stop()
        _engine = Engine()
    _engine.start(float(os.environ.get("FBADMIN_ALERT_INTERVAL", "60")))
    return _engine


def shutdown() -> None:
    if _engine is not None:
        _engine.stop()
