# Data compare

Data compare finds the rows that differ between the same tables in two databases and writes a
SQL script of `INSERT`, `UPDATE` and `DELETE` statements that makes the **target** equal to the
**source**. The two databases can be on different servers and different Firebird versions
(3, 4, 5 and 6).

Typical uses: bring a test database in line with production reference data, check that a replica
or a restored backup has the same rows, or move configuration tables between environments.

## Opening it

- Right-click a database in the tree → **Compare data…**. The database becomes the source.
- Right-click a table → **Compare data with another database…** to start with only that table
  selected.

Pick the source and the target (server and database). The list of tables loads by itself.

## Which tables can be compared

A table is compared when it exists in both databases and has a key on columns present on both
sides:

1. the primary key (the source's, when both databases have the same one),
2. otherwise a unique constraint. Rows whose key contains `NULL` cannot be matched and are
   skipped (the result shows how many).

Tables that exist in only one database, global temporary tables and tables without a primary or
unique key are listed under **Not comparable**, with the reason.

Only the columns present in both tables are compared. The **Notes** column of the table list
shows columns that exist on one side only, columns whose type differs (the values are still
compared), and warnings such as a target column that is `NOT NULL` without a default (inserts
into it would fail). Computed columns are never compared or written.

## Options

| Option | Effect |
| --- | --- |
| Table checkboxes | Which tables to compare. By default every comparable table. |
| WHERE filter | A condition (without `WHERE`) applied to *both* sides, e.g. `CREATED > DATE '2024-01-01'`. Rows outside the filter are ignored. |
| INSERT / UPDATE / DELETE | Which kinds of statements go into the script. The counts are always shown. Unticking DELETE gives a "merge" that never removes target rows. |
| Ignore columns | Comma-separated `COLUMN` or `TABLE.COLUMN` names (case-insensitive) left out of the comparison, e.g. `LAST_MODIFIED`. Key columns cannot be ignored. Ignored columns are not written by inserts, so they get their default in the target. |
| Sample rows per table | How many differing rows of each kind (insert, update, delete) are kept per table for display (0–10,000). It does not limit the script. |

## The result

The comparison runs as a background job (also listed in **Processes**). For every table you see:

- **Only in source** – rows that will be inserted,
- **Different** – rows that will be updated (only the changed columns are set),
- **Only in target** – rows that will be deleted,
- **Identical** rows,
- the **changed columns** with the number of rows where each differs.

Click a table name to see its sample rows. For updates, the source (new) and target (old) values
are shown on two lines with the changed cells highlighted.

## The sync script

**Download script** saves a `.sql` file. It is meant to run connected to the target database:

```
isql -ch UTF8 -user SYSDBA target.fdb -i datasync_target.sql
```

or open it with **Open in Query Tool (target)** (offered for scripts up to 2 MB) to review and
run it there.

- Statement order: all `INSERT`s with parent tables first, then the `UPDATE`s, then all `DELETE`s
  with child tables first. The order of tables comes from the foreign keys
  (`RDB$REF_CONSTRAINTS`) of both databases, so new child rows find their new parents, and old
  parents are deleted only after their children were deleted or moved.
- Every value is a literal: `DATE '…'`, `TIME '…'`, `TIMESTAMP '…'`, `TRUE`/`FALSE`, exact
  decimals, `CAST('…' AS DECFLOAT(34))`, time zone values with their zone
  (`TIMESTAMP '2024-03-01 10:20:30.1234 Europe/Bucharest'`), binary values and binary BLOBs as
  `x'…'` hex literals. Text containing line breaks or other control characters is written as
  `_UTF8 x'…'` so no tool can alter it. Long values are split into chunks joined with `||`.
- The script ends with `COMMIT`.
- Run it with the UTF8 connection character set (`isql -ch UTF8`).
- Identity columns: inserts into a `GENERATED ALWAYS` identity column use
  `OVERRIDING SYSTEM VALUE` (Firebird 4+); such columns are not updated.

## Apply to target

**Apply to target…** runs the script on the target from fbadmin, in **one transaction**: if any
statement fails, everything is rolled back and the target is unchanged. You must type the target
database's name to confirm.

- Long BLOB values are sent as parameters instead of literals here, which is much faster.
- An `UPDATE` or `DELETE` that finds no row means the target changed after the comparison; the
  apply stops and rolls back. Compare again and retry.
- A script can be applied once; **Compare again** checks the result (it should show no
  differences).
- Triggers on the target fire as usual. Disable them first if they must not run.
- Applying is refused when the target server is registered as **read-only** (HTTP 403). The
  comparison itself only reads, in read-only snapshot transactions, so it works on read-only
  servers.

## How the comparison works

Both sides are read with `SELECT … ORDER BY <key>` and merge-joined row by row, so memory use
does not depend on table size, and the statements are written to temporary files as they are
produced (deleted when fbadmin exits or when the result is dropped; the last 20 comparisons are
kept).

The merge needs both streams in the same order as fbadmin compares keys. The server's `ORDER BY`
follows the column's collation, which may differ from a plain binary ordering — for example
`UNICODE_CI`, language collations, single-byte character sets, or two servers with different
collations. fbadmin therefore checks while reading that every key is greater than the previous
one. If a key arrives out of order, that table is compared again with both sides sorted by
fbadmin itself (an external merge sort in temporary files), and the result shows
**sorted in fbadmin**. Either way both sides are matched with one consistent ordering, so a
collation difference never produces false differences.

Values are compared as Firebird returns them: `NULL` equals only `NULL`, numbers by value
(`1.50` equals `1.5`), `CHAR` without trailing padding, BLOBs by content, time zone values
including their zone (the time zone types are read as Firebird's text, so `10:00 Europe/Bucharest`
and `08:00 UTC` differ even though they are the same instant).

## Limitations

- Keys are compared exactly (case and accents matter). With a case-insensitive key collation,
  rows whose keys differ only in case appear as one delete and one insert, and the insert fails
  with a unique-key violation because deletes run last.
- Rows of a self-referencing table are inserted in key order; a child row with a lower key than
  its new parent fails. Swapping values of a non-key unique column between rows also fails.
- `FLOAT`/`DOUBLE PRECISION` NaN and infinity cannot be written as literals; such a table fails
  with a clear error.
- Arrays are not supported.
- BLOB literals are slow for Firebird to concatenate: in the downloaded script, BLOBs of more than
  a few hundred KB make statements slow to run (the in-app apply does not have this problem).
- On Firebird 6, the tables of the `PUBLIC` schema are compared.

## API

| Method | Path | |
| --- | --- | --- |
| POST | `/api/datacompare/tables` | `{source, target, ignore_columns}` → tables with key, columns, notes or reason |
| POST | `/api/datacompare/compare` | `{source, target, tables: [{name, where, ignore_columns}], ignore_columns, inserts, updates, deletes, max_differences}` → job (202) |
| GET | `/api/datacompare/jobs/{job_id}` | live result: per-table counts, statements, `applied` |
| GET | `/api/datacompare/jobs/{job_id}/samples?table=T` | sample rows (`insert`, `update`, `delete`) |
| GET | `/api/datacompare/jobs/{job_id}/script` | the SQL script (download) |
| POST | `/api/servers/{sid}/databases/{db}/datacompare/{job_id}/apply` | `{confirm: true}` → apply job (202); 403 on read-only servers |

`source` and `target` are `{server_id, db_id}` of registered databases.
