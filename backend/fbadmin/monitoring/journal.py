"""Replication journal tracking (milestone 0.8).

Journal and archive segments are files on the database server, which neither the wire protocol
nor the Services API can list. What fbadmin can observe remotely is

* the replication sequence (``RDB$GET_CONTEXT('SYSTEM', 'REPLICATION_SEQUENCE')``): the number of
  the current journal segment on a primary, the last applied one on a replica. Sampled over time it
  gives the segment rate, and compared between both sides the lag and a catch-up estimate;
* the segment events in ``firebird.log`` (with ``verbose_logging = true`` in replication.conf the
  replica logs every applied segment; errors are always logged).
"""

from __future__ import annotations

import re
import threading
import time
from collections import deque
from datetime import datetime
from typing import Any

_MAX_SAMPLES = 720  # e.g. 2 hours at one sample per 10 s


class SequenceTracker:
    """In-memory history of replication sequences per (server, database)."""

    def __init__(self) -> None:
        self._data: dict[tuple[str, str], deque[tuple[float, int]]] = {}
        self._lock = threading.Lock()

    def record(self, key: tuple[str, str], seq: int | None, now: float | None = None) -> None:
        if seq is None:
            return
        now = time.time() if now is None else now
        with self._lock:
            d = self._data.setdefault(key, deque(maxlen=_MAX_SAMPLES))
            if d and d[-1][1] == seq and now - d[-1][0] < 60:
                return  # unchanged: keep at most one sample a minute while idle
            d.append((now, seq))

    def history(self, key: tuple[str, str]) -> list[tuple[float, int]]:
        with self._lock:
            return list(self._data.get(key, ()))

    def rate_per_hour(self, key: tuple[str, str], window: float = 3600) -> float | None:
        """Segments per hour over the last ``window`` seconds (None without two samples)."""
        h = self.history(key)
        if len(h) < 2:
            return None
        end_t, end_s = h[-1]
        start = next(((t, s) for t, s in h if end_t - t <= window), h[0])
        span = end_t - start[0]
        if span <= 0:
            return None
        return max(0.0, (end_s - start[1]) * 3600 / span)


tracker = SequenceTracker()

_TS = re.compile(r"\b(?:Mon|Tue|Wed|Thu|Fri|Sat|Sun) (Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\s+(\d{1,2}) "
                 r"(\d\d:\d\d:\d\d) (\d{4})")
_SEGMENT = re.compile(r"\bsegment\s+(\d+)", re.I)
_ERROR = re.compile(r"\b(ERROR|error|failed|cannot|unable)\b")


def _timestamp(entry: str) -> str | None:
    m = _TS.search(entry)
    if not m:
        return None
    try:
        return datetime.strptime(" ".join(m.groups()), "%b %d %H:%M:%S %Y").isoformat()
    except ValueError:
        return None


def parse_log(entries: list[str]) -> dict[str, Any]:
    """Segment events and errors from the replication entries of firebird.log."""
    events = []
    for e in entries:
        segs = [int(s) for s in _SEGMENT.findall(e)]
        error = bool(_ERROR.search(e))
        if not segs and not error:
            continue
        lines = [ln.strip() for ln in e.splitlines() if ln.strip()]
        events.append({
            "time": _timestamp(e),
            "segment": max(segs) if segs else None,
            "error": error,
            "text": " ".join(lines[1:] if len(lines) > 1 else lines),
        })
    segs = [ev["segment"] for ev in events if ev["segment"] is not None and not ev["error"]]
    return {
        "events": events,
        "last_segment": max(segs) if segs else None,
        "errors": sum(1 for ev in events if ev["error"]),
        "last_error": next((ev for ev in reversed(events) if ev["error"]), None),
    }


def catch_up_seconds(segments_behind: int | None, replica_rate_per_hour: float | None) -> float | None:
    """Time for the replica to apply ``segments_behind`` at its recent rate (None if unknown)."""
    if not segments_behind:
        return 0.0 if segments_behind == 0 else None
    if not replica_rate_per_hour:
        return None
    return segments_behind * 3600 / replica_rate_per_hour
