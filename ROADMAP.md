# fbadmin roadmap

The goal is **feature parity with pgAdmin 4 for the things Firebird has**, plus
first-class support for Firebird-specific tooling: the Services API, MON$ tables, the
Trace API, nbackup, gstat, replication and the Firebird 5 profiler.

Legend: ✅ done · 🚧 in progress · ⬜ planned · 💡 idea / needs research

Supported Firebird versions: **3.0, 4.0, 5.0** (tested in CI). Support for **6.0**
(SQL schemas) is planned in milestone 0.9. Firebird 2.5 is end-of-life and out of
scope.

---

## Milestone 0.1 — Foundation ✅ (current)

**Backend (Python, FastAPI, pure-Python `firebirdsql` driver)**
- ✅ Server registry (`servers.json`, mode 0600), optional saved passwords, password prompt on demand
- ✅ Connection manager: a shared metadata connection per database, plus one connection and transaction per Query Tool tab
- ✅ Idle session reaper
- ✅ Catalog introspection for tables, views, procedures, functions (PSQL, UDR and legacy UDF), packages, triggers (DML, DB and DDL), sequences, domains, exceptions, indices (expression and partial), roles, users and system tables
- ✅ DDL extraction for every object type, including identity, computed, collation, FK rules, descending, expression and partial indices, `TYPE OF` parameters and comments
- ✅ DDL round-trip test (extract, recreate, compare)
- ✅ Dependencies (`RDB$DEPENDENCIES`)
- ✅ Script runner that understands `SET TERM`, PSQL blocks without `SET TERM`, `q'{}'` strings and isql-only commands
- ✅ Query execution: multiple result sets, plans, auto-commit or manual transactions, row limits
- ✅ Paged table data with filter and sort; grid edits applied in one transaction (keyed by PK or `RDB$DB_KEY`)
- ✅ MON$ monitoring: attachments, statements, transactions and I/O; kill attachment, cancel statement
- ✅ Services API: server info, `firebird.log`, backup, restore, sweep, gstat
- ✅ Background jobs ("Processes") with live logs
- ✅ Create, register, unregister and drop databases
- ✅ Test suite (unit plus integration against a live server), CI matrix for Firebird 3, 4 and 5

**Frontend (React, TypeScript, Vite, CodeMirror 6)**
- ✅ pgAdmin layout: Object Explorer with lazy tree and context menus; Dashboard, Properties, SQL and Dependencies tabs
- ✅ Query Tool: Firebird SQL dialect, F5 and Ctrl+Enter, execute selection, explain, history, file open and save, CSV/JSON export, virtualized result grid
- ✅ Data view with inline editing, NULL handling, pagination, sorting and WHERE filter
- ✅ Monitor dashboard with auto-refresh
- ✅ Dialogs: register server, register or create database, password, backup, restore, create table (with SQL preview), user, confirm-drop (type the name to confirm)
- ✅ Light, dark and auto themes

---

## Milestone 0.2 — Query Tool, professional grade

- ⬜ **Cancel a running query.** Cancel via a second attachment (`DELETE FROM MON$STATEMENTS`), and use `op_cancel` when the driver supports it.
- ⬜ Statement timeout (`SET STATEMENT TIMEOUT`, Firebird 4+) and lock timeout, set from the toolbar
- ⬜ Transaction options: isolation (snapshot, read committed with read consistency, snapshot table stability), read-only, wait or no wait
- ⬜ **Parameter prompt** for `:name` and `?` placeholders, with typed inputs
- ⬜ **Detailed (explained) plan** (`isc_info_sql_explain_plan`, Firebird 3+), shown as a tree and as a graphical plan
- ⬜ Per-statement performance stats: reads, fetches, writes and marks from `MON$IO_STATS` / `MON$RECORD_STATS`, before and after
- ⬜ Server-side cursors: fetch more rows on scroll instead of a hard row limit
- ⬜ Export results as CSV, TSV, JSON, XLSX, SQL `INSERT` statements or Markdown
- ⬜ BLOB viewer and editor: text, hex, image preview, JSON pretty-print; download and upload
- ⬜ SQL formatter (keyword case, indentation) and comment/uncomment
- ⬜ Column-aware autocompletion (aliases, procedures, functions, package members, `RDB$`/`MON$` columns)
- ⬜ Snippets and templates library, saved queries and favourites
- ⬜ Find and replace, multiple cursors, minimap
- ⬜ Error position highlighted in the editor (from the "line X, column Y" in Firebird errors)
- ⬜ Results: column resize, freeze, copy with headers, filter results client-side
- ⬜ Macros and keyboard shortcut customisation
- ⬜ "Query plan diff" between two statements
- 💡 AI assistant: explain a plan, suggest indices, convert SQL from other dialects (opt-in, pluggable provider)

