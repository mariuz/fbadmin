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
3. Write the release notes in `docs/release-notes/vX.Y.Z.md`; the release workflow puts them
   at the top of the GitHub Release, above the generated list of changes.
4. Tag and push: `git tag -a v2.1.0 -m "fbadmin 2.1.0" && git push origin v2.1.0`.

The tag starts three workflows:

| Workflow | Output |
|---|---|
| `release.yml` | sdist + wheel (with the UI) → PyPI (trusted publishing) and a GitHub Release |
| `docker.yml` | `ghcr.io/mariuz/fbadmin:1.0.0`, `:1.0`, `:latest` for amd64 and arm64 |
| `desktop.yml` | Windows zip + MSI, macOS dmg (arm64, x86_64), Linux AppImage/deb/rpm, attached to the release |

`release.yml` refuses to publish when the tag does not match `__version__`.

### One-time setup

- **PyPI trusted publishing**: the distribution is **`firebird-admin`**, because the name
  `fbadmin` on PyPI belongs to an unrelated older project. The import name and the `fbadmin` /
  `fbadmin-desktop` commands are unchanged. On pypi.org, add a GitHub publisher for project
  *firebird-admin*: owner `mariuz`, repository `fbadmin`, workflow `release.yml`, environment
  `pypi`. Before the first upload, add it as a "pending publisher" under *Your account →
  Publishing*. No API token is stored in GitHub.
- **GitHub environment** `pypi` (Settings → Environments), optionally with required
  reviewers so a person approves each upload.
- **GHCR**: the workflow pushes with `GITHUB_TOKEN`; make the package public once in the
  package settings.
- **Pages**: Settings → Pages → Source: *GitHub Actions* (for `docs.yml`).

## Python package (PyPI)

```bash
scripts/build-dist.sh                 # npm ci + build UI, python -m build, scripts/check_dist.py
SMOKE=1 scripts/build-dist.sh         # also install the wheel in a fresh venv and fetch the UI
scripts/smoke-wheel.sh dist/*.whl 5106
```

`scripts/check_dist.py` fails when the wheel lacks `fbadmin/static/index.html`, when an
asset referenced by `index.html` is missing, or when a console script is missing. The
`wheel` job in CI runs the same steps on every push.

The wheel (`firebird_admin-X.Y.Z-py3-none-any.whl`, installed with `pip install firebird-admin`)
is pure Python and installs two commands: `fbadmin` (server,
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
pip install "./backend[desktop,native]" pyinstaller pillow
(cd frontend && npm ci && npm run build)
python packaging/embedded/fetch-embedded.py                     # Firebird embedded engine -> build/firebird-embedded
pyinstaller packaging/pyinstaller/fbadmin.spec --noconfirm     # -> dist/fbadmin/ or dist/fbadmin.app
packaging/linux/build-packages.sh 0.2.0 appimage deb rpm        # Linux: needs fpm (and rpmbuild)
wix build packaging/windows/fbadmin.wxs -arch x64 -wx -d SourceDir=%CD%\dist\fbadmin -d Version=0.2.0 -o fbadmin.msi
```

| Platform | Format | How |
|---|---|---|
| Windows | `.zip` (portable), `.msi` | `Compress-Archive`; WiX v5 (`packaging/windows/fbadmin.wxs`) |
| macOS | `.dmg` with `fbadmin.app` | PyInstaller `BUNDLE` + `hdiutil` (arm64 on macos-14, x86_64 on macos-15-intel) |
| Linux | AppImage, `.deb`, `.rpm` | `packaging/linux/build-packages.sh` (appimagetool, fpm) on Ubuntu 22.04 |
| Linux | Flatpak | `packaging/flatpak/io.github.mariuz.fbadmin.yml` (GNOME runtime). The `flatpak` job in `desktop.yml` builds a `.flatpak` bundle, and a failure there doesn't fail the workflow. |

Notes and limitations:

- **Signing**: `desktop.yml` signs the builds once these repository secrets exist (Settings →
  Secrets and variables → Actions). Without them the packages are unsigned, the workflow
  notes that, and macOS Gatekeeper and Windows SmartScreen warn on first start.

  | Secret | Content |
  |---|---|
  | `MACOS_CERT_P12` | base64 of the *Developer ID Application* certificate and key (`.p12`) |
  | `MACOS_CERT_PASSWORD` | password of that `.p12` |
  | `MACOS_SIGN_IDENTITY` | `Developer ID Application: Your Name (TEAMID)` |
  | `APPLE_ID`, `APPLE_TEAM_ID`, `APPLE_APP_PASSWORD` | notarization: Apple ID, team ID, app-specific password |
  | `WINDOWS_CERT_PFX` | base64 of the Authenticode code-signing certificate (`.pfx`) |
  | `WINDOWS_CERT_PASSWORD` | password of that `.pfx` |

  - **macOS:** the app is signed with the hardened runtime and
    `packaging/macos/entitlements.plist`, then the dmg is signed, notarized, stapled and
    checked with `spctl`.
  - **Windows:** `fbadmin.exe` and the MSI are signed with `signtool` and an RFC 3161
    timestamp. The zip contains the signed exe.

  Create base64 values with `base64 -i cert.p12 | pbcopy` (macOS) or
  `[Convert]::ToBase64String([IO.File]::ReadAllBytes("cert.pfx"))` (PowerShell).
- **Linux window**: the native window uses WebKitGTK through PyGObject, which is bundled at
  build time; the `.deb`/`.rpm` depend on the system WebKitGTK 4.1 libraries. The
  AppImage expects them on the host as well and otherwise falls back to the browser.
- **Flatpak**: the manifest installs the wheel with pip during the build (needs
  `--share=network` at build time). For Flathub, generate a vendored module with
  `flatpak-pip-generator` instead.
- **Embedded engine (local databases)**: the Windows, macOS and Linux builds bundle the
  minimal Firebird 5 embedded file set in `<app>/firebird`. It is fetched from the pinned,
  SHA-256-checked release by `packaging/embedded/fetch-embedded.py`, and its licences and
  notice go to `firebird/licenses/`. It adds about 7 MB (Linux), 18 MB (Windows) and 20 MB
  (macOS) compressed. The `Smoke test local (embedded) databases` step exercises it in the
  frozen app. That step is verified on Linux and does not fail the build on
  Windows/macOS yet. The Flatpak does not include it. Details, licences and open items:
  [EMBEDDED.md](EMBEDDED.md).
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
