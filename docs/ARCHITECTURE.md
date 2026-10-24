# Architecture

```
┌────────────────────────── browser ──────────────────────────┐
│ React + TypeScript (frontend/src)                           │
│  Tree ─ Dashboard/Properties/SQL/Dependencies ─ QueryTool   │
│  DataView ─ Monitor ─ Processes ─ dialogs                   │
└───────────────▲─────────────────────────────────────────────┘
                │ JSON over HTTP (/api/...)
┌───────────────┴─────────── fbadmin (Python) ────────────────┐
│ app.py           FastAPI app, error mapping, static files   │
│ api/servers.py   registry, server info, backup/restore jobs │
│ api/databases.py objects, DDL, data grid, monitor, execute  │
│ api/query.py     Query Tool sessions (own transaction each) │
│ api/jobs.py      background job status + logs               │
│ metadata/        Catalog: RDB$/SEC$/MON$ introspection + DDL │
│ query.py         script runner (uses sqlsplit.py)           │
│ connections.py   connection manager, Services API           │
│ store.py         servers.json registry                      │
└───────────────┬─────────────────────────────────────────────┘
                │ Firebird wire protocol (pure Python firebirdsql)
          ┌─────┴──────┐
          │  Firebird  │ 3.0 / 4.0 / 5.0
          └────────────┘
```

## Key decisions

**Two drivers, chosen automatically.** [`firebirdsql`](https://github.com/nakagami/pyfirebirdsql)
speaks the wire protocol directly: Srp authentication, wire encryption, and Services API
calls for backup, restore, sweep, gstat and the log, with no `libfbclient` to install, so
fbadmin works everywhere Python does. When a Firebird 4+ client library is available (the
desktop app and the Docker image include one) the default setting uses the much faster
native [`firebird-driver`](https://pypi.org/project/firebird-driver/) instead, through an
adapter (`native.py`) that gives the rest of the app the same DB-API surface and error
classes. See [DRIVERS.md](DRIVERS.md).

**Connections.**
- *Metadata connection*: one per registered database, used by the object explorer,
  dashboards, data grid and dialogs. Each request commits at the end, so a new object
  shows up right away and MON$ snapshots are fresh.
- *Query sessions*: each Query Tool tab opens its own attachment with its own
  transaction. In auto-commit mode every statement is committed. In manual mode the
  transaction stays open until Commit or Rollback, and the UI shows a "transaction
  open" warning.
- `firebirdsql` connections are not thread-safe, so each one is guarded by a lock.
  FastAPI runs the sync endpoints in a thread pool.

**DDL extraction.** `metadata/catalog.py` reads the system tables directly, rather than
shelling out to `isql -x`, so it works remotely and per object. The integration test
`test_ddl_roundtrip` extracts every object, replays the DDL into an empty database and
checks that re-extracting gives identical text.

**Script splitting.** `sqlsplit.py` splits scripts the way isql does (`SET TERM`) and also
the way GUI users expect: a `;` inside `BEGIN … END` or `CASE … END`, or in a PSQL
`DECLARE` section, does not end the statement. Pasted procedures therefore just work.

**Frontend state.** It is deliberately simple: one React context (`context.tsx`) with
servers, selection, tabs, dialogs and a per-tree-node version counter used to refresh
subtrees. Tabs stay mounted while hidden, so a Query Tool keeps its editor, results and
open transaction when you switch tabs.

## Adding a new object type

1. Add `_list_<type>` and `_get_<type>` (returning `properties` and `ddl`) to `Catalog`.
2. Add the type to `OBJECT_TYPES` in `metadata/catalog.py` and `frontend/src/types.ts`.
3. Add an icon in `components/Icons.tsx` (`TYPE_ICONS`), a drop keyword in
   `_DROP_KEYWORDS`, and a template in `templates.ts`.
4. Add a test, and include the type in the round-trip test if it has DDL.
