# Deployment

## Docker

The official image is multi-arch (`linux/amd64`, `linux/arm64`), runs as the unprivileged
user `fbadmin` (uid 1000), keeps its data in `/data` and has a health check on
`/api/health`.

| Tag | Meaning |
|---|---|
| `ghcr.io/mariuz/fbadmin:latest`, `:1.2.3`, `:1.2` | Releases |
| `ghcr.io/mariuz/fbadmin:edge` | Latest `main` |

```bash
docker run -d --name fbadmin -p 127.0.0.1:5050:5050 -v fbadmin:/data \
  -e FBADMIN_LOG_FORMAT=json ghcr.io/mariuz/fbadmin
```

`docker compose up` in the repository starts fbadmin together with a Firebird 5 server
(register host `firebird`, password `masterkey`).

## Helm chart

The chart in `deploy/helm/fbadmin` creates a Deployment (one replica, `Recreate`), a
Service, an optional Ingress, a PersistentVolumeClaim for `/data` and, in server mode, a
Secret with the admin password and the master password that encrypts saved credentials.
Generated passwords are kept across upgrades.

```bash
helm install fbadmin deploy/helm/fbadmin -n fbadmin --create-namespace \
  --set serverMode.enabled=true \
  --set ingress.enabled=true --set ingress.hosts[0].host=fbadmin.example.com \
  --set ingress.tls[0].secretName=fbadmin-tls --set ingress.tls[0].hosts[0]=fbadmin.example.com
kubectl -n fbadmin get secret fbadmin-credentials -o jsonpath='{.data.admin-password}' | base64 -d
```

Useful values (see `values.yaml` for all of them):

| Value | Default | |
|---|---|---|
| `serverMode.enabled` | `false` | Login screen and encrypted credentials |
| `serverMode.adminUser` / `adminPassword` | `admin` / generated | Initial administrator (`FBADMIN_ADMIN_USERNAME` / `FBADMIN_ADMIN_PASSWORD`). Saved Firebird passwords are encrypted per user, see SECURITY.md |
| `serverMode.existingSecret` | | Use your own Secret (key `admin-password`) |
| `persistence.size` / `storageClass` / `existingClaim` | `1Gi` | Data volume |
| `config.logFormat` | `json` | |
| `config.rateLimit` | `600` | Requests per minute per client |
| `config.updateCheck` | `off` | |
| `extraEnv` | `[]` | Any other `FBADMIN_*` variable |

The pod runs with a read-only root file system, no capabilities, `RuntimeDefault`
seccomp and no service account token. `helm test fbadmin` checks the health endpoint.

## Kubernetes example with Firebird

`deploy/k8s` is a plain kustomize example: a Firebird 5 StatefulSet (official
`firebirdsql/firebird` image, persistent volume, demo database), fbadmin, and a
NetworkPolicy that lets only fbadmin reach Firebird.

```bash
kubectl apply -k deploy/k8s
kubectl -n fbadmin port-forward svc/fbadmin 5050:80
# http://127.0.0.1:5050: "Firebird (cluster)" and its demo database are already registered
```

## Preconfigured servers

Deployments can declare their Firebird servers in a file instead of registering them by
hand. Point `FBADMIN_SERVERS_FILE` at a YAML or JSON file:

```yaml
servers:
  - name: Production                    # id: cfg-production (stable across restarts)
    host: firebird.databases.svc.cluster.local
    port: 3050
    user: SYSDBA
    password_env: FIREBIRD_PASSWORD     # environment variable, e.g. from a Kubernetes Secret
    read_only: false                    # true blocks everything but SELECT
    databases:
      - { name: main, path: /var/lib/firebird/data/main.fdb }
```

fbadmin registers or updates these entries at every start. It reads passwords from the
environment each time it connects and never writes them to disk. Databases you add by hand
are kept. A server you delete from the file is removed from fbadmin at the next start. In
server mode the configured servers are *shared*, so every user sees them.

With the Helm chart, list them in `firebirdServers` and give each a `passwordSecret: { name, key }`:

```bash
helm install fbadmin deploy/helm/fbadmin \
  --set-json 'firebirdServers=[{"name":"Production","host":"firebird.databases.svc","user":"SYSDBA","passwordSecret":{"name":"firebird-secret","key":"root-password"},"databases":[{"name":"main","path":"main"}]}]'
```

## With a Firebird Kubernetes operator

fbadmin needs only a Firebird endpoint and credentials, so it works with any way of running
Firebird on Kubernetes: the StatefulSet above, a Helm chart, or an operator such as the
community [Kuberbird](https://groups.google.com/g/firebird-general/c/goDYu1cN1tU)
operator. Deploy the instance with the operator, then:

1. In `firebirdServers` (Helm) or the `servers.yaml` ConfigMap (kustomize), set `host` to
   the Service the operator created for the instance (`<service>.<namespace>.svc`).
2. Set `passwordSecret` / `password_env` to the Secret that holds the SYSDBA (or admin user)
   password.
3. List the databases the operator manages under `databases`.
4. If the operator's namespace restricts ingress, add a NetworkPolicy that lets the fbadmin
   pods reach port 3050, like the one in `deploy/k8s/fbadmin.yaml`.

Backups that the operator schedules (gbak/nbackup) and backups you start from fbadmin's
maintenance tools are independent of each other. Coordinate their schedules so they don't
run at the same time.

Change `root-password` in `deploy/k8s/firebird.yaml` before applying it anywhere real.
