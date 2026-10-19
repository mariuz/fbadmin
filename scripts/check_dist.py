#!/usr/bin/env python3
"""Check built distributions: the wheel and sdist must contain the prebuilt web UI.

Usage: python scripts/check_dist.py dist/
Exits non-zero (with a message) when something is missing.
"""

from __future__ import annotations

import re
import sys
import tarfile
import zipfile
from pathlib import Path


def check_wheel(path: Path) -> list[str]:
    errors = []
    with zipfile.ZipFile(path) as z:
        names = z.namelist()
        if "fbadmin/static/index.html" not in names:
            errors.append(f"{path.name}: fbadmin/static/index.html missing (build the frontend first)")
        else:
            html = z.read("fbadmin/static/index.html").decode()
            for ref in re.findall(r'(?:src|href)="/(assets/[^"]+)"', html):
                if f"fbadmin/static/{ref}" not in names:
                    errors.append(f"{path.name}: index.html references {ref}, which is not in the wheel")
        if not any(n.startswith("fbadmin/static/assets/") and n.endswith(".js") for n in names):
            errors.append(f"{path.name}: no JavaScript bundle under fbadmin/static/assets/")
        eps = next((n for n in names if n.endswith(".dist-info/entry_points.txt")), None)
        text = z.read(eps).decode() if eps else ""
        for script in ("fbadmin =", "fbadmin-desktop ="):
            if script not in text:
                errors.append(f"{path.name}: console script {script.rstrip(' =')!r} missing")
        if any("/tests/" in n or n.startswith("tests/") for n in names):
            errors.append(f"{path.name}: tests should not be packaged")
    return errors


def check_sdist(path: Path) -> list[str]:
    with tarfile.open(path) as t:
        names = t.getnames()
    if not any(n.endswith("fbadmin/static/index.html") for n in names):
        return [f"{path.name}: fbadmin/static/index.html missing from the sdist"]
    return []


def main() -> int:
    dist = Path(sys.argv[1] if len(sys.argv) > 1 else "dist")
    wheels = sorted(dist.glob("*.whl"))
    sdists = sorted(dist.glob("*.tar.gz"))
    if not wheels:
        print(f"no wheel found in {dist}", file=sys.stderr)
        return 1
    errors = [e for w in wheels for e in check_wheel(w)] + [e for s in sdists for e in check_sdist(s)]
    for e in errors:
        print("ERROR:", e, file=sys.stderr)
    if not errors:
        print("OK:", ", ".join(p.name for p in wheels + sdists))
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
