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
| `FBADMIN_DRIVER` | `firebirdsql` | `native` to use `firebird-driver` + libfbclient for metadata connections |
| `FBADMIN_FBCLIENT` | | Path of `libfbclient` for the native driver |
| `FBADMIN_UPDATE_CHECK` | `ask` | `off` forbids the opt-in update check (see [Privacy](../PRIVACY.md)) |
| `FBADMIN_SERVERS_FILE` | unset | YAML/JSON file of Firebird servers to register at startup; see [Deployment](deployment.md#preconfigured-servers) |
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

## Native driver (optional)

`FBADMIN_DRIVER=native` uses [`firebird-driver`](https://pypi.org/project/firebird-driver/)
on top of the Firebird client library for metadata connections:

```bash
pip install "firebird-admin[native]"      # plus libfbclient: apt install libfbclient2, or a Firebird install
FBADMIN_DRIVER=native FBADMIN_FBCLIENT=/opt/firebird/lib/libfbclient.so fbadmin
```

Limitations: Query Tool sessions, creating and dropping databases and the Services API
(backup, restore, gstat, log) keep using the pure-Python driver, because they rely on its
transaction parameter blocks and statement information. Wire encryption follows the client
library's `firebird.conf` (`WireCrypt`), not the per-server checkbox. The official Docker
image does not contain libfbclient.
