# Privacy

**fbadmin has no telemetry, and never will by default.** It does not collect usage
statistics, crash reports or identifiers, and it does not load fonts, scripts or images
from third-party servers: the web UI is served entirely by your fbadmin.

## What fbadmin connects to

- The Firebird servers you register, with the credentials you give.
- In the desktop app: its own server on `127.0.0.1`.
- **Only if you opt in**: the GitHub Releases API, to check for a newer version.

## The update check

The check is off until you tick **Check GitHub for new releases** in **About** (top bar).
The choice is stored in your browser. When it is on, the UI asks the fbadmin server
(`GET /api/version/latest`), which makes one anonymous HTTPS request to
`https://api.github.com/repos/mariuz/fbadmin/releases/latest` with a generic
`User-Agent: fbadmin-update-check` header and caches the answer for six hours. Nothing
else is sent: no version, no settings, no identifiers. GitHub sees the IP address of the
machine running fbadmin, as for any web request.

Administrators can forbid the check for everyone with `FBADMIN_UPDATE_CHECK=off`; the
About dialog then says it is disabled. The Helm chart sets it to `off` by default.

## Data at rest

Registered servers are stored in the data directory (`~/.fbadmin` or `FBADMIN_DATA_DIR`)
with file mode 0600. Query history, snippets, parameter values and preferences are kept
in your browser's local storage only.

## Logs

Request logs contain the method, path, status, duration and client IP address. They never
contain SQL text, parameter values or passwords.
