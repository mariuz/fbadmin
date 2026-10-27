# Schema history in git

*Added in 2.0.* fbadmin can keep the history of a database's schema in a git repository. Each
*snapshot* extracts the full DDL and writes it as one file per object; when an object changed,
fbadmin commits the files. The git log then tells you which tables, views or procedures changed,
when, and who took the snapshot, with a readable diff for each object.

Open it from the database's context menu: **Schema history (git)…**.

Requirements: the `git` command-line tool on the fbadmin host (on its `PATH`, or set
`FBADMIN_GIT=/path/to/git`). No Python package is needed. Without git the tab says so and the
snapshot button is disabled.

## Setting it up

| Setting | Meaning |
|---|---|
| Repository folder | A folder on the fbadmin host. A relative path is created inside `FBADMIN_SCHEMA_HISTORY_ROOT` (default `<data dir>/schema-history`). If the folder is not a git repository yet, fbadmin runs `git init`. An existing repository works too. |
| Sub-folder | Optional. The snapshot goes into this folder of the repository, so one repository can hold several databases. fbadmin only writes, deletes and commits files inside it; your README, CI files or staged changes elsewhere are left alone. |
| Branch | The branch fbadmin commits to and pushes (default `main`). |
| Remote URL | Optional. After each commit fbadmin pushes to it (`git push origin HEAD:<branch>`). The remote is saved as `origin` in the repository. |
| HTTPS token | Optional, only for HTTPS remotes, and only as `env:NAME`: the token is read from that environment variable of the fbadmin process when pushing. |
| Token user name | Sent with the token (default `x-access-token`, which GitHub accepts; GitLab uses `oauth2`). |
| Push after each commit | Turn off to push only with the **Push** button. |
| Committer name / e-mail | The commit's committer (default `fbadmin <fbadmin@localhost>`). |
| Take a snapshot after DDL run through fbadmin | See [When snapshots are taken](#when-snapshots-are-taken). |

**Credentials.** fbadmin never stores a password or token. Pushing uses, in this order:
the SSH keys of the user fbadmin runs as (for `git@host:…` and `ssh://` URLs; host keys must already
be known, since git runs non-interactively), the host's git credential helper (for example
`git config --global credential.helper store`), or the `env:NAME` token. The token is passed to git
as an HTTP header through environment variables, so it does not appear in the process list, in the
repository's `.git/config`, in fbadmin's configuration file or in error messages. A remote URL with
a password in it (`https://user:secret@host/…`) is refused.

**Server mode.** Only administrators can create, change or remove a configuration. The repository
folder must be inside `FBADMIN_SCHEMA_HISTORY_ROOT` (absolute paths outside it, symbolic links that
lead outside it and `..` are refused). Users who can open the database can take snapshots and read
the history. In desktop mode any folder may be used.

Configurations are stored in `<data dir>/schema_history.json`. **Stop history** removes the
configuration; the repository folder is kept.

## What a snapshot contains

```
tables/CUSTOMER.sql          CREATE TABLE with keys, foreign keys, checks and comments
views/V_CUSTOMER_TOTALS.sql
procedures/SP_ORDERS_FOR.sql
functions/…  packages/…  triggers/…
domains/…  sequences/…  exceptions/…  indices/…  roles/…  collations/…
grants.sql                   all GRANT statements
database.sql                 COMMENT ON DATABASE (only when there is one)
full.sql                     the whole schema as one script in dependency order (like isql -x)
.fbadmin/snapshot.json       the normalized schema, used to generate migration scripts
```

On Firebird 6 every schema is a top-level folder (`PUBLIC/tables/CUSTOMER.sql`,
`SALES/views/…`), and `full.sql` creates the schemas and switches the search path for each.

The output is deterministic, so an unchanged database gives byte-identical files and no commit:
objects are sorted by name, there are no timestamps or version numbers in the files, trailing
spaces are removed, line endings are LF, and sequence values (which change with the data) are left
out. Object names are used as file names; characters other than `A-Z 0-9 _ $` are written as `%XX`
(for example a quoted `"Customer"` becomes `C%75%73%74%6F%6D%65%72.sql`), so names never collide
on case-insensitive file systems and never leave their folder.

