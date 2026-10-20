# -*- mode: python ; coding: utf-8 -*-
# PyInstaller spec for the fbadmin desktop app (one-folder build).
#
#   pip install ./backend[desktop] pyinstaller pillow
#   (cd frontend && npm ci && npm run build)       # the UI must be built first
#   pyinstaller packaging/pyinstaller/fbadmin.spec --noconfirm
#
# Output: dist/fbadmin/ (Windows/Linux, run fbadmin[.exe]) and dist/fbadmin.app (macOS).
# The packaging steps (zip/MSI, dmg, AppImage/deb/rpm) are in .github/workflows/desktop.yml.
import json
import os
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
from fbadmin import __version__  # noqa: E402

icons = root / "packaging" / "icons"
icon = {"win32": icons / "fbadmin.ico", "darwin": icons / "fbadmin.png"}.get(sys.platform, icons / "fbadmin.png")

hidden = (
    collect_submodules("fbadmin")
    + collect_submodules("uvicorn")
    + collect_submodules("firebirdsql")
    + ["webview"]
)
datas = [(str(static), "fbadmin/static")]

# Firebird embedded engine for local databases (docs/EMBEDDED.md): the file set extracted by
# packaging/embedded/fetch-embedded.py is copied verbatim to <app>/firebird, where
# fbadmin.embedded finds it. Without it the app works as before, minus local databases.
# FBADMIN_REQUIRE_EMBEDDED=1 (CI) makes a missing file set an error.
embedded = Path(os.environ.get("FBADMIN_EMBEDDED_BUNDLE") or root / "build" / "firebird-embedded")
if (embedded / "fbadmin-embedded.json").is_file():
    info = json.loads((embedded / "fbadmin-embedded.json").read_text("utf-8"))
    print(f"fbadmin.spec: bundling the Firebird {info['build']} embedded engine ({info['platform']}) from {embedded}")
    datas.append((str(embedded), "firebird"))
    hidden += collect_submodules("firebird.driver") + collect_submodules("firebird.base")
elif os.environ.get("FBADMIN_REQUIRE_EMBEDDED"):
    raise SystemExit(f"{embedded} has no embedded engine: run packaging/embedded/fetch-embedded.py first")

a = Analysis(  # noqa: F821
    [str(here / "launcher.py")],
    pathex=[str(backend)],
    datas=datas,
    hiddenimports=hidden,
    excludes=["tkinter", "pytest", "hypothesis", "IPython"],
    noarchive=False,
    # fbadmin.driver_patches reads the driver's source (inspect.getsource) to patch
    # params_to_blr, so ship firebirdsql's .py files next to the compiled modules.
    module_collection_mode={"firebirdsql": "pyz+py"},
)
pyz = PYZ(a.pure)  # noqa: F821
exe = EXE(  # noqa: F821
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="fbadmin",
    console=False,
    icon=str(icon),
)
coll = COLLECT(exe, a.binaries, a.datas, name="fbadmin")  # noqa: F821

if sys.platform == "darwin":
    app = BUNDLE(  # noqa: F821
        coll,
        name="fbadmin.app",
        icon=str(icon),
        bundle_identifier="io.github.mariuz.fbadmin",
        version=__version__,
        info_plist={
            "CFBundleName": "fbadmin",
            "CFBundleDisplayName": "fbadmin",
            "NSHighResolutionCapable": True,
            "LSMinimumSystemVersion": "11.0",
        },
    )
