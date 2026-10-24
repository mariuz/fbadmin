#!/usr/bin/env bash
# Download the pinned Firebird 5 release (SHA-256 checked) and extract the embedded engine
# file set, for local databases in development and for the embedded tests:
#
#   scripts/embedded-test-setup.sh [DEST]          # default: build/firebird-embedded
#   FBADMIN_TEST_EMBEDDED_DIR=build/firebird-embedded \
#     .venv/bin/python -m pytest backend/tests/test_embedded.py
#   FBADMIN_EMBEDDED_DIR=build/firebird-embedded .venv/bin/python -m fbadmin   # "Local database" button
#
# Needs the firebird-driver package (a dependency of fbadmin on Python 3.11+). On Linux the
# engine also needs libtommath.so.1 and the system ICU: libtommath is copied into the file
# set from the system (apt install libtommath1), or fetched with `apt-get download` when it
# is not installed. The same directory is what the PyInstaller spec bundles into the
# desktop app. See docs/EMBEDDED.md.
set -euo pipefail
root="$(cd "$(dirname "$0")/.." && pwd)"
dest="${1:-$root/build/firebird-embedded}"
PY="${PYTHON:-}"
if [ -z "$PY" ]; then
  if [ -x "$root/.venv/bin/python" ]; then PY="$root/.venv/bin/python"; else PY=python3; fi
fi

case "$(uname -s)-$(uname -m)" in
  Linux-x86_64) plat=linux-x64 ;;
  Darwin-arm64) plat=macos-arm64 ;;
  Darwin-x86_64) plat=macos-x64 ;;
  *) echo "embedded-test-setup: unsupported platform $(uname -s)-$(uname -m)" >&2; exit 1 ;;
esac

args=()
tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT
if [ "$plat" = linux-x64 ]; then
  lib="$(ldconfig -p 2>/dev/null | awk '/libtommath\.so\.1 .*x86-64/ {print $NF; exit}')"
  if [ -z "$lib" ] && command -v apt-get >/dev/null; then
    echo "embedded-test-setup: libtommath.so.1 not installed, downloading the Debian/Ubuntu package" >&2
    (cd "$tmp" && apt-get download libtommath1 >/dev/null)
    dpkg-deb -x "$tmp"/libtommath1_*.deb "$tmp/tommath"
    lib="$(find "$tmp/tommath" -name 'libtommath.so.1' | head -1)"
  fi
  [ -n "$lib" ] && args+=(--libtommath "$lib")
fi

"$PY" "$root/packaging/embedded/fetch-embedded.py" --platform "$plat" --dest "$dest" "${args[@]}"
"$PY" -c "import firebird.driver" 2>/dev/null \
  || echo "embedded-test-setup: install firebird-driver too: $PY -m pip install firebird-driver" >&2
echo "Embedded engine ready: FBADMIN_EMBEDDED_DIR=$dest (tests: FBADMIN_TEST_EMBEDDED_DIR=$dest)"
