# Configuration

fbadmin is configured with command-line options and environment variables.

## Command line

```text
fbadmin [--host HOST] [--port PORT] [--no-browser] [--reload]
        [--log-level {critical,error,warning,info,debug}] [--log-format {text,json}]
fbadmin-desktop [--port PORT] [--browser] [--log-level LEVEL]
```

## Environment variables

| Variable | Default | Meaning |
|---|---|---|
| `FBADMIN_DATA_DIR` | `~/.fbadmin` | Data directory (`servers.json`, mode 0600) |
| `FBADMIN_HOST` / `FBADMIN_PORT` | `127.0.0.1` / `5050` | Bind address |
| `FBADMIN_MAX_ROWS` | `1000` | Default row limit per query result |
| `FBADMIN_SESSION_IDLE_TIMEOUT` | `3600` | Seconds before an idle Query Tool connection is closed |
| `FBADMIN_LOG_LEVEL` | `info` | Same as `--log-level` |
| `FBADMIN_LOG_FORMAT` | `text` | `json` for structured logs (same as `--log-format`) |
| `FBADMIN_ACCESS_LOG` | `1` | `0` turns off the per-request log |
| `FBADMIN_RATE_LIMIT` | `0` (off) | API requests per minute per client IP (token bucket, same burst) |
| `FBADMIN_RATE_LIMIT_TRUST_PROXY` | `0` | `1`: identify clients by `X-Forwarded-For` (only behind a trusted proxy) |
| `FBADMIN_MAX_REQUEST_BYTES` | `33554432` (32 MiB) | Maximum request body; larger requests get HTTP 413. `0` = unlimited |
| `FBADMIN_META_POOL_SIZE` | `3` | Metadata connections per database (see below) |
| `FBADMIN_DRIVER` | `auto` | Database driver for server connections: `auto` (native `firebird-driver` when a Firebird 4+ client library is available, else pure-Python `firebirdsql`), `native` or `firebirdsql`. Overrides the setting in the UI (see [Drivers](../DRIVERS.md)) |
| `FBADMIN_FBCLIENT` | | Path of `libfbclient` for the native driver (default: the embedded engine's, else the system library path). Overrides the setting in the UI |
| `FBADMIN_UPDATE_CHECK` | `ask` | `off` forbids the opt-in update check (see [Privacy](../PRIVACY.md)) |
| `FBADMIN_SERVERS_FILE` | unset | YAML/JSON file of Firebird servers to register at startup; see [Deployment](deployment.md#preconfigured-servers) |
| `FBADMIN_KUBERNETES` | auto | `off` turns off the [Kubernetes page](../KUBERNETES.md); `on` also uses `$KUBECONFIG` / `~/.kube/config`. Auto: the pod's service account |
| `FBADMIN_KUBECONFIG` / `FBADMIN_KUBE_CONTEXT` | unset | Kubeconfig file (and context) for the Kubernetes page |
| `FBADMIN_K8S_NAMESPACES` | own namespace | Namespaces with Firebird clusters, comma-separated; `*` for all |
| `FBADMIN_BACKUP_KEY` | unset | Backup encryption keys (`fbk1:…`, comma-separated; the first encrypts), see [Backup encryption](../BACKUP_ENCRYPTION_DRILLS.md#keys) |
| `FBADMIN_REPLICATION_CONF` | unset | Server mode: the `replication.conf` files the [replica setup](../REPLICATION.md#writing-replicationconf) may write |
| `FBADMIN_MIGRATIONS_ROOT` | unset | Server mode: [migration](../MIGRATIONS.md#sources) directories on the fbadmin host must be inside it |
| `FBADMIN_PLUGINS` | on | `off` loads no plugins: a safe mode for troubleshooting, also used when generating the API types |

## Logging

Text logs look like `2026-10-18 20:01:02,345 INFO    fbadmin.access: GET /api/servers 200 3.1ms`.
With `--log-format json` every line is one JSON object:

```json
{"ts": "2026-10-18T20:01:02.345+00:00", "level": "info", "logger": "fbadmin.access",
 "msg": "GET /api/servers 200 3.1ms", "method": "GET", "path": "/api/servers",
 "status": 200, "duration_ms": 3.1, "client": "10.0.0.7"}
```

Requests for static assets are logged at `debug` level, server errors (5xx) at `error`.
Query text and parameters are never logged.

## Request limits

The rate limiter and the body size limit are generic and apply to every API request
(login throttling in server mode is separate). A rejected request gets HTTP 429 with a
`Retry-After` header (`/api/health` is never throttled, so probes keep working), or HTTP 413, and a JSON body with `code` `rate_limited` or
`request_too_large`. The UI polls a few endpoints every few seconds per open tab, so keep
the rate limit well above 60 requests per minute per user.

## Metadata connections

The object explorer, dashboards, DDL and the data grid share a small pool of
connections per database (`FBADMIN_META_POOL_SIZE`). The first connection is used
whenever it is free; extra ones open only while it is busy (a slow DDL extraction, a big
`MON$` query) and close again after two idle minutes. Every Query Tool tab has its own
connection, separate from the pool.

## Database driver

The **Database driver** setting (user menu → *Database driver…*, administrators only in
server mode) chooses the driver for every server connection (object explorer, Query Tool,
create/drop database and the Services API):

- **Automatic** (default): [`firebird-driver`](https://pypi.org/project/firebird-driver/) on
  top of the Firebird client library when a **Firebird 4 or newer** client is available,
  otherwise the pure-Python `firebirdsql`. The panel and the startup log say which one is in
  use and why;
- **Native**: always `firebird-driver` (refused when it cannot be used);
- **Pure Python**: always `firebirdsql`, no native library.

`firebird-driver` is installed with fbadmin (Python 3.11+); the client library comes with the
desktop app and the Docker image, or from a Firebird installation or your distribution's
`libfbclient2` (Firebird 4 from Debian 13 and Ubuntu 25.04 on). The setting is stored in
`driver.json` in the data directory. `FBADMIN_DRIVER` and `FBADMIN_FBCLIENT` override it:

```bash
FBADMIN_FBCLIENT=/opt/firebird/lib/libfbclient.so fbadmin   # Automatic with this client library
FBADMIN_DRIVER=firebirdsql fbadmin                          # always the pure-Python driver
```

The event monitor stays on `firebirdsql`. See [Drivers](../DRIVERS.md) for the selection
rules, the fallback, the per-feature table and the benchmarks.