## Milestone 0.3 — Object management dialogs (pgAdmin-style create/alter)

Each dialog has a form with **General / Definition / … / SQL** tabs. The SQL tab shows the
exact DDL before it runs, like pgAdmin.

- ⬜ **Table designer:** columns (add, alter, rename, reorder via `ALTER COLUMN … POSITION`), constraints, indices, triggers and comments; generates `ALTER` scripts for existing tables
- ⬜ Column dialog: type, domain, default, NOT NULL, collation, identity options, computed
- ⬜ Constraint dialogs: PK, UK, FK (with rule options and referenced-column picker) and CHECK
- ⬜ Index dialog: unique, descending, expression, partial (`WHERE`, Firebird 5), activate/deactivate, recompute selectivity
- ⬜ View editor with a column list and "check option"
- ⬜ **PSQL editor** for procedures, functions, triggers and packages, with compile errors mapped to lines, "compile and test", and dependency warnings before `ALTER`
- ⬜ Sequence, domain, exception and role dialogs
- ⬜ **Grants / privileges editor** (`RDB$USER_PRIVILEGES`) per object and per user or role, including column-level privileges and `WITH GRANT OPTION`
- ⬜ Rename support: `ALTER TABLE … ALTER COLUMN … TO`, sequence, domain and index renames
- ⬜ Drop with dependency preview ("these 3 views and 1 procedure depend on it")
- ⬜ Comments editor (`COMMENT ON`) for every object
- ⬜ `SQL SECURITY DEFINER/INVOKER` (Firebird 4) on tables, triggers and routines
- ⬜ External tables, GTT options and UDR modules (`EXTERNAL NAME … ENGINE`)
- ⬜ Collations and character sets browser; `CREATE COLLATION`
- ⬜ Shadows, BLOB filters and generators with system flags (read-only views)

## Milestone 0.4 — Security and multi-user server mode

- ⬜ **Server mode:** fbadmin user accounts with a login page, sessions, CSRF protection and secure cookies
- ⬜ **Master password:** encrypt saved Firebird passwords (Argon2id key derivation plus AES-GCM), like pgAdmin's master password
- ⬜ Authentication backends: internal, LDAP, OAuth2/OIDC (Google, GitHub, Keycloak, Entra ID), reverse-proxy header
- ⬜ Per-fbadmin-user server lists; shared server groups
- ⬜ Audit log of executed DDL and DML (who, when, what, on which database)
- ⬜ Read-only mode per server registration ("production safety")
- ⬜ TLS and wire-compression options; per-server auth plugin selection (Srp256, Srp, Legacy_Auth)
- ⬜ Firebird user management via the Services API and via SQL across multiple security databases
- ⬜ `CREATE/ALTER/DROP MAPPING` UI (global and local mappings, Win_Sspi, trusted auth)
- ⬜ System privileges and `CREATE ROLE … SET SYSTEM PRIVILEGES` (Firebird 4)
- ⬜ User tags (`TAGS (...)`), `USING PLUGIN` selection, `RDB$ADMIN` handling

## Milestone 0.5 — Maintenance and server tools

- ⬜ Backup and restore with **upload and download** of `.fbk` files through the browser, streaming via the Services API (`stdout` backup)
- ⬜ Backup options: parallel workers (`-par`, Firebird 5), include/skip tables (Firebird 4+), zip, crypt plugin and key name
- ⬜ **nbackup:** incremental backups by level, lock/unlock, fixup, restore chain
- ⬜ gfix operations: validate (online and full), mend, `-sweep`, set sweep interval, forced writes, page buffers, read-only/read-write, SQL dialect, `-icu`, limbo transactions (list, commit, rollback)
- ⬜ **Shutdown and online:** full, single and multi modes, with force/attach/transaction timeouts
- ⬜ Database properties editor: set linger, set default charset, set replication, page buffers
- ⬜ **gstat parsed into charts:** table and index fill distribution, record versions, fragments, index depth, "tables needing attention"
- ⬜ **Trace API UI:** create a trace from a template (slow queries, errors, connections, PSQL), live streaming view, filter and search, save to file
- ⬜ Encryption: `ALTER DATABASE ENCRYPT/DECRYPT` with progress (`MON$CRYPT_STATE`)
- ⬜ Scheduled jobs: backups and sweeps on a cron schedule, with retention (pgAgent-like)
- ⬜ `firebird.conf` and `databases.conf` viewer (read-only, via `RDB$CONFIG` in Firebird 4+)

## Milestone 0.6 — Schema tools

