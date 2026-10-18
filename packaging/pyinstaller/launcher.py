"""Entry point of the frozen desktop app (PyInstaller)."""

import os
import sys
from pathlib import Path

# Windowed apps (Windows .exe, macOS .app) have no console: sys.stdout/stderr are None and
# logging would fail. Send output to <data dir>/desktop.log instead.
if sys.stderr is None or sys.stdout is None:
    data = Path(os.environ.get("FBADMIN_DATA_DIR") or Path.home() / ".fbadmin")
    data.mkdir(parents=True, exist_ok=True)
    log = open(data / "desktop.log", "a", encoding="utf-8", buffering=1)  # noqa: SIM115
    sys.stdout = sys.stdout or log
    sys.stderr = sys.stderr or log

from fbadmin.desktop import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
