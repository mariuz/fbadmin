# Change requests

A change request lets someone who may not change a database propose a change to it. An administrator
of the database reviews what the change does to the schema, approves or rejects it, and fbadmin
applies it. Every step is recorded.

Open **Change requests…** in a database's context menu.

## Proposing a change

**New change request** asks for a title, an optional description and the change itself:

* **a SQL script:** DDL such as `ALTER TABLE`, `CREATE INDEX` or `CREATE OR ALTER PROCEDURE`, or DML
  that fixes data;
* **versioned migration scripts:** files named `V<version>__<description>.sql`, as for
  [versioned migrations](MIGRATIONS.md). Scripts already applied to the database are skipped, and
  applying them records them in the migration history.

Anyone with at least the *read* level on the database can propose a change. A developer with read
access, who is refused by the Query Tool when running DDL, can still propose it.

The author can **edit** the SQL until the change is applied. Each edit is a new revision: it clears
the approvals and needs a new dry run. The author can also **withdraw** the request.

## Review

Review needs the *admin* level on the database.

1. **Run the dry run.** fbadmin restores a copy of the database next to it (metadata only by
   default, or a full copy that also catches data errors), runs the change there, then drops the
   copy. The request shows the result: each table, index, procedure or other object that is
   *created*, *altered* or *dropped*, side by side before and after. The database itself is not
   touched.
2. **Approve** (with an optional comment) or **Reject** (a comment is required; the author reads it).
   Approving needs a passed dry run of the current revision. In server mode, the author cannot
   approve their own change.
3. **Apply the change.** Statements run one after the other and each is committed (Firebird DDL needs
   it). The first error stops the run, and the request becomes *failed* with the error; the author can
   then revise it. When fbadmin stops while applying, the request is marked *failed* on restart: check
   the database before trying again.

**The review belongs to the schema it was made on.** fbadmin refuses to apply a change when the
database's schema changed since the dry run, for example when someone else altered a table in the
meantime. Run the dry run again. If it finds the same schema changes, the approvals stay. Otherwise
they are cleared and the change needs a new approval.

| Status | Meaning |
|---|---|
| *open* | waiting for a dry run and approval |
| *approved* | approved; can be applied |
| *rejected* | rejected by a reviewer; the author can revise it, which opens it again |
| *withdrawn* | withdrawn by its author |
| *applying* | being applied right now |
| *applied* | applied to the database |
| *failed* | applying failed; the author can revise it |

## The audit trail

Each step is kept in the request's **History**, with who did it and when. Comments are part of the
history too. The same steps are recorded in:

* the [audit log](SECURITY.md) (server mode), together with each statement the change ran;
* the database's [timeline](TIMELINE.md), under *schema*: proposed, approved, rejected, withdrawn,
  applied and failed, with the SQL;
* the [schema history](SCHEMA_HISTORY.md), when the database has one: applying makes a snapshot like
  any DDL run from fbadmin.

The `change.request` [alert event](ALERTS.md#events) sends a message when a change is proposed,
approved, rejected, withdrawn, applied or fails. A rule on that event for a database tells its
reviewers that something waits for them, and tells authors what became of their requests.

## Who may do what

| | Needs |
|---|---|
| see requests, propose, comment | the *read* level on the database (users without it don't see its requests) |
| edit, withdraw | being the author, until the change is applied |
| dry run, approve, reject, apply | the *admin* level on the database; applying also passes the applier's object rules |

In desktop mode there is one user, who can do everything, including approving their own change.

| Setting | Default | |
|---|---|---|
| `FBADMIN_CHANGE_APPROVALS` | `1` | approvals needed before a change can be applied |
| `FBADMIN_CHANGE_SELF_APPROVE` | off | let authors approve their own changes in server mode |

Requests are kept in `<data dir>/change_requests.json`.

## API

| Method | Path | |
|---|---|---|
| GET | `/api/changes?server_id=&database_id=&status=` | requests the user may see (`status=active`: open and approved) |
| POST | `/api/changes` `{server_id, database_id, title, description, kind, sql \| scripts}` | propose |
| GET / PUT | `/api/changes/{id}` | a request with its dry run, approvals and history / edit it (author) |
| POST | `/api/changes/{id}/comments` `{text}` | comment |
| POST | `/api/changes/{id}/dry-run` `{copy_mode}` | dry run (a job) |
| POST | `/api/changes/{id}/approve` `{text}` | approve |
| POST | `/api/changes/{id}/reject` `{text}` | reject |
| POST | `/api/changes/{id}/withdraw` | withdraw (author) |
| POST | `/api/changes/{id}/apply` | apply (a job); 409 when the schema changed since the dry run |
