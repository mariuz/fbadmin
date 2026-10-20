"""Saved PSQL test cases, persisted in ``<data dir>/psql_tests.json`` (file mode 0600).

A test case belongs to one routine of one registered database: input values (text, ``null`` for
NULL) and the expected outcome, either rows (``expect.rows``: a list of rows, each a list of values;
a function returns one row with its result) or an error (``expect.error``: text the error message
must contain, empty for any error).
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


def _id() -> str:
    return uuid.uuid4().hex[:10]


class Expect(BaseModel):
    rows: list[list[Any]] | None = None
    error: str | None = None


class TestCase(BaseModel):
    id: str = Field(default_factory=_id)
    server_id: str
    database_id: str
    otype: Literal["procedures", "functions"]
    routine: str
    name: str
    inputs: dict[str, str | None] = Field(default_factory=dict)
    expect: Expect = Field(default_factory=Expect)
    created_at: float = Field(default_factory=time.time)
    updated_at: float = Field(default_factory=time.time)
    last_status: str | None = None  # pass / fail / error
    last_run_at: float | None = None


class TestStore:
    def __init__(self, data_dir: Path | None = None):
        self.dir = Path(data_dir or settings.data_dir)
        self.file = self.dir / "psql_tests.json"
        self._lock = threading.RLock()
        self.tests: dict[str, TestCase] = {}
        if self.file.exists():
            data = json.loads(self.file.read_text())
            self.tests = {t["id"]: TestCase(**t) for t in data.get("tests", [])}

    def _save(self) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        tmp = self.file.with_suffix(".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump({"tests": [t.model_dump() for t in self.tests.values()]}, f, indent=1)
        os.replace(tmp, self.file)

    def list(self, server_id: str, database_id: str, otype: str | None = None,
             routine: str | None = None) -> list[TestCase]:
        with self._lock:
            out = [t for t in self.tests.values() if t.server_id == server_id and t.database_id == database_id
                   and (otype is None or t.otype == otype) and (routine is None or t.routine == routine)]
        return sorted(out, key=lambda t: (t.otype, t.routine, t.created_at))

    def get(self, server_id: str, database_id: str, test_id: str) -> TestCase:
        t = self.tests.get(test_id)
        if t is None or t.server_id != server_id or t.database_id != database_id:
            raise KeyError(f"Unknown test {test_id!r}")
        return t

    def add(self, t: TestCase) -> TestCase:
        if not t.name.strip():
            raise ValueError("A test needs a name")
        with self._lock:
            self.tests[t.id] = t
            self._save()
        return t

    def update(self, server_id: str, database_id: str, test_id: str, changes: dict[str, Any]) -> TestCase:
        with self._lock:
            cur = self.get(server_id, database_id, test_id)
            data = cur.model_dump()
            for k in ("name", "inputs", "expect"):
                if changes.get(k) is not None:
                    data[k] = changes[k]
            data["updated_at"] = time.time()
            new = TestCase(**data)
            if not new.name.strip():
                raise ValueError("A test needs a name")
            self.tests[test_id] = new
            self._save()
            return new

    def record(self, test_id: str, status: str) -> None:
        with self._lock:
            t = self.tests.get(test_id)
            if t is not None:
                t.last_status, t.last_run_at = status, time.time()
                self._save()

    def delete(self, server_id: str, database_id: str, test_id: str) -> None:
        with self._lock:
            self.get(server_id, database_id, test_id)
            del self.tests[test_id]
            self._save()


_store: TestStore | None = None
_store_dir: Path | None = None


def get_store() -> TestStore:
    """One store per data directory (tests switch the data directory)."""
    global _store, _store_dir
    if _store is None or _store_dir != Path(settings.data_dir):
        _store_dir = Path(settings.data_dir)
        _store = TestStore(_store_dir)
    return _store
