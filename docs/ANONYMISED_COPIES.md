# Anonymised copies of production

**Anonymised copy…** (database context menu) copies a database to a development or test server with
personal data masked. Rules are set per column, and the copy stays consistent across foreign keys.

## How a copy is made

1. The source is backed up through the Services API into a temporary file on the fbadmin host (mode
   0600). That file is restored as a **scratch copy on the source server**, next to the source or at the
   path set in the profile, and deleted right after the restore.
2. The scratch copy is **masked in place**:
   * triggers are switched off;
   * foreign keys on masked columns are dropped;
   * every row is updated;
   * the foreign keys are created again, which proves the masked keys still match;
   * the triggers are switched back on.
3. **Leak check**: up to 50 original values of each masked column are looked for in the masked copy.
4. The masked copy is backed up and restored on the **target server**: a new file, or with **Replace
   the file if it exists**, an earlier copy. It is registered under the name given, and the scratch copy
   is dropped.

Unmasked data never reaches the target server. If the job fails or is cancelled, the scratch copy and
the temporary files are removed. Each copy, and each failure, appears on the source database's
[timeline](TIMELINE.md).

## Masking

| Strategy | Result | For |
|---|---|---|
| fake e-mail | `user3f9a…@example.invalid` | text |
| fake full name, first name, last name, city | e.g. `Lena White`, `Paris` | text |
| pseudonym (hash) | a hex string (or a number for integer columns) | text, integers, binary |
| scrambled digits | the same format with other digits: `+40 721 555 123` → `+82 164 907 551` | text |
| placeholder words | as many words as the original | text, text BLOBs |
| redact (keep the end) | `RO49 AAAA 1B31 0075` → `XX99 XXXX 9X99 0075` | text |
| number ± percent | the value moved by up to the percentage, same scale | numbers |
| date ± days | the date moved by up to the number of days | dates, timestamps |
| shuffle between rows | real values, moved to other rows | not for key columns |
| fixed value, empty (NULL) | the same for every row (NULL only for nullable columns) | any |

**Deterministic.** A value is replaced using a keyed hash (HMAC-SHA256) of the value with the profile's
own secret. The same e-mail address therefore becomes the same fake address in every table and in every
refresh of the copy, so testers can rely on stable data. The secret is kept with the profile on the
fbadmin host and never shown. Without it, the fake values can't be traced back to the originals.

**Foreign keys.** A rule on a column that is part of a foreign key applies to every column linked to it
by foreign keys, in both directions:

* a rule on `CUSTOMERS.EMAIL` also masks `ORDERS.CUSTOMER_EMAIL`, shown as *masked through the foreign
  key*;
* since the same value gives the same result, parent and child keys still match;
* two different rules on linked columns are refused;
* a key column can't be shuffled or emptied.

Columns in a single-column unique key get a short suffix on list-based values (names, cities, words),
so they stay unique.

**Suggestions.** fbadmin suggests rules from column names: e-mail, first and last names, full names,
phone numbers, cities, addresses, birth dates, IBAN, card, tax and passport numbers, passwords and
tokens, salaries, notes and IP addresses. **Apply suggestions** fills them in. Check them: column
names don't always say what a column holds.

**Preview masked rows** shows the first rows of a table as they will look; hover a masked value to see
the original. Nothing is written.

## What is not masked

* Values outside the masked columns: free text in other columns, BLOBs, names inside JSON or XML.
* Data in procedures, triggers, views and generators (their source is copied as is).
* Values that may legitimately equal another row's original are not leak-checked: date shifts, number
  noise, redaction, fixed values and shuffles.

## Permissions and storage

* Making a copy needs the admin level on the source database and on the target server (or the target
  database, when it replaces a registered one).
* Profiles (rules, target, secret) are kept in `<data dir>/anonymise.json` (mode 0600).
* Temporary backups go to `<data dir>/anonymise/work/` and are deleted after each step.

## API

All under `/api/servers/{sid}/databases/{dbid}/anonymise`.

| Method | Path | |
|---|---|---|
| GET | `` | the profiles of the database (without their secret) |
| GET | `/columns` | tables and columns: kind, keys, strategies that fit, suggestions, foreign-key links |
| POST / PUT / DELETE | `/profiles`, `/profiles/{id}` | `{name, rules: {TABLE: {COLUMN: {strategy, value, keep, percent, days}}}, target: {server_id, path, register_as, replace}, scratch_path}` |
| POST | `/profiles/{id}/check` | the columns that will be masked, rules along foreign keys included |
| POST | `/preview` `{table, rules, profile_id, limit}` | original and masked rows |
| POST | `/profiles/{id}/run` | make the copy (a job); the result is in the profile's `last_run` |
