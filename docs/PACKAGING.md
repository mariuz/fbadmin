# Packaging and releases

Everything is built by GitHub Actions from a version tag. This page explains the pieces
and how to build them locally.

## Version

The version lives in one place: `__version__` in `backend/fbadmin/__init__.py`
(`pyproject.toml` reads it dynamically). Keep `frontend/package.json` and the Helm chart's
`appVersion` in step when releasing.

## Release checklist

1. Bump `__version__`, `deploy/helm/fbadmin/Chart.yaml` (`appVersion`, and `version` if the
   chart changed) and the `<release>` entry in
   `packaging/flatpak/io.github.mariuz.fbadmin.metainfo.xml`.
2. Run `scripts/gen-api-types.sh` if the API changed; make sure CI is green.
3. Tag and push: `git tag v1.0.0 && git push origin v1.0.0`.

The tag starts three workflows:

| Workflow | Output |
|---|---|
| `release.yml` | sdist + wheel (with the UI) → PyPI (trusted publishing) and a GitHub Release |
| `docker.yml` | `ghcr.io/mariuz/fbadmin:1.0.0`, `:1.0`, `:latest` for amd64 and arm64 |
| `desktop.yml` | Windows zip + MSI, macOS dmg (arm64, x86_64), Linux AppImage/deb/rpm, attached to the release |

`release.yml` refuses to publish when the tag does not match `__version__`.

### One-time setup

- **PyPI trusted publishing**: on pypi.org, project *fbadmin* → *Publishing* → add a
  GitHub publisher: owner `mariuz`, repository `fbadmin`, workflow `release.yml`,
  environment `pypi`. No API token is stored in GitHub. (For the very first upload use a
  "pending publisher".)
- **GitHub environment** `pypi` (Settings → Environments), optionally with required
  reviewers so a person approves each upload.
- **GHCR**: the workflow pushes with `GITHUB_TOKEN`; make the package public once in the
  package settings.
- **Pages**: Settings → Pages → Source: *GitHub Actions* (for `docs.yml`).

## Python package (PyPI)

```bash
scripts/build-dist.sh                 # npm ci + build UI, python -m build, scripts/check_dist.py
SMOKE=1 scripts/build-dist.sh         # also install the wheel in a fresh venv and fetch the UI
scripts/smoke-wheel.sh dist/fbadmin-*.whl 5106
```

`scripts/check_dist.py` fails when the wheel lacks `fbadmin/static/index.html`, when an
asset referenced by `index.html` is missing, or when a console script is missing. The
`wheel` job in CI runs the same steps on every push.

The wheel is pure Python (`py3-none-any`) and installs two commands: `fbadmin` (server,
opens the browser) and `fbadmin-desktop` (native window). Extras: `desktop` (pywebview),
`native` (firebird-driver), `dev` (tests).

## Docker image

`Dockerfile` builds the UI in a `node` stage (on the build platform, since the output is
architecture-independent) and installs the backend into `python:3.12-slim`. It runs as
uid 1000, exposes 5050, keeps data in `/data` and has a `HEALTHCHECK` on `/api/health`.
`docker.yml` builds `linux/amd64,linux/arm64` with QEMU + Buildx, pushes `edge` from `main`
and semver tags from releases, with SBOM and provenance attestations. Pull requests that
touch the Dockerfile build both platforms without pushing.

## Desktop app

