# Versioned migrations

Numbered SQL scripts in git, applied to a database in order, with a record in the database of what was
applied and a **dry run on a copy first**. It works like Flyway, from the UI (**Migrations…** in a
database's context menu) and from the command-line tool (`fbadmin migrate`).

## Scripts

A migration script is named `V<version>__<description>.sql`:

```
db/migrations/
  V1__customers.sql
  V1.1__customer_email.sql
  V2__orders.sql
  V2026_10_30_1__invoice_numbers.sql
```

* The **version** is numbers separated by dots or underscores (`1`, `1.1`, `2026_10_30_1`). Scripts run
  in numeric order (`1.2` before `1.10`); `1.0` is the same version as `1`, and two scripts with the
  same version are an error.
* The **description** follows two underscores; underscores in it become spaces.
* Scripts can be in sub-folders. Other files are ignored; `.sql` files that don't follow the pattern
  (for example Flyway's repeatable `R__` scripts) are listed as warnings.
* A script is an ordinary SQL script, as in the Query Tool: several statements, `SET TERM` for PSQL
  bodies, and `COMMIT`. Every statement is committed as it runs, because Firebird can't use a table in
  the transaction that created it. The script stops at the first error.

## The record in the database

Each database records its migrations in `FBADMIN_MIGRATIONS`: version, description, script name, the
SHA-256 checksum of the script, `SQL` or `BASELINE`, success, who applied it, when, how long it took, the
source (for example `main@3f2a9c1d0e`) and the error of a failed script. A small `FBADMIN_MIGRATIONS_LOCK`
table makes sure only one run at a time changes a database, across fbadmin servers and command-line tools.
fbadmin creates both tables on the first run.

The checksum ignores line endings and a byte order mark, so a checkout on Windows is the same script.

## States and rules

| State | Meaning |
|---|---|
| applied | Recorded, same checksum |
| pending | Not applied yet: the next run applies it |
| changed | Applied, but the script was edited since. **Blocks** runs: write a new script instead (or **Repair** to accept the new checksum, for example after a comment changed) |
| failed | The last run failed in this script. **Blocks** runs: Firebird committed the statements before the error, so fix the database by hand, correct the script, then **Repair** (it removes the failed record) |
| late (out of order) | Pending, with a lower version than an applied one (a branch merged late). **Blocks** runs unless out-of-order scripts are allowed |
| script missing | Applied, but the script is gone (a warning) |
| baseline | Older than the baseline: never run on this database |
| above the target | Newer than the version given as the target (`--target`) |

## Dry run first

A dry run copies the database on its own server and runs the pending scripts on the copy. The copy is
`fbadmin_migrate_<id>.fdb` next to the database, unless you give another path. fbadmin reports each
script (statements, time, or the failing line and statement) and the **schema changes** the scripts make
(tables, views, procedures, ... created, altered or dropped, with the DDL before and after). Then it
drops the copy.

* **Metadata-only copy** (default): fast, whatever the size of the database; scripts that insert or
  update data run on empty tables.
* **Full copy**: with the data, so a script that fails on real data (a new `NOT NULL` column, a unique
  index on duplicates) fails in the dry run too.

**Apply** wants a passed dry run of the same scripts against the database as it is now. If the scripts
or the database's record changed since, or the dry run is older than 24 hours, run it again. *Apply
without a dry run* exists, and is written to the audit log as such.

## Sources

* **A git repository** (UI): URL (HTTPS, SSH or a local path), branch, tag or commit, and the folder
  of the scripts. fbadmin fetches it before each status, dry run and apply. A private HTTPS repository
  takes a token as `env:NAME` (an environment variable of the fbadmin server, never stored); SSH uses
  the fbadmin host's keys.
* **A directory on the fbadmin host**. In server mode only administrators can set one, and only inside
  `FBADMIN_MIGRATIONS_ROOT`.
* **The request itself**: the command-line tool in remote mode sends the scripts of a local directory.

## Existing databases: baseline

A database that existed before its migrations (made by hand, or from an older tool) is given a
**baseline**: "this database is at version 3". Scripts up to version 3 are never run on it; version 4
is the first one applied. A baseline is only possible while no script has been applied.

## Command-line tool

```bash
fbadmin migrate status   @Production/app --dir db/migrations           # --check: exit 1 when pending
fbadmin migrate dry-run  @Production/app --dir db/migrations --copy full
fbadmin migrate apply    @Production/app --dir db/migrations           # dry run, then apply
fbadmin migrate apply    /data/app.fdb --host db1 -u SYSDBA --password-env FB_PW --dir db/migrations
fbadmin migrate baseline @Production/app --version 3
fbadmin migrate repair   @Production/app --dir db/migrations
fbadmin --url https://fbadmin.example.com --token-env FBADMIN_TOKEN migrate apply @Production/app --dir db/migrations
```

`apply` runs a dry run first and stops when it fails (`--no-dry-run` skips it). The recorded source is the
directory's git commit (or `--ref`). With `--url` the fbadmin server runs everything with its own
connection to Firebird and writes the audit log; the token needs the rights to change the database.
Exit codes: 0 ok, 1 scripts pending (`status --check`), 4 a script, the dry run or the rules failed.

A CI job that migrates on every merge:

```yaml
- run: pip install firebird-admin
- run: fbadmin --url "$FBADMIN_URL" --token-env FBADMIN_TOKEN migrate apply @Production/app --dir db/migrations
  env:
    FBADMIN_URL: https://fbadmin.example.com
    FBADMIN_TOKEN: ${{ secrets.FBADMIN_TOKEN }}
```

## API

| Endpoint | |
|---|---|
| `GET/PUT/DELETE …/migrations/source` | the source: `{kind: git\|directory, url, ref, path, token, out_of_order, copy_mode, scratch}` |
| `GET …/migrations?target=` | status of the configured source (items, pending, errors, warnings, current version, whether a dry run is needed) |
| `POST …/migrations/status` `{scripts?, source_ref?, out_of_order?, target?}` | status, of inline scripts (`[{name, sql}]`) or the source |
| `POST …/migrations/dry-run` `{…, copy_mode?, scratch?}` | dry run (job); `GET …/migrations/dry-run` is the last report |
| `POST …/migrations/apply` `{…, skip_dry_run?}` | apply (job); 409 when a dry run is needed |
| `GET …/migrations/runs/{job}` | the report of a dry run or apply job |
| `POST …/migrations/baseline` `{version, description}`, `POST …/migrations/repair` | baseline, repair |

`…` = `/api/servers/{sid}/databases/{db}`. Dry runs, applies, baselines and repairs are written to the
audit log.

## Tested

`backend/tests/test_migrations.py` covers the naming, ordering and status rules, a git source through the
API (dry runs that pass and fail, apply, a failed script, repair, an edited script, a late script, a tag),
inline scripts, baselines, the lock, and the command-line tool, on Firebird 3, 4, 5 and 6.
`test_cli_remote.py` runs `fbadmin migrate --url` against an fbadmin server. `e2e/tests/migrations.spec.ts`
goes through the UI, with accessibility checks.
