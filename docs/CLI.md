# Command-line tool

The `fbadmin` command also has subcommands for scripts, CI pipelines and cron jobs. They use the
same code as the web app: backups and restores go through the Firebird Services API, the DDL comes
from the same extractor, and `diff` builds the same migration script as **Schema diff**.

| Command | What it does |
|---|---|
| `fbadmin backup` | Back up a database (gbak) to a file on this machine or on the server. Can compress, write a checksum, test-restore and delete old backups. |
| `fbadmin restore` | Restore a backup (plain, `.gz` or `.zst`, or from stdin) or a backup file on the server. |
| `fbadmin extract` | Print the full DDL of a database, or write one file per object. |
| `fbadmin diff` | Compare two schemas (databases or `.sql` files) and print the migration script. Can also compare table data. |
| `fbadmin sql` | Run an SQL script. Stops at the first error. |
| `fbadmin check` | Connect and print the Firebird version, ODS and page size. For health checks. |
| `fbadmin servers` | List the servers and databases saved in fbadmin. |

`fbadmin` with no subcommand still starts the web server, with the same options as before.
`fbadmin serve` does the same thing. Run `fbadmin COMMAND --help` to see every option.

## Connecting

Name the database the way Firebird tools do:

```sh
fbadmin check localhost:/var/lib/firebird/data/app.fdb      # host:path
fbadmin check db.example.com/3051:app                         # host/port:alias
fbadmin check inet://db.example.com:3051/app                  # URL form
fbadmin check --host db.example.com employee                  # path or alias, with --host
```

| Option | Default |
|---|---|
| `--host`, `--port` | `$ISC_HOST` or `localhost`, port 3050 |
| `-u`, `--user` | `$ISC_USER` or `SYSDBA` |
| `--password-env NAME` | reads the password from the environment variable `NAME` |
| `-p`, `--password` | the password itself (other users can see it in the process list, so avoid it) |
| `--role`, `--charset` | no role, `UTF8` |
| `--auth-plugin`, `--no-wire-crypt`, `--wire-compress` | driver defaults |

Without `--password` or `--password-env`, fbadmin uses `$ISC_PASSWORD`, like `gbak` and `isql`.
Passwords are never printed, not even with `--json` or `--debug`.

### Saved servers

On the machine where fbadmin runs, a cron job can use the servers you saved in the web app instead
of repeating host, user and password:

```sh
fbadmin servers                                   # list what is saved (no passwords are shown)
fbadmin backup @Production/app --dir /backups     # @SERVER/DATABASE
fbadmin backup --server Production --database app --dir /backups
```

Servers and databases are matched by name (not case-sensitive) or id. A database also matches by
its path. If the server has only one database, you can leave the database out.

Where fbadmin looks:

- `--data-dir DIR`, or `$FBADMIN_DATA_DIR`, or `~/.fbadmin`. Use the same data directory as the web
  app.
- Servers from `FBADMIN_SERVERS_FILE` (see [Security](SECURITY.md)). Their passwords come from the
  `password_env` variable named in that file, so set it for the cron job too.
- **Desktop mode** (`servers.json`): saved passwords are used. If you set a master password, the
  saved passwords are encrypted: put the master password in `FBADMIN_MASTER_PASSWORD` (or name
  another variable with `--master-password-env NAME`).
- **Server mode**: only the servers that administrators share are available. Their passwords are
  not stored in the shared registry, so pass one with `--password-env` or `ISC_PASSWORD`. Servers
  that a user added for themselves are encrypted with that user's login and the command line cannot
  read them.

`--password-env` or `--password` always takes priority over a saved password.

## Remote mode

With `--url`, a command runs through a running fbadmin server instead of connecting to Firebird
itself. It uses the server's registered servers, saved passwords and permissions, so the machine
running the command needs neither Firebird access nor any Firebird password: only an
[API token](PERMISSIONS.md) of a fbadmin user.

```sh
export FBADMIN_URL=https://fbadmin.example.com
export FBADMIN_TOKEN=fbat_...          # user menu → "Sign-in & API tokens…" in the web app
fbadmin servers                        # what this token can see
fbadmin check @Production/app
fbadmin backup @Production/app -o app.fbk.gz --checksum
fbadmin restore app.fbk.gz @Staging/app --replace
fbadmin --url https://fbadmin.example.com --token-env CI_FBADMIN_TOKEN diff db/schema.sql @Production/app --exit-code
```

| Option | Meaning |
|---|---|
| `--url URL` | The fbadmin server (or `$FBADMIN_URL`). Before or after the command. |
| `--token-env NAME` | Read the API token from this environment variable. Default `$FBADMIN_TOKEN`. |
| `--token TOKEN` | The token itself (other users can see it in the process list, so avoid it). |
| `--ca-cert FILE` | A CA certificate (PEM) for a server certificate signed by your own CA. |

