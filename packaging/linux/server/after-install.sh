#!/bin/sh
# deb: $1 = configure, $2 = the previous version (empty on a first install); rpm: $1 = 1 install, 2 upgrade
set -e
command -v systemctl >/dev/null 2>&1 || exit 0
[ -d /run/systemd/system ] || exit 0
systemctl daemon-reload || true
if { [ "$1" = configure ] && [ -z "$2" ]; } || [ "$1" = 1 ]; then
  systemctl enable --now fbadmin.service || true
else
  systemctl try-restart fbadmin.service || true
fi
