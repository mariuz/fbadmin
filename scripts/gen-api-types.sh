#!/usr/bin/env bash
# Generate TypeScript types for the REST API from FastAPI's OpenAPI schema.
#   scripts/gen-api-types.sh          -> rewrites frontend/src/api-types.gen.ts
#   scripts/gen-api-types.sh --check  -> fails if the committed file is out of date (CI)
# Needs the backend importable (pip install -e backend) and `npm install` in frontend/.
# Python interpreter: $PYTHON, else .venv/bin/python, else python3.
set -euo pipefail
root="$(cd "$(dirname "$0")/.." && pwd)"
out="$root/frontend/src/api-types.gen.ts"
PY="${PYTHON:-}"
if [ -z "$PY" ]; then
  if [ -x "$root/.venv/bin/python" ]; then PY="$root/.venv/bin/python"; else PY=python3; fi
fi
tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT

# Import the app from this checkout (not an installed copy) without touching ~/.fbadmin.
(cd "$root/backend" && FBADMIN_DATA_DIR="$tmp/data" "$PY" -c '
import json, sys
from fbadmin.app import create_app
json.dump(create_app().openapi(), sys.stdout, indent=1, sort_keys=True)
') > "$tmp/openapi.json"

"$root/frontend/node_modules/.bin/openapi-typescript" "$tmp/openapi.json" --default-non-nullable false -o "$tmp/api-types.ts" >/dev/null
{
  echo "// GENERATED FILE - do not edit. Run scripts/gen-api-types.sh after changing the API."
  echo "// Types for the fbadmin REST API, generated from FastAPI's OpenAPI schema by openapi-typescript."
  echo "// Usage: import type { components, paths } from \"./api-types.gen\";"
  echo
  cat "$tmp/api-types.ts"
} > "$tmp/final.ts"

if [ "${1:-}" = "--check" ]; then
  if ! diff -u "$out" "$tmp/final.ts" >"$tmp/diff" 2>&1; then
    head -50 "$tmp/diff"
    echo "frontend/src/api-types.gen.ts is out of date: run scripts/gen-api-types.sh and commit the result" >&2
    exit 1
  fi
  echo "api-types.gen.ts is up to date"
else
  cp "$tmp/final.ts" "$out"
  echo "wrote ${out#"$root"/}"
fi
