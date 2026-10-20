# Multi-server overview

**▦ Overview** in the toolbar opens one page with the health of every registered server and
database. Each server is a card and each database a row, with a status badge:

| Status | Meaning |
|---|---|
| ✓ OK | Reachable and nothing needs attention. |
| ! Warning | Reachable, but something needs attention (see the lines under the database name). |
| ✕ Critical | Unreachable, no answer in time, or a critical condition. |
| ? Unknown | Not checked, because no password is saved or the saved passwords are locked. |

A server's status is the worst status of the server and its databases.

## What is shown

For each server:

- the server version and architecture, and the number of attachments on the whole server (Services API);
- why it could not be checked, with the error message.

For each database:

| Column | Source |
|---|---|
| Version | engine version and on-disk structure (ODS) |
| Size | `MON$PAGES × MON$PAGE_SIZE`, and the page count |
| Attachments, Transactions | from `MON$ATTACHMENTS` and `MON$TRANSACTIONS`, without fbadmin's own check |
| OAT gap | next transaction − oldest active transaction |
| Long transactions | transactions running longer than 1 hour (`FBADMIN_OVERVIEW_LONG_TX`, seconds), and the age of the oldest one |
| Open for | time since the oldest attachment connected. In SuperServer the cache writer and garbage collector attach when the database is opened, so this is how long the database has been open. |
| Last backup | the newest of: the last successful scheduled backup (fbadmin's schedules) and the last `nbackup` in `RDB$BACKUP_HISTORY` |
| Replication | Firebird 4+: replica mode, or "primary" with the number of published tables. With a replication-lag alert rule, also how many journal segments the replica is behind. |

Lines under the database name explain warnings:

- the OAT gap or the OAT − OIT gap is at least 10,000 (`FBADMIN_OVERVIEW_GAP_WARN`); an OAT gap ten
  times that is critical;
- long-running transactions;
- the database is locked by `nbackup` (changes go to the delta file);
- an alert rule for the database is firing (see [Alerts](ALERTS.md)). The overview shows the result
  of the rule's last check. It does not run the check again;
- the last run of a scheduled job of the database failed.

The overview reuses fbadmin's monitoring probes (the same values as the dashboard and the
Performance tab). Its queries work on Firebird 3, 4, 5 and 6.

## Using the page

- The status buttons at the top show how many databases are in each state. Click one or more to
  show only those; click again to clear.
- The text filter matches server names, hosts, groups, database names and paths.
- **Sort**: worst status first (default) or by name.
- **Auto-refresh**: off, 30 s, 1 min (default) or 5 min. **Check now** checks again at once.
  The sort and refresh settings are remembered in the browser.
- Click a server or database name, or **Dashboard**, to select it in the Object Explorer and show
  its dashboard. **Performance** opens the database's Performance tab.

## How the checks run

- Every server and every database is checked at the same time, in a pool of background threads
  (`FBADMIN_OVERVIEW_WORKERS`, default 16).
- The page waits at most 8 seconds (`FBADMIN_OVERVIEW_TIMEOUT`). Checks that take longer are
  shown as "No answer within 8 s" and keep running in the background. While a check of a server or
  database is still running, the next refresh waits for that same check instead of starting a new
  one, so a server that is down does not pile up threads or connections.
- Results are cached for 15 seconds per fbadmin user (`FBADMIN_OVERVIEW_TTL`); **Check now** and
  auto-refresh skip the cache.
- A database that is already connected in the Object Explorer is checked on that connection.
  Other databases get a short-lived attachment that is closed after the check, so the
  Object Explorer still shows them as disconnected.
- The overview only uses saved passwords and never asks for one. Without a saved password, the
  server and its databases show **Password not saved**. Connect from the Object Explorer and tick
  "save password", or set the password in the server's properties. When the master password is set
  but not entered, they show **Saved password locked**.

## Server mode

Each user sees the same servers as in their Object Explorer: their own servers and the shared
ones. Passwords of shared servers are per user, so a shared server can be **Password not saved**
for one user and checked for another. Results are cached per user.

## API

`GET /api/overview` returns every server with its databases, the values above, the issue codes
(`oat_gap`, `oit_gap`, `long_transactions`, `nbackup_stalled`, `alert`, `job_failed`) and the
error codes (`unreachable`, `timeout`, `password_not_saved`, `locked`). Add `?refresh=true` to skip
the cache.
