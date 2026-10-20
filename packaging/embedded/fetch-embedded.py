#!/usr/bin/env python3
"""Download a pinned Firebird release and extract the minimal *embedded* engine file set.

    python packaging/embedded/fetch-embedded.py --platform linux-x64 --dest build/firebird-embedded

The result is a directory fbadmin uses as its local-database engine: point
``FBADMIN_EMBEDDED_DIR`` at it (development, tests) or let the PyInstaller spec bundle it
(``build/firebird-embedded`` -> ``<app>/firebird``). See docs/EMBEDDED.md.

* The release archive is checked against the SHA-256 pinned below before anything is
  extracted (``--archive`` uses a local copy, still checked).
* Only what the embedded engine needs is copied: the client library, the Engine13 and
  profiler plugins, character sets (``intl/``), time-zone data, messages, configuration,
  ICU / runtime libraries where the release ships them, and the licence files.
* Linux: the official tarball does not ship ``libtommath.so.1`` (the installer takes it from
  the distribution); it is copied from the build machine (``--libtommath`` or the usual
  library directories, e.g. ``apt install libtommath1``). ICU comes from the system.
* macOS: the libraries find their dependencies through ``@rpath`` set by Firebird's own
  executables; on a Mac the script adds ``@loader_path/..`` rpaths with ``install_name_tool``
  and re-signs ad hoc (pass ``--no-macos-fixups`` to skip).

Only the Python standard library is used, so it runs on every CI runner.
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import io
import json
import os
import shutil
import struct
import subprocess
import sys
import tarfile
import tempfile
import urllib.request
import zipfile
import zlib
from pathlib import Path, PurePosixPath
from xml.etree import ElementTree

VERSION = "5.0.4"
BUILD = "5.0.4.1812"
RELEASE_URL = f"https://github.com/FirebirdSQL/firebird/releases/download/v{VERSION}/"
MIRROR_URL = f"https://downloads.sourceforge.net/project/firebird/v{VERSION}/"

# SHA-256 of the official release assets (the SHA-1 of each also matches the checksum
# SourceForge publishes for its mirror copy; see docs/EMBEDDED.md).
ARCHIVES = {
    "linux-x64": (f"Firebird-{BUILD}-0-linux-x64.tar.gz",
                  "ab6a15a0258f38b022be496bb5e038c14e8628ce9acd0f9a06288a3baedd917b"),
    "windows-x64": (f"Firebird-{BUILD}-0-windows-x64.zip",
                    "01e844fce4d5f53272a76205dbc3a1ba4b782ab8e8eadcd808cdbccd9ce13b72"),
    "macos-arm64": (f"Firebird-{BUILD}-0-macos-arm64.pkg",
                    "0c3495ec457720f3b3b51e1be0f36485f589732d6af38c9b04f1eb171d8d5509"),
    "macos-x64": (f"Firebird-{BUILD}-0-macos-x64.pkg",
                  "f6b587d8d16f528b9ce94bf006e204cc171620fbb64b90730790e7af612c2325"),
}

# destination path -> path inside the release (relative to its Firebird root)
COMMON = {
    "firebird.conf": "firebird.conf",
    "plugins.conf": "plugins.conf",
    "firebird.msg": "firebird.msg",
    "intl/fbintl.conf": "intl/fbintl.conf",
    "tzdata/metaZones.res": "tzdata/metaZones.res",
    "tzdata/timezoneTypes.res": "tzdata/timezoneTypes.res",
    "tzdata/windowsZones.res": "tzdata/windowsZones.res",
    "tzdata/zoneinfo64.res": "tzdata/zoneinfo64.res",
}
FILES = {
    # Linux: real files under their sonames (no symlinks: they do not survive every packager)
    "linux-x64": {
        "lib/libfbclient.so.2": "lib/libfbclient.so.5.0.4",
        "lib/libtomcrypt.so.1": "lib/libtomcrypt.so.1.0.1",
        "plugins/libEngine13.so": "plugins/libEngine13.so",
        "plugins/libDefault_Profiler.so": "plugins/libDefault_Profiler.so",
        "intl/fbintl": "intl/fbintl",
        "licenses/IDPLicense.txt": "IDPLicense.txt",
        "licenses/IPLicense.txt": "IPLicense.txt",
    },
    "windows-x64": {
        "fbclient.dll": "fbclient.dll",
        "plugins/engine13.dll": "plugins/engine13.dll",
        "plugins/default_profiler.dll": "plugins/default_profiler.dll",
        "intl/fbintl.dll": "intl/fbintl.dll",
        "icudt63.dll": "icudt63.dll",
        "icudt63l.dat": "icudt63l.dat",
        "icuin63.dll": "icuin63.dll",
        "icuuc63.dll": "icuuc63.dll",
        "msvcp140.dll": "msvcp140.dll",
        "msvcp140_1.dll": "msvcp140_1.dll",
        "vcruntime140.dll": "vcruntime140.dll",
        "vcruntime140_1.dll": "vcruntime140_1.dll",
        "zlib1.dll": "zlib1.dll",
        "tzdata/ids.dat": "tzdata/ids.dat",
        "licenses/IDPLicense.txt": "IDPLicense.txt",
        "licenses/IPLicense.txt": "IPLicense.txt",
    },
    # macOS: Firebird.framework/Versions/A/Resources of the installer payload
    "macos": {
        "lib/libfbclient.dylib": "lib/libfbclient.dylib",
        "lib/libtommath.dylib": "lib/libtommath.dylib",
        "lib/libtomcrypt.dylib": "lib/libtomcrypt.dylib",
        # the engine dlopen()s "lib/libicuuc.71.1.dylib" and "lib/libicui18n.71.1.dylib" (relative
        # to the working directory, see fbadmin/embedded.py); both link @loader_path/libicudata.71.dylib,
        # and i18n links @loader_path/libicuuc.71.dylib (redirected to the .71.1 file by macos_fixups)
        "lib/libicuuc.71.1.dylib": "lib/libicuuc.71.1.dylib",
        "lib/libicui18n.71.1.dylib": "lib/libicui18n.71.1.dylib",
        "lib/libicudata.71.dylib": "lib/libicudata.71.1.dylib",
        "tzdata/ids.dat": "tzdata/ids.dat",
        "plugins/libEngine13.dylib": "plugins/libEngine13.dylib",
        "plugins/libDefault_Profiler.dylib": "plugins/libDefault_Profiler.dylib",
        "intl/libfbintl.dylib": "intl/libfbintl.dylib",
        "licenses/IPLicense.txt": "License.txt",
    },
}
# licences of bundled third-party libraries (vendored next to this script)
EXTRA_LICENSES = {
    "linux-x64": ["libtommath.txt", "libtomcrypt.txt"],
    "windows-x64": ["ICU-63.txt", "zlib.txt"],
    "macos": ["ICU-71.txt", "libtommath.txt", "libtomcrypt.txt", "IDPLicense.txt"],
}
HERE = Path(__file__).resolve().parent


def log(msg: str) -> None:
    print(f"fetch-embedded: {msg}", file=sys.stderr)


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def download(name: str, expected: str, cache: Path) -> Path:
    cache.mkdir(parents=True, exist_ok=True)
    dest = cache / name
    if dest.is_file() and sha256(dest) == expected:
        log(f"using cached {dest}")
        return dest
    last: Exception | None = None
    for base in (RELEASE_URL, MIRROR_URL):
        url = base + name
        log(f"downloading {url}")
        tmp = dest.with_suffix(dest.suffix + ".part")
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "fbadmin-fetch-embedded"})
            with urllib.request.urlopen(req, timeout=300) as r, open(tmp, "wb") as f:
                shutil.copyfileobj(r, f, 1 << 20)
        except Exception as exc:  # noqa: BLE001 - try the mirror
            log(f"  failed: {exc}")
            last = exc
            continue
        got = sha256(tmp)
        if got != expected:
            tmp.unlink()
            last = RuntimeError(f"checksum mismatch for {url}: {got} != {expected}")
            log(f"  {last}")
            continue
        tmp.replace(dest)
        return dest
    raise SystemExit(f"cannot download {name}: {last}")


# --- archive readers: return {relative path inside the Firebird root: bytes} -------------
def read_linux(archive: Path, wanted: set[str]) -> dict[str, bytes]:
    with tarfile.open(archive) as outer:
        member = next(m for m in outer.getmembers() if m.name.endswith("/buildroot.tar.gz"))
        inner_bytes = outer.extractfile(member).read()
    out: dict[str, bytes] = {}
    with tarfile.open(fileobj=io.BytesIO(inner_bytes)) as inner:
        for m in inner.getmembers():
            rel = str(PurePosixPath(m.name)).removeprefix("./").removeprefix("opt/firebird/")
            if rel in wanted and m.isfile():
                out[rel] = inner.extractfile(m).read()
    return out


def read_windows(archive: Path, wanted: set[str]) -> dict[str, bytes]:
    out: dict[str, bytes] = {}
    with zipfile.ZipFile(archive) as z:
        for info in z.infolist():
            rel = info.filename.replace("\\", "/")
            if rel in wanted:
                out[rel] = z.read(info)
    return out


def _xar_files(data: bytes) -> dict[str, bytes]:
    """Files of a xar archive (the macOS .pkg container)."""
    if data[:4] != b"xar!":
        raise SystemExit("not a xar (.pkg) archive")
    hsize, _ver, toc_len, _toc_raw, _alg = struct.unpack(">HHQQI", data[4:28])
    toc = ElementTree.fromstring(zlib.decompress(data[hsize:hsize + toc_len]))
    heap = hsize + toc_len
    out: dict[str, bytes] = {}

    def walk(el, prefix: str) -> None:
        for f in el.findall("file"):
            path = f"{prefix}{f.findtext('name')}"
            if f.findtext("type") == "directory":
                walk(f, path + "/")
                continue
            d = f.find("data")
            if d is None:
                continue
            off, length = int(d.findtext("offset")), int(d.findtext("length"))
            raw = data[heap + off:heap + off + length]
            style = d.find("encoding").get("style", "")
            out[path] = zlib.decompress(raw) if ("gzip" in style or "zlib" in style) else raw

    walk(toc.find("toc"), "")
    return out


def _cpio_files(raw: bytes, prefix: str, wanted: set[str]) -> dict[str, bytes]:
    """Regular files of an odc/newc cpio archive under ``prefix`` (symlinks are skipped)."""
    out: dict[str, bytes] = {}
    pos = 0
    while pos < len(raw):
        magic = raw[pos:pos + 6]
        if magic == b"070707":
            h = raw[pos:pos + 76]
            mode, nlen, size = int(h[18:24], 8), int(h[59:65], 8), int(h[65:76], 8)
            name = raw[pos + 76:pos + 75 + nlen].decode()
            start = pos + 76 + nlen
            pos = start + size
        elif magic in (b"070701", b"070702"):
            h = raw[pos:pos + 110]
            mode, size, nlen = int(h[14:22], 16), int(h[54:62], 16), int(h[94:102], 16)
            name = raw[pos + 110:pos + 109 + nlen].decode()
            start = (pos + 110 + nlen + 3) & ~3
            pos = (start + size + 3) & ~3
        else:
            raise SystemExit(f"unexpected cpio header at {pos}")
        if name == "TRAILER!!!":
            break
        name = name.removeprefix("./")
        if (mode & 0o170000) == 0o100000 and name.startswith(prefix) and name[len(prefix):] in wanted:
            out[name[len(prefix):]] = raw[start:start + size]
    return out


def read_macos(archive: Path, wanted: set[str]) -> dict[str, bytes]:
    files = _xar_files(archive.read_bytes())
    payload = next(v for k, v in files.items() if k.endswith("Firebird.pkg/Payload"))
    if payload[:2] == b"\x1f\x8b":
        payload = gzip.decompress(payload)
    return _cpio_files(payload, "Versions/A/Resources/", wanted)


# --- extraction ------------------------------------------------------------------------
def find_libtommath(explicit: str | None) -> Path:
    candidates = [explicit] if explicit else [
        "/usr/lib/x86_64-linux-gnu/libtommath.so.1", "/usr/lib64/libtommath.so.1", "/usr/lib/libtommath.so.1",
        "/lib/x86_64-linux-gnu/libtommath.so.1",
    ]
    for c in candidates:
        if c and Path(c).is_file():
            return Path(c).resolve()
    raise SystemExit("libtommath.so.1 not found: install it (Debian/Ubuntu: apt install libtommath1, "
                     "Fedora: dnf install libtommath) or pass --libtommath PATH")


def macos_fixups(dest: Path) -> None:
    """Make the dylibs find @rpath/lib/... without Firebird's executables, then re-sign."""
    for rel in ("lib/libfbclient.dylib", "plugins/libEngine13.dylib", "plugins/libDefault_Profiler.dylib",
                "intl/libfbintl.dylib", "lib/libtomcrypt.dylib"):
        f = dest / rel
        if f.is_file():
            subprocess.run(["install_name_tool", "-add_rpath", "@loader_path/..", str(f)], check=False)
    subprocess.run(["install_name_tool", "-change", "@loader_path/libicuuc.71.dylib", "@loader_path/libicuuc.71.1.dylib",
                    str(dest / "lib/libicui18n.71.1.dylib")], check=True)
    for f in sorted(dest.rglob("*.dylib")):
        subprocess.run(["codesign", "--force", "--sign", "-", str(f)], check=True)


