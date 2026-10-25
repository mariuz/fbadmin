# Visual query builder

The query builder makes a `SELECT` without typing SQL: pick tables and views, join them, pick
columns, add conditions. The SQL is shown as you go. You can run it there, or open it in the
Query Tool.

## Opening it

- Right-click a database → **Query builder**.
- Right-click a table or view → **Add to query builder**. This opens the builder for that database,
  or adds the table to the builder that is already open.

There is one builder tab per database (per schema on Firebird 6). The builder remembers its last
query for each database in the browser, so closing the tab or reloading the page keeps your work.

## Adding tables and views

- Pick one in **Add table** in the toolbar and click **Add**, or
- drag a table or view from the Object Explorer onto the canvas.

Each table gets a short alias (`CUSTOMER` → `C`, `ORDER_LINES` → `OL`). You can change it in the
**Tables** grid. Click the refresh button next to **Add** when tables were created after the
builder was opened.

**Firebird 6:** the toolbar has a **Schema** picker. It chooses which schema's tables the list
shows. Tables are always written with their schema (`PUBLIC.CUSTOMER`, `APP.ITEMS`), so a query
can mix schemas and runs the same whatever the search path is.

## Joins

When you add a table, the builder looks at the foreign keys between it and the tables already on
the canvas and adds an `INNER JOIN` for each related pair (one per pair).

You can also:

- drag a column onto a column of another table: this adds a join on those two columns, or adds the
  condition to the existing join between the two tables;
- click the label on a join line (`INNER`, `LEFT`, ...) to edit it in the **Joins** grid;
- in the **Joins** grid, change the join type (`INNER`, `LEFT`, `RIGHT`, `FULL`), the tables, the
  conditions (`=`, `<>`, `<`, `<=`, `>`, `>=`), or write a **Custom ON** condition in SQL, which
  replaces the generated one.

Outer joins are drawn with dashed lines. A table that is not joined to the others is added with
`CROSS JOIN`, and the builder warns about it.

## Columns

Tick columns in the table boxes, or use the **Columns** grid, where each row has:

| Field | Meaning |
|---|---|
| Output | Unticked rows are not selected but can still be used for sorting. |
| Column or expression | A column, `alias.*`, or *(expression)* for any SQL expression, e.g. `UPPER(C.NAME)`. |
| Aggregate | `COUNT`, `COUNT DISTINCT`, `SUM`, `AVG`, `MIN`, `MAX`, `LIST`. |
| Alias | The column name in the result (`AS ...`). Plain words are upper-cased, like Firebird does. |
| Sort, Sort order | `ORDER BY` direction and position (lower numbers sort first). |

Without any column the query selects `*` (one table) or `C.*, O.*` (several).

**GROUP BY is automatic:** as soon as one output column has an aggregate, every other output
column is grouped (and so are unselected columns used for sorting).

## Conditions (WHERE and HAVING)

Each row of the **Conditions** grid is a column (or expression), an operator and a value. Rows with an
aggregate go to `HAVING`, the others to `WHERE`. Rows are combined with `AND` or `OR` as chosen;
as in SQL, `AND` binds before `OR`.

Operators: `=`, `<>`, `<`, `<=`, `>`, `>=`, `LIKE`, `NOT LIKE`, `CONTAINING`, `STARTING WITH`,
`IN`, `NOT IN` (comma-separated list), `BETWEEN` (two values), `IS NULL`, `IS NOT NULL`.

How values are written into the SQL:

| You type | SQL |
|---|---|
| `42`, `-1.5` | `42`, `-1.5` |
| `:city` | `:city` (a parameter — see below) |
| `NULL`, `TRUE`, `CURRENT_DATE`, `CURRENT_TIMESTAMP`, `'NOW'`, ... | as typed |
| `'it''s'` (already quoted) | as typed |
| `=CURRENT_DATE - 7` (starts with `=`) | `CURRENT_DATE - 7` (raw SQL) |
| anything else, e.g. `Rome`, `2024-01-31` | `'Rome'`, `'2024-01-31'` |

## DISTINCT and row limits

**DISTINCT** adds `SELECT DISTINCT`. **Rows** and **Skip** limit the result; **Limit** picks the
syntax:

| Limit | SQL | Servers |
|---|---|---|
| OFFSET / FETCH (default) | `OFFSET 20 ROWS FETCH NEXT 10 ROWS ONLY` | Firebird 3+ |
| ROWS m TO n | `ROWS 21 TO 30` | all |
| FIRST / SKIP | `SELECT FIRST 10 SKIP 20 ...` | all |

## SQL, Run and the Query Tool

The SQL panel updates on every change. Identifiers are quoted only when needed, with the server's
own reserved-word list (a column named `POSITION` is written `"POSITION"`).

- **Run** executes the query in its own session and shows the first 500 rows in **Results**.
  Parameters (`:name`) get an input box under the SQL.
- **Open in Query Tool** opens a new Query Tool tab with the SQL (in the builder's schema on
  Firebird 6).
- The copy button copies the SQL.

### Saving the builder state with the query

Tick **Save builder state in the SQL** before opening or copying the query. A comment line is
then added at the end:

```sql
SELECT ...
-- fbadmin:query-builder eyJ2ZXJzaW9uIjox...
```

Save that SQL anywhere (a file, a snippet). Later, **Load from SQL…** in the builder takes the
whole text and restores the tables, positions, joins, columns and conditions. Columns and foreign
keys are read again from the database; tables that no longer exist are left out, with a message.

The builder does **not** parse other SQL: a `SELECT` without that comment is refused with a
message rather than half-understood.

## Touch screens

Drag a table by its header with a finger, and pinch (or use the zoom buttons) to zoom. To join two
columns without a mouse, tap **⇄** next to one column, then next to a column of another table; a
hint shows the first column until you tap the second (or **Cancel**). See [Phones and
tablets](MOBILE.md#on-a-tablet).

## Keyboard

The canvas is a mouse convenience. Everything can be done with the keyboard in the grids under it
(**Columns**, **Conditions**, **Joins**, **Tables**) and the toolbar: add tables with the
**Add table** list, edit joins and aliases, remove tables, and so on. All controls have labels for
screen readers.

## API

The builder uses two read-only endpoints; the SQL is generated in the browser.

- `GET /api/servers/{sid}/databases/{dbid}/query-builder/relations?schema=` — tables and views
  (`name`, `kind`), `engine_major`, `schemas_supported`, `schema`, `schemas`, and `reserved` (the
  reserved words that force quoting). On Firebird 6 without `schema` it lists `PUBLIC`.
- `GET /api/servers/{sid}/databases/{dbid}/query-builder/relations/{name}?schema=` — `columns`
  (`name`, `type`, `pk`, `fk`, `not_null`) and `foreign_keys` (`columns`, `ref_table`,
  `ref_columns`).

## Limits

- Foreign keys are suggested only between tables of the same schema.
- No subqueries, `UNION`, `WITH`, window functions or derived tables: write those in the Query Tool
  (an *(expression)* column can hold a scalar subquery).
- When one table is joined to the canvas twice through different paths, the extra conditions are
  added to the later join with `AND`.
