# Administration

fbadmin runs in one of two shapes:

- **Desktop / local mode** (default): one user, listening on `127.0.0.1`, no login screen,
  like pgAdmin's desktop mode. Registered servers live in `~/.fbadmin/servers.json`
  (mode 0600).
- **Server mode**: a shared installation (Docker, Kubernetes) with a login screen,
  per-user server lists and encrypted saved passwords. Its setup is described in the
  security guide (`docs/SECURITY.md`), which is added to this section when the feature
  ships. Until then, only publish fbadmin on trusted networks.

| Page | Contents |
|---|---|
| [Configuration](configuration.md) | Environment variables, logging, request limits, driver choice, connection pool |
| [Deployment](deployment.md) | Docker image, Docker Compose, Helm chart, Kubernetes example with Firebird |
| [Privacy](../PRIVACY.md) | What fbadmin sends over the network (nothing, unless you opt in to the update check) |

## Checklist for a shared installation

1. Turn on server mode and set the admin and master passwords from a secret (the Helm
   chart does this with `serverMode.enabled=true`).
2. Put fbadmin behind HTTPS (an ingress or reverse proxy).
3. Set `FBADMIN_RATE_LIMIT` (requests per minute per client) and, behind a proxy,
   `FBADMIN_RATE_LIMIT_TRUST_PROXY=1`.
4. Use `--log-format json` and ship the logs; every request is logged with its status and
   duration.
5. Keep the data directory on a persistent volume and back it up.
6. Leave `FBADMIN_UPDATE_CHECK=off` if the installation must not reach the internet.