def extract(platform: str, archive: Path, dest: Path, *, libtommath: str | None, macos_fix: bool) -> dict:
    files = FILES["macos" if platform.startswith("macos") else platform] | COMMON
    wanted = set(files.values())
    reader = {"linux-x64": read_linux, "windows-x64": read_windows}.get(platform, read_macos)
    found = reader(archive, wanted)
    missing = sorted(wanted - set(found))
    if missing:
        raise SystemExit(f"not in {archive.name}: {', '.join(missing)}")
    if dest.exists():
        shutil.rmtree(dest)
    for d_rel, src_rel in sorted(files.items()):
        target = dest / d_rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(found[src_rel])
        if not platform.startswith("windows") and (d_rel.startswith(("lib/", "plugins/")) or d_rel == "intl/fbintl"
                                                  or d_rel.endswith(".dylib")):
            target.chmod(0o755)
    if platform == "linux-x64":
        src = find_libtommath(libtommath)
        shutil.copyfile(src, dest / "lib" / "libtommath.so.1")
        (dest / "lib" / "libtommath.so.1").chmod(0o755)
        log(f"libtommath.so.1 from {src}")
    licdir = dest / "licenses"
    for name in EXTRA_LICENSES["macos" if platform.startswith("macos") else platform]:
        shutil.copyfile(HERE / "licenses" / name, licdir / name)
    shutil.copyfile(HERE / "NOTICE.txt", licdir / "NOTICE.txt")
    if platform.startswith("macos") and macos_fix:
        if shutil.which("install_name_tool") and shutil.which("codesign"):
            macos_fixups(dest)
        else:
            log("WARNING: not on macOS - rpaths not added, the dylibs will not load (see --help)")
    size = sum(f.stat().st_size for f in dest.rglob("*") if f.is_file())
    info = {"version": VERSION, "build": BUILD, "platform": platform, "archive": archive.name,
            "sha256": sha256(archive), "size": size,
            "files": sorted(str(f.relative_to(dest)).replace(os.sep, "/") for f in dest.rglob("*") if f.is_file())}
    (dest / "fbadmin-embedded.json").write_text(json.dumps(info, indent=2) + "\n", "utf-8")
    log(f"{dest}: {len(info['files'])} files, {size / 1e6:.1f} MB")
    return info


