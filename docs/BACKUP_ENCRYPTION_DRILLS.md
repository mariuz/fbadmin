# Backup encryption and restore drills

Two additions to [scheduled backups](MAINTENANCE.md#scheduled-jobs) (roadmap 2.2): backups that fbadmin
keeps and uploads can be **encrypted**, and **restore drills** regularly prove that the newest backup can
be restored, the way it would be after a loss.

## Backup encryption

With **Encrypt the backups** on a backup schedule (or **encrypted** next to *Backup to fbadmin archive*),
the backup is encrypted while it streams in from gbak, so no plaintext copy is ever written. The file is
called `*.fbk.enc`, and the copies uploaded to cloud storage are the same encrypted files. Neither the
fbadmin host's disk nor the bucket holds a readable backup.

Everything that reads an archived backup decrypts it on the way to gbak: restores from the archive, test
restores, restore drills, and an uploaded `.fbk.enc` restored through the browser. **Download** gives the
encrypted file; **Download decrypted** gives the `.fbk`.

### Keys

A backup key is a random 256-bit key, written as `fbk1:` followed by 43 characters. Keys come from two
places:

* **`FBADMIN_BACKUP_KEY`**: one key, or several separated by commas. The first one encrypts new
  backups; the others only decrypt older ones (after a key change). Suited to Docker, Kubernetes
  (a Secret) and server installations, because the key isn't stored next to the backups.
* **Created in fbadmin** (*Schedules → Backup encryption → Create a backup key*): kept in
  `<data dir>/backup-keys.json` (mode 0600). The key is shown once as the **recovery key**: store it in
  a password manager or secret store. Without it the backups can't be restored if the data directory is
  lost. **Show** displays it again later.

*Backup encryption* lists the keys by their id (the first 16 hex digits of a hash of the key, never the
key itself), shows which one encrypts new backups, and counts how many archived backups each key is
needed for. A key that archived backups still need is only removed on purpose. Keys from another
fbadmin, or older keys, are added with **Add**. When archived backups need a key fbadmin doesn't have,
the section names it.

In server mode only administrators see and manage keys. Every change is written to the audit log.

### Format

```
header (28 bytes): "FBADMENC" | version 1 | key id (8 bytes) | nonce prefix (7) | chunk size (4)
chunks:            AES-256-GCM of 1 MiB of the backup + 16-byte tag
```

Each chunk's nonce is the nonce prefix, the chunk number and a "last chunk" flag, and the header is the
associated data of every chunk. A changed, reordered, truncated or extended file fails to decrypt with
*"The encrypted backup is damaged, truncated or was changed"* instead of restoring wrong data.

### Without fbadmin

```bash
fbadmin backup-decrypt app_20261027-020000.fbk.enc --key fbk1:…     # -> app_20261027-020000.fbk
fbadmin backup-decrypt app.fbk.enc --key-file /run/secrets/backup-key -o app.fbk
gbak -c app.fbk /data/app.fdb                                        # or: fbadmin restore
fbadmin backup-key        # a new random key, for FBADMIN_BACKUP_KEY (not stored anywhere)
```

Without `--key`, `backup-decrypt` uses `FBADMIN_BACKUP_KEY` and the keys in fbadmin's data directory
(`--data-dir`).

## Restore drills

A test restore right after the backup proves that the file is fine. A **restore drill** proves more:
that the backup you'd actually use can be restored, with what you'd have then. A drill is a schedule
(kind *Restore drill*, for example weekly) that:

1. takes the **newest backup**, either of a backup schedule in fbadmin's archive, or in **cloud
   storage**. The cloud copy is downloaded first; it is the copy that is left when the fbadmin host
   is gone;
2. checks that fbadmin has its **key**, if it is encrypted;
3. **restores** it into a scratch database, on the database's own server or **on another server** (a
   standby or test machine), at `fbadmin_drill_<id>.fdb` next to the database unless another path is
   set;
4. runs an **online validation** of the restored database;
5. opens it and **reads every table** (row counts);
6. **compares** its tables and row counts with the live database. Missing or extra tables are a
   warning; other row counts are expected (changes since the backup) and listed;
7. drops the scratch database (or keeps it, registered as *(restore drill)*).

It also measures two limits:

* **Newest backup at most N hours old** (default 26): how much data a loss now would cost. An older
  newest backup fails the drill, so a backup schedule that silently stopped is noticed.
* **Recovery at most N minutes**: download plus restore. A slower recovery fails the drill.

A drill fails when the backup is missing or too old, the key is missing, the restore fails, the
validation finds errors, or a table can't be read. It raises the `restore.drill_failed` alert, and when
a drill works again, one *resolved* message follows (see [Alerts](ALERTS.md)). The **Drill report** of
the schedule shows the backup used (encrypted or not, its age, size and key), the time to download and
restore, each check, and the table comparison. The run history keeps a one-line summary of every drill.

A drill needs the admin level on the server it restores on, and the backup schedule it uses must be
visible to the user who saves it. A cloud source needs an administrator (storage credentials).

## API

| Endpoint | |
|---|---|
| `GET /api/backup-keys` | keys (ids, source, which encrypts, archived backups per key), keys that are missing |
| `POST /api/backup-keys` | create a key; returns it once (`key`) |
| `POST /api/backup-keys/import` `{key, activate}` | add a key |
| `POST /api/backup-keys/{id}/activate`, `/export`, `DELETE /api/backup-keys/{id}?force=` | choose, show, remove |
| `POST /api/schedules` `{kind: "backup", encrypt: true, …}` | encrypted backup schedule |
| `POST /api/schedules` `{kind: "restore_drill", drill: {…}}` | restore drill (`source`: `schedule` or `storage`, `backup_schedule_id` or `target_id` + `prefix`, `server_id`, `path`, `validate_db`, `count_rows`, `compare`, `max_age_hours`, `max_minutes`, `keep`) |
| `GET /api/schedules/{id}/drill` | the latest drill report |
| `GET …/maintenance/archive/download?path=&decrypt=true` | an archived backup, decrypted |

## Tested

`backend/tests/test_backup_encryption_drills.py` covers the format (round trips at chunk boundaries,
tampering, truncation, the wrong key), the keys, encrypted schedules with test restores, decrypted
downloads and restores, `fbadmin backup-decrypt`, and drills: passing, a missing backup, a missing key, a
stale backup, the alerts and a kept database, on Firebird 3, 4 and 5.
`test_drill_storage_real.py` downloads in ranges from S3, Azure and GCS emulators and runs a drill from
each. `e2e/tests/drills.spec.ts` goes through the UI, with accessibility checks.
