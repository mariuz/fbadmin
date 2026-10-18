# fbadmin

**A pgAdmin-style web administration and development tool for [Firebird SQL](https://firebirdsql.org).**

fbadmin gives Firebird a modern, browser-based admin UI. It has an object explorer,
DDL extraction, a query tool with plans and transactions, an editable data grid, live
monitoring and backup/restore. It runs as a local app (like pgAdmin's desktop mode) or
as a container next to your server.

![Query Tool](docs/screenshots/query-tool.png)

> **Status:** early development (v0.1). See the [ROADMAP](ROADMAP.md) for where it is going.
> Tested against Firebird **3.0, 4.0 and 5.0**.

## Features

| Area | What works today |
|---|---|
| **Object Explorer** | Servers → databases → tables, views, procedures, functions, packages, triggers, sequences, domains, exceptions, indices, roles, users and system tables. Columns are shown under each table. Objects load lazily. |
| **Properties** | Columns (domain, identity, computed, collation), constraints (PK/UK/FK/CHECK with rules), indices (selectivity, expression, partial), triggers, parameters, comments. |
| **SQL (DDL)** | Re-creatable DDL for every object type. A round-trip test checks it on every CI run: extract, recreate in an empty database, compare. |
| **Dependencies** | "Depends on" and "Used by" from `RDB$DEPENDENCIES`, with click-through navigation. |
| **Query Tool** | CodeMirror editor with Firebird keywords and table autocompletion. Runs a selection or a whole script: `SET TERM`, and PSQL blocks without `SET TERM`, are both split correctly. Multiple result sets, execution plans, auto-commit or manual transactions (commit/rollback), row limits, query history, open/save `.sql` files, CSV/JSON export. |
| **Data view** | Paged table data with sort and `WHERE` filters. Edit inside the grid: add, update and delete rows, all saved in one transaction. Rows are keyed by primary key, or by `RDB$DB_KEY` when the table has none. |
| **Dashboard** | Database header info (ODS, page size, size, dialect, charset, forced writes, sweep interval, replica mode) and transaction counters (OIT/OAT/OST, with a gap warning). Object counts per type. |
| **Monitor** | Live `MON$` view of attachments, running statements and transactions, plus I/O stats. Disconnect an attachment or cancel a running statement. |
| **Maintenance** | Backup and restore through the Services API, plus sweep, `gstat` statistics and index selectivity recompute. These run as background **Processes** with live logs. |
| **Server** | Server version and architecture, attached databases, security database, `firebird.log` viewer. Create, register and drop databases. |
| **Security** | User management (`SEC$USERS`) with a create/edit dialog and SQL preview. Roles with their members. |
| **UI** | Resizable panes, context menus everywhere, light, dark and auto themes, a Create Table dialog with SQL preview. |

<table>
<tr><td><img src="docs/screenshots/properties.png" alt="Properties"></td><td><img src="docs/screenshots/ddl.png" alt="DDL"></td></tr>
<tr><td><img src="docs/screenshots/data.png" alt="Data grid"></td><td><img src="docs/screenshots/monitor.png" alt="Monitor"></td></tr>
</table>

## Quick start

### With Docker Compose (includes a Firebird 5 server)

```bash
docker compose up --build
# open http://localhost:5050
# Add server → host "firebird", user SYSDBA, password "masterkey",
# first database: /var/lib/firebird/data/demo.fdb
```

### From source

Requirements: Python 3.10+ and Node 18+. You do **not** need a Firebird client library:
fbadmin uses the pure-Python [`firebirdsql`](https://github.com/nakagami/pyfirebirdsql) driver.

```bash
git clone https://github.com/mariuz/fbadmin && cd fbadmin
python -m venv .venv && . .venv/bin/activate
pip install -e "backend[dev]"
(cd frontend && npm install && npm run build)   # builds the UI into backend/fbadmin/static
fbadmin                                          # opens http://localhost:5050
```

Command-line options: `fbadmin --host 0.0.0.0 --port 5050 --no-browser`.

| Environment variable | Default | Meaning |
|---|---|---|
| `FBADMIN_DATA_DIR` | `~/.fbadmin` | Where `servers.json` (registered servers) is stored, with mode 0600 |
| `FBADMIN_HOST` / `FBADMIN_PORT` | `127.0.0.1` / `5050` | Bind address |
| `FBADMIN_MAX_ROWS` | `1000` | Default row limit per query result |
| `FBADMIN_SESSION_IDLE_TIMEOUT` | `3600` | Seconds before an idle Query Tool connection is closed |

> ⚠️ **Security:** v0.1 has **no login screen**. It is meant for local use (it binds to
> 127.0.0.1 by default), like pgAdmin's desktop mode. Saved passwords are stored in
> plain text in `servers.json` (file mode 0600). Multi-user "server mode" with
> authentication and encrypted credentials is the first item of the roadmap's
> security milestone.

## Development

```bash
# backend with auto-reload
fbadmin --reload --no-browser
# frontend dev server with hot reload (proxies /api to :5050)
cd frontend && npm run dev     # http://localhost:5173

# tests: unit tests always run; integration tests need a Firebird server
docker run -d --name fb5 -p 3050:3050 -e FIREBIRD_ROOT_PASSWORD=masterkey firebirdsql/firebird:5
pytest backend/tests
```

Integration tests create and drop temporary databases. You can point them at any server
with `FBADMIN_TEST_HOST`, `FBADMIN_TEST_PORT`, `FBADMIN_TEST_PASSWORD` and
`FBADMIN_TEST_DIR` (a directory on the server).

See [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) for how the pieces fit together and
[CONTRIBUTING.md](CONTRIBUTING.md) for guidelines.

## Why not FlameRobin / IBExpert / DBeaver?

[FlameRobin](https://github.com/mariuz/flamerobin) is a great native desktop tool. fbadmin
is a different shape: it runs in a **browser**, so you can deploy it next to a server or in
Kubernetes. It is open source (MIT), and it is modelled on the pgAdmin workflow that
PostgreSQL users already know. IBExpert is Windows-only and commercial. DBeaver is generic
and does not cover Firebird-specific administration in depth (MON$, the Services API,
gstat, trace, nbackup and the like).

## License

MIT. See [LICENSE](LICENSE).
