#!/usr/bin/env bash
# Start (or restart) local Firebird 3/4/5/6 test servers in Docker.
# FB5 -> 127.0.0.1:3050, FB3 -> :3053, FB4 -> :3054, FB6 snapshot -> :3056 (SYSDBA / masterkey).
set -euo pipefail
if ! docker info >/dev/null 2>&1; then
  (setsid dockerd >/tmp/dockerd.log 2>&1 &)
  for _ in $(seq 1 30); do docker info >/dev/null 2>&1 && break; sleep 1; done
fi
start() {  # name tag port
  if docker inspect "$1" >/dev/null 2>&1; then
    docker start "$1" >/dev/null
  else
    docker run -d --name "$1" -p "$3:3050" -e FIREBIRD_ROOT_PASSWORD=masterkey "firebirdsql/firebird:$2" >/dev/null
  fi
}
start fb5 5 3050
start fb3 3 3053
start fb4 4 3054
start fb6 6-snapshot 3056
for p in 3050 3053 3054 3056; do
  for _ in $(seq 1 30); do (echo >/dev/tcp/127.0.0.1/$p) 2>/dev/null && break; sleep 1; done
done
docker ps --format '{{.Names}} {{.Status}}'
