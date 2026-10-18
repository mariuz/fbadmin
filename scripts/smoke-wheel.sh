#!/usr/bin/env bash
# Install a built wheel into a throw-away venv, start it and check that it serves the UI.
#   scripts/smoke-wheel.sh dist/fbadmin-0.2.0-py3-none-any.whl [port]
set -euo pipefail
wheel="$(realpath "$1")"
port="${2:-${SMOKE_PORT:-5106}}"
PY="${PYTHON:-python3}"
work="$(mktemp -d)"
trap 'kill "${pid:-0}" 2>/dev/null || true; rm -rf "$work"' EXIT

"$PY" -m venv "$work/venv"
"$work/venv/bin/pip" install -q "$wheel"
FBADMIN_DATA_DIR="$work/data" "$work/venv/bin/fbadmin" --no-browser --port "$port" --log-level warning &
pid=$!
for _ in $(seq 1 60); do
  curl -fsS "http://127.0.0.1:$port/api/health" >/dev/null 2>&1 && break
  sleep 0.5
done
curl -fsS "http://127.0.0.1:$port/api/health"; echo
html="$(curl -fsS "http://127.0.0.1:$port/")"
grep -q '<div id="root">' <<<"$html" || { echo "UI index.html not served" >&2; exit 1; }
asset="$(grep -oE '/assets/[^"]+\.js' <<<"$html" | head -1)"
curl -fsS -o /dev/null "http://127.0.0.1:$port$asset" || { echo "asset $asset not served" >&2; exit 1; }
"$work/venv/bin/fbadmin-desktop" --help >/dev/null
echo "OK: wheel installs and serves the UI (index.html and $asset)"
