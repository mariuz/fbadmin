# Two-factor sign-in, API tokens and permissions

These features exist in **server mode** only (see [Security and server mode](SECURITY.md)). In
desktop mode there is no login, so there is nothing to protect with a second factor, no token to
send, and no permission to check: the local user can do everything, as before.

| Feature | Who sets it up | Where |
|---|---|---|
| Two-factor sign-in (TOTP) | each user; admins can require it | user menu → *Sign-in & API tokens* |
| API tokens | each user; admins see and revoke all | user menu → *Sign-in & API tokens* |
| Permissions (grants, groups) | admins | user menu → *Permissions & two-factor* |

Nothing changes after an upgrade until you use them: without grants every user keeps the access
they had, and nobody is asked for a code until they turn two-factor sign-in on (or an admin
requires it).

## Two-factor sign-in (TOTP)

fbadmin supports the time-based one-time passwords of RFC 6238, the 6-digit codes of
authenticator apps such as Aegis, FreeOTP, Google Authenticator, Microsoft Authenticator or
1Password (HMAC-SHA1, 30-second steps).

**Turn it on.** Open *Sign-in & API tokens*, click *Set up two-factor authentication*, scan the QR
code with the app (or type the key shown next to it), and enter the code the app shows. The
second factor is only active after this confirmation. fbadmin then shows **ten recovery codes**:
save them (copy or download). Each one signs you in once if you lose your phone.

**Sign in.** After your password, fbadmin asks for the code from the app, or one of your recovery
codes (*Use a recovery code*).

* A code is accepted for one 30-second step before and after the current one (clock drift).
* Each code works **once**: fbadmin remembers the last step used, so a code seen by someone else
  cannot be replayed.
* Wrong codes count against the same limit as wrong passwords (`FBADMIN_LOGIN_RATE`, default
  5 per 300 s per user and per client address), and one sign-in attempt allows at most 5 codes.
  The password step expires after 5 minutes.

**Recovery codes** are stored as Argon2id hashes, like passwords. *New recovery codes…* replaces
them (you need a current code). *Turn off…* needs a current code too, and is not offered when an
admin requires two-factor sign-in.

**Administrators** (*Permissions & two-factor* → *Two-factor authentication*):

* *Require two-factor authentication for every internal and LDAP user*, or tick *Required* for
  single users. A user who must use it and has not set it up yet enrols on the sign-in page
  (QR code, code, recovery codes) before getting a session.
* *Reset* removes a user's authenticator secret and recovery codes (lost phone). The user signs
  in with the password only, and enrols again if it is required.

**Which accounts.** Internal and LDAP accounts. Users who sign in with OAuth2/OIDC or through an
authenticating reverse proxy never type a password into fbadmin; require the second factor at
that identity provider (Keycloak, Entra ID, Google, oauth2-proxy, ...).

**Where the secret is stored.** The TOTP secret must be readable by fbadmin at every sign-in, so
it cannot be hashed. It is encrypted with AES-256-GCM (the same code as saved Firebird passwords)
under a server key that is *not* in `fbadmin.db`:

* `FBADMIN_SECRET_KEY` if set (any long random string, for example from your secrets manager), or
* `<data_dir>/secret.key`, created on first use with file mode `0600`.

A copy of `fbadmin.db` alone (a backup, say) does not reveal the secrets. Keep `secret.key` with
the data directory: if it is lost or `FBADMIN_SECRET_KEY` changes, users with two-factor sign-in
can only sign in with a recovery code until an admin resets them. The per-user vault (the key
derived from the login password that protects saved Firebird passwords) is deliberately *not*
used: an LDAP password change or an admin's password reset replaces that vault, which would lock
the user out of their second factor.

## API tokens

Personal tokens let scripts and automation call the fbadmin REST API as you.

**Create.** *Sign-in & API tokens* → *New token*: a name, a scope, an expiry (7, 30, 90, 365 days
or never) and optionally the servers it may use. The token (`fbat_...`) is shown **once**; fbadmin
keeps only its SHA-256 hash.

**Use.** Send it in the `Authorization` header. No session cookie and no CSRF token are needed:

```bash
TOKEN=fbat_...
curl -s -H "Authorization: Bearer $TOKEN" http://fbadmin.example.org:5050/api/servers
curl -s -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
     -d '{"sql": "SELECT COUNT(*) FROM CUSTOMER"}' \
     http://fbadmin.example.org:5050/api/servers/<server-id>/databases/<db-id>/execute
```

