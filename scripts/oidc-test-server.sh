#!/usr/bin/env bash
# A real OpenID Connect provider (Dex) for tests/test_oidc_real.py.
#   scripts/oidc-test-server.sh   -> issuer http://127.0.0.1:5556/dex, client "fbadmin" / "fbadmin-secret"
#   FBADMIN_TEST_OIDC_ISSUER=http://127.0.0.1:5556/dex pytest backend/tests/test_oidc_real.py
# Users (Dex static passwords): carol@example.org / carol-pw, dan@example.org / dan-pw.
# The redirect URI is the in-process test client's: http://testserver/api/auth/oidc/dex/callback.
set -euo pipefail
PORT="${OIDC_PORT:-5556}"
NAME="${OIDC_CONTAINER:-fbdex}"
dir="${OIDC_DIR:-${TMPDIR:-/tmp}/fbadmin-dex}"
mkdir -p "$dir"
cat > "$dir/config.yaml" <<CONF
issuer: http://127.0.0.1:$PORT/dex
storage:
  type: memory
web:
  http: 0.0.0.0:5556
oauth2:
  skipApprovalScreen: true
  responseTypes: [code]
staticClients:
  - id: fbadmin
    secret: fbadmin-secret
    name: fbadmin
    redirectURIs:
      - http://testserver/api/auth/oidc/dex/callback
enablePasswordDB: true
staticPasswords:
  - email: carol@example.org
    # bcrypt("carol-pw")
    hash: "\$2b\$10\$R8bfljLdSAVLhwYJ/GoJdeO6KyBf.8MKDQvifKIxENhvxLz9eztoW"
    username: carol
    userID: 0b8c2a1e-0000-4000-8000-000000000001
  - email: dan@example.org
    # bcrypt("dan-pw")
    hash: "\$2b\$10\$tDOKpk1pSi/f7E5Nt3TolOpH9EVXpSJV7poc/OfihZgdgjDwCVr32"
    username: dan
    userID: 0b8c2a1e-0000-4000-8000-000000000002
CONF
docker rm -f "$NAME" >/dev/null 2>&1 || true
docker run -d --name "$NAME" -p "$PORT:5556" -v "$dir/config.yaml:/etc/dex/config.yaml:ro" \
  ghcr.io/dexidp/dex:v2.41.1 dex serve /etc/dex/config.yaml >/dev/null
for _ in $(seq 1 60); do curl -fsS "http://127.0.0.1:$PORT/dex/.well-known/openid-configuration" >/dev/null 2>&1 && break; sleep 0.5; done
echo "Dex on http://127.0.0.1:$PORT/dex"
