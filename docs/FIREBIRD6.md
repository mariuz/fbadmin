# Firebird 6 and SQL schemas

Firebird 6 adds SQL schemas: every table, view, procedure, function, package, trigger, sequence,
exception, domain and index belongs to a schema (`PUBLIC` by default, system objects to `SYSTEM`).
fbadmin supports them as follows. Firebird 6 is still pre-release, so it is tested against the
`firebirdsql/firebird:6-snapshot` image. That CI job is allowed to fail.

## Object Explorer

For a Firebird 6 database, the tree gets a schema level:

```
Server
└─ Database
   ├─ APP          ← schema
   │  ├─ Tables
   │  ├─ Views
   │  └─ ...
   ├─ PUBLIC
   └─ SYSTEM       (system tables)
```

Each schema has its own object folders. Roles, users, shadows and BLOB filters are database-wide,
so they appear only under `PUBLIC`. Older servers keep the familiar Database → folders tree.

Selecting a node in a schema makes that schema the database's *active schema*:

- The properties, SQL and dependencies panels show that schema's object.
- Dialogs (create, alter, drop) work in that schema.
- A tab you open (data grid, Query Tool) keeps the schema it was opened in and switches back to it when you return to the tab.

## Extracted DDL

- **One object:** the SQL tab and script menus prefix objects outside `PUBLIC` with
  `SET SEARCH_PATH TO <schema>, SYSTEM;`. The unqualified names in the DDL, and in PSQL bodies,
  then resolve as they do in the source database.
- **Whole database:** *Tools → Extract DDL* writes `CREATE SCHEMA` statements first. It then writes
  one block per schema, `PUBLIC` first, each starting with `SET SEARCH_PATH`. Roles and role grants
  appear once; privileges are grouped with their schema. The script recreates the database exactly:
  a test extracts it, replays it into an empty database and compares.

Schema diff, data export and the ERD work on the active schema.

## Query Tool

A Query Tool opened from a schema starts with `SET SEARCH_PATH TO <schema>, SYSTEM`. On Firebird 6
the toolbar shows a **Schema** picker for the session's search path. Changing it needs the
transaction to be committed or rolled back first.

Plans on Firebird 6 name tables with their schema (`Table "PUBLIC"."ORD" Full Scan`). The plan
viewer, the index advisor and the AI assistant's context all understand this form.

## API

Requests that work on database metadata take an optional `X-Fbadmin-Schema: <name>` header. The
backend then:

- limits the catalog queries to that schema;
- sets the search path of its metadata connection for the request;
- opens Query Tool sessions with that search path.

`GET /api/servers/{sid}/databases/{dbid}/schemas` returns `{supported, schemas}`. On older servers it
returns `supported: false`. `PUT /api/query/sessions/{id}/options` accepts `search_schema`.

## Known differences in the Firebird 6 snapshots

These are engine behaviours fbadmin works around. They may change before the 6.0 release.

- Index lookups in system tables with a *parameter* (`WHERE RDB$RELATION_NAME = ?`) find nothing.
  On Firebird 6, fbadmin's catalog inlines those values as quoted literals.
- The profiler tables live in their own schema, `PLG$PROFILER`.
- `gstat -h` refuses a database in full shutdown. The header then reports `full` from the error.
- Dropping and re-adding a UNIQUE constraint leaves an internal `RDB$TEMP_DEPEND_*` index that cannot
  be dropped. fbadmin hides it.
- `MON$COMPILED_STATEMENTS` lists only statements cached by attachments that are still open.
- The server reports versions as `LI-T6.0.0.…` (`T` for test builds).

No new data types have shipped in the snapshots yet. The type list (`RDB$TYPES`) matches
Firebird 5.

## Running the tests against Firebird 6

```bash
scripts/test-servers.sh                      # also starts fb6 (6-snapshot) on port 3056
FBADMIN_TEST_PORT=3056 pytest -q backend/tests
cd e2e && FB6_PORT=3056 npx playwright test  # schemas.spec.ts is skipped without Firebird 6
```
