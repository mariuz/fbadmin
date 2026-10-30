# fbadmin roadmap

The goal is **feature parity with pgAdmin 4 for the things Firebird has**, plus
first-class support for Firebird-specific tooling: the Services API, MON$ tables, the
Trace API, nbackup, gstat, replication and the Firebird 5 profiler.

Legend: ✅ done · 🚧 in progress · ⬜ planned · 💡 idea / needs research

Supported Firebird versions: **3.0, 4.0, 5.0** (tested in CI) and the **6.0 snapshots**
(SQL schemas; a CI job that is allowed to fail). Firebird 2.5 is end-of-life and out of
scope.

**Status:** milestones 0.1 to 2.0, the engineering backlog and the phone and tablet layout are
implemented and verified in CI against real services. Firebird 6.0 final support waits for the
6.0 release; milestones 2.1, 2.2, 2.3 and 3.0 below are planned. The current release is **2.1.1** ([release notes](docs/release-notes/v2.1.1.md)): on
[PyPI](https://pypi.org/project/firebird-admin/) (`pip install firebird-admin`), on
[GitHub Releases](https://github.com/mariuz/fbadmin/releases) with desktop installers for Windows,
macOS and Linux, as a Docker image, and documented at https://mariuz.github.io/fbadmin/.
Code signing is deferred by decision of the project owner, so the desktop builds ship unsigned. The
signing steps (macOS Developer ID with notarization, Windows Authenticode) are already in the
desktop workflow and turn on as soon as the certificates are added as repository secrets; see
[docs/PACKAGING.md](docs/PACKAGING.md#desktop-app).

---

## Milestone 0.1 — Foundation ✅

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

## Milestone 0.2 — Query Tool, professional grade ✅

- ✅ **Cancel a running query:** a second attachment deletes the session's row in `MON$STATEMENTS`. The Execute button turns into Cancel with a live timer.
- ✅ Statement timeout (`SET STATEMENT TIMEOUT`, Firebird 4+) and lock timeout, set from the toolbar
- ✅ Transaction options: isolation (read committed, read consistency on Firebird 4+, no record version, snapshot, snapshot table stability), read-only, wait or no wait
- ✅ **Parameter prompt** for `:name` and `?` placeholders. Values are remembered and NULL is supported. Placeholders inside PSQL modules are left alone.
- ✅ **Detailed (explained) plan** (`isc_info_sql_explain_plan`), shown with tables, indices and operators highlighted
- ✅ Graphical (box-and-arrow) plan diagram (nested operator boxes from the detailed plan, plus a plan diff)
- ✅ Per-statement performance stats: page reads, fetches, writes and marks, plus sequential/index reads, inserts, updates, deletes, backouts, purges and expunges (from `MON$IO_STATS` / `MON$RECORD_STATS`)
- ✅ Server-side cursors: the last result set stays open and "Load more" fetches the next rows
- ✅ Export results as CSV, TSV, JSON, SQL `INSERT` statements or Markdown
- ✅ Export to XLSX
- ✅ Cell viewer for long text and BLOBs: text, JSON pretty-print, hex dump, image preview, download
- ✅ BLOB editing and upload in the data grid
- ✅ SQL formatter (Shift+Alt+F, keywords upper-cased, parameters kept) and comment toggle (Ctrl+/)
- ✅ Column-aware autocompletion: table and alias columns, `RDB$`/`MON$`/`SEC$` tables, procedures, functions and package members
- ✅ Snippets library with built-in examples, save the selection as a snippet, export
- ✅ Find and replace (Ctrl+F) and multiple cursors / rectangular selection (CodeMirror)
- ✅ Minimap
- ✅ Error position underlined in the editor and marked in the gutter (from Firebird's "line X, column Y"), including when only a selection was run
- ✅ Results: client-side row filter, sort, copy as TSV
- ✅ Results: column resize and freeze (any number of leading columns)
- ✅ Macros and keyboard shortcut customisation
- ✅ "Query plan diff" between two statements
- ✅ AI assistant: explain a plan, suggest indices, convert SQL from other dialects (opt-in; pluggable providers, Anthropic built in; real API calls unverified in CI)

## Milestone 0.3 — Object management dialogs (pgAdmin-style create/alter) ✅

Each dialog has a form with **General / Definition / … / SQL** tabs. The SQL tab shows the
exact DDL before it runs, like pgAdmin.

- ✅ **Table designer:** columns (add, alter, rename, reorder via `ALTER COLUMN … POSITION`), constraints, indices, triggers and comments; generates `ALTER` scripts for existing tables
- ✅ Column dialog: type, domain, default, NOT NULL, collation, identity options, computed
- ✅ Constraint dialogs: PK, UK, FK (with rule options and referenced-column picker) and CHECK
- ✅ Index dialog: unique, descending, expression, partial (`WHERE`, Firebird 5), activate/deactivate, recompute selectivity
- ✅ View editor with a column list and "check option"
- ✅ **PSQL editor** for procedures, functions, triggers and packages, with compile errors mapped to lines, "compile and test", and dependency warnings before `ALTER`
- ✅ Sequence, domain, exception and role dialogs
- ✅ **Grants / privileges editor** (`RDB$USER_PRIVILEGES`) per object and per user or role, including column-level privileges and `WITH GRANT OPTION`
- ✅ Rename support: `ALTER TABLE … ALTER COLUMN … TO`, sequence, domain and index renames
- ✅ Drop with dependency preview ("these 3 views and 1 procedure depend on it")
- ✅ Comments editor (`COMMENT ON`) for every object
- ✅ `SQL SECURITY DEFINER/INVOKER` (Firebird 4) on tables, triggers and routines
- ✅ External tables, GTT options and UDR modules (`EXTERNAL NAME … ENGINE`)
- ✅ Collations and character sets browser; `CREATE COLLATION`
- ✅ Shadows, BLOB filters and generators with system flags (read-only views)

## Milestone 0.4 — Security and multi-user server mode ✅

- ✅ **Server mode:** fbadmin user accounts with a login page, sessions, CSRF protection and secure cookies
- ✅ **Master password:** encrypt saved Firebird passwords (Argon2id key derivation plus AES-GCM), like pgAdmin's master password
- ✅ Authentication backends: internal, LDAP, OAuth2/OIDC (Google, GitHub, Keycloak, Entra ID), reverse-proxy header (verified against real services in CI: OpenLDAP `slapd` for search+bind, simple bind and the admin group; Dex for the OIDC authorization-code flow with PKCE, admin mapping, domain restriction and forged state)
- ✅ Per-fbadmin-user server lists; shared server groups
- ✅ Audit log of executed DDL and DML (who, when, what, on which database)
- ✅ Read-only mode per server registration ("production safety")
- ✅ TLS and wire-compression options; per-server auth plugin selection (Srp256, Srp, Legacy_Auth)
- ✅ Firebird user management via the Services API and via SQL across multiple security databases
- ✅ `CREATE/ALTER/DROP MAPPING` UI (global and local mappings, Win_Sspi, trusted auth)
- ✅ System privileges and `CREATE ROLE … SET SYSTEM PRIVILEGES` (Firebird 4)
- ✅ User tags (`TAGS (...)`), `USING PLUGIN` selection, `RDB$ADMIN` handling

## Milestone 0.5 — Maintenance and server tools ✅

- ✅ Backup and restore with **upload and download** of `.fbk` files through the browser, streaming via the Services API (`stdout` backup)
- ✅ Backup options: parallel workers (`-par`, Firebird 5), include/skip tables (Firebird 4+), zip, crypt plugin and key name
- ✅ **nbackup:** incremental backups by level, lock/unlock, fixup, restore chain
- ✅ gfix operations: validate (online and full), mend, `-sweep`, set sweep interval, forced writes, page buffers, read-only/read-write, SQL dialect, `-icu`, limbo transactions (list, commit, rollback)
- ✅ **Shutdown and online:** full, single and multi modes, with force/attach/transaction timeouts
- ✅ Database properties editor: set linger, set default charset, set replication, page buffers
- ✅ **gstat parsed into charts:** table and index fill distribution, record versions, fragments, index depth, "tables needing attention"
- ✅ **Trace API UI:** create a trace from a template (slow queries, errors, connections, PSQL), live streaming view, filter and search, save to file
- ✅ Encryption: `ALTER DATABASE ENCRYPT/DECRYPT` with progress (`MON$CRYPT_STATE`) (verified with Firebird's sample crypt plugin, `fbSampleDbCrypt`, in CI: encrypt, progress, header and MON$ state, data still readable, decrypt)
- ✅ Scheduled jobs: backups and sweeps on a cron schedule, with retention (pgAgent-like)
- ✅ `firebird.conf` and `databases.conf` viewer (read-only, via `RDB$CONFIG` in Firebird 4+) (effective values from `RDB$CONFIG`)

## Milestone 0.6 — Schema tools ✅

- ✅ **Full database DDL extract** (like `isql -x`): ordered by dependencies, with `SET TERM` output, grants and comments, downloadable
- ✅ **Schema diff:** compare two databases (or a database and a DDL file) and produce a migration script
- ✅ **ERD tool:** generate a diagram from existing tables and FKs; design new tables visually and produce DDL (pgAdmin 6+ ERD parity)
- ✅ **Import/export wizard:** CSV, JSON or XLSX into a table, with column mapping and type conversion; tables out to CSV, JSON or SQL inserts
- ✅ Copy data between databases or servers
- ✅ Search objects by name, comment or PSQL source (pgAdmin "Search Objects")
- ✅ Test data generator per column type and domain
- ✅ Unused or invalid objects report: `RDB$VALID_BLR = 0`, inactive indices and triggers, unused domains

## Milestone 0.7 — Monitoring and performance ✅

- ✅ **Real-time dashboard charts:** attachments, transactions per second, I/O and record stats (`MON$RECORD_STATS`), memory (`MON$MEMORY_USAGE`)
- ✅ Table statistics (`MON$TABLE_STATS`, Firebird 4+): top tables by reads, inserts, updates and backouts
- ✅ Compiled statement cache browser (`MON$COMPILED_STATEMENTS`, Firebird 5)
- ✅ Long-running transaction and OIT/OAT gap alerts, with "who is holding the OAT"
- ✅ Lock and deadlock view (lock print, where available) (from MON$ record-wait counters; `fb_lock_print` needs local access to the server)
- ✅ History: store periodic snapshots in a local SQLite database to show trends
- ✅ **Profiler UI** (Firebird 5 `RDB$PROFILER` / `PLG$PROF_*`): start a session, run a statement, show the PSQL hot-spot tree and per-line timings
- ✅ Index advisor: suggest indices from slow-query traces and natural plans
- ✅ Prometheus `/metrics` exporter for registered databases (optional)

## Milestone 0.8 — Replication and high availability (Firebird 4+) ✅

- ✅ Publications: `ALTER DATABASE ENABLE/DISABLE PUBLICATION`, include/exclude tables
- ✅ Replica mode switching (read-only / read-write), replication status and lag (lag from the replication sequence of both sides, plus a catch-up estimate from the sampled apply rate)
- ✅ `replication.conf` helper and journal/archive monitoring (journal tracking from the replication sequence and the segment events in firebird.log; segment files can't be listed remotely)

## Milestone 0.9 — Firebird 6 support ✅

- ✅ **SQL schemas:** a new tree level (Database → schemas → objects), an active schema per database (the `X-Fbadmin-Schema` header), schema-scoped catalog, `SET SEARCH_PATH` in extracted DDL, whole-database extract with one block per schema, and a Query Tool schema picker ([docs](docs/FIREBIRD6.md))
- ✅ Firebird 6 snapshot quirks handled: schema-qualified plans, the profiler's `PLG$PROFILER` schema, the gstat header in full shutdown, `T` build versions, and binary and charset parameter encoding (driver patches)
- ✅ New Firebird 6 data types and DDL as they ship: none in the snapshots so far (`RDB$TYPES` matches Firebird 5); the full test suite passes against `6-snapshot`
- ✅ CI matrix with Firebird 6 snapshots (`continue-on-error`), plus an e2e spec for schemas

## Milestone 1.0 — Packaging and polish ✅

- ✅ `pip install firebird-admin` on [PyPI](https://pypi.org/project/firebird-admin/) (1.0.1 published by the release workflow through trusted publishing), with the prebuilt UI bundled in the wheel. The name `fbadmin` on PyPI belongs to an unrelated project; the commands are still `fbadmin` and `fbadmin-desktop`. CI builds the sdist and wheel on every push, runs `twine check --strict`, and installs the wheel into a fresh venv where it serves the UI
- ✅ Official multi-arch Docker image (amd64 and arm64), Helm chart, Kubernetes example with the Firebird operator (`deploy/k8s`: StatefulSet. `deploy/k8s-operator`: a `FirebirdCluster` from the [cloudnative-firebird](https://github.com/mariuz/cloudnative-firebird) operator, with fbadmin registered from a ConfigMap; a kind workflow runs it end to end. Helm `firebirdServers` for preconfigured servers)
- ✅ **Desktop app:** pywebview or Tauri shell with auto-update, for Windows (MSI), macOS (dmg) and Linux (AppImage, deb, rpm, Flatpak). Verified in CI on every packaging change: the MSI installs, runs and uninstalls on Windows; the dmg mounts and the app runs on macOS arm64 and Intel; the deb installs and the AppImage runs on Linux; the rpm and Flatpak bundle are built. Self-update downloads the package, verifies it against `SHA256SUMS`, then runs the MSI, replaces the AppImage or opens the dmg; this was exercised on a real AppImage, which replaced itself and restarted. Builds ship unsigned by the owner's decision (signing is deferred); the signing steps (macOS codesign + notarization, Windows signtool for the exe and MSI) are ready and run once the signing secrets are configured, and packages are attached to GitHub Releases only for version tags
- ✅ Preferences dialog: editor font, keymap, grid NULL display, date formats, confirmations
- ✅ Internationalisation (i18n): English, Romanian, Portuguese (Brazil), Spanish, Russian, German, French and Czech (2589 strings in each language; CI checks that every catalog is complete)
- ✅ Accessibility: keyboard tree navigation, ARIA roles, high-contrast theme (landmarks, ARIA tabs, menus and dialogs, labelled fields, axe-core audit in e2e in three themes)
- ✅ Documentation site (MkDocs), a user guide with screenshots, and an admin guide for server mode (built with `mkdocs build --strict` in CI; it deploys to GitHub Pages as soon as Pages is enabled in the repository settings, Source: GitHub Actions)
- ✅ Plugin API for extra tree nodes, dashboard panels and tools
- ✅ Telemetry: none by default, ever

---

## Engineering backlog ✅

- ✅ Frontend end-to-end tests with Playwright, run in CI against a Firebird container (83 specs: core flows, every feature module, Firebird 6 schemas, accessibility, languages, alerts, backup storage, data compare, local databases, events, upgrade checks, PSQL debugging, 2FA/tokens/permissions, query library, overview, query builder, charts, schema history, phones and tablets)
- ✅ Generate a typed API client from FastAPI's OpenAPI schema (request bodies typed through `Body<path, method>`; most responses are still hand-typed)
- ✅ Code-split the frontend bundle (CodeMirror and grid chunks)
- ✅ Async driver (`firebirdsql.aio`) or a worker pool for long-running statements, so one slow query never blocks the metadata connection
- ✅ Optional native driver (`firebird-driver` / libfbclient) for features the wire protocol driver lacks *(CI only: no libfbclient in the dev container)*
- ✅ Structured logging and a `--log-level` option
- ✅ Rate limiting and request-size limits for server mode
- ✅ Fuzz tests for the SQL splitter; property tests for DDL round-trips with generated schemas
- ✅ Compatibility tests against the [firebird-qa](https://github.com/FirebirdSQL/firebird-qa) corpus of DDL *(weekly `compat.yml`; needs `FBADMIN_QA_DIR` locally)*

## Milestone 1.1 — Firebird 6.0 final, alerts and backups 🚧

- ⬜ **Firebird 6.0 final release:** test against the released 6.0, make the Firebird 6 CI job required (no `continue-on-error`), support any new data types and DDL that ship, and remove the snapshot workarounds the engine no longer needs (see [docs/FIREBIRD6.md](docs/FIREBIRD6.md)) *(waiting for the release: as of October 2026 only the `6-snapshot` image exists)*
- ✅ **Alerts:** email (SMTP), Slack-compatible and signed JSON webhook channels; event rules (failed scheduled job, failed test restore, validation errors, failed upload) and condition rules checked every minute (OAT gap with the holder named, long-running transactions, replication lag, unreachable database), with cool-downs, "resolved" messages and a delivery history ([docs/ALERTS.md](docs/ALERTS.md)). Verified against a real SMTP server (Mailpit), a local webhook receiver and Firebird 3, 4, 5 and 6
- ✅ **Backup verification:** scheduled backups can be test-restored into a scratch database (opened, then dropped); old backups are only pruned and a backup only uploaded after it passed. Also on demand per archived backup. Scheduled validations count Firebird's reported errors (checked with a deliberately damaged data page)
- ✅ **Cloud backup targets:** S3 and S3-compatible services, Azure Blob Storage and Google Cloud Storage, with per-schedule folder and retention; no cloud SDKs (requests signed by fbadmin), `env:` references for secrets. Verified against SeaweedFS (S3 API), Azurite and fake-gcs-server, including multipart uploads and a full backup → test restore → upload → retention run; real AWS, Azure and Google accounts are not part of CI

## Milestone 1.2 — Desktop and data tools ✅

- ✅ **Embedded engine in the desktop app:** "Local database" registrations open a `.fdb` file with the bundled Firebird 5 embedded engine (no server): tree, DDL, data grid, the full Query Tool, monitoring, backup/restore, nbackup, validation and gstat; trace, user management and encryption are hidden for local files. Never available in multi-user server mode. Verified on Linux (tests, the full e2e suite and a PyInstaller build) and by a required smoke test of the packaged app on Windows, macOS arm64/Intel and Linux in CI (create, query, backup and restore a local database) ([docs/EMBEDDED.md](docs/EMBEDDED.md))
- ✅ **Data compare:** compare the rows of tables in two databases by primary or unique key (streamed merge-join, with a fallback sort when collations order keys differently), see inserts/updates/deletes with sample rows, download a sync script (FK-ordered, every Firebird type) or apply it to the target in one transaction ([docs/DATA_COMPARE.md](docs/DATA_COMPARE.md))
- ✅ **Event monitor:** register `POST_EVENT` names and watch them arrive live with counts and a log, pick names found in PSQL sources, post a test event; clear errors when the server's `RemoteAuxPort` is not reachable ([docs/EVENTS.md](docs/EVENTS.md))
- ✅ **Upgrade assistant:** checks before moving to Firebird 4, 5 or 6 (newly reserved words from each version's keyword list, legacy UDFs with built-in replacements, dialect 1, authentication, time zones, external tables, schemas and more), a tailored backup/restore plan, and an optional trial restore on the newer server that recompiles every view, procedure and trigger there ([docs/UPGRADE.md](docs/UPGRADE.md))
- ✅ **PSQL debugging help:** trace runs of procedures, functions and EXECUTE BLOCKs (an instrumented copy records the line and variables after each statement, then everything is rolled back; step back and forth), per-line profiler timings on Firebird 5, and saved test cases per routine ([docs/PSQL_DEBUGGING.md](docs/PSQL_DEBUGGING.md))

## Milestone 2.0 — Teams and productivity ✅

- ✅ **Two-factor login (TOTP)** for server mode: authenticator apps with a QR code, single-use recovery codes, replay protection and rate limiting, per-user or global enforcement by admins ([docs/PERMISSIONS.md](docs/PERMISSIONS.md))
- ✅ **API tokens** for scripts and automation: personal `Bearer` tokens, read-only or read-write, optionally limited to some servers, stored hashed, revocable, with last use and audit entries
- ✅ **Finer permissions:** grants per user or group × server × database × object pattern × level (none / read / write / ddl / admin), enforced centrally for every endpoint and SQL statement; no grants keep the previous behaviour
- ✅ **Shared query library:** saved queries with folders, tags, search and revision history, private or shared with all users in server mode; "Save to library" in the Query Tool ([docs/QUERY_LIBRARY.md](docs/QUERY_LIBRARY.md))
- ✅ **Multi-server overview:** one page with the health of every server and database (reachability, versions, attachments, transaction gaps, size, last backup, replication, firing alerts, failed jobs), checked concurrently with timeouts and without password prompts ([docs/OVERVIEW.md](docs/OVERVIEW.md))
- ✅ **Visual query builder:** tables and views on a canvas (or from the tree), joins suggested from foreign keys, columns, aggregates with automatic GROUP BY, criteria, ordering and row limits per server version, live SQL preview; fully usable from the keyboard ([docs/QUERY_BUILDER.md](docs/QUERY_BUILDER.md))
- ✅ **Charts from query results:** bar, stacked, line, area, pie/donut and scatter charts next to the result grid, with aggregation, top-N, time axes, accessible colours and markers, SVG/PNG export ([docs/CHARTS.md](docs/CHARTS.md))
- ✅ **ERD export** to standalone SVG and PNG (1×/2×), whole diagram or visible area, with an optional title block
- ✅ **Schema history in git:** deterministic one-file-per-object DDL snapshots committed on demand, after DDL or on a schedule, with diffs, objects at any commit, push to a remote and migration scripts between commits ([docs/SCHEMA_HISTORY.md](docs/SCHEMA_HISTORY.md))
- ✅ **Command-line tool:** `fbadmin backup`, `restore`, `extract`, `diff`, `sql` and `check` for CI and cron jobs, with exit codes and JSON output ([docs/CLI.md](docs/CLI.md))

Also fixed in this milestone:
- The `firebirdsql` driver decoded INT128, NUMERIC(>18), DECFLOAT and time zone values wrongly and truncated large parameters; fbadmin now patches these (see [docs/upstream-firebirdsql-fb4-types.md](docs/upstream-firebirdsql-fb4-types.md)).
- In server mode, scheduled jobs, history sampling and alert condition checks run as the user who set them up (they previously failed for lack of a signed-in user), and server-wide monitoring and update settings are admin-only.

## Milestone 2.1 — Polish and hardening ⬜

- ⬜ **Upstream the driver fixes:** report the `firebirdsql` INT128 / DECFLOAT / time zone bugs with the minimal reproduction in [docs/upstream-firebirdsql-fb4-types.md](docs/upstream-firebirdsql-fb4-types.md), contribute the fix, and drop fbadmin's local patches once a fixed driver is released
- ✅ **Cancel long-running jobs:** a Cancel button for backups (archive and server file), restores streamed from fbadmin, sweeps, validations, test restores, scheduled runs, upgrade trial restores, data compare and applying its script, and debugger runs. The server work stops too: the job's gbak/gfix attachment (found by a per-job process name) is detached, or its running statement is cancelled; partial backups and half-restored databases are removed. A restore from a file on the server cannot be stopped from outside and has no Cancel ([docs/MAINTENANCE.md](docs/MAINTENANCE.md#cancelling-a-job))
- ✅ **Data compare:** results, sample rows, logs and sync scripts are kept in the data directory across restarts (the last 20 per user, reopened from *Recent comparisons*); keys are matched like the target's collation (case, accents and trailing spaces, probed on the target server), so keys that differ only by case become one UPDATE; rows of self-referencing tables are inserted parents first and deleted children first ([docs/DATA_COMPARE.md](docs/DATA_COMPARE.md))
- ✅ **Command-line remote mode:** `fbadmin --url … --token-env …` runs `servers`, `check`, `sql`, `extract`, `diff` (also `--data`), `backup` (streamed to the client, to a server file or into the server's archive) and `restore` (upload or server file) through a running fbadmin server with an API token, using its registered servers, saved passwords and permissions; job logs are followed live and Ctrl+C cancels the job on the server ([docs/CLI.md](docs/CLI.md#remote-mode))
- ⬜ **Real cloud tests:** run the storage tests against real AWS S3, Azure Blob Storage and Google Cloud Storage accounts in CI (short-lived credentials via OIDC), not only emulators
- ✅ **Native driver in CI and as an app setting:** choose the pure-Python `firebirdsql` or the native `firebird-driver` + libfbclient driver in the app (or with `FBADMIN_DRIVER`); native now covers the Query Tool, create/drop database and the Services API too; the full backend suite and the e2e suite run under both drivers in CI, with a cross-driver equivalence test; correctness and benchmark comparison in [docs/DRIVERS.md](docs/DRIVERS.md)
- ✅ **Native driver by default:** the default driver setting is *Automatic*: the native `firebird-driver` when a Firebird 4+ client library (with ICU) is available — the desktop app and Docker image bundle the Firebird 5 client, and Ubuntu 25.04+/Debian 13 ship a Firebird 4 one — otherwise, or if the client library fails, the pure-Python `firebirdsql`; both remain selectable. Service options newer than the client library (e.g. parallel workers with a Firebird 4 client) are offered only when both sides support them; the bundled client now negotiates ChaCha wire encryption
- ✅ **Server-mode startup race:** several processes on one data directory (workers, replicas, an old instance still stopping) all created the first administrator from `FBADMIN_ADMIN_*`, and all but one crashed with `UNIQUE constraint failed: users.username` / "already exists"; two people finishing the setup page at once could even create two administrators. The first user is now created atomically (`BEGIN IMMEDIATE`, also across processes), and the schema upgrade tolerates a concurrent one; regression tests start six processes at once
- ✅ **Tablet comfort:**
  - ERD: pointer events, with finger pan, move and pinch zoom, and larger touch areas for the foreign-key handles.
  - Query builder: touch dragging of tables, pinch and button zoom, and tap-to-join, since HTML drag and drop does not work with touch.
  - Table designer: grids scroll sideways with usable field widths and finger-sized controls.
  - Tested with real touch input on an emulated iPad ([docs/MOBILE.md](docs/MOBILE.md#on-a-tablet))
- ✅ **Large schemas:** with 20,000 tables:
  - Opening a database takes 0.25 s instead of 7.5 s (folder counts by `COUNT(*)`).
  - Folders with more than 400 objects render only the visible rows; expanded state and screen-reader positions are kept.
  - Autocompletion of very large schemas loads table names first (2.5 s instead of 14 s) and fetches the columns of the tables a query uses.
  - Load tests run in the backend and e2e suites ([docs/user-guide/object-explorer.md](docs/user-guide/object-explorer.md#very-large-schemas))
- ⬜ **firebirdsql 1.5.1:** once it is on PyPI, require it (`firebirdsql>=1.5.1`), remove the three workarounds for 1.5.0 (bind-describe assert, read-ahead buffer, compressed fetch wait) and rerun the benchmarks in [docs/DRIVERS.md](docs/DRIVERS.md). 1.5.1 carries fbadmin's upstream fixes and the faster wire encryption ([nakagami/pyfirebirdsql#144](https://github.com/nakagami/pyfirebirdsql/pull/144), [#145](https://github.com/nakagami/pyfirebirdsql/pull/145))
- ⬜ **Revisit the *Automatic* driver choice with benchmarks:** with firebirdsql 1.5.1 the pure-Python driver fetches rows faster than the native one on Firebird 4+, while BLOBs and backups stay much faster natively; decide from measurements whether Automatic stays native everywhere
- ⬜ **Prepared statements in fbadmin's loops:** grid edits, imports and data compare scripts run one statement many times; use `prep()` / `executemany` so they gain from the driver's fewer round trips (about 2x on a LAN with the pending upstream change)
- ⬜ **Supply-chain hardening:** an SBOM for each package, signed container images and build provenance (attestations), dependency scanning in CI, and a security policy (`SECURITY.md` for reporting vulnerabilities)

## Milestone 2.2 — Operations ⬜

- ✅ **Schema drift detection:** a scheduled *schema drift check* compares a database with its schema history in git (a branch, tag, commit or a fetched `origin/<branch>`; its own repository or another database's) or with another database; differences are listed as missing, extra or changed with both DDL versions and the script that makes the database match; object types and name patterns can be ignored; the `schema.drift` alert is sent for new or changed drift and once when it is gone ([docs/SCHEMA_DRIFT.md](docs/SCHEMA_DRIFT.md))
- ✅ **Versioned migrations:** `V<version>__<description>.sql` scripts from git (or a directory) applied in order; each database records what was applied (checksums, who, when, how long, failures) in `FBADMIN_MIGRATIONS`; changed, failed and out-of-order scripts block a run; a dry run on a metadata-only or full copy of the database reports each script and the schema changes, and apply needs a passed one; baseline for existing databases, repair after a failure, one run at a time; from the UI and with `fbadmin migrate` (locally or through an fbadmin server) ([docs/MIGRATIONS.md](docs/MIGRATIONS.md))
- ✅ **Replication setup wizard:** **Set up a replica…** configures primary and replica end to end: the plan and its checks, both `replication.conf` blocks (written by fbadmin when the file is reachable), publication, a check that the primary writes its journal, the replica made from a physical copy (`nbackup -SEQ`: no change lost or applied twice), read-only replica mode and registration, then a check row that must arrive on the replica; asynchronous or synchronous, with a shared directory or a manual transfer ([docs/REPLICATION.md](docs/REPLICATION.md#setup-wizard))
- ✅ **Backup encryption and restore drills:** backups kept by fbadmin and uploaded to the cloud can be encrypted while they stream in (AES-256-GCM in authenticated chunks, `*.fbk.enc`; keys from `FBADMIN_BACKUP_KEY` or created in the UI with a recovery key shown once; restores, test restores and drills decrypt on the way; `fbadmin backup-decrypt` without the server); scheduled restore drills restore the newest backup from the archive or from cloud storage, on another server if wanted, validate it, read every table, compare with the live database, check the backup's age and the recovery time, keep a report and raise `restore.drill_failed` ([docs/BACKUP_ENCRYPTION_DRILLS.md](docs/BACKUP_ENCRYPTION_DRILLS.md))
- ⬜ **Query regression tracking:** keep slow statements and their plans over time and flag statements whose plan or duration got worse
- ⬜ **Audit log forwarding:** send the audit log to a central logging system (syslog, webhook)
- ✅ **Kubernetes operator integration:** the ⎈ Kubernetes button in the top bar lists the `FirebirdCluster`s of the cloudnative-firebird operator with their instances, backups, scheduled backups, databases, users and events; scale, hibernate, rolling or single-instance restart, switchover, fence, back up, restore into a new file, instance logs, and register the cluster as an fbadmin server with the password from its Secret; uses fbadmin's service account (RBAC in `deploy/k8s-operator` and the Helm chart's `kubernetes` values) or a kubeconfig; every action is audited ([docs/KUBERNETES.md](docs/KUBERNETES.md))

## Milestone 2.3 — Insight and collaboration ⬜

- ⬜ **SQL notebooks:** SQL cells, notes, results and charts in one saved document, shared in server mode; for runbooks ("monthly health check", "month-end close") and for handing over an investigation
- ⬜ **Anonymised copies of production:** copy a database to a development or test server with masking rules per column (fake e-mails and names, shuffled values, scrambled numbers), consistent across foreign keys; built on backup/restore, the test data generator and data compare
- ⬜ **Guided performance check-up ("why is it slow?"):** one ranked report from what fbadmin already collects (OAT gap, sweep settings, gstat fill and record versions, outdated index selectivity, cache size, lock waits) with a fix action per finding, such as *recompute selectivity* or *sweep now*
- ⬜ **Database timeline:** one history per database of schema changes, audit log entries, backups and test restores, alerts and upgrades, to answer "what changed before it broke?"
- ⬜ **Push notifications for alerts:** Web Push to the installed phone app, next to the e-mail, Slack and webhook channels
- ⬜ **Shareable links** in server mode to an object, a saved query or a result snapshot, opened with the viewer's own permissions

## Milestone 3.0 — Teams and distribution ⬜

- ⬜ **SAML sign-in** and **SCIM** user provisioning from a company directory
- ⬜ **More install channels:** apt and yum repositories, Homebrew, winget, Flatpak on Flathub
- ⬜ **Independent security review** of server mode
- ⬜ **Screen reader testing** with NVDA and VoiceOver, beyond the automated axe checks

## Ongoing ⬜

- ⬜ **Code signing** of the desktop builds (deferred; the workflow is ready and needs the owner's certificates as repository secrets)
- ⬜ **Community translations** through Weblate, and more languages
- ✅ **Phone and tablet layout** for checking on servers away from a desk: the Object Explorer as a drawer, a ⋯ menu for the toolbar, touch-sized targets, long-press and ⋮ action sheets, full-screen dialogs, the server overview as the phone landing page, a collapsible sidebar on tablets, and add-to-home-screen icons; tested on emulated Pixel, iPhone and iPad screens with accessibility audits in all themes ([docs/MOBILE.md](docs/MOBILE.md))

## How to contribute to the roadmap

Open an issue with the `roadmap` label. Describe the workflow (what you do today, for
example in isql, FlameRobin or IBExpert) and what fbadmin should do. Items are
prioritised by how many people need them and how Firebird-specific they are: things no
generic SQL tool does well come first.
