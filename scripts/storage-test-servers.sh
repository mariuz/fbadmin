#!/usr/bin/env bash
# Cloud storage emulators for tests/test_storage_real.py (Docker):
#   S3     SeaweedFS (S3 gateway with auth)       -> http://127.0.0.1:8333  key fbadmin / fbadmin-secret-key
#   Azure  Azurite (blob service)                 -> http://127.0.0.1:10000/devstoreaccount1 (well-known key)
#   GCS    fake-gcs-server (JSON API, no auth)    -> http://127.0.0.1:4443
#   scripts/storage-test-servers.sh          # then paste the printed exports and run
#   pytest backend/tests/test_storage_real.py
# The tests create the bucket/container themselves. Images can be overridden with S3_IMAGE,
# AZURITE_IMAGE and GCS_IMAGE (CI uses Docker Hub; this default list uses mirrors).
set -euo pipefail
S3_PORT="${S3_PORT:-8333}"
AZ_PORT="${AZURITE_PORT:-10000}"
GCS_PORT="${GCS_PORT:-4443}"
S3_IMAGE="${S3_IMAGE:-mirror.gcr.io/chrislusf/seaweedfs:latest}"
AZURITE_IMAGE="${AZURITE_IMAGE:-mcr.microsoft.com/azure-storage/azurite:latest}"
GCS_IMAGE="${GCS_IMAGE:-mirror.gcr.io/fsouza/fake-gcs-server:latest}"
dir="${STORAGE_DIR:-${TMPDIR:-/tmp}/fbadmin-storage}"
AZ_KEY="Eby8vdM02xNOcqFlqUwJPLlmEtlCDXJ1OUzFT50uSRZ6IFsuFq2UVErCz4I6tq/K1SZFPTOtr/KBHBeksoGMGw=="

if ! docker info >/dev/null 2>&1; then
  (setsid dockerd >/tmp/dockerd.log 2>&1 &)
  for _ in $(seq 1 30); do docker info >/dev/null 2>&1 && break; sleep 1; done
fi
mkdir -p "$dir"
cat > "$dir/s3.json" <<'JSON'
{
  "identities": [
    {
      "name": "fbadmin",
      "credentials": [{"accessKey": "fbadmin", "secretKey": "fbadmin-secret-key"}],
      "actions": ["Admin", "Read", "List", "Tagging", "Write"]
    }
  ]
}
JSON
chmod 644 "$dir/s3.json"

for c in fbs3 fbazurite fbgcs; do docker rm -f "$c" >/dev/null 2>&1 || true; done
docker run -d --name fbs3 -p "$S3_PORT:8333" -v "$dir/s3.json:/etc/seaweedfs/s3.json:ro" \
  "$S3_IMAGE" server -dir=/data -s3 -s3.port=8333 -s3.config=/etc/seaweedfs/s3.json >/dev/null
docker run -d --name fbazurite -p "$AZ_PORT:10000" "$AZURITE_IMAGE" \
  azurite-blob --blobHost 0.0.0.0 --blobPort 10000 --skipApiVersionCheck --loose >/dev/null
docker run -d --name fbgcs -p "$GCS_PORT:4443" "$GCS_IMAGE" \
  -scheme http -port 4443 -public-host "127.0.0.1:$GCS_PORT" -external-url "http://127.0.0.1:$GCS_PORT" >/dev/null

wait_http() {  # name url (any HTTP status counts as up)
  for _ in $(seq 1 90); do
    if curl -s -o /dev/null --noproxy '*' -m 2 "$2"; then return 0; fi
    sleep 1
  done
  echo "$1 did not start:" >&2
  docker logs --tail 50 "$1" >&2 || true
  exit 1
}
wait_http fbs3 "http://127.0.0.1:$S3_PORT/"
wait_http fbazurite "http://127.0.0.1:$AZ_PORT/devstoreaccount1?comp=list"
wait_http fbgcs "http://127.0.0.1:$GCS_PORT/storage/v1/b"

cat <<ENV
# storage emulators are up; export these to run backend/tests/test_storage_real.py
export FBADMIN_TEST_S3_ENDPOINT=http://127.0.0.1:$S3_PORT
export FBADMIN_TEST_S3_ACCESS_KEY=fbadmin
export FBADMIN_TEST_S3_SECRET_KEY=fbadmin-secret-key
export FBADMIN_TEST_S3_BUCKET=fbadmin-test
export FBADMIN_TEST_AZURE_ENDPOINT=http://127.0.0.1:$AZ_PORT/devstoreaccount1
export FBADMIN_TEST_AZURE_ACCOUNT=devstoreaccount1
export FBADMIN_TEST_AZURE_KEY=$AZ_KEY
export FBADMIN_TEST_GCS_ENDPOINT=http://127.0.0.1:$GCS_PORT
ENV
