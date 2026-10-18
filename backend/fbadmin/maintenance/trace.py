"""Trace API sessions: start from templates, stream output in a background thread, parse into events."""

from __future__ import annotations

import collections
import re
import threading
import time
import uuid
from pathlib import Path

from ..config import settings
from . import svc as S

MAX_EVENTS = 5000

TEMPLATES: dict[str, dict] = {
    "slow_queries": {
        "label": "Slow queries",
        "description": "Statements, procedures and functions slower than the threshold, with plan and performance.",
        "params": {"time_threshold": 1000},
        "body": ["log_statement_finish = true", "log_procedure_finish = true", "log_function_finish = true",
                 "print_plan = true", "print_perf = true", "time_threshold = {time_threshold}"],
    },
    "all_statements": {
        "label": "All statements",
        "description": "Every finished statement with its plan (time threshold 0).",
        "params": {"time_threshold": 0},
        "body": ["log_statement_finish = true", "print_plan = true", "time_threshold = {time_threshold}"],
    },
    "errors": {
        "label": "Errors and warnings",
        "description": "Errors and warnings raised by any statement or connection.",
        "params": {},
        "body": ["log_errors = true", "log_warnings = true"],
    },
    "connections": {
        "label": "Connections and transactions",
        "description": "Attach/detach and transaction start/end.",
        "params": {},
        "body": ["log_connections = true", "log_transactions = true"],
    },
    "psql": {
        "label": "PSQL execution",
        "description": "Procedure, function and trigger start/finish with performance counters.",
        "params": {"time_threshold": 0},
        "body": ["log_procedure_start = true", "log_procedure_finish = true", "log_function_start = true",
                 "log_function_finish = true", "log_trigger_start = true", "log_trigger_finish = true",
                 "print_perf = true", "time_threshold = {time_threshold}"],
    },
}


def database_pattern(path: str) -> str:
    """A trace ``database =`` pattern matching the file name of ``path`` in any directory."""
    base = re.split(r"[\\/]", path)[-1]
    # SIMILAR TO metacharacters become "_" (any single character).
    base = re.sub(r"[\[\]()|^\-+*%_?{}\\\s]", "_", base)
    return "%[\\\\/]" + base


def build_config(template: str, database: str | None = None, *, time_threshold: int | None = None,
                 include_filter: str | None = None, exclude_filter: str | None = None,
                 max_sql_length: int = 4000, custom: str | None = None) -> str:
    if template == "custom":
        if not custom or not custom.strip():
            raise ValueError("A custom trace configuration is required")
        return custom
    if template not in TEMPLATES:
        raise ValueError(f"Unknown trace template {template!r}")
    t = TEMPLATES[template]
    params = {**t["params"]}
    if time_threshold is not None:
        params["time_threshold"] = int(time_threshold)
    lines = ["enabled = true"] + [b.format(**params) for b in t["body"]]
    lines.append(f"max_sql_length = {int(max_sql_length)}")
    for key, val in (("include_filter", include_filter), ("exclude_filter", exclude_filter)):
        if val:
            lines.append(f'{key} = "{val}"')
    head = f"database = {database_pattern(database)}" if database else "database"
    return head + "\n{\n" + "\n".join("  " + x for x in lines) + "\n}\n"


_HEADER_RE = re.compile(r"^(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d+)\s+\((\d+):(\w+)\)\s+(.+?)\s*$")
_ATT_RE = re.compile(r"\(ATT_(\d+),\s*([^:,]+)")
_MS_RE = re.compile(r"^\s*(\d+) ms")


def parse_event(seq: int, header: re.Match, body: list[str]) -> dict:
    ev: dict = {"seq": seq, "time": header.group(1), "pid": int(header.group(2)), "type": header.group(4),
                "attachment": None, "user": None, "duration_ms": None, "sql": None, "plan": None,
                "text": "\n".join([header.group(0)] + body).rstrip()}
    sql: list[str] | None = None
    plan: list[str] = []
    in_plan = False
    for line in body:
        s = line.strip()
        if ev["attachment"] is None:
            m = _ATT_RE.search(line)
            if m:
                ev["attachment"], ev["user"] = int(m.group(1)), m.group(2)
        if s.startswith("-----") and sql is None:
            sql = []
            continue
        if sql is not None and ev["sql"] is None:
            if s.startswith("^^^^") or re.match(r"\d+ records? fetched", s) or _MS_RE.match(line):
                ev["sql"] = "\n".join(sql).strip()
                in_plan = s.startswith("^^^^")
                if not in_plan:
                    m = _MS_RE.match(line)
                    if m:
                        ev["duration_ms"] = int(m.group(1))
                continue
            sql.append(line)
            continue
        if in_plan:
            if not s or re.match(r"\d+ records? fetched", s) or _MS_RE.match(line):
                in_plan = False
            else:
                plan.append(line)
                continue
        m = _MS_RE.match(line)
        if m and ev["duration_ms"] is None:
            ev["duration_ms"] = int(m.group(1))
    if sql is not None and ev["sql"] is None:
        ev["sql"] = "\n".join(sql).strip()
    if plan:
        ev["plan"] = "\n".join(plan).strip()
    return ev


def parse_session_list(lines: list[str]) -> list[dict]:
    out: list[dict] = []
    cur: dict | None = None
    for line in lines:
        s = line.strip()
        m = re.match(r"Session ID:\s*(\d+)", s)
        if m:
            cur = {"id": int(m.group(1)), "name": None, "user": None, "date": None, "flags": []}
            out.append(cur)
            continue
        if cur is None or ":" not in s:
            continue
        k, v = s.split(":", 1)
        k, v = k.strip().lower(), v.strip()
        if k == "flags":
            cur["flags"] = [f.strip() for f in v.split(",") if f.strip()]
        elif k in ("name", "user", "date"):
            cur[k] = v or None
    return out


