#!/usr/bin/env bash
# Start a Firebird 5 container with the sample database crypt plugins (fbSampleDbCrypt +
# fbSampleKeyHolder from the official release tarball) for tests/test_encryption_plugin.py.
#   scripts/crypt-test-server.sh            -> container fb5crypt on 127.0.0.1:3055
#   FBADMIN_TEST_CRYPT_PORT=3055 pytest backend/tests/test_encryption_plugin.py
# The sample "encryption" is a XOR with one byte: for tests only, never for real data.
set -euo pipefail
FB_VERSION="${FB_VERSION:-5.0.4}"
FB_BUILD="${FB_BUILD:-5.0.4.1812-0}"
PORT="${CRYPT_PORT:-3055}"
NAME="${CRYPT_CONTAINER:-fb5crypt}"
dir="${CRYPT_PLUGIN_DIR:-${TMPDIR:-/tmp}/fbadmin-crypt-plugins}"
mkdir -p "$dir"
if [ ! -f "$dir/libfbSampleDbCrypt.so" ]; then
  tmp="$(mktemp -d)"
  trap 'rm -rf "$tmp"' EXIT
  curl -fsSL -o "$tmp/fb.tar.gz" \
    "https://github.com/FirebirdSQL/firebird/releases/download/v${FB_VERSION}/Firebird-${FB_BUILD}-linux-x64.tar.gz"
  tar xzf "$tmp/fb.tar.gz" -C "$tmp"
  tar xzf "$tmp"/Firebird-*/buildroot.tar.gz -C "$tmp" ./opt/firebird/examples/prebuilt/plugins
  cp "$tmp"/opt/firebird/examples/prebuilt/plugins/libfbSample{DbCrypt,KeyHolder}.so "$dir/"
fi
printf 'Auto = true\n' > "$dir/fbSampleDbCrypt.conf"
printf 'Auto = true\nOnlyOwnKey = false\n' > "$dir/fbSampleKeyHolder.conf"
docker rm -f "$NAME" >/dev/null 2>&1 || true
docker run -d --name "$NAME" -p "$PORT:3050" -e FIREBIRD_ROOT_PASSWORD=masterkey \
  -e FIREBIRD_CONF_KeyHolderPlugin=fbSampleKeyHolder \
  -v "$dir/libfbSampleDbCrypt.so:/opt/firebird/plugins/libfbSampleDbCrypt.so:ro" \
  -v "$dir/libfbSampleKeyHolder.so:/opt/firebird/plugins/libfbSampleKeyHolder.so:ro" \
  -v "$dir/fbSampleDbCrypt.conf:/opt/firebird/plugins/fbSampleDbCrypt.conf:ro" \
  -v "$dir/fbSampleKeyHolder.conf:/opt/firebird/plugins/fbSampleKeyHolder.conf:ro" \
  "firebirdsql/firebird:${FB_VERSION%%.*}" >/dev/null
for _ in $(seq 1 30); do (echo >/dev/tcp/127.0.0.1/"$PORT") 2>/dev/null && break; sleep 1; done
echo "$NAME on 127.0.0.1:$PORT (crypt plugin fbSampleDbCrypt)"
