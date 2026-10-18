# Query Tool guide

| Action | Shortcut / control |
|---|---|
| Execute the selection, or the whole script | **F5** or **Ctrl/Cmd+Enter** |
| Cancel the running statement | the red **Cancel** button (replaces Execute while running) |
| Show the plan and detailed plan without running | **Explain** |
| Comment or uncomment lines | **Ctrl/Cmd+/** |
| Format the selection or the whole script | **Shift+Alt+F** or **Format** |
| Find / replace | **Ctrl/Cmd+F** |
| Autocomplete | **Ctrl+Space**. Type `alias.` to get that table's columns |

## Parameters

`:name` and `?` markers outside strings and comments prompt for values before the script
runs. A name used several times is asked for once. Values are sent as text, and Firebird
converts them to the target type. Tick **NULL** to send a NULL. Values are remembered in
the browser for the next run.

Markers inside `CREATE PROCEDURE/FUNCTION/TRIGGER/PACKAGE` and `EXECUTE BLOCK` are
left alone, because there `:var` refers to PSQL variables.

## Transactions

Each Query Tool tab has its own attachment and transaction.

- **Auto-commit on**: each statement is committed when it succeeds. A failed statement
  is rolled back.
- **Auto-commit off**: work stays in an open transaction (shown in the status bar) until
  you press **Commit** or **Rollback**, or run `COMMIT;` / `ROLLBACK;` in the script.
- The settings menu (labelled with the current isolation) sets isolation, read-only, lock
  resolution (wait / no wait), lock timeout, and statement timeout (Firebird 4+). These
  can only change while no transaction is open.

## Large results

The **Rows** setting limits the first fetch. When the last statement of a script returns
more rows, its cursor stays open and **Load more** fetches the next page. The open
cursor keeps its transaction alive. It is closed (and committed in auto-commit mode)
when you run another statement, commit or roll back.

## Statistics

With **Stats** ticked, each statement shows the change in its attachment's counters in
`MON$IO_STATS` and `MON$RECORD_STATS`: page fetches, reads, writes and marks, plus
sequential and indexed record reads, inserts, updates, deletes, backouts, purges and
expunges. These are a quick way to spot full table scans: compare *Seq. reads* with
*Index reads*.

## Plans

**Explain** (or **F7**) shows the classic plan plus the detailed plan.
- **Diagram** draws the detailed plan as a tree, colour-coded by node type: red for full table scans, blue for table access, green for index access, amber for sort/aggregate/hash.
- **Text** shows the raw plan text.
- **Pin plan**, then change the query and Explain again. **Compare with pinned** then shows a line diff of the two plans, which is handy for checking that a new index is actually used.

## Results

- Drag a column header's right edge to resize the column; double-click the edge to reset it.
- **Freeze 1st** keeps the first column visible while you scroll sideways.
- Export to CSV, TSV, JSON, SQL `INSERT`, Markdown or **Excel (XLSX)**.

## BLOBs in the data editor

While editing a cell, the **…** button opens a larger editor. Text BLOBs are edited as multi-line text. Binary BLOBs can be replaced by uploading a file (**Load from file…**) or downloaded. Changes are saved together with the other grid edits.

## Preferences, shortcuts and macros

The ⚙ button in the top bar opens Preferences:
- **General**: theme (auto, light, dark or high contrast), language, NULL display text, date format, confirm before drop.
- **Editor**: font size and minimap.
- **Keyboard**: rebind Execute, Explain, Format and Toggle comment, using CodeMirror key names such as `Mod-Enter`, `F5` or `Shift-Alt-f`.
- **Macros**: bind SQL to **Ctrl/Cmd+1…9**. `$SELECTION$` is replaced by the selected text. For example, `SELECT COUNT(*) FROM $SELECTION$` counts the rows of the selected table name.

## AI assistant (opt-in)

The assistant is off by default. To enable it, set `FBADMIN_AI_PROVIDER=anthropic` and install the extra with `pip install 'fbadmin[ai]'`. The Anthropic SDK then picks up your credentials, for example `ANTHROPIC_API_KEY`.
- `FBADMIN_AI_MODEL` changes the model (default `claude-opus-5-5`).
- `FBADMIN_AI_EFFORT` changes the effort level (default `high`).

The Query Tool then gets an **✦ Assistant** menu with five tasks:
- Explain the plan
- Suggest indices
- Convert SQL from another dialect
- Explain the last error
- Ask a free question

Only the SQL text, its plan and the definitions of the tables it uses are sent to the provider. Table data is never sent. Code blocks in the answer have an **Insert into editor** button.

Other providers can be plugged in. Subclass `fbadmin.ai.AIProvider`, then register it with `fbadmin.ai.register_provider(name, cls)` or under the `fbadmin.ai_providers` entry point group, and select it with `FBADMIN_AI_PROVIDER=<name>`.