class TraceSession:
    def __init__(self, server_id: str, name: str, config: str, template: str, database: str | None):
        self.id = uuid.uuid4().hex[:12]
        self.server_id = server_id
        self.name = name
        self.config = config
        self.template = template
        self.database = database
        self.session_id: int | None = None
        self.status = "starting"
        self.error: str | None = None
        self.started_at = time.time()
        self.stopped_at: float | None = None
        self.events: collections.deque[dict] = collections.deque(maxlen=MAX_EVENTS)
        self.event_count = 0
        self.line_count = 0
        self.file = settings.data_dir / "traces" / f"{self.id}.log"
        self._lock = threading.Lock()
        self._pending: tuple[re.Match, list[str]] | None = None
        self._stop = threading.Event()
        self.started = threading.Event()

    def summary(self) -> dict:
        return {"id": self.id, "server_id": self.server_id, "name": self.name, "template": self.template,
                "database": self.database, "session_id": self.session_id, "status": self.status,
                "error": self.error, "started_at": self.started_at, "stopped_at": self.stopped_at,
                "event_count": self.event_count, "line_count": self.line_count, "config": self.config}

    # --- output handling -------------------------------------------------------
    def _flush(self) -> None:
        if self._pending is None:
            return
        header, body = self._pending
        self._pending = None
        with self._lock:
            self.event_count += 1
            self.events.append(parse_event(self.event_count, header, body))

    def feed(self, line: str, out) -> None:
        self.line_count += 1
        out.write(line + "\n")
        if self.session_id is None:
            m = re.search(r"Trace session ID (\d+) started", line)
            if m:
                self.session_id = int(m.group(1))
                self.status = "running"
                self.started.set()
                return
        m = _HEADER_RE.match(line)
        if m is None and line.startswith("Error creating trace session"):
            # Configuration errors are reported per database, when it is attached.
            self.error = self.error or "Trace configuration error (see events)"
            stamp = time.strftime("%Y-%m-%dT%H:%M:%S.0000")
            m = _HEADER_RE.match(f"{stamp} (0:0) TRACE_CONFIG_ERROR")
            self._flush()
            self._pending = (m, [line.rstrip()])
            return
        if m:
            self._flush()
            self._pending = (m, [])
        elif self._pending is not None:
            self._pending[1].append(line.rstrip())

    def run(self, service_factory) -> None:
        self.file.parent.mkdir(parents=True, exist_ok=True)
        try:
            with service_factory() as svc, open(self.file, "a", encoding="utf-8") as out:
                S.start(svc, S.Spb(S.ACT_TRACE_START).str(S.TRC_NAME, self.name).str(S.TRC_CFG, self.config))
                while True:
                    line, finished = S.read_line(svc, 1)
                    if finished:
                        break
                    if line is None:
                        self._flush()
                        out.flush()
                        if self._stop.is_set() and self.session_id is None:
                            break
                        continue
                    self.feed(line, out)
                    if self.session_id is None and self.line_count > 0 and "error" in line.lower():
                        self.error = line.strip()
            self._flush()
            self.status = "failed" if self.error and self.session_id is None else "stopped"
        except Exception as exc:  # noqa: BLE001
            self._flush()
            self.status = "failed"
            self.error = str(exc).strip() or exc.__class__.__name__
        finally:
            self.stopped_at = time.time()
            self.started.set()

    def events_after(self, after: int = 0, q: str | None = None, types: list[str] | None = None,
                     limit: int = 1000) -> list[dict]:
        with self._lock:
            evs = [e for e in self.events if e["seq"] > after]
        if types:
            evs = [e for e in evs if any(e["type"].startswith(t) for t in types)]
        if q:
            ql = q.lower()
            evs = [e for e in evs if ql in e["text"].lower()]
        return evs[:limit]


class TraceManager:
    def __init__(self) -> None:
        self.sessions: dict[str, TraceSession] = {}
        self._lock = threading.Lock()

    def start(self, server_id: str, service_factory, *, name: str, config: str, template: str,
              database: str | None, wait: float = 10) -> TraceSession:
        sess = TraceSession(server_id, name, config, template, database)
        with self._lock:
            self.sessions[sess.id] = sess
        threading.Thread(target=sess.run, args=(service_factory,), name=f"trace-{sess.id}", daemon=True).start()
        sess.started.wait(wait)
        return sess

    def get(self, local_id: str) -> TraceSession:
        try:
            return self.sessions[local_id]
        except KeyError:
            raise KeyError(f"Unknown trace session {local_id!r}") from None

    def list(self, server_id: str | None = None) -> list[TraceSession]:
        return sorted((s for s in self.sessions.values() if server_id is None or s.server_id == server_id),
                      key=lambda s: s.started_at, reverse=True)

    def remove(self, local_id: str) -> None:
        sess = self.sessions.pop(local_id, None)
        if sess:
            Path(sess.file).unlink(missing_ok=True)


def stop_session(svc, session_id: int) -> list[str]:
    return S.run(svc, S.Spb(S.ACT_TRACE_STOP).int(S.TRC_ID, session_id))


def server_sessions(svc) -> list[dict]:
    return parse_session_list(S.run(svc, S.Spb(S.ACT_TRACE_LIST)))


_manager = TraceManager()


def get_trace_manager() -> TraceManager:
    return _manager
