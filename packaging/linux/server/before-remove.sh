#!/bin/sh
# deb: $1 = remove / upgrade; rpm: $1 = 0 remove, 1 upgrade. The data in /var/lib/fbadmin stays.
set -e
command -v systemctl >/dev/null 2>&1 || exit 0
if [ "$1" = remove ] || [ "$1" = 0 ]; then
  systemctl disable --now fbadmin.service || true
fi