The per-object files use the same DDL as the [schema diff](SCHEMA_TOOLS.md#schema-diff) shows. PSQL
in these files has no `SET TERM`; use `full.sql` to recreate a database with isql.

## Commits

A commit is made only when at least one object file changed. Its message lists the objects:

```
Schema snapshot of sales: 1 added, 1 changed, 1 dropped

Added:
  table AUDIT_LOG
Changed:
  table CUSTOMER
Dropped:
  index IX_ORDERS_AMOUNT

Snapshot taken by alice (manual)
```

The author is the fbadmin user who took the snapshot (server mode: user name and e-mail from the
fbadmin user account); in desktop mode, and for scheduled snapshots, it is the configured committer.
The trigger is `manual`, `schedule <name>` or `after DDL`. Commits are never signed and repository
hooks are skipped (`--no-verify`).

If a push fails, the commit stays in the local repository, the tab shows **Last push failed: …**
with git's message, and **Push** retries. A scheduled snapshot whose push failed is marked failed
in the schedule history (and raises the *schedule failed* alert).

## When snapshots are taken

- **Take snapshot** in the tab.
- **On a schedule**: *Maintenance → Scheduled jobs → New schedule*, job **Schema snapshot**, with a
  cron expression. The run appears in Processes and in the schedule history (with the commit id).
- **After DDL run through fbadmin** (optional): after a script with `CREATE`, `ALTER`, `DROP`,
  `RECREATE`, `COMMENT ON`, `GRANT`, `REVOKE` or `DECLARE EXTERNAL FUNCTION` runs in the Query Tool
  or in an object dialog, fbadmin takes a snapshot in the background a few seconds later
  (`FBADMIN_SCHEMA_HISTORY_DELAY`, default 5 seconds; further DDL in that time restarts the wait, so a
  long script gives one commit). In the Query Tool with autocommit off, the snapshot waits for the
  **Commit**. Running the DDL is not slowed down: the hook only checks the statement text. DDL run by
  other programs is picked up by the next manual or scheduled snapshot. To be told about such changes
  instead, schedule a [drift check](SCHEMA_DRIFT.md) against the history.

## Reading the history

The tab lists the commits (newest first) with author, time and the number of added (+), changed (≠)
and dropped (−) objects. Selecting a commit shows the objects it touched; selecting an object shows
its unified diff, with SQL keywords highlighted.

- **Show at this commit** shows the object's full DDL as it was in that commit.
- **Show before** shows it as it was before the commit (for changed and dropped objects).
- **Migration script from** *commit* **to this commit** → **Generate** builds a script that changes
  a database with the older schema into the newer one (or back, when you choose a newer commit). It
  uses the [schema diff](SCHEMA_TOOLS.md#schema-diff) on the two stored snapshots, so it needs no
  database connection and has the same safe statement order and the same limitations. Download it,
  copy it, or open it in the Query Tool to review and run it.

You can of course also use any git tool on the repository (`git log -p -- tables/CUSTOMER.sql`).

## API

All under `/api/servers/{server_id}/databases/{db_id}/schema-history`:

| Method | Path | |
|---|---|---|
| GET | `` | configuration (or `null`) and git status |
| PUT | `` | create or change the configuration (admin in server mode) |
| DELETE | `` | remove the configuration (admin in server mode) |
| POST | `/snapshot` | take a snapshot: `{committed, commit, changes, push, files}` |
| POST | `/push` | push now (502 with git's message on failure) |
| GET | `/log?limit=&skip=` | commits with their added/changed/dropped objects |
| GET | `/commits/{sha}` | the commit's per-file unified diffs |
| GET | `/commits/{sha}/files` | the object files at a commit |
| GET | `/commits/{sha}/file?path=` | one file's content at a commit |
| GET | `/migration?from_sha=&to_sha=` | migration script between two commits |

## Code

- backend: `backend/fbadmin/schemahistory/` - `layout.py` (files, deterministic output, snapshot
  JSON), `gitrepo.py` (the `git` command-line wrapper), `__init__.py` (configuration, snapshot,
  commit and push, history, migration, after-DDL hook); API `backend/fbadmin/api/schema_history.py`;
  schedule kind `schema_snapshot` in `backend/fbadmin/maintenance/scheduler.py`
- frontend: `frontend/src/features/schema-history/`
- tests: `backend/tests/test_schema_history.py`, `e2e/tests/schema-history.spec.ts`
