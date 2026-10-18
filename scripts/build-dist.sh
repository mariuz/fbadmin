#!/usr/bin/env bash
# Build the PyPI distributions (sdist + wheel) with the prebuilt web UI inside.
#   scripts/build-dist.sh            -> dist/fbadmin-X.Y.Z.tar.gz, dist/fbadmin-X.Y.Z-py3-none-any.whl
#   SKIP_UI=1 scripts/build-dist.sh  -> reuse an existing backend/fbadmin/static
#   SMOKE=1 scripts/build-dist.sh    -> also install the wheel in a fresh venv and fetch the UI
set -euo pipefail
root="$(cd "$(dirname "$0")/.." && pwd)"
cd "$root"
PY="${PYTHON:-python3}"

if [ "${SKIP_UI:-0}" != "1" ]; then
  (cd frontend && npm ci --no-audit --no-fund && npm run build)
fi
test -f backend/fbadmin/static/index.html || { echo "backend/fbadmin/static is empty: build the frontend" >&2; exit 1; }

rm -rf dist backend/build backend/*.egg-info
"$PY" -m build backend --outdir dist
"$PY" scripts/check_dist.py dist

if [ "${SMOKE:-0}" = "1" ]; then
  "$root/scripts/smoke-wheel.sh" dist/fbadmin-*.whl
fi