Name databases the way they are registered on the fbadmin server: `@SERVER/DATABASE`, or
`--server`/`--database` (by name, case-insensitive, or id; the database also by its path). Host
names, paths and the Firebird connection options (`--host`, `--password-env`, …) are refused, since
the fbadmin server decides where to connect. `restore` also accepts `@SERVER:PATH` for a new
database file on that server, and `--register-as NAME` registers it there afterwards.

| Command | In remote mode |
|---|---|
| `servers` | The servers and databases the token's user can see. |
| `check` | Version, ODS and page size of a database, or `--server-only`. |
| `sql` | Runs the script through the server (each statement committed). `--single-transaction` and `--create` are local only. |
| `extract` | The full DDL. `--split`, `--types` and `--schema` are local only. |
| `diff` | Databases on the server, or a `.sql` file (run in a temporary database next to the other side). `--data` compares rows with the server's data compare. |
| `backup` | `-o FILE`/`--dir`/`-o -`: streamed from the server to this machine, with `--compress`, `--checksum` and `--keep` as usual. `--server-file PATH`: written by the Firebird server. `--archive`: kept in the fbadmin server's backup archive. `--verify` is local only. |
| `restore` | A local file (plain, `.gz` or `.zst`, or stdin) is uploaded and restored by the server; `--server-file PATH` restores a file on the Firebird server. Replacing a registered database needs `--replace`. |

Backups, restores and data comparisons run as jobs on the server; their log is shown while they
run (`-v` for every line), and **Ctrl+C cancels the job** on the server (press it twice to stop
waiting). What a command may do follows the token: a read-only token can run `check`, `extract`,
`diff` and `SELECT`s, but not change data or restore (exit code 3, "Not allowed").

Uploads count toward the server's request size limit (16 MB by default in server mode; set
`FBADMIN_MAX_REQUEST_SIZE` on the server). For larger restores, copy the backup to the Firebird
server and use `--server-file`.

## Output and exit codes

