# Database timeline

**Timeline…** (database context menu) shows one history of the database, newest first and grouped by
day, to answer "what changed before it broke?".

| Category | What | From |
|---|---|---|
| **Schema** | DDL run from the Query Tool and the object dialogs (failed statements too), dropped objects, schema imports | fbadmin's event log |
| | commits of the [schema history](SCHEMA_HISTORY.md) repository, with the objects they added, changed or dropped | the schema history repository, when configured |
| | [versioned migrations](MIGRATIONS.md) applied | `FBADMIN_MIGRATIONS` in the database |
| | scheduled schema snapshots and [drift checks](SCHEMA_DRIFT.md) (drift is a warning) | the scheduler |
| **Data** | rows changed in the grid, [data compare](DATA_COMPARE.md) scripts applied, generated test data | fbadmin's event log |
| **Backups** | backups made in fbadmin, restores over the database, nbackup runs and locks | fbadmin's event log |
| | scheduled backups (with their test restores and uploads), [restore drills](BACKUP_ENCRYPTION_DRILLS.md) | the scheduler |
| | nbackup history | `RDB$BACKUP_HISTORY` |
| **Maintenance** | sweeps, validation, shutdown and online, property changes (sweep interval, page buffers, forced writes…), encryption, limbo transactions, recomputed index selectivity, scheduled sweeps and validation, [scheduled check-ups](MONITORING.md#scheduled-check-up) (a run with findings to alert on is a warning) | fbadmin's event log, the scheduler |
| **Upgrades** | the server's Firebird version or the database's on-disk structure (ODS) changed since fbadmin last looked; upgrade trials | fbadmin's event log |
| **Alerts** | alerts sent for the database or its server | the [alert](ALERTS.md) history |
| **Audit log** | SQL run on the database: DDL, DML and other statements, plus queries with **Include queries from the audit log**. Shown to fbadmin administrators only. | the [audit log](SECURITY.md) (server mode) |

Each event shows its time, its category, what happened, who did it and whether it failed. **Details**
shows the statements, the error or the job's results. Links open the schema history, migrations,
alerts, schedules or audit log.

**Filtering.**

* **Period**: 24 hours to everything.
* **before**: a date and time. Pick when something broke to see what happened in the period before it.
* The category buttons show their counts; select one or more to see only those categories.
* **Search** looks through titles, details and users.

## fbadmin's event log

Changes made through fbadmin are recorded as the requests happen, in desktop and server mode, with the
signed-in user. Jobs (backups, sweeps, validation…) are recorded when they start, and their outcome
(finished, failed, cancelled) when they end. Refused requests are not recorded. Changes made outside
fbadmin (isql, applications) appear through the schema history, migrations, nbackup history and the
audit log, not in this log.

Firebird versions are noted whenever fbadmin looks at the database: the timeline, the
[server overview](OVERVIEW.md). The first look is the baseline; later changes become upgrade events.

The log is `<data dir>/timeline.sqlite` (mode 0600). Events are kept for two years
(`FBADMIN_TIMELINE_KEEP_DAYS`).

Sources that can't be read right now are listed above the timeline and the rest still shows; for
example, when the database is down, migrations and nbackup history are missing.

## API

`GET /api/servers/{sid}/databases/{dbid}/timeline?since=&until=&category=&q=&limit=&audit_queries=`

* `since`, `until`: Unix seconds.
* `category`: comma-separated, from `schema`, `data`, `backup`, `maintenance`, `upgrade`, `alert` and
  `audit`.

The response is `{events, total, has_more, counts, problems, audit, engine_version, ods}`. Each event
is:

```
{id, ts, category, action, title, detail, status, user, source, ref, finished_at, error}
```

* `status`: `ok`, `failed`, `warning`, `info`, `running` or `cancelled`.
* `ref`: what the event links to: a schema history commit, an alert, a schedule, a job or an audit
  entry.

Reading the timeline needs read access to the database.
