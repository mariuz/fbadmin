# Maintenance and server tools (milestone 0.5)

Everything here goes through the Firebird **Services API** (or plain SQL), so fbadmin
does not need access to the server's file system. Open the tools from the object
explorer: right-click a database → *Maintenance tools*, a server → *Server tools*,
or use the *Maintenance* block on the database dashboard and the **Schedules**
button in the top bar.

![gstat charts](screenshots/maintenance-gstat.png)

## Backup and restore through the browser

| Action | How it works |
|---|---|
| **Download .fbk** | `POST …/maintenance/backup/stream` starts gbak with `stdout` as the backup file and returns a one-time token; the browser then downloads `GET /api/servers/{sid}/maintenance/downloads/{token}`. Data is read with `isc_info_svc_to_eof` in 64 KB chunks and streamed — nothing is buffered in memory. Option errors are reported before the download starts. |
| **Backup to fbadmin archive** | Same stream, written to `<data dir>/backups/manual/*.fbk` as a background job. |
| **Backup to server file** | Classic gbak to a path on the Firebird server (verbose log in Processes). |
| **Upload & restore** | The browser sends the file as the raw request body; the backend spools it to `<data dir>/uploads` in chunks and runs gbak with `stdin` as the backup file, answering `isc_info_svc_stdin` requests from the file. Tick *Keep* to store the upload in the archive. |
| **Restore from archive / server file** | Restore a stored backup, or a `.fbk` that already sits on the server. |

Backup options: metadata only, garbage collection, transportable, ignore checksums/limbo,
**zip** (FB4+), **parallel workers** (FB5, `isc_spb_bkp_parallel_workers`), **skip data**
(FB3+) / **include data** (FB4+) table patterns (`SIMILAR TO`), **crypt plugin, key holder
and key name** (FB4+). Restore options: replace, page size, buffers, deactivate indices,
no validity, commit per table, use all space, metadata only, read-only, parallel workers,
skip/include data, crypt plugin/key, fix UNICODE_FSS data. Options that the server version
does not support are disabled in the UI and rejected by the API with a clear message.

![backup](screenshots/maintenance-backup.png)

## nbackup

* **Incremental backups** by level (`isc_action_svc_nbak`) to files on the server, optional
  direct I/O and "no database triggers". The history comes from `RDB$BACKUP_HISTORY`.
* **Lock / unlock** for file-system copies: `ALTER DATABASE BEGIN BACKUP` / `END BACKUP`.
  The state (normal / locked / merge) is shown from the header page and `MON$BACKUP_STATE`.
* **Fixup** (`nbackup -F`, `isc_action_svc_nfix`) — Firebird 4+ only (FB3 has no service for it).
* **Restore chain** (`isc_action_svc_nrest`): list the level 0, 1, … files in order.

## Validate & repair (gfix)

* **Online validation** (`isc_action_svc_validate`, FB3+) with table/index include/exclude
  patterns and lock timeout; **full** (`-v -full`), **read-only check** (`-v -n`) and **mend**.
* **Sweep** (FB5: parallel workers), **ICU fix** (`gfix -icu`, `isc_spb_rpr_icu`).
* **Limbo transactions**: list (`isc_info_svc_limbo_trans`), commit or roll back each one.

Full validation, mend and ICU need exclusive access; fbadmin closes its own connections first.

## Shutdown and online

New-style shutdown (FB 2.5+ SPB): mode **multi / single / full**, method **force /
attachments / transactions** with a timeout. Bring online as **normal, multi or single**.
Shutdown can only become more restrictive (multi → single → full); use *Bring online* with
a less restrictive mode to step back. The current state is read from the header page, so it
is visible even when the database is in full shutdown.

## Database properties

Page buffers, sweep interval, forced writes, reserve space, SQL dialect and read-only are
set through the Services API; **linger** (`ALTER DATABASE SET LINGER TO` / `DROP LINGER`)
and the **default character set** (`ALTER DATABASE SET DEFAULT CHARACTER SET`) through SQL.
Only changed fields are applied. Replication settings are handled by the replication feature.

## Statistics (gstat) charts

The server's gstat output (data + index pages + record versions) is parsed into JSON
(`fbadmin/maintenance/gstat.py`, unit-tested with real FB3/4/5 outputs in
`backend/tests/fixtures`). The UI shows summary cards, top tables by size / records / back
versions / fill, index depth, fill-distribution histograms per table and index, and a
**tables needing attention** list:

| Finding | Default rule |
|---|---|
| Many record versions | back versions ≥ 20 % of records (critical ≥ 100 %), tables ≥ 100 records |
| Long version chains | max versions ≥ 5 |
| Fragmented records | fragments ≥ 10 % of records |
| Low page fill | average fill < 50 % over ≥ 10 data pages |
| Deep index | depth ≥ 4 |
| Poor selectivity | max duplicates ≥ 90 % of ≥ 1000 nodes |
| Transaction gap | next − oldest active > 10 000 |

Output of a command-line `gstat -a -r` can be pasted and parsed too
(`POST /api/servers/{sid}/maintenance/gstat/parse`).

## Trace API

