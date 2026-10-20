#!/usr/bin/env bash
# Start Firebird 3/4/5/6 test servers for the event monitor tests.
#
# Firebird delivers events over a second ("auxiliary") TCP connection that the *client* opens
# to a port the server advertises (RemoteAuxPort, random by default). Behind Docker port
# mapping that only works when the aux port is fixed and published 1:1 (host port = container
# port), so each container gets RemoteAuxPort set through FIREBIRD_CONF_RemoteAuxPort.
#
#   name   version      main port  aux port
#   fbev3  3            3062       3063
#   fbev4  4            3064       3065
#   fbev5  5            3060       3061
#   fbev6  6-snapshot   3066       3067
#
# Usage: scripts/events-test-server.sh [3|4|5|6 ...]   (default: 3 4 5)
# Prints the environment exports for the tests (FBADMIN_TEST_EVENTS_PORT per version).
set -euo pipefail
versions=("$@")
[ ${#versions[@]} -eq 0 ] && versions=(3 4 5)
if ! docker info >/dev/null 2>&1; then
  (setsid dockerd >/tmp/dockerd.log 2>&1 &)
  for _ in $(seq 1 30); do docker info >/dev/null 2>&1 && break; sleep 1; done
fi
for v in "${versions[@]}"; do
  case "$v" in
    3) port=3062; aux=3063; tag=3 ;;
    4) port=3064; aux=3065; tag=4 ;;
    5) port=3060; aux=3061; tag=5 ;;
    6) port=3066; aux=3067; tag=6-snapshot ;;
    *) echo "unknown version $v" >&2; exit 2 ;;
  esac
  name="fbev$v"
  if docker inspect "$name" >/dev/null 2>&1; then
    docker start "$name" >/dev/null
  else
    docker run -d --name "$name" -p "$port:3050" -p "$aux:$aux" \
      -e FIREBIRD_ROOT_PASSWORD=masterkey -e "FIREBIRD_CONF_RemoteAuxPort=$aux" \
      "firebirdsql/firebird:$tag" >/dev/null
  fi
  for _ in $(seq 1 60); do (echo >/dev/tcp/127.0.0.1/$port) 2>/dev/null && break; sleep 1; done
  echo "# Firebird $v: main port $port, RemoteAuxPort $aux"
  echo "export FBADMIN_TEST_EVENTS_PORT=$port"
done
