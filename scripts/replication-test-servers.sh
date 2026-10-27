#!/usr/bin/env bash
# Two Firebird servers for the replication setup tests (backend/tests/test_replication_setup.py):
#   fbrepl-p (primary) -> 127.0.0.1:3060, fbrepl-r (replica) -> 127.0.0.1:3061, SYSDBA / masterkey.
# They share the volume /repl (journal, archive and nbackup copies) and reach each other by name on the
# network fbrepl. Each server's replication.conf is a file on this host, so fbadmin writes it in the tests:
#   $FBADMIN_REPL_DIR/primary/replication.conf and $FBADMIN_REPL_DIR/replica/replication.conf
# FB_VERSION selects the image (default 5); REPL_PRIMARY_PORT / REPL_REPLICA_PORT the host ports. Run the tests with:
#   FBADMIN_TEST_REPL=1 FBADMIN_REPL_DIR=... python -m pytest tests/test_replication_setup.py
set -euo pipefail
FB_VERSION="${FB_VERSION:-5}"
DIR="${FBADMIN_REPL_DIR:-/tmp/fbadmin-repl}"
PPORT="${REPL_PRIMARY_PORT:-3060}"
RPORT="${REPL_REPLICA_PORT:-3061}"
if ! docker info >/dev/null 2>&1; then
  (setsid dockerd >/tmp/dockerd.log 2>&1 &)
  for _ in $(seq 1 30); do docker info >/dev/null 2>&1 && break; sleep 1; done
fi
docker rm -f fbrepl-p fbrepl-r >/dev/null 2>&1 || true
docker volume rm fbrepl-share >/dev/null 2>&1 || true
docker network create fbrepl >/dev/null 2>&1 || true
for side in primary replica; do
  mkdir -p "$DIR/$side"
  # the defaults block of Firebird's own file; the tests add one block per database
  printf '# replication.conf of the %s test server (written by the fbadmin tests)\n\ndatabase\n{\n}\n' "$side" \
    > "$DIR/$side/replication.conf"
  chmod 644 "$DIR/$side/replication.conf"
done
run() {  # name port side
  docker run -d --name "$1" --network fbrepl --network-alias "$1" -p "$2:3050" \
    -e FIREBIRD_ROOT_PASSWORD=masterkey -v fbrepl-share:/repl \
    -v "$DIR/$3/replication.conf:/opt/firebird/replication.conf" "firebirdsql/firebird:$FB_VERSION" >/dev/null
}
run fbrepl-p "$PPORT" primary
run fbrepl-r "$RPORT" replica
for c in fbrepl-p fbrepl-r; do  # until the server answers on its service manager
  for _ in $(seq 1 60); do
    docker exec "$c" /opt/firebird/bin/fbsvcmgr service_mgr user SYSDBA password masterkey info_server_version \
      >/dev/null 2>&1 && break
    sleep 1
  done
done
docker exec fbrepl-p sh -c 'chown firebird:firebird /repl && chmod 775 /repl'
docker ps --filter name=fbrepl --format '{{.Names}} {{.Image}} {{.Status}}'