- Progress messages go to stderr. `-q` turns them off, `-v` adds detail (for example gbak's output).
- Results go to stdout. With `--json`, stdout gets exactly one JSON document, also when the command
  fails: `{"ok": false, "error": "...", "exit_code": 3}`.
- Errors are one line on stderr: `fbadmin: error: ...`. Add `--debug` to see the Python traceback.

| Exit code | Meaning |
|---|---|
| 0 | Success. |
| 1 | `diff --exit-code` found differences. `check`: the server or database is not healthy. |
| 2 | Usage error: a bad option, a missing file, no password. |
| 3 | Cannot connect or log in. In remote mode also: the fbadmin server cannot be reached, the token is invalid, or the token's user is not allowed to do this. |
| 4 | The operation failed: a gbak error, an SQL error in a script, a failed data comparison. |
| 5 | `backup --verify`: the backup was written, but the test restore failed. |
| 130 | Interrupted (Ctrl+C). In remote mode the server's job was cancelled. |

## backup

Choose one destination:

| Destination | Example |
|---|---|
| `-o FILE` | a file on the machine running fbadmin. The backup is streamed through the Services API, so no shared disk with the Firebird server is needed. A `.gz` or `.zst` name turns on compression. |
| `-o -` | stdout, for pipes. |
| `--dir DIR` | a new file `NAME_YYYYmmdd-HHMMSS.fbk` in `DIR`. `NAME` is the database file name, or `--name`. |
| `--server-file PATH` | a file on the Firebird server, written by the server itself. |

```sh
fbadmin backup localhost:/data/app.fdb -o app.fbk
fbadmin backup localhost:/data/app.fdb -o app.fbk.gz --checksum
fbadmin backup localhost:/data/app.fdb --dir /backups --compress gzip --keep 14 --verify
fbadmin backup localhost:/data/app.fdb --server-file /data/backups/app.fbk --zip
fbadmin backup localhost:/data/app.fdb -o - | ssh backup-host 'cat > app.fbk'
```

| Option | Meaning |
|---|---|
| `--compress gzip\|zstd\|none` | Compress the local file. zstd needs Python 3.14 or later. |
| `--zip` | gbak's own compression inside the backup (Firebird 4+). Works with `--server-file`. |
| `--checksum` | Also write `FILE.sha256`, which `sha256sum -c` can check. |
| `--verify` | Restore the backup into a scratch database next to the source (`fbadmin_verify_*.fdb`), open it, count its objects, then drop it. Use `--verify-path` when the database is an alias. |
| `--keep N` | With `--dir`: keep the newest `N` backups of this `NAME` and delete older ones. Only runs after the backup (and `--verify`) succeeded, so a failed backup never removes good ones. Other files in the directory are left alone. |
| `--metadata-only`, `--no-gc`, `--non-transportable`, `--ignore-checksums`, `--ignore-limbo` | gbak options. |
| `--skip-table NAME`, `--skip-data PATTERN` | Leave out the data of some tables. `--skip-table` can be repeated; it becomes the `SIMILAR TO` pattern `NAME1\|NAME2`, so `_` and `%` act as wildcards. |
| `--include-table NAME`, `--include-data PATTERN` | Back up only the data of some tables (Firebird 4+). |
| `--parallel N` | Parallel workers (Firebird 5+). |
| `--crypt-plugin`, `--keyholder`, `--key-name` | Encrypted backups (Firebird 4+). |

A local backup is written to `FILE.part` first and renamed when it is complete, so an interrupted
backup never looks like a finished one.

## restore

```sh
fbadmin restore app.fbk localhost:/data/app_copy.fdb
fbadmin restore app.fbk.gz localhost:/data/app.fdb --replace
fbadmin restore --server-file /data/backups/app.fbk localhost:/data/app_copy.fdb
ssh backup-host 'cat app.fbk' | fbadmin restore - localhost:/data/app_copy.fdb
```

A database that already exists is never overwritten unless you pass `--replace`.
Compressed files (`gzip`, `zstd`) are recognised by their content, not by their name.

Other options: `--page-size`, `--buffers`, `--parallel N` (Firebird 5+), `--deactivate-indexes`,
`--no-shadow`, `--no-validity`, `--one-at-a-time`, `--use-all-space`, `--metadata-only`,
`--read-only`, `--skip-table`/`--skip-data`, `--include-table`/`--include-data`,
`--fix-fss-data CHARSET`, `--fix-fss-metadata CHARSET`, and the encryption options.

## migrate

Versioned migrations from a directory of `V<version>__<description>.sql` scripts, see
[Versioned migrations](MIGRATIONS.md#command-line-tool):

```bash
fbadmin migrate status  @Production/app --dir db/migrations --check   # exit 1 when scripts are pending
fbadmin migrate apply   @Production/app --dir db/migrations           # a dry run on a copy first
```

## backup-decrypt, backup-key

Backups that fbadmin encrypted (`*.fbk.enc`, see [Backup encryption](BACKUP_ENCRYPTION_DRILLS.md)) are
decrypted without the web server, for example on a recovery machine:

```bash
fbadmin backup-decrypt app_20261027-020000.fbk.enc --key fbk1:…          # writes app_20261027-020000.fbk
fbadmin backup-decrypt app.fbk.enc --key-file /run/secrets/backup-key -o /tmp/app.fbk
fbadmin backup-key                                                       # a new key for FBADMIN_BACKUP_KEY
```

Without `--key`/`--key-file` the key comes from `FBADMIN_BACKUP_KEY` or fbadmin's data directory
(`--data-dir`). A wrong key or a damaged file ends with exit code 4 and no output file.

## extract

```sh
fbadmin extract localhost:/data/app.fdb > schema.sql
fbadmin extract localhost:/data/app.fdb -o schema.sql --no-grants --no-comments
fbadmin extract localhost:/data/app.fdb --types tables,indices,views
fbadmin extract localhost:/data/app.fdb --split schema/       # schema/tables/CUSTOMERS.sql, ...
```

Without filters, the output is the same as **Extract DDL** in the web app: every object in
dependency order, with `SET TERM` around PSQL.

| Option | Meaning |
|---|---|
| `-o FILE` | Write to a file instead of stdout. |
| `--types LIST` | Only these object types: `collations, domains, udfs, sequences, exceptions, roles, tables, procedures, functions, packages, views, indices, triggers`. Grants and comments of the objects left out are left out too. |
| `--schema NAME` | Only one schema (Firebird 6). Without it, every schema is extracted. |
| `--split DIR` | One file per object, `DIR/TYPE/NAME.sql` (on Firebird 6 with several schemas `DIR/SCHEMA/TYPE/NAME.sql`). Each file holds the DDL that `diff` compares. |
| `--no-grants`, `--no-comments`, `--no-sequence-values`, `--no-header`, `--no-set-term` | Leave parts out. |

## diff

`fbadmin diff SOURCE TARGET` prints the script that makes **TARGET** look like **SOURCE**: SOURCE is
the schema you want, TARGET is the database to update. Either side can be a `.sql` DDL file
(or `file:PATH`). fbadmin runs the file in a temporary database on the other side's server, next
to that database (or in `--temp-dir`, a directory on the server), and drops it afterwards.

```sh
# what would change production to match staging?
fbadmin diff staging:/data/app.fdb prod:/data/app.fdb -o migrate.sql

# CI drift check: does the database match the schema in git?
fbadmin diff schema.sql localhost:/data/app.fdb --exit-code

# apply the migration
fbadmin sql localhost:/data/app.fdb -f migrate.sql
```

| Option | Meaning |
|---|---|
| `--exit-code` | Exit with 1 when there are differences (0 when there are none). |
| `-o FILE` | Write the script to a file. |
| `--json` | Print the list of differences (`type`, `name`, `create`/`alter`/`drop`) and the script as JSON. |
| `-v` | List every difference on stderr. |
| `--data [TABLE ...]` | Also compare the rows of these tables (all tables of both databases if you name none) and add the data sync script after the schema script. See [Data compare](DATA_COMPARE.md). |
| `--no-schema` | With `--data`: compare only rows. |
| `--target-user`, `--target-password-env` | Different credentials for TARGET. By default both sides use the same ones. |

Check the script before you run it on production. Changes that cannot be done with `ALTER` are
listed as warnings at the top of the script.

## sql

```sh
fbadmin sql localhost:/data/app.fdb -f migrations/001.sql -f migrations/002.sql
fbadmin sql localhost:/data/app.fdb -c "SELECT COUNT(*) FROM CUSTOMERS"
fbadmin sql --create localhost:/data/test.fdb -f schema.sql     # create the database first
echo "SELECT 1 FROM RDB\$DATABASE;" | fbadmin sql localhost:/data/app.fdb
```

The script runner is the Query Tool's: it understands `SET TERM`, `COMMIT`, `ROLLBACK` and ignores
other isql commands. Each statement is committed when it succeeds. With `--single-transaction`,
everything runs in one transaction that is rolled back if a statement fails. The run stops at the
first error and exits with 4. The error message names the file and line. `SELECT` results are printed
tab-separated (at most `--max-rows`, default 1000).

## check

```sh
fbadmin check localhost:/data/app.fdb
# OK localhost/3050:/data/app.fdb: Firebird 5.0.1, ODS 13.1, page size 16384, dialect 3, charset UTF8
fbadmin check --server-only --host db.example.com
```

Exit code 0 when the connection works, 1 otherwise.

## Examples

### cron: nightly backup with retention

```cron
# m h dom mon dow  command
15 2 * * *  FBADMIN_MASTER_PASSWORD="$(cat /etc/fbadmin/master)" /usr/local/bin/fbadmin backup @Production/app --dir /backups/app --compress gzip --checksum --keep 14 --verify -q || echo "app backup failed" | mail -s "backup failed" ops@example.com
```

Without saved servers:

```cron
15 2 * * *  ISC_PASSWORD="$(cat /etc/firebird/sysdba.pw)" /usr/local/bin/fbadmin backup db1:/data/app.fdb --dir /backups/app --compress gzip --keep 14 -q
```

With `-q`, cron only sends mail when there is an error message.

### Docker

The fbadmin image contains the command-line tool:

```sh
docker run --rm -e ISC_PASSWORD -v "$PWD/backups:/backups" ghcr.io/mariuz/fbadmin \
  fbadmin backup db.example.com:/data/app.fdb --dir /backups --compress gzip --keep 7
```

### GitHub Actions: schema drift check

```yaml
name: schema
on: [pull_request]
jobs:
  drift:
    runs-on: ubuntu-latest
    services:
      firebird:
        image: firebirdsql/firebird:5
        env:
          FIREBIRD_ROOT_PASSWORD: masterkey
        ports: ["3050:3050"]
    env:
      ISC_PASSWORD: masterkey
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: "3.12"
      - run: pip install firebird-admin
      - name: Wait for Firebird
        run: |
          for i in $(seq 30); do fbadmin check --server-only && break; sleep 2; done
      - name: Build the database from the migrations
        run: |
          fbadmin sql --create localhost:/var/lib/firebird/data/ci.fdb -c "COMMIT"
          for f in db/migrations/*.sql; do
            fbadmin sql localhost:/var/lib/firebird/data/ci.fdb -f "$f" || exit 1
          done
      - name: Compare with the schema in git
        run: |
          fbadmin diff db/schema.sql localhost:/var/lib/firebird/data/ci.fdb --exit-code -o drift.sql
      - if: failure()
        run: cat drift.sql
```

The database path (`/var/lib/firebird/data/ci.fdb`) is inside the Firebird container; fbadmin
reaches it through port 3050.

### GitLab CI: backup before a deployment

```yaml
backup:
  stage: deploy
  image: python:3.12-slim
  variables:
    ISC_PASSWORD: $PROD_FIREBIRD_PASSWORD   # a masked CI/CD variable
  script:
    - pip install firebird-admin
    - fbadmin backup "$PROD_DB" -o "backup-$CI_PIPELINE_ID.fbk.gz" --checksum --verify
    - fbadmin diff db/schema.sql "$PROD_DB" -o migrate.sql
  artifacts:
    paths: ["backup-*.fbk.gz*", "migrate.sql"]
    expire_in: 1 week
```

`PROD_DB` is a database name such as `db.internal/3050:/data/app.fdb`.
