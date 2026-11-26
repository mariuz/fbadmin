# Running as a service (Windows Server, Linux)

The **server edition** runs fbadmin as an operating system service: it starts with the machine, restarts
after a failure, and is installed, upgraded and removed like any other server software. It is the same
fbadmin as `pip install firebird-admin`, built without the desktop window.

| | Windows Server | Linux |
|---|---|---|
| Package | `fbadmin-server-<version>-x64.msi` | `fbadmin-server_<version>_amd64.deb`, `fbadmin-server-<version>.x86_64.rpm` |
| Program | `C:\Program Files\fbadmin-server` | `/opt/fbadmin-server` (`/usr/bin/fbadmin-server`) |
| Service | `fbadmin`, account `NT SERVICE\fbadmin` (a virtual account: no password to manage) | `fbadmin.service`, a dynamic user (`DynamicUser=yes`) |
| Settings | `C:\ProgramData\fbadmin\fbadmin.toml` | `/etc/fbadmin/fbadmin.toml` |
| Data | `C:\ProgramData\fbadmin` | `/var/lib/fbadmin` |
| Logs | `C:\ProgramData\fbadmin\logs` (rotated) | the journal (`journalctl -u fbadmin`), optionally a log file |
| Update check | off (roll out new versions yourself) | off (the package manager updates) |

