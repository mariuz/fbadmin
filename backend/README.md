# fbadmin

A pgAdmin-style web administration and development tool for Firebird SQL (3.0, 4.0, 5.0):
object explorer, DDL extraction, a query tool with plans and transactions, an editable data
grid, live monitoring and backup/restore. No Firebird client library needed.

```bash
pip install firebird-admin
fbadmin                 # starts on http://localhost:5050 and opens your browser
fbadmin-desktop         # the same in a native window (pip install "firebird-admin[desktop]")
```

Options: `--host`, `--port`, `--no-browser`, `--log-level`, `--log-format json`.

- Documentation: https://mariuz.github.io/fbadmin/
- Source, issues and roadmap: https://github.com/mariuz/fbadmin
- Docker image: `ghcr.io/mariuz/fbadmin`

fbadmin sends no telemetry. Without server mode it has no login screen and listens on
127.0.0.1 only.
