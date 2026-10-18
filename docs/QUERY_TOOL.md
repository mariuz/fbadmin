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
