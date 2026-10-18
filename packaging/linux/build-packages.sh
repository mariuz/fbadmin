#!/usr/bin/env bash
# Package the PyInstaller one-folder build (dist/fbadmin) as AppImage, .deb and .rpm.
#   packaging/linux/build-packages.sh <version> [appimage|deb|rpm ...]
# Needs: appimagetool (downloaded if missing) for AppImage, fpm (gem install fpm) for deb/rpm,
# rpmbuild for rpm. Output goes to dist/packages/.
#
# The native window uses pywebview's GTK backend, so the packages depend on WebKitGTK
# (libwebkit2gtk-4.1 + GObject introspection). Without it fbadmin-desktop opens the
# default browser instead.
set -euo pipefail
version="${1:?version}"; shift || true
formats=("$@"); [ ${#formats[@]} -eq 0 ] && formats=(appimage deb rpm)
root="$(cd "$(dirname "$0")/../.." && pwd)"
src="$root/dist/fbadmin"
out="$root/dist/packages"
arch="$(uname -m)"
test -x "$src/fbadmin" || { echo "run pyinstaller first (dist/fbadmin missing)" >&2; exit 1; }
mkdir -p "$out"
work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT

# Common file system layout: /opt/fbadmin + launcher + desktop entry + icons.
stage="$work/stage"
mkdir -p "$stage/opt" "$stage/usr/bin" "$stage/usr/share/applications" \
         "$stage/usr/share/icons/hicolor/512x512/apps" "$stage/usr/share/icons/hicolor/scalable/apps"
cp -a "$src" "$stage/opt/fbadmin"
ln -s /opt/fbadmin/fbadmin "$stage/usr/bin/fbadmin-desktop"
cp "$root/packaging/linux/io.github.mariuz.fbadmin.desktop" "$stage/usr/share/applications/"
cp "$root/packaging/icons/fbadmin.png" "$stage/usr/share/icons/hicolor/512x512/apps/io.github.mariuz.fbadmin.png"
cp "$root/packaging/icons/fbadmin.svg" "$stage/usr/share/icons/hicolor/scalable/apps/io.github.mariuz.fbadmin.svg"

fpm_common=(-s dir -C "$stage" -n fbadmin -v "$version" --license MIT
  --url https://github.com/mariuz/fbadmin --maintainer "fbadmin contributors"
  --description "pgAdmin-style administration tool for Firebird SQL (desktop app)")

for f in "${formats[@]}"; do
  case "$f" in
    deb)
      fpm "${fpm_common[@]}" -t deb -a native \
        --depends libgtk-3-0 --depends "libwebkit2gtk-4.1-0 | libwebkit2gtk-4.0-37" \
        --depends gir1.2-webkit2-4.1 \
        -p "$out/fbadmin_${version}_ARCH.deb" opt usr
      ;;
    rpm)
      fpm "${fpm_common[@]}" -t rpm -a native \
        --depends gtk3 --depends webkit2gtk4.1 \
        --rpm-rpmbuild-define "_build_id_links none" \
        -p "$out/fbadmin-${version}.ARCH.rpm" opt usr
      ;;
    appimage)
      appdir="$work/fbadmin.AppDir"
      mkdir -p "$appdir/usr"
      cp -a "$src" "$appdir/usr/fbadmin"
      cp "$root/packaging/linux/io.github.mariuz.fbadmin.desktop" "$appdir/"
      sed -i 's/^Exec=.*/Exec=fbadmin/' "$appdir/io.github.mariuz.fbadmin.desktop"
      cp "$root/packaging/icons/fbadmin.png" "$appdir/io.github.mariuz.fbadmin.png"
      cat > "$appdir/AppRun" <<'EOF'
#!/bin/sh
here="$(dirname "$(readlink -f "$0")")"
exec "$here/usr/fbadmin/fbadmin" "$@"
EOF
      chmod +x "$appdir/AppRun"
      tool="$(command -v appimagetool || true)"
      if [ -z "$tool" ]; then
        tool="$work/appimagetool"
        curl -fsSL -o "$tool" "https://github.com/AppImage/appimagetool/releases/download/continuous/appimagetool-${arch}.AppImage"
        chmod +x "$tool"
      fi
      ARCH="$arch" APPIMAGE_EXTRACT_AND_RUN=1 "$tool" "$appdir" "$out/fbadmin-${version}-${arch}.AppImage"
      ;;
    *) echo "unknown format $f" >&2; exit 2 ;;
  esac
done
ls -l "$out"
