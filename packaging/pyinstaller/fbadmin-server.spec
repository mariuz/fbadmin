# -*- mode: python ; coding: utf-8 -*-
# PyInstaller spec for the headless server build (issue #11): fbadmin without the desktop window, for the
# Windows service (MSI with WinSW) and the Linux server packages (systemd). One-folder build.
#
#   pip install ./backend pyinstaller
#   (cd frontend && npm ci && npm run build)       # the UI must be built first
#   pyinstaller packaging/pyinstaller/fbadmin-server.spec --noconfirm
#
# Output: dist/fbadmin-server/fbadmin-server[.exe] (a console program: `fbadmin-server serve --config ...`,
# and the command-line tool: `fbadmin-server backup ...`).
import importlib.util
import sys
from pathlib import Path

from PyInstaller.utils.hooks import collect_submodules

here = Path(SPECPATH).resolve()  # noqa: F821 - provided by PyInstaller
root = here.parents[1]
backend = root / "backend"
static = backend / "fbadmin" / "static"
if not (static / "index.html").is_file():
    raise SystemExit("backend/fbadmin/static/index.html is missing: build the frontend first")

sys.path.insert(0, str(backend))

icons = root / "packaging" / "icons"
hidden = collect_submodules("fbadmin") + collect_submodules("uvicorn") + collect_submodules("firebirdsql")
# the native driver (driver setting auto) with the Firebird client installed on the server (fbclient)
if importlib.util.find_spec("firebird.driver"):
    hidden += collect_submodules("firebird.driver") + collect_submodules("firebird.base")

a = Analysis(  # noqa: F821
    [str(here / "server_launcher.py")],
    pathex=[str(backend)],
    datas=[(str(static), "fbadmin/static")],
    hiddenimports=hidden,
    excludes=["tkinter", "pytest", "hypothesis", "IPython", "webview"],
    noarchive=False,
    # fbadmin.driver_patches reads the driver's source (inspect.getsource): ship firebirdsql's .py files
    module_collection_mode={"firebirdsql": "pyz+py"},
)
pyz = PYZ(a.pure)  # noqa: F821
exe = EXE(  # noqa: F821
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="fbadmin-server",
    console=True,
    icon=str(icons / "fbadmin.ico") if sys.platform == "win32" else None,
)
coll = COLLECT(exe, a.binaries, a.datas, name="fbadmin-server")  # noqa: F821