Start a trace session from a template — **slow queries** (statement/procedure/function
finish above a threshold, with plan and performance), **all statements**, **errors and
warnings**, **connections and transactions**, **PSQL execution** — or write a custom
configuration (the template config is shown and can be edited). Sessions are scoped to one
database (`database = %[\\/]<file name>`) or all databases, with optional include/exclude
SQL filters.

A background thread reads the trace output (`isc_info_svc_line` with a 1 s service timeout)
and splits it into events (time, type, attachment, user, duration, SQL, plan). The view
polls new events every second; filter by text, event type or minimum duration, follow the
tail, inspect an event, **stop** the session, **save the full log** (written to
`<data dir>/traces/<id>.log` while streaming). Sessions started elsewhere are listed (from
`isc_action_svc_trace_list`) and can be stopped. Configuration errors are reported by the
server per database when it is attached and show up as `TRACE_CONFIG_ERROR` events.

![trace](screenshots/maintenance-trace.png)

## Encryption

`ALTER DATABASE ENCRYPT WITH <plugin> [KEY <name>]` / `DECRYPT`; progress from
`MON$DATABASE.MON$CRYPT_PAGE / MON$PAGES` and `MON$CRYPT_STATE` (FB4+), polled while
encryption runs. A crypt plugin must be installed on the server — without one Firebird
reports "Crypt plugin … failed to load", which is shown as is.

## Configuration viewer

Read-only `RDB$CONFIG` (Firebird 4+): effective `firebird.conf` / `databases.conf` values
for the database, with default and source file. On Firebird 3 the page explains that the
viewer needs Firebird 4.

## Scheduled jobs

pgAgent-like schedules for **backups** (streamed into `<data dir>/backups/<schedule id>/`
with **retention**: keep the last N files), **sweeps** and **online validations**.

* Standard 5-field cron expressions (`minute hour day-of-month month day-of-week`) with
  lists, ranges, steps, month/day names and `@hourly`/`@daily`/`@weekly`/`@monthly`/`@yearly`
  (own parser: `fbadmin/maintenance/cron.py`). The editor previews the next run times.
* Schedules and run history are stored in `<data dir>/schedules.json` and
  `schedule_history.json`. An in-process thread (started in the app lifespan) checks every
  15 s (`FBADMIN_SCHEDULER_INTERVAL`) and runs due schedules as normal background jobs.
* Times use the fbadmin host's local time. Runs missed while fbadmin was not running are
  skipped. The server password must be saved, otherwise the run fails with "password required".
* *Run now*, enable/disable, edit, delete (optionally with its backup files), history, and
  download of each stored backup.

![schedules](screenshots/maintenance-schedules.png)

## API overview

| Endpoint | Purpose |
|---|---|
| `GET /api/servers/{sid}/maintenance/capabilities` | engine version and supported options |
| `POST …/databases/{db}/maintenance/backup/stream` → `GET …/maintenance/downloads/{token}` | streamed download |
| `POST …/databases/{db}/maintenance/backup` | backup job (`target`: `archive` or `server`) |
| `POST /api/servers/{sid}/maintenance/restore` | restore job from `archive` or `server` file |
| `POST /api/servers/{sid}/maintenance/restore/upload?database_path=…` | raw-body upload + restore |
| `GET/DELETE /api/servers/{sid}/maintenance/archive`, `GET …/archive/download?path=` | archive |
| `POST …/maintenance/nbackup`, `/nbackup/lock`, `/nbackup/unlock`, `/nbackup/fixup`, `GET /nbackup/history` | nbackup |
| `POST /api/servers/{sid}/maintenance/nrestore` | restore an nbackup chain |
| `POST …/maintenance/validate`, `/sweep`, `/icu` | gfix jobs |
| `GET …/maintenance/limbo`, `POST …/limbo/{id}/{commit\|rollback\|recover}` | limbo |
| `POST …/maintenance/shutdown`, `/online` | shutdown / online |
| `GET/PUT …/maintenance/properties` | properties |
| `POST …/maintenance/gstat` → `GET /api/servers/{sid}/maintenance/gstat/{job}` | parsed statistics |
| `POST …/maintenance/encrypt`, `/decrypt`, `GET /crypt-state` | encryption |
| `GET …/maintenance/config` | RDB$CONFIG |
| `/api/servers/{sid}/trace/…` | templates, preview, sessions, events, stop, download |
| `/api/schedules/…` | schedules CRUD, run, history, backups, cron preview |

`…` = `/api/servers/{sid}/databases/{db}`.

## Implementation notes

* `fbadmin/maintenance/svc.py` builds raw SPBs and runs the service query loop itself
  (the pure-Python driver only implements a few actions and reads line lengths as one byte).
* Tests: `backend/tests/test_maintenance.py` (live server: roundtrips with data
  verification, nbackup chain, shutdown/online, properties, limbo via a prepared 2PC
  transaction, validation, gstat, trace, scheduler, RDB$CONFIG, encryption error path) and
  `test_maintenance_units.py` (parser, cron, trace parsing, SPB). They pass on Firebird 3, 4 and 5;
  version-gated features assert the "requires Firebird N" error on older servers.
* nbackup and "backup to server file" tests leave their `.nbk`/`.fbk` files in the server's
  data directory (the Services API cannot delete files).