def host_platform() -> str:
    import platform as p

    machine = p.machine().lower()
    if sys.platform == "win32":
        return "windows-x64"
    if sys.platform == "darwin":
        return "macos-arm64" if machine in ("arm64", "aarch64") else "macos-x64"
    return "linux-x64"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--platform", choices=sorted(ARCHIVES), default=host_platform())
    ap.add_argument("--dest", default="build/firebird-embedded", help="output directory (replaced)")
    ap.add_argument("--cache", default=str(Path(tempfile.gettempdir()) / "fbadmin-embedded-cache"),
                    help="download cache directory")
    ap.add_argument("--archive", help="use this local release archive instead of downloading it")
    ap.add_argument("--libtommath", help="Linux: path of libtommath.so.1 to bundle")
    ap.add_argument("--no-macos-fixups", action="store_true", help="macOS: skip install_name_tool/codesign")
    args = ap.parse_args(argv)
    name, expected = ARCHIVES[args.platform]
    if args.archive:
        archive = Path(args.archive)
        got = sha256(archive)
        if got != expected:
            raise SystemExit(f"{archive}: SHA-256 {got} does not match the pinned {expected}")
    else:
        archive = download(name, expected, Path(args.cache))
    extract(args.platform, archive, Path(args.dest), libtommath=args.libtommath,
            macos_fix=not args.no_macos_fixups)
    return 0


if __name__ == "__main__":
    sys.exit(main())