**Scope.**

* *read-only*: queries, metadata, monitoring. Statements are classified like on a read-only
  server: only `SELECT`/`WITH` and session commands pass; `INSERT`/`UPDATE`/`DELETE`,
  `EXECUTE PROCEDURE`, `EXECUTE BLOCK` and DDL are refused with
  `403 {"code": "token_read_only"}`, and so is every other change (registrations, maintenance).
  A token also has its own connections, separate from your browser sessions, and those use
  read-only Firebird transactions.
* *read-write*: everything you may do yourself.
* *Servers*: when you tick some, every other server is invisible to the token (404).

A token never does more than its owner: your permissions (below) apply on top, and a disabled
account's tokens stop working at once. Tokens cannot create tokens, change passwords, the master
password or two-factor settings (`403 token_forbidden`). The audit log records which token ran a
statement (column *token*, shown under the user name).

**Saved passwords.** Saved Firebird passwords are encrypted with your vault key, which is in
memory only while you are signed in. When you create a token while signed in, fbadmin stores a
copy of that key wrapped with a key derived from the token itself, so the token can use your
saved passwords while you are signed out, and only someone holding the token can unwrap it. If
your saved passwords were locked when you created the token (or an admin later set a new password
for you), connect requests must send the Firebird password (`POST /api/servers/{id}/connect`
with `{"password": "..."}`).

**Expiry, last use, revoke.** The token list shows when each token was last used and from which
address (updated at most once a minute). *Revoke* stops a token at once. Administrators see every
user's tokens under *Permissions & two-factor* → *API tokens of all users* and can revoke them.

The API: `GET/POST /api/access/me/tokens`, `DELETE /api/access/me/tokens/{id}`,
`GET /api/admin/tokens`, `DELETE /api/admin/tokens/{id}`.

## Permissions

Until now, access was decided per server registration: users see their own servers and the shared
ones, and a server registered as read-only only allows queries. Permissions narrow this down per
database and per object for non-admin users.

### The model

A **grant** gives a *subject* a *level* on a *scope*:

* **Subject**: one fbadmin user, or a **group** of users (*Groups* section).
* **Scope**: a server registration (or *every server*, `*`), optionally one of its databases,
  optionally objects whose names match a pattern (`CUSTOMER`, `SALARY*`, `TMP_?`;
  case-insensitive; on Firebird 6 the pattern matches the name with or without its schema).
* **Level**, from low to high:

| Level | Allows |
|---|---|
| `none` | nothing: the server, database or object is hidden |
| `read` | queries (`SELECT`), metadata, data viewer, monitoring, exports |
| `write` | read + `INSERT`/`UPDATE`/`DELETE`/`MERGE`, grid edits, `EXECUTE PROCEDURE`, imports |
| `ddl` | write + `CREATE`/`ALTER`/`DROP`, `GRANT`/`REVOKE`, comments, `EXECUTE BLOCK` |
| `admin` | ddl + admin tools: backup, restore, sweep, validation, shutdown, database properties, Firebird users/roles/mappings, trace, server log, scheduled maintenance |

**Which grant applies.** For a request, fbadmin looks at the grants of the user and of their
groups that match the server, database and object, and takes the **most specific** one: an object
pattern beats a database grant, a database grant beats a server grant, and an exact server beats
`*`. If two grants are equally specific (one for the user, one for a group), the higher level
wins. With no matching grant, the level is `none`.

**Who is restricted.**

* **Administrators** are never restricted.
* A user **without any grant** (direct or through a group) keeps the behaviour of earlier
  versions: full access to the servers they can see. This is what existing installations get after
  an upgrade. Admins can change it: *Users without grants* → *no access until they get a grant*.
* A user with at least one grant only gets what their grants allow.