- ⬜ **Full database DDL extract** (like `isql -x`): ordered by dependencies, with `SET TERM` output, grants and comments, downloadable
- ⬜ **Schema diff:** compare two databases (or a database and a DDL file) and produce a migration script
- ⬜ **ERD tool:** generate a diagram from existing tables and FKs; design new tables visually and produce DDL (pgAdmin 6+ ERD parity)
- ⬜ **Import/export wizard:** CSV, JSON or XLSX into a table, with column mapping and type conversion; tables out to CSV, JSON or SQL inserts
- ⬜ Copy data between databases or servers
- ⬜ Search objects by name, comment or PSQL source (pgAdmin "Search Objects")
- ⬜ Test data generator per column type and domain
- ⬜ Unused or invalid objects report: `RDB$VALID_BLR = 0`, inactive indices and triggers, unused domains

## Milestone 0.7 — Monitoring and performance

- ⬜ **Real-time dashboard charts:** attachments, transactions per second, I/O and record stats (`MON$RECORD_STATS`), memory (`MON$MEMORY_USAGE`)
- ⬜ Table statistics (`MON$TABLE_STATS`, Firebird 4+): top tables by reads, inserts, updates and backouts
- ⬜ Compiled statement cache browser (`MON$COMPILED_STATEMENTS`, Firebird 5)
- ⬜ Long-running transaction and OIT/OAT gap alerts, with "who is holding the OAT"
- ⬜ Lock and deadlock view (lock print, where available)
- ⬜ History: store periodic snapshots in a local SQLite database to show trends
- ⬜ **Profiler UI** (Firebird 5 `RDB$PROFILER` / `PLG$PROF_*`): start a session, run a statement, show the PSQL hot-spot tree and per-line timings
- ⬜ Index advisor: suggest indices from slow-query traces and natural plans
- ⬜ Prometheus `/metrics` exporter for registered databases (optional)

## Milestone 0.8 — Replication and high availability (Firebird 4+)

- ⬜ Publications: `ALTER DATABASE ENABLE/DISABLE PUBLICATION`, include/exclude tables
- ⬜ Replica mode switching (read-only / read-write), replication status and lag
- ⬜ `replication.conf` helper and journal/archive monitoring

## Milestone 0.9 — Firebird 6 support

- ⬜ **SQL schemas:** a new tree level (Database → Schemas → objects), schema-qualified DDL, search path
- ⬜ New Firebird 6 data types and DDL as they ship
- ⬜ CI matrix with Firebird 6 snapshots

## Milestone 1.0 — Packaging and polish

- ⬜ `pip install fbadmin` on PyPI, with the prebuilt UI bundled in the wheel
- ⬜ Official multi-arch Docker image (amd64 and arm64), Helm chart, Kubernetes example with the Firebird operator
- ⬜ **Desktop app:** pywebview or Tauri shell with auto-update, for Windows (MSI), macOS (dmg) and Linux (AppImage, deb, rpm, Flatpak)
- ⬜ Preferences dialog: editor font, keymap, grid NULL display, date formats, confirmations
- ⬜ Internationalisation (i18n): English, Romanian, Portuguese (Brazil), Spanish, Russian, German, French and Czech
- ⬜ Accessibility: keyboard tree navigation, ARIA roles, high-contrast theme
- ⬜ Documentation site (MkDocs), a user guide with screenshots, and an admin guide for server mode
- ⬜ Plugin API for extra tree nodes, dashboard panels and tools
- ⬜ Telemetry: none by default, ever

---

## Engineering backlog (ongoing)

- ⬜ Frontend end-to-end tests with Playwright, run in CI against a Firebird container
- ⬜ Generate a typed API client from FastAPI's OpenAPI schema
- ⬜ Code-split the frontend bundle (CodeMirror and grid chunks)
- ⬜ Async driver (`firebirdsql.aio`) or a worker pool for long-running statements, so one slow query never blocks the metadata connection
- ⬜ Optional native driver (`firebird-driver` / libfbclient) for features the wire protocol driver lacks
- ⬜ Structured logging and a `--log-level` option
- ⬜ Rate limiting and request-size limits for server mode
- ⬜ Fuzz tests for the SQL splitter; property tests for DDL round-trips with generated schemas
- ⬜ Compatibility tests against the [firebird-qa](https://github.com/FirebirdSQL/firebird-qa) corpus of DDL

## How to contribute to the roadmap

Open an issue with the `roadmap` label. Describe the workflow (what you do today, for
example in isql, FlameRobin or IBExpert) and what fbadmin should do. Items are
prioritised by how many people need them and how Firebird-specific they are: things no
generic SQL tool does well come first.
