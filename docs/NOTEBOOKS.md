# SQL notebooks

A notebook keeps SQL cells, notes, results and charts in one saved document. Use one as a runbook
("monthly health check", "month-end close") or to hand over an investigation. Notebooks are saved by
fbadmin. In server mode they can be shared with every fbadmin user.

Open them with **📓 Notebooks** in the top bar, or create one from a database's context menu with
**New notebook…**.

## Cells

* **SQL cells** run on the notebook's database. **Run** (or Ctrl+Enter in the cell) runs the cell;
  with text selected, only the selection runs. **Run all** runs the SQL cells from the top and stops at
  the first error. Each statement commits when it succeeds (autocommit).
* **Notes** are written in Markdown: headings (`##`), lists (`-`, `1.`), **bold**, `code` and
  code blocks. Double-click a note, or use **Edit**, to change it.

Cells can be moved up and down, added after any cell (**+ SQL**, **+ Note**) and deleted.

Cells run through the same sessions as the [Query Tool](QUERY_TOOL.md), so your fbadmin permissions
apply to every statement exactly as there. A notebook can't do anything the Query Tool wouldn't let you
do.

## Results and charts

The output of a cell's last run is saved with the notebook:

* the messages and errors;
* up to five results, each with at most 1000 rows (`FBADMIN_NOTEBOOK_ROWS`);
* who ran it, when, and how long it took.

The results show in the [data grid](user-guide/data-grid.md) (sort, filter, copy, export).

**Chart the result** turns the cell's last result into a chart using the same chart view as the Query
Tool: bar, horizontal bar, stacked bar, line, area, pie, donut and scatter, with a category, value
columns, grouping, aggregation and sorting. See [Charts from query results](CHARTS.md). The chart
settings are saved with the cell, so the chart comes back when the notebook is opened.

**Clear results** removes all saved results, for example before sharing a notebook. A notebook is at
most 8 MB (`FBADMIN_NOTEBOOK_MAX_BYTES`).

## Saving, history and conflicts

Notebooks save themselves about a second after each change. The state next to the database shows
*Saved*, *Unsaved changes* or *Saving…*.

**History…** lists the saved revisions of the content (cells and notes, without results). Any revision
can be restored, and the current content stays in the history. Quick saves by the same person within
ten minutes update one revision, so autosave doesn't flood the history. The last 30 revisions are kept
(`FBADMIN_NOTEBOOK_REVISIONS`).

When two people edit the same shared notebook, a save based on an older revision is refused. The
notebook then shows who saved it in the meantime, with two choices:

* **Load their version**;
* **Save mine as a new notebook**.

## Sharing (server mode)

A notebook is **Private** (only its owner sees it) or **Shared with every fbadmin user**:

* the owner and fbadmin administrators can change or delete a shared notebook;
* everybody else can open it, run its cells and **Save a copy**, but their changes and results aren't
  saved;
* administrators never see other users' private notebooks.

**Saved results follow the viewer's permissions.** They show only to users who may read the
notebook's database as a whole. Users without access, or with
[per-table permissions](PERMISSIONS.md) on it, see the SQL and the notes but not the results, and a
banner says so. The same applies to exports and copies. When such a user edits the notebook, the
results they couldn't see are kept.

Read-only API tokens can open notebooks but not change them.

## Export and import

* **Export Markdown**: notes as they are, SQL in code blocks, results as tables (the first 50 rows),
  charts as a line of text. Good for a ticket, a wiki or a pull request.
* **Export notebook**: the whole notebook as JSON (`.fbnb.json`), with its results.
  `GET /api/notebooks/{id}/export?results=false` leaves the results out.
* **Import…** (in the Notebooks list) opens an exported notebook as a new notebook of yours, on the
  database selected in the Object Explorer.

## Storage

Each notebook is a JSON file in `<data dir>/notebooks/` (mode 0600, written atomically).

## API

| Method | Path | |
|---|---|---|
| GET | `/api/notebooks?q=&scope=all\|mine\|shared&server_id=&database_id=` | notebooks you can see (without their cells) |
| POST | `/api/notebooks` `{name, server_id, database_id, visibility, cells}` | create |
| GET | `/api/notebooks/{id}` | a notebook with its cells (results hidden when you may not read the database) |
| PUT | `/api/notebooks/{id}` `{base_rev, name, description, server_id, database_id, visibility, cells}` | save; `409` with the current notebook when `base_rev` is not the latest |
| DELETE | `/api/notebooks/{id}` | delete |
| POST | `/api/notebooks/{id}/copy` `{name}` | a private copy |
| GET | `/api/notebooks/{id}/revisions`, `/revisions/{rev}` | history |
| POST | `/api/notebooks/{id}/revisions/{rev}/restore` | restore a revision |
| GET | `/api/notebooks/{id}/export?format=json\|md&results=` | export |
| POST | `/api/notebooks/import` `{notebook, server_id, database_id}` | import an exported notebook |

A cell is `{id, type: "sql" | "markdown", source, chart, output}`. Cells are run by the client through
`/api/query/sessions`; the API stores the document.
