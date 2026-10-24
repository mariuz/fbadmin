# Upgrade assistant

The upgrade assistant checks a database before you move it to a newer Firebird major version
(3 → 4, 5 or 6; 4 → 5 or 6; 5 → 6). It lists what will break or behave differently, points at the
affected objects, says how to fix each one, and writes a migration plan for this database.

Open it from the database's context menu: **Upgrade assistant…**.

## Running the checks

1. Choose the **Target version**. Only versions newer than the server the database runs on are offered.
2. Click **Run checks**. The checks only read metadata, so they are quick and safe on a production
   database.

The report shows:

- the source and target version, ODS, page size and dialect;
- the number of **blockers** (will fail after the upgrade unless fixed), **warnings** (may break;
  review) and **info** items (good to know). The check boxes filter the list;
- the findings, grouped by category. Each finding has an explanation, a fix, the affected objects and,
  where Firebird can do it, the exact statement (for example `ALTER TABLE "WINDOW" ALTER COLUMN "LOCAL" TO LOCAL_;`).
  Click an object to select it in the Object Explorer;
- the **migration plan**: numbered steps with the commands for this database (server, path, page size,
  parallel workers);
- **Download Markdown** / **Download HTML** save the whole report to share or attach to a change request.

## Trial restore

Choose a newer server in **Trial restore on** to test the real thing. The target server must be registered in
fbadmin. The assistant then:

1. backs the database up with the **source** server's gbak (through the Services API) into a temporary
   file on the fbadmin host;
2. restores that file with the **target** server's gbak into a scratch database
   (`fbadmin_upgrade_<random>.fdb`, next to the target server's first registered database, or next to the
   source database; you can enter another path);
3. compares the object counts, reads the target server's settings from `RDB$CONFIG` and lists modules
   that the restore marked invalid;
4. **recompiles every view, procedure, function, package and trigger** from its stored source on the target
   server (`CREATE OR ALTER ...`). An object that fails becomes a blocker with the server's error message,
   for example `Token unknown - line 5, column 12 / LOCAL`;
5. drops the scratch database, unless you tick **Keep and register the restored database**: it is then
   registered on the target server as "*name* (Firebird *N* trial)" so you can point a test application at it.

**Metadata only (faster)** backs up the metadata without data. Use it for a quick code check; leave it off to
also test that the data restores (for example malformed strings).

The restored database keeps working even where the recompile fails: gbak restores the compiled form (BLR), not the source.
The failure tells you that the next `ALTER`, or any script that recreates the object, will fail.

The trial uses disk space for one backup on the fbadmin host and one database on the target server, and
loads the source server like any backup. Run it outside peak hours on large databases. **Cancel** next to the job's log stops the trial at any
step (backup, restore or recompile) and drops the scratch database.

## What is checked

| Finding | Severity | When |
|---|---|---|
| Names that are reserved words in the target (tables, views, columns, procedures and their parameters, functions and arguments, packages, domains, sequences, exceptions) | blocker | any upgrade |
| Triggers, indices, constraints, roles, collations, BLOB filters or users with such names | warning | any upgrade |
| PSQL modules, views, check constraints, computed columns, defaults, domain checks and index expressions that use a newly reserved word, outside strings, comments and quoted names | warning | any upgrade |
| Objects that do not compile on the target server | blocker | trial restore |
| Legacy UDFs (`DECLARE EXTERNAL FUNCTION`), with the built-in replacement for each ib_udf/fbudf entry point | blocker from Firebird 3, warning from 4/5 | target 4+ |
| BLOB filters (native libraries) | warning | any upgrade |
| External tables and `ExternalFileAccess` | warning | any upgrade |
| SQL dialect 1 | warning | any upgrade |
| Users of `Legacy_UserManager`; Srp256 default and the per-server security database | warning / info | 3 → 4+ |
| `CURRENT_TIMESTAMP` / `CURRENT_TIME` in stored code; new data types and `DataTypeCompatibility` | warning / info | 3 → 4+ |
| Read consistency for READ COMMITTED (`ReadConsistency`) | info | 3 → 4+ |
| `CREATE SEQUENCE ... START WITH` first value | info | 3 → 4+ |
| SQL SECURITY | info | 3 → 4+ |
| Code that reads `RDB$`/`MON$`/`SEC$` tables (31 → 63 character UTF8 names from 4; schema names in 6) | info, warning for 6 | any upgrade |
| UNICODE_FSS columns | info | any upgrade |
| Active database (ON CONNECT, DDL...) triggers | info | any upgrade |
| Invalid modules (`RDB$VALID_BLR = 0`) | warning | any upgrade |
| Routines named like a new built-in function | info | any upgrade |
| ODS change (and Firebird 5 opening Firebird 4 databases directly) | info | any upgrade |
| Page size below 8192 | warning for 6, info otherwise | any upgrade |
| Secondary files and shadows | info | any upgrade |
| Encrypted database | warning | any upgrade |
| Objects move into the `PUBLIC` schema; objects named `PUBLIC`, `SYSTEM` or `PLG$PROFILER` | info / warning | target 6 |