Grants usually name **shared servers** (admins cannot see users' own registrations). Restricted
users cannot add, edit or remove server registrations; they can still store their own Firebird
password for a shared server. A `*` grant also covers servers they registered before.

### Examples

1. *Alice may only query the `sales` database:* user `alice`, server `prod`, database `sales`,
   level `read`. Alice sees the `prod` server with the `sales` database only; her connections to it
   use read-only transactions.
2. *Developers may change the test server, but only read production:* group `devs`, server
   `test`, level `ddl`; group `devs`, server `prod`, level `read`.
3. *Support may edit customer data but must not see salaries:* user `bob`, server `prod`,
   database `hr`, level `write`; user `bob`, server `prod`, database `hr`, objects `SALARY*`,
   level `none`. `SALARY*` tables disappear from Bob's object tree and autocompletion, and any
   statement that mentions them is refused.
4. *A backup operator:* user `ops`, server `*`, level `admin`, and *Users without grants* stays
   *full access* for everybody else.

*Effective rights* (admin tab) shows the result for any user; users see their own under
*My access*.

### How it is enforced

The checks run in the backend for every request, not in the browser:

* **Visibility.** Servers and databases at level `none` are removed from the server list and answer
  `404` everywhere, including endpoints that take ids in the request body (Query Tool sessions,
  data and schema compare, AI assistant, schedules).
* **Endpoints.** Each endpoint needs a level (viewing: `read`; grid edits, imports, debugger runs:
  `write`; drops: `ddl`; maintenance, backups, Firebird users, trace: `admin`). Refusals are
  `403 {"code": "permission_denied"}`.
* **SQL.** Scripts in the Query Tool and the dialogs are split into statements and classified:
  queries need `read`, DML (including `EXECUTE PROCEDURE`) needs `write`, DDL needs `ddl`.
  `EXECUTE BLOCK` needs `ddl` too, because its PSQL can run any statement through
  `EXECUTE STATEMENT`. The target object of a statement (`INSERT INTO x`, `ALTER TABLE x`, `CREATE INDEX i
  ON x`, `GRANT ... ON x`) is checked against object grants, and a statement that names a hidden
  object anywhere is refused.
* **Firebird.** Where a user may only read a database, fbadmin opens their connections to it with
  read-only transactions, so Firebird itself rejects writes, even ones hidden in a procedure called
  by a `SELECT`.
* **Whole-database tools.** DDL extraction, schema report, ERD, search, privileges, data/schema
  compare and the upgrade check read every object. They are refused when some objects of the
  database are hidden from the user.
* **Schedules.** Creating, changing, running or deleting a scheduled backup, sweep or validation
  needs `admin` on that database, and uploading backups to cloud storage targets is for fbadmin
  administrators only (the targets hold their cloud credentials).
* Changing grants closes the affected users' open connections, so new rights apply right away.

**Limits.** Object rights are a convenience layer on top of fbadmin's own API; they are not a
replacement for Firebird's security. Object checks rely on names in the SQL text, so a view or
procedure the user may read can still read a hidden table through Firebird, and PSQL with
`EXECUTE STATEMENT` (for users with `ddl`) can build names the check never sees. For a hard boundary,
also give each person their own Firebird user with Firebird `GRANT`s (*Users, roles & mappings*),
and let them use their own Firebird password for the shared server.

The API: `GET /api/admin/access`, `PUT /api/admin/access/settings`,
`POST/PUT/DELETE /api/admin/access/grants[/{id}]`, `PUT/DELETE /api/admin/access/groups/{name}`,
`GET /api/admin/access/effective/{user_id}`, and `GET /api/access/me` for the signed-in user (or
token).

## Background tasks in server mode

Scheduled jobs, monitoring history and alert condition checks run in background threads, with
no signed-in user. In server mode each of them runs **as the user who set it up**: the user who
last saved the schedule, enabled history tracking for the database, or saved the alert rule.
It sees that user's server registrations, uses their saved passwords, and is limited by their
permissions.

How it works:

- Saving one of these issues an **internal API token** for that user. The token is limited to
  the servers involved, never expires, and carries a wrapped copy of the user's key for saved
  passwords, like a personal token created while signed in.
- The token is stored encrypted with the server key (`FBADMIN_SECRET_KEY` or
  `<data dir>/secret.key`). It is not shown in the user's own token list. Administrators see it
  under **Administration → API tokens**, named `schedule: …`, `history: …` or `alert rule: …`,
  and can revoke it there.
- Saving the item again issues a new token and revokes the old one. Deleting the schedule or
  rule, or turning tracking off, revokes it.
- If the user is disabled or deleted, or the token is revoked, the task stops running and shows
  why: the schedule's run history, the tracked database's last error, or the rule's state.
  Save it again to run it as yourself.

Desktop mode has a single local user, so none of this applies there.
