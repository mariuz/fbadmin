"""Server mode on a fresh data directory: several processes starting at once (workers, replicas on one
volume) and concurrent setup requests create exactly one first administrator."""

from __future__ import annotations

import os
import subprocess
import sys
import threading
import time
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]

CHILD = r"""
import sys, time
t = float(sys.argv[1])
while time.time() < t:
    pass
from fbadmin.api.deps import get_state
st = get_state()
print("users", st.security.db.user_count())
"""


def test_processes_starting_together_create_one_admin(tmp_path):
    env = {k: v for k, v in os.environ.items() if not k.startswith("FBADMIN_") or k.startswith("FBADMIN_TEST")}
    env.update(PYTHONPATH=str(BACKEND), FBADMIN_DATA_DIR=str(tmp_path), FBADMIN_AUTH_CONFIG=str(tmp_path / "auth.json"),
               FBADMIN_SERVER_MODE="1", FBADMIN_ADMIN_USERNAME="root", FBADMIN_ADMIN_PASSWORD="a-long-password",
               FBADMIN_PLUGINS="off")
    start = time.time() + 3  # all import first, then race on the empty user table
    procs = [subprocess.Popen([sys.executable, "-c", CHILD, str(start)], env=env, stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE) for _ in range(6)]
    results = [p.communicate(timeout=120) for p in procs]
    for p, (out, err) in zip(procs, results, strict=True):
        assert p.returncode == 0, err.decode()[-2000:]
        assert out.decode().strip() == "users 1"


def test_concurrent_setup_requests_create_one_admin(tmp_path, monkeypatch):
    from .test_security import _client

    from fbadmin.api import deps

    with _client(tmp_path, monkeypatch, SERVER_MODE="1") as c:
        codes = []
        barrier = threading.Barrier(4)

        def setup(i: int) -> None:
            barrier.wait()
            r = c.post("/api/auth/setup", json={"username": f"admin{i}", "password": "a-long-password"})
            codes.append(r.status_code)

        threads = [threading.Thread(target=setup, args=(i,)) for i in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(60)
        assert sorted(codes) == [200, 409, 409, 409], codes
        assert deps.get_state().security.db.user_count() == 1
    deps.reset_state()
