# Getting started

fbadmin talks to Firebird over the network with the pure-Python
[`firebirdsql`](https://github.com/nakagami/pyfirebirdsql) driver, so you do not need a
Firebird client library. It works with Firebird 3.0, 4.0 and 5.0 servers, and with the
Firebird 6.0 snapshots (see [Firebird 6 and SQL schemas](FIREBIRD6.md)).

## Desktop app

Installers for Windows (`.msi`), macOS (`.dmg`, Apple silicon and Intel) and Linux (AppImage,
`.deb`, `.rpm`, Flatpak) are attached to each
[GitHub release](https://github.com/mariuz/fbadmin/releases), along with a `SHA256SUMS` file.
The desktop app can update itself: About → **Download and install**.

## Install with pip

Requires Python 3.10 or newer. The package contains the prebuilt web UI.

```bash
python -m pip install firebird-admin
fbadmin                      # starts on http://localhost:5050 and opens your browser
fbadmin --no-browser --port 8080
```

Command-line options:

| Option | Meaning |
|---|---|
| `--host`, `--port` | Bind address (default `127.0.0.1:5050`) |
| `--no-browser` | Do not open a browser window |
| `--log-level` | `critical`, `error`, `warning`, `info` (default) or `debug` |
| `--log-format` | `text` (default) or `json` (one JSON object per line) |

Optional extras:

```bash
pip install "firebird-admin[desktop]"   # native window for fbadmin-desktop (pywebview)
# firebird-driver (the native driver) is installed with fbadmin; it is used automatically when a
# Firebird 4+ client library is present, see Administration → Configuration
```

## Desktop app

`fbadmin-desktop` starts fbadmin on a free local port and shows it in a native window
(WebView2 on Windows, WebKit on macOS and Linux). If no native web view is available it
opens your browser instead. Installers built from each release are on the
[releases page](https://github.com/mariuz/fbadmin/releases):

| Platform | Files |
|---|---|
| Windows | `fbadmin-X.Y.Z-x64.msi`, or the portable `fbadmin-X.Y.Z-windows-x64.zip` |
| macOS | `fbadmin-X.Y.Z-macos-arm64.dmg` (Apple silicon), `...-macos-x86_64.dmg` (Intel) |
| Linux | `fbadmin-X.Y.Z-x86_64.AppImage`, `fbadmin_X.Y.Z_amd64.deb`, `fbadmin-X.Y.Z.x86_64.rpm` |

The builds are not code-signed yet: on macOS right-click the app and choose **Open** the
first time; on Windows choose **More info → Run anyway** in SmartScreen.

## Docker

```bash
docker run -d --name fbadmin -p 127.0.0.1:5050:5050 -v fbadmin:/data ghcr.io/mariuz/fbadmin
```

The repository's `docker-compose.yml` starts fbadmin together with a Firebird 5 server.
See [Deployment](admin/deployment.md) for Kubernetes and Helm.

## First steps

1. Click **Add server**. Enter the host and port of your Firebird server, the user
   (usually `SYSDBA`) and the password. Optionally enter the path (or alias) of a first
   database, for example `/var/lib/firebird/data/employee.fdb`.
2. Expand the server and the database in the **Object Explorer**.
3. Select a table: the **Properties**, **SQL** (DDL) and **Dependencies** tabs describe it.
4. Open the **Query Tool** from the toolbar to run SQL.

!!! warning "Local use by default"
    Without server mode fbadmin has no login screen and listens on `127.0.0.1` only.
    Do not expose it on a network you do not trust. See [Administration](admin/index.md).
