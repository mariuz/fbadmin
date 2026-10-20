# Alerts

fbadmin can send a message when something needs attention: a scheduled backup failed, a backup
could not be restored, validation found errors, the transaction gap is growing, a replica is
falling behind, or a database cannot be reached. Open **🔔 Alerts** in the toolbar, or
**Alert rules…** in a database's context menu.

Alerts have two parts:

- **Channels** say where messages go: email, Slack or a webhook.
- **Rules** say when to send and to which channels.

## Channels

| Kind | Settings | Notes |
|---|---|---|
| Email (SMTP) | host, port, security (`starttls`, `ssl`, `none`), user name, password, from, to | `to` takes several addresses separated by commas. The default port is 587 for STARTTLS, 465 for SSL and 25 for plain SMTP. |
| Slack-compatible webhook | incoming webhook URL | Works with Slack, Mattermost, Rocket.Chat and other services that accept Slack-style `{"text": …}` payloads. |
| Webhook (JSON) | URL, optional signing secret | fbadmin POSTs the alert as JSON (see below). |

**Send test** delivers a test message right away and shows the error if delivery fails.

### Secrets

Passwords, webhook URLs and signing secrets are never shown again after saving:

- The API returns `********` in their place.
- Saving the form with `********` keeps the stored value.

Any setting can be written as `env:NAME`; fbadmin then reads it from its environment each time
it sends. That keeps secrets out of the data directory, which suits Docker and Kubernetes
secrets:

```yaml
# docker compose
environment:
  SLACK_WEBHOOK: https://hooks.slack.com/services/...
# the channel's webhook URL setting: env:SLACK_WEBHOOK
```

Channels and rules are stored in `<data dir>/alerts.json` (file mode `0600`). In server mode,
only administrators can see or change alerts.

### Webhook payload

```json
{
  "id": "3f9c1a2b7d4e",
  "event": "backup.verify_failed",
  "level": "critical",
  "title": "Backup of sales failed its test restore",
  "message": "The backup sales_20261020-020000.fbk of /data/sales.fdb could not be restored: …",
  "server": "production", "server_id": "a1b2c3", "database": "sales", "database_id": "d4e5f6",
  "time": 1792461600.0,
  "rule": "Backups", "rule_id": "9a8b7c",
  "details": {"file": "…"}
}
```

The headers are:

- `X-Fbadmin-Event`: the event id.
- `X-Fbadmin-Signature`: sent when the channel has a signing secret. Its value is
  `sha256=<hex HMAC-SHA256 of the raw body, keyed with the secret>`. Check it on the receiving
  side before trusting the payload.

## Rules

A rule picks an event, an optional scope, the channels and a **cool-down**. While a rule is
cooling down, the same alert is not sent again (60 minutes by default; 0 sends every time).

### Events

fbadmin raises these when they happen. A rule can cover all servers, one server or one
database.

| Event | When |
|---|---|
| `schedule.failed` | Any scheduled job fails: a backup, sweep or validation, a failed test restore, a failed upload, or validation errors. |
| `backup.verify_failed` | A scheduled backup could not be restored in its [test restore](MAINTENANCE.md#test-restores). |
| `validation.errors` | A scheduled online validation reported errors. |
| `upload.failed` | A backup could not be uploaded to its [cloud storage target](MAINTENANCE.md#cloud-storage-targets). |

`schedule.failed` also covers the three more specific events. A rule on `schedule.failed` and
another on `backup.verify_failed` therefore both send their message when a test restore fails.

### Conditions

Condition rules watch one database. A background thread checks them every minute
(`FBADMIN_ALERT_INTERVAL`, in seconds); **Check now** runs a check at once.

| Condition | Threshold (default) | What is checked |
|---|---|---|
| `oat_gap` | 10,000 transactions | Next transaction minus the oldest active transaction (OAT), from `MON$DATABASE`. The alert names the attachment holding the OAT: user, address and process. It is critical at ten times the threshold. |
| `long_transaction` | 3,600 seconds | Transactions older than the threshold in `MON$TRANSACTIONS`. fbadmin's own and system attachments are ignored. |
| `replication_lag` | 5 segments | How many journal segments the replica's replication sequence is behind the primary's. The rule names both sides (Firebird 4 or newer). |
| `database.unreachable` | — | fbadmin cannot open its metadata connection. |

How a condition is reported:

- An alert is sent when the condition starts.
- While it lasts, the alert repeats each time the cool-down ends.
- One **Resolved** message is sent when it clears; this can be turned off per rule.
- The rules table shows each rule's last check (ok, firing or error) and the error when the
  check could not run, for example a password that is not saved.

The checks use the database's metadata connection, so the server password must be saved. On
the monitoring tables, use an account that sees all attachments (SYSDBA, the owner, or a user
with the `MONITOR_ANY_ATTACHMENT` privilege). Otherwise other users' transactions are not
visible.

## History

**Sent alerts** lists the last 500 alerts with the delivery result for each channel. It is
stored in `<data dir>/alert_history.json`. A failed delivery is recorded there and logged; it
never stops the job that raised the alert.

## API

| Endpoint | Purpose |
|---|---|
| `GET /api/alerts/events` | event and condition types with default thresholds |
| `GET/POST /api/alerts/channels`, `PUT/DELETE /api/alerts/channels/{id}` | channels (secrets masked) |
| `POST /api/alerts/channels/{id}/test` | send a test message (`{ok, result}` or `{ok: false, error}`) |
| `GET/POST /api/alerts/rules`, `PUT/DELETE /api/alerts/rules/{id}` | rules, with the last check of condition rules |
| `POST /api/alerts/rules/{id}/check` | run a condition rule's check now |
| `GET/DELETE /api/alerts/history` | sent alerts |

## Testing

`backend/tests/test_alerts.py` covers several layers:

- Webhook and Slack deliveries go to a real local HTTP server, and the HMAC signature is
  checked.
- The condition checks run against a real Firebird server, with a transaction held open.
- Test restores and validation run on real schedules.
- Email goes to a real SMTP server (Mailpit) when `FBADMIN_TEST_SMTP_HOST` is set:

```bash
scripts/smtp-test-server.sh            # prints the FBADMIN_TEST_SMTP_* exports
scripts/storage-test-servers.sh        # S3/Azure/GCS emulators, for the backup-upload test
pytest -q backend/tests/test_alerts.py
```
