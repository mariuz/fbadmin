# Shareable links

A link points to an object, a saved query, a notebook or a snapshot of a query result. Links look
like `https://fbadmin.example.com/#/link/k3J9…`. **A link grants nothing:** it opens with the
permissions of whoever opens it, so it can be pasted into a ticket or a chat without giving anyone
access they don't already have.

| Link to | Made from | Opens | Who can open it |
|---|---|---|---|
| **an object** (table, view, procedure…) | **Copy link** in the object's context menu | the object's properties | users who see the server and database and may read the object |
| **a saved query** | **🔗 Copy link** on a [library](QUERY_LIBRARY.md) item | the query in the Query Tool on its database | users who see the item (the owner's, or shared) |
| **a notebook** | **🔗 Copy link** in a [notebook](NOTEBOOKS.md) | the notebook | users who see the notebook; its results follow the notebook's own rules |
| **a result snapshot** | the **🔗 Share** view next to a Query Tool result | a copy of the rows, with the SQL and who shared it | users who may read the database the rows came from as a whole |

Making a link copies it to the clipboard. Where the browser doesn't allow that (plain `http://`
other than localhost), a dialog shows the link to copy. You can only make links you could open
yourself.

## Opening a link

* If you need to sign in first (password or single sign-on), the link opens once you are signed in.
* If your permissions don't allow what the link points to, fbadmin says so and names who shared it,
  and nothing is shown.
* A link stops working when it expires or is revoked, and when its query or notebook is deleted or
  made private. A link to an object that was dropped still opens, and the object view says the object
  is gone.

## Snapshots

A snapshot keeps the rows the Query Tool had fetched (at most 1000, `FBADMIN_SNAPSHOT_ROWS`) and the
SQL, as they were when the link was made. It is a copy: later changes to the data don't show. Like
saved notebook results, a snapshot is only shown to users who may read its database as a whole: not
to users with no access, nor to users with [per-table permissions](PERMISSIONS.md) on it. A snapshot
expires after 30 days unless another time is chosen (1 day, 7 days, a year, never). It is at most
5 MB.

## Managing links

**🔗 Links** in the [Library](QUERY_LIBRARY.md) lists the links you made, with what they point to,
when they expire and how often they were opened. **Show link** copies one again; **Revoke** stops it
working for everyone. fbadmin administrators can list (**Everyone's links**) and revoke all links.
Expired links are removed.

Links are kept in `<data dir>/links.json` (mode 0600). Read-only API tokens can open links but not
make or revoke them.

## API

| Method | Path | |
|---|---|---|
| POST | `/api/links` `{kind, title, server_id, database_id, otype, name, schema, item_id, snapshot, expires_days}` | make a link (`kind`: `object`, `query`, `notebook`, `snapshot`); returns its `id` and `path` |
| GET | `/api/links?all=` | your links (administrators: `all=true` for everyone's) |
| GET | `/api/links/{id}` | what the link opens, for the current user; `403` with `code: link_denied` when they may not |
| DELETE | `/api/links/{id}` | revoke |