The packages are attached to every [GitHub release](https://github.com/mariuz/fbadmin/releases). Settings and data are
kept on upgrade and on removal.

## Windows Server

Install interactively, or silently with your software distribution (Group Policy, Intune, SCCM):

```bat
msiexec /i fbadmin-server-2.2.0-x64.msi /qn PORT=5050 HOST=0.0.0.0 FIREWALL=1 ^
    SERVER_MODE=1 ADMIN_USER=admin ADMIN_PASSWORD="..." ^
    CERTFILE=C:\tls\fbadmin.crt KEYFILE=C:\tls\fbadmin.key
```

| Property | Meaning |
|---|---|
| `PORT` | Port to listen on, written into `fbadmin.toml` (default 5050) |
| `HOST` | Address to listen on, written into `fbadmin.toml` (default `127.0.0.1`: this machine only) |
| `FIREWALL=1` | Adds an inbound Windows Firewall rule `fbadmin` for the port (removed on uninstall) |
| `SERVER_MODE=1` | Multi-user [server mode](../SECURITY.md) with logins (`0` turns it off) |
| `ADMIN_USER`, `ADMIN_EMAIL`, `ADMIN_PASSWORD` | The first administrator of server mode (`ADMIN_USER` defaults to `admin`; the password needs 8 characters or more) |
| `CERTFILE`, `KEYFILE` | Serve HTTPS with these PEM files; the service must be able to read them |

The settings are written into `fbadmin.toml` only when given, so an upgrade without them keeps your settings.
The service starts at the end of the install; open `http://<server>:5050/` (`https://` with a certificate).

**The first administrator.** The account properties go into a one-time file, `initial-admin.ini` in the data
directory, which fbadmin reads at its first start in server mode and **deletes once the account exists**. They
are not stored in the registry or in `fbadmin.toml`, and `ADMIN_PASSWORD` is hidden in MSI logs. When accounts
exist already (an upgrade), the file is deleted and nothing changes. Without `ADMIN_PASSWORD`, the first visitor
of the page creates the administrator: install with `HOST=127.0.0.1` until you have done that yourself, or
give the password at install time.

- Manage it like any service: `Get-Service fbadmin`, `Restart-Service fbadmin` (after changing
  `fbadmin.toml`), or *Services* (`services.msc`).
- The data directory holds saved passwords and keys: the installer lets only SYSTEM, Administrators and the
  service open it.
- The service host is [WinSW](https://github.com/winsw/winsw) (MIT), installed as `fbadmin-service.exe`: it runs
  `fbadmin-server.exe`, restarts it after a failure (10 s, then 30 s), and stops it with Ctrl+C, so fbadmin
  finishes running requests and shuts down cleanly. Its own log, with fbadmin's output, is in
  `C:\ProgramData\fbadmin\logs` (rotated at 10 MB, 5 files kept).
- Uninstall: *Apps and features*, or `msiexec /x fbadmin-server-2.2.0-x64.msi /qn`.
- The server edition and the desktop app install side by side.

## Linux (deb, rpm)

```bash
sudo apt install ./fbadmin-server_2.2.0_amd64.deb      # Debian, Ubuntu
sudo dnf install ./fbadmin-server-2.2.0.x86_64.rpm     # RHEL, Fedora, Rocky, Alma
```

The service is enabled and started on install, and restarted on upgrade. Change `/etc/fbadmin/fbadmin.toml`
(kept on upgrade: it is a configuration file of the package), then `sudo systemctl restart fbadmin`. Logs are in
the journal: `journalctl -u fbadmin -f`.

The unit runs as a dynamic user with a read-only system (`ProtectSystem=strict`), a private `/tmp` and no access
to home directories; it can write only `/var/lib/fbadmin` and `/var/log/fbadmin`. A certificate for HTTPS must
be readable by the service, e.g. in `/etc/fbadmin/tls`.

## The configuration file

Every setting of fbadmin is an environment variable ([Configuration](configuration.md)). `fbadmin.toml` sets
them in one place:

```toml
[server]
host = "0.0.0.0"          # FBADMIN_HOST
port = 5050               # FBADMIN_PORT
server_mode = true        # FBADMIN_SERVER_MODE=1: logins (recommended when others can reach fbadmin)
log_file = "logs/fbadmin.log"   # FBADMIN_LOG_FILE, relative to the data directory, rotated

[env]                     # any other variable by its name
FBADMIN_MAX_UPLOAD_BYTES = 2147483648
```

A key of `[server]` (or at the top level) sets `FBADMIN_<KEY>`; `true` / `false` become `1` / `0`. An environment
variable that is already set wins over the file, and command-line options win over both. The file is
`--config PATH`, else `$FBADMIN_CONFIG`, else `fbadmin.toml` in the data directory when it exists. It works the
same with `pip` installs and in containers.

## HTTPS

Do not expose fbadmin over plain HTTP beyond the machine it runs on. Either:

- **the built-in option**: `ssl_certfile` and `ssl_keyfile` (PEM files) in `fbadmin.toml`; or
- **a reverse proxy** that terminates TLS, with `proxy_headers = true` in `fbadmin.toml` and fbadmin listening on
  `127.0.0.1`.

**IIS** with [Application Request Routing](https://www.iis.net/downloads/microsoft/application-request-routing) and
URL Rewrite: enable the proxy (*ARR → Server Proxy Settings → Enable proxy*), bind the site to HTTPS with your
certificate and add a rewrite rule:

```xml
<!-- web.config of the IIS site -->
<configuration>
  <system.webServer>
    <rewrite>
      <rules>
        <rule name="fbadmin" stopProcessing="true">
          <match url="(.*)" />
          <action type="Rewrite" url="http://127.0.0.1:5050/{R:1}" />
          <serverVariables>
            <set name="HTTP_X_FORWARDED_PROTO" value="https" />
          </serverVariables>
        </rule>
      </rules>
    </rewrite>
    <!-- uploads of large backups -->
    <security><requestFiltering><requestLimits maxAllowedContentLength="4294967295" /></requestFiltering></security>
  </system.webServer>
</configuration>
```

Allow the server variable `HTTP_X_FORWARDED_PROTO` in *URL Rewrite → View Server Variables*.

**Caddy** (Windows or Linux) gets and renews a certificate by itself:

```text
fbadmin.example.com {
    reverse_proxy 127.0.0.1:5050
}
```

See [Security](../SECURITY.md) for server mode, logins and the checklist of a shared deployment.

## Health check

`GET /api/health` answers `{"status": "ok", "version": "..."}` without a login, for load balancers, monitoring
and service managers.

## Building the packages

The release workflow (`.github/workflows/desktop.yml`, jobs *server-windows* and *server-linux*) builds and tests
them: install silently, wait for the health check, restart the service, uninstall. To build by hand:

```bash
(cd frontend && npm ci && npm run build)
pip install ./backend pyinstaller
pyinstaller packaging/pyinstaller/fbadmin-server.spec --noconfirm      # dist/fbadmin-server
packaging/linux/build-server-packages.sh 2.2.0 deb rpm                 # Linux, needs fpm
```

On Windows, download [WinSW-x64.exe v2.12.0](https://github.com/winsw/winsw/releases/tag/v2.12.0) and build the
MSI with WiX 5 as shown at the top of `packaging/windows/server/fbadmin-server.wxs`.
