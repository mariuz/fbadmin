# Schema drift detection

A *drift check* compares the live schema of a database with a **baseline** on a schedule, and tells you
when they differ. The baseline is either:

- **The schema history in git** ([Schema history](SCHEMA_HISTORY.md)): the schema recorded at a branch,
  tag or commit. By default it's the database's own repository and its configured branch. Changes made
  outside fbadmin since the last recorded snapshot then show up, for example from isql, a deployment
  script or another tool. A tag such as `approved` or a branch that your team pushes to (`origin/main`,
  fetched first) turns git into the source of truth for production.
- **Another database**: for example production compared with staging, or a replica with its primary.

## Setting up a check

*Schedules → New schedule*, job **Schema drift check**, then choose the database and a cron expression.

| Option | Meaning |
|---|---|
| Compare with | *Schema history (git)* or *Another database*. |
| Repository | Whose schema history: this database's (default), or another database's. For example, compare production with the history of the staging database. The repository must be set up in that database's *Schema history* tab. |
| Branch, tag or commit | Default: the configured branch. `origin/<branch>` needs *Fetch the remote first*. |
| Fetch the remote first | Fetches the repository's remote before comparing, with the token from the schema history configuration. |
| Baseline database | For *Another database*. It must be a different database. |
| Ignore | Object types that are not compared: comments, grants, indices, triggers, views, procedures, functions, packages, sequences, domains, exceptions, roles, collations, tables, database settings. *Comments* removes `COMMENT ON` from every object, so an object that differs only by its comment counts as unchanged. |
| Ignore names | Patterns with `*` and `?`, separated by commas, case-insensitive (for example `TMP_*, *_BAK`). |

*Run now* checks at once.

## The report

Each run compares the two schemas, object by object, and reports each difference as one of:

| Status | Meaning |
|---|---|
| **Missing** | In the baseline, not in the database (dropped, or never created). |
| **Extra** | In the database, not in the baseline (created outside the recorded schema). |
| **Changed** | Different in the database, for example an added column, a changed procedure body, a different index or comment. |

The run's status is **drift** when anything differs, and *finished* otherwise; the history shows the
counts. Selecting the schedule shows the latest **drift report**:
- the differences by object type, with the baseline's and the database's DDL side by side;
- the **script** that makes the database match the baseline (the same migration as the
  [schema diff](SCHEMA_TOOLS.md)), which you can download, copy or open in the Query Tool to review and
  run.

Differences that are ignored are counted as *ignored* but not listed. The schema diff works on whole
schemas, so when something was ignored, the script may still change those objects. A note at the top
of the script says so; review it before running.

The latest report of each schedule is kept in `<data dir>/drift/<schedule id>.json` and deleted with the
schedule. Reports list up to 2,000 differences; the summary and the script always cover all of them.

On Firebird 6, every SQL schema is compared. A schema that exists on only one side gets `CREATE SCHEMA`
or `DROP SCHEMA` in the script.

## Alerts

The [alert](ALERTS.md) event `schema.drift` is raised when a check finds drift.
- **The same drift again:** the rule's cool-down applies, so an unchanged drift isn't sent on every run.
- **Different drift** (other objects, or other changes to them): sent at once, even during the cool-down.
- **Drift gone:** one *resolved* message.

The message names the baseline, the counts and the first ten objects.

## Permissions

In server mode, as for every schedule:
- creating a drift check needs the *admin* level on the database it checks;
- the check runs as the user who saved it.

It also reads the baseline, so that user needs at least *read* access to the baseline database, or to
the database whose schema history is used.

## API

| Endpoint | Purpose |
|---|---|
| `POST /api/schedules` with `"kind": "schema_drift"` and `"drift": {…}` | Create a drift check. `drift` holds `baseline` (`history` or `database`), `history_server_id` / `history_database_id`, `ref`, `fetch`, `server_id` / `database_id` (for `database`), `ignore_types` and `ignore_names`. |
| `POST /api/schedules/{id}/run` | Check now (a background job). |
| `GET /api/schedules/{id}/drift` | The latest report: `drift`, `summary` (`missing`, `extra`, `changed`), `items` (with `baseline_ddl` and `database_ddl`), `ignored`, `baseline`, `script`, `warnings`. |
| `GET /api/schedules/{id}/history` | Runs with `status` `drift` or `finished`, the summary and the baseline. |

## Code

- `backend/fbadmin/maintenance/drift.py`: options, comparison, baselines and reports.
- `maintenance/scheduler.py`: the `schema_drift` kind and its alerts.
- `schemahistory.snapshots_at`: the schema stored at a git ref.
- UI: `frontend/src/features/maintenance/drift.tsx`.
- Tests:
  - `backend/tests/test_schema_drift.py`: history and database baselines, tags, ignores, alerts, permissions; runs on Firebird 3 to 6;
  - `e2e/tests/drift.spec.ts`: the schedule form, the report in three themes, the script.
