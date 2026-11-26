"""Issue #11: fbadmin.toml configuration file, log file with rotation, graceful shutdown on SIGTERM."""

from __future__ import annotations

import os
import signal
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

import pytest

from fbadmin import configfile


def test_read_and_precedence(tmp_path):
    f = tmp_path / "fbadmin.toml"
    f.write_text('﻿# comment\n[server]\nhost = "0.0.0.0"\nport = 5151\nserver_mode = true\n'
                 'log_file = "logs/fb.log"\n\n[env]\nFBADMIN_MAX_UPLOAD_BYTES = 2147483648\nTZ = "UTC"\n', "utf-8")
    assert configfile.read(f) == {"FBADMIN_HOST": "0.0.0.0", "FBADMIN_PORT": "5151", "FBADMIN_SERVER_MODE": "1",
                                  "FBADMIN_LOG_FILE": "logs/fb.log", "FBADMIN_MAX_UPLOAD_BYTES": "2147483648",
                                  "TZ": "UTC"}
    env = {"FBADMIN_PORT": "6000"}  # the environment wins over the file
    applied = configfile.apply(f, env)
    assert env["FBADMIN_PORT"] == "6000" and "FBADMIN_PORT" not in applied
    assert env["FBADMIN_HOST"] == "0.0.0.0" and env["FBADMIN_CONFIG"] == str(f)
    # top-level keys work like [server]
    f.write_text("port = 7000\n", "utf-8")
    assert configfile.read(f) == {"FBADMIN_PORT": "7000"}


@pytest.mark.parametrize("text, message", [
    ("port = [1, 2]\n", "expected a string"),
    ("[database]\nx = 1\n", "unknown table"),
    ("port = \n", "fbadmin.toml"),
    ('[env]\n"BAD-NAME" = 1\n', "invalid variable name"),
])
def test_invalid_files(tmp_path, text, message):
    f = tmp_path / "fbadmin.toml"
    f.write_text(text, "utf-8")
    with pytest.raises(configfile.ConfigError, match=message):
        configfile.read(f)


def test_default_path(tmp_path):
    assert configfile.default_path({"FBADMIN_DATA_DIR": str(tmp_path)}) is None
    (tmp_path / "fbadmin.toml").write_text("", "utf-8")
    assert configfile.default_path({"FBADMIN_DATA_DIR": str(tmp_path)}) == tmp_path / "fbadmin.toml"
    assert configfile.default_path({"FBADMIN_CONFIG": "/etc/fbadmin/fbadmin.toml"}) == Path("/etc/fbadmin/fbadmin.toml")


def test_cli_uses_the_file_and_flags_win(tmp_path, monkeypatch):
    import fbadmin.__main__ as M

    for k in ("FBADMIN_HOST", "FBADMIN_PORT", "FBADMIN_CONFIG", "FBADMIN_LOG_FILE", "FBADMIN_SSL_CERTFILE",
              "FBADMIN_OPEN_BROWSER", "FBADMIN_PROXY_HEADERS"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("FBADMIN_DATA_DIR", str(tmp_path))
    (tmp_path / "fbadmin.toml").write_text('[server]\nhost = "0.0.0.0"\nport = 5151\nopen_browser = false\n'
                                           'proxy_headers = true\nlog_file = "logs/fbadmin.log"\n', "utf-8")
    calls = []
    monkeypatch.setattr(M.uvicorn, "run", lambda *a, **kw: calls.append(kw))
    monkeypatch.setattr(M.threading, "Timer", lambda *a, **kw: pytest.fail("a browser would open"))
    try:
        M.main(["serve", "--port", "5252"])
        kw = calls[0]
        assert kw["host"] == "0.0.0.0" and kw["port"] == 5252  # the flag wins over the file
        assert kw["proxy_headers"] is True and kw["timeout_graceful_shutdown"] == 10
        handler = kw["log_config"]["handlers"]["file"]
        assert handler["class"] == "logging.handlers.RotatingFileHandler"
        assert handler["filename"] == str(tmp_path / "logs" / "fbadmin.log") and handler["backupCount"] == 5
        # a broken file stops with a message
        (tmp_path / "bad.toml").write_text("port = [", "utf-8")
        with pytest.raises(SystemExit, match="bad.toml"):
            M.main(["--config", str(tmp_path / "bad.toml")])
    finally:
        from fbadmin.config import reload_settings

        for k in ("FBADMIN_HOST", "FBADMIN_PORT", "FBADMIN_CONFIG", "FBADMIN_LOG_FILE", "FBADMIN_OPEN_BROWSER",
                  "FBADMIN_PROXY_HEADERS"):
            os.environ.pop(k, None)
        reload_settings()


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.mark.skipif(sys.platform == "win32", reason="SIGTERM is POSIX")
def test_service_run_logs_to_file_and_stops_on_sigterm(tmp_path):
    port = _free_port()
    (tmp_path / "fbadmin.toml").write_text(f'[server]\nport = {port}\nopen_browser = false\n'
                                           'log_file = "logs/fbadmin.log"\n', "utf-8")
    env = {k: v for k, v in os.environ.items() if not k.startswith("FBADMIN_")} | {"FBADMIN_DATA_DIR": str(tmp_path)}
    proc = subprocess.Popen([sys.executable, "-m", "fbadmin", "serve"], env=env,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        for _ in range(100):
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/health", timeout=1) as r:
                    assert b'"ok"' in r.read()
                break
            except OSError:
                time.sleep(0.2)
        else:
            raise AssertionError("fbadmin did not start")
        proc.send_signal(signal.SIGTERM)
        # uvicorn shuts down, then re-raises the signal: systemd counts SIGTERM as a clean stop
        assert proc.wait(20) in (0, -signal.SIGTERM)
    finally:
        if proc.poll() is None:
            proc.kill()
    log = (tmp_path / "logs" / "fbadmin.log").read_text("utf-8")
    assert "Application startup complete" in log and "Shutting down" in log
    assert "Application shutdown complete" in log and "Finished server process" in log


@pytest.mark.skipif(sys.platform == "win32" or os.geteuid() == 0, reason="root reads any file")
def test_unreadable_file(tmp_path):
    f = tmp_path / "fbadmin.toml"
    f.write_text("port = 1\n", "utf-8")
    f.chmod(0)
    with pytest.raises(configfile.ConfigError, match="Cannot read"):
        configfile.read(f)
