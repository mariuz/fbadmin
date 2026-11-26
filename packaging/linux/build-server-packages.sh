#!/usr/bin/env bash
# Package the headless server build (dist/fbadmin-server, packaging/pyinstaller/fbadmin-server.spec) as .deb and
# .rpm with a systemd unit (issue #11).
#   packaging/linux/build-server-packages.sh <version> [deb|rpm ...]
# Needs fpm (gem install fpm), and rpmbuild for rpm. Output: dist/packages/fbadmin-server_*.deb, fbadmin-server-*.rpm.
#
# Installs /opt/fbadmin-server, /usr/bin/fbadmin-server, /etc/fbadmin/fbadmin.toml (a config file: kept on upgrade)
# and fbadmin.service, which is enabled and started on install. Data: /var/lib/fbadmin (kept on removal).
set -euo pipefail
version="${1:?version}"; shift || true
formats=("$@"); [ ${#formats[@]} -eq 0 ] && formats=(deb rpm)
root="$(cd "$(dirname "$0")/../.." && pwd)"
src="$root/dist/fbadmin-server"
here="$root/packaging/linux/server"
out="$root/dist/packages"
test -x "$src/fbadmin-server" || { echo "run pyinstaller packaging/pyinstaller/fbadmin-server.spec first" >&2; exit 1; }
mkdir -p "$out"
work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT

stage="$work/stage"
mkdir -p "$stage/opt" "$stage/usr/bin" "$stage/etc/fbadmin" "$stage/usr/lib/systemd/system"
cp -a "$src" "$stage/opt/fbadmin-server"
ln -s /opt/fbadmin-server/fbadmin-server "$stage/usr/bin/fbadmin-server"
install -m 0644 "$here/fbadmin.toml" "$stage/etc/fbadmin/fbadmin.toml"
install -m 0644 "$here/fbadmin.service" "$stage/usr/lib/systemd/system/fbadmin.service"

fpm_common=(-s dir -C "$stage" -n fbadmin-server -v "$version" --license MIT
  --url https://github.com/mariuz/fbadmin --maintainer "fbadmin contributors"
  --description "Web administration for Firebird SQL, as a systemd service (fbadmin server edition)"
  --config-files /etc/fbadmin/fbadmin.toml
  --after-install "$here/after-install.sh" --before-remove "$here/before-remove.sh"
  --after-remove "$here/after-remove.sh")

for f in "${formats[@]}"; do
  case "$f" in
    deb) fpm "${fpm_common[@]}" -t deb -a native --depends systemd -p "$out/fbadmin-server_${version}_ARCH.deb" opt usr etc ;;
    rpm) fpm "${fpm_common[@]}" -t rpm -a native --depends systemd --rpm-rpmbuild-define "_build_id_links none" \
           -p "$out/fbadmin-server-${version}.ARCH.rpm" opt usr etc ;;
    *) echo "unknown format $f" >&2; exit 2 ;;
  esac
done
ls -l "$out"