`fbadmin-desktop` (`backend/fbadmin/desktop.py`) starts uvicorn on a free `127.0.0.1` port
in a background thread and opens a [pywebview](https://pywebview.flowrl.com/) window
(WebView2 / WKWebView / WebKitGTK). Without pywebview or a GUI backend it falls back to the
default browser and keeps serving until Ctrl+C.

The frozen app is built with PyInstaller:

```bash
pip install "./backend[desktop]" pyinstaller pillow
(cd frontend && npm ci && npm run build)
pyinstaller packaging/pyinstaller/fbadmin.spec --noconfirm     # -> dist/fbadmin/ or dist/fbadmin.app
packaging/linux/build-packages.sh 0.2.0 appimage deb rpm        # Linux: needs fpm (and rpmbuild)
wix build packaging/windows/fbadmin.wxs -arch x64 -d SourceDir=dist\fbadmin -d Version=0.2.0 -o fbadmin.msi
```

| Platform | Format | How |
|---|---|---|
| Windows | `.zip` (portable), `.msi` | `Compress-Archive`; WiX v5 (`packaging/windows/fbadmin.wxs`) |
| macOS | `.dmg` with `fbadmin.app` | PyInstaller `BUNDLE` + `hdiutil` (arm64 on macos-14, x86_64 on macos-13) |
| Linux | AppImage, `.deb`, `.rpm` | `packaging/linux/build-packages.sh` (appimagetool, fpm) on Ubuntu 22.04 |
| Linux | Flatpak | `packaging/flatpak/io.github.mariuz.fbadmin.yml` (GNOME runtime). The `flatpak` job in `desktop.yml` builds a `.flatpak` bundle, and a failure there doesn't fail the workflow. |

Notes and limitations:

- **Signing**: builds are not code-signed or notarized yet. Add an Apple Developer ID
  (`codesign` + `notarytool`) and a Windows Authenticode certificate (`signtool`) as
  secrets to remove the Gatekeeper/SmartScreen warnings.
- **Linux window**: the native window uses WebKitGTK through PyGObject, which is bundled at
  build time; the `.deb`/`.rpm` depend on the system WebKitGTK 4.1 libraries. The
  AppImage expects them on the host as well and otherwise falls back to the browser.
- **Flatpak**: the manifest installs the wheel with pip during the build (needs
  `--share=network` at build time). For Flathub, generate a vendored module with
  `flatpak-pip-generator` instead.
- **MSI**: built with WiX v5's `Files` harvesting; the step is allowed to fail without
  failing the job, the zip is always produced.

### Updates

The desktop app never updates itself silently. Users opt in to **Check GitHub for new
releases** in the About dialog. When a newer release exists, the top bar shows "Update X.Y.Z"
and the About dialog offers **Download and install** (`backend/fbadmin/updater.py`):

1. It picks the package for this platform from the latest release: `fbadmin-X.Y.Z-x64.msi`,
   `fbadmin-X.Y.Z-macos-<arch>.dmg` or `fbadmin-X.Y.Z-<arch>.AppImage`.
2. It downloads the package into `<data dir>/updates` and checks it against the release's
   `SHA256SUMS`, which the desktop workflow publishes. A package with no checksum, or a wrong
   one, is refused.
3. It installs the package:

   | Install | What happens |
   |---|---|
   | Windows (MSI) | Runs `msiexec /i … /passive`, then fbadmin quits so the installer can replace it. |
   | Linux (AppImage) | Atomically replaces the running AppImage (`$APPIMAGE`), then restarts. |
   | macOS (dmg) | Opens the disk image; you drag fbadmin to Applications. An unsigned app cannot replace itself. |

deb, rpm, Flatpak, pip, Docker and Helm installs don't update themselves. Their package
manager does that, and the UI only links to the release. Self-update needs the desktop
launcher (`FBADMIN_DESKTOP=1`), so a server install never offers it, and
`FBADMIN_UPDATE_CHECK=off` turns it off together with the check. See [Privacy](PRIVACY.md).

## Helm chart and Kubernetes

See [Deployment](admin/deployment.md). CI runs `helm lint` and `helm template` with server
mode and ingress enabled.

## Documentation site

```bash
pip install -r requirements-docs.txt
mkdocs serve           # http://127.0.0.1:8000
mkdocs build --strict
```

`docs.yml` deploys `main` to GitHub Pages. To add a page, put it in `docs/` and list it
in `nav` in `mkdocs.yml`.
