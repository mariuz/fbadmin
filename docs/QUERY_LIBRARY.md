# Query library

The query library keeps saved queries and snippets on the fbadmin server instead of in one
browser. Open **📚 Library** in the toolbar.

In desktop mode it is simply your library. In server mode every fbadmin user has their own
items and can share items with the whole team.

## Saving a query

In the Query Tool, click the **Save to library** button (the disk icon at the right of the
toolbar). It saves the selected text, or the whole editor when nothing is selected. The dialog asks for:

| Field | Notes |
|---|---|
| Name | Required. Proposed from the first line of the SQL. |
| Description | Optional free text; it is searched too. |
| Folder | A path such as `Reports/Monthly`. Folders are created as you use them. |
| Tags | Comma-separated, e.g. `report, customers`. |
| Visibility | Server mode only: **Private** (only you) or **Shared with every fbadmin user**. |
| Default database | The database the query opens on. Defaults to the Query Tool's database; choose **None** to always use the database selected in the Object Explorer. |

You can also create a query in the library itself with **New query**.

## Finding and running queries

The left column filters the list:

- **Search** matches the name, description, folder, tags and SQL text. Every word must match,
  and case is ignored, so `customer city` finds `SELECT * FROM CUSTOMER WHERE CITY = :city`.
- **Folders** show the items of a folder and its sub-folders.
- **Tags** filter by one tag (click it again to clear).
- In server mode, **Show** picks all items you can see, only your own, or only shared ones.

Select an item to see its details:

- **Run** opens a new Query Tool tab with the SQL and runs it.
- **Open in Query Tool** opens it without running (double-clicking the item in the list does the same).
- **Opens on** shows which database is used: the item's default database if it is registered
  in your Object Explorer, otherwise the database selected in the Object Explorer.
- Parameters such as `:city` are listed under **Parameters**. As in the Query Tool, fbadmin asks
  for their values when the query runs.

## Editing, copies and history

- **Edit** changes the name, description, folder, tags, visibility, default database or SQL.
- **Save a copy** creates a new private item from any item you can see, including shared items
  that you are not allowed to change.
- **History** lists the saved revisions with their author and time. **View** shows a revision's
  SQL; **Restore** saves it again as the newest revision, so the history is never lost.
  fbadmin keeps the last 20 revisions of each item (`FBADMIN_LIBRARY_REVISIONS`).
- **Delete** removes the item and its history.

## Sharing in server mode

| | Owner | fbadmin administrator | Other users |
|---|---|---|---|
| Private item | see, change, delete | — (not visible) | — (not visible) |
| Shared item | see, change, delete, make private | see, change, delete | see, run, save a copy |

- The owner is the user who created the item. Changing it never transfers ownership; the
  revision history records who saved each revision.
- Only the owner can make a shared item private again.
- fbadmin has no user groups yet, so "shared" means shared with every fbadmin user.
- The default database is stored by its fbadmin id. A shared item whose default database is
  one of the owner's private registrations opens on the database selected in your Object Explorer instead.

## Snippets from older versions

Before 2.0 the Query Tool kept snippets in the browser only. The first time you open the library
or save to it, fbadmin copies those snippets into the library (folder `Snippets`). Items you
already have with the same name and SQL are skipped, so opening fbadmin in another
browser does not create duplicates. The Query Tool's **Snippets** pane keeps working as before.

## Storage

The library is stored in `<data dir>/library.json` (file mode 0600, replaced atomically on
every change). It holds every user's items, so back it up with the rest of the data directory.

## API

| Method and path | Purpose |
|---|---|
| `GET /api/library` | mode, user, folders and tags (with counts) |
| `GET /api/library/items?q=&tag=&folder=&scope=all\|mine\|shared` | search |
| `POST /api/library/items` | create |
| `GET /api/library/items/{id}` | one item with its revisions |
| `PUT /api/library/items/{id}` | change (403 when you may not) |
| `DELETE /api/library/items/{id}` | delete |
| `POST /api/library/items/{id}/copy` | save a private copy |
| `GET /api/library/items/{id}/revisions` | revision history, newest first |
| `POST /api/library/items/{id}/revisions/{rev}/restore` | restore a revision |
| `POST /api/library/import` | import snippets (skips duplicates) |