What the assistant does not check: SQL inside your applications (search it for the words in the report),
SQL built as strings for `EXECUTE STATEMENT`, and the server configuration of the old server (the
trial restore reads the new server's settings).

## ODS per version

Checked by creating a database on each test server and reading `MON$ODS_MAJOR` / `MON$ODS_MINOR`:

| Firebird | ODS |
|---|---|
| 3.0 | 12.0 |
| 4.0 | 13.0 |
| 5.0 | 13.1 |
| 6.0 (snapshot) | 14.0 |

A new major ODS means the new server cannot open the old file: move the database with gbak (backup by the old
server, restore by the new one). Never use nbackup or a file copy across major versions. Firebird 5 can open a
Firebird 4 (ODS 13.0) database and upgrades it to 13.1 on the first read-write attachment; that is one-way.

## Facts the checks rely on

These were verified on Firebird 3.0.14, 4.0.7, 5.0.4 and a 6.0 snapshot, or in the Firebird sources:

- Reserved words: Firebird 5 and 6 have the `RDB$KEYWORDS` table (Firebird 4 does not). The lists for 3.0 and
  4.0 come from the Firebird sources (`src/common/keywords.cpp`, and for 3.0 the `non_reserved_word` rule of
  `src/dsql/parse.y`). Newly reserved: 3 → 4: `BINARY DECFLOAT INT128 LATERAL LOCAL PUBLICATION RESETTING
  SCHEMA TIMEZONE_HOUR TIMEZONE_MINUTE UNBOUNDED VARBINARY WINDOW WITHOUT` and some `RDB$` functions;
  4 → 5: `RDB$RESET_CONTEXT`; 5 → 6: `BTRIM CALL CURRENT_SCHEMA GREATEST GROUPS LEAST LISTAGG LTRIM
  PERCENTILE_CONT PERCENTILE_DISC RTRIM TRUNCATE WITHIN`.
- Defaults on Firebird 4, 5 and 6: `UdfAccess = None`, `ExternalFileAccess = None`, `AuthServer = Srp256`,
  `UserManager = Srp`, `ReadConsistency = 1`.
- ib_udf and fbudf are not part of Firebird 4 and later; `misc/upgrade/v4.0/udf_replace.sql` (shipped since 4.0)
  redeclares their common functions as PSQL or `udf_compat` UDR functions.
- `CURRENT_TIMESTAMP` returns `TIMESTAMP WITH TIME ZONE` from Firebird 4.
- A sequence created with `START WITH 10` returns 11 first on Firebird 3, 10 on Firebird 4 and later.
- System tables: no table or column was removed from 3.0 to the 6.0 snapshot; names became `VARCHAR(63)`
  `UTF8` in Firebird 4 (were `VARCHAR(31)` `UNICODE_FSS`).
- Firebird 6 has a minimum page size of 8192 (the snapshot creates 8192 when 4096 is asked for).
- A PSQL function named like a new built-in function (for example `BASE64_ENCODE` on Firebird 4) still wins an
  unquoted call.

Firebird 6 is a snapshot: re-run the assistant against the release before upgrading production.

## Keyword lists

The lists are stored in `backend/fbadmin/upgrade/keywords_data.py`, so the checks work without the target
server. Regenerate them with:

```bash
python scripts/gen-upgrade-keywords.py --fb5 127.0.0.1:3050 --fb6 127.0.0.1:3056
```

The script reads `RDB$KEYWORDS` from the Firebird 5 and 6 servers and downloads the 3.0 and 4.0 sources from
GitHub; a source that cannot be reached keeps its current list.

## API

| Method and path | Purpose |
|---|---|
| `GET /api/upgrade/versions` | Supported versions, ODS per version, keyword list sources |
| `POST /api/servers/{sid}/databases/{dbid}/upgrade/check` `{"target": "5.0"}` | Run the checks; returns the report (findings, counts, plan) |
| `POST /api/servers/{sid}/databases/{dbid}/upgrade/trial` `{"target_server_id", "scratch_path"?, "metadata_only", "keep"}` | Start the trial restore as a background job (202) |
| `GET /api/upgrade/jobs/{job_id}/result` | The job's status and, when finished, the full report including the trial |
| `POST /api/upgrade/render` `{"report", "format": "md" \| "html"}` | The report as a document: `{filename, media_type, content}` |
