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
| `serverMode.adminUser` / `adminPassword` / `masterPassword` | `admin` / generated / generated | |
| `serverMode.existingSecret` | | Use your own Secret (keys `admin-password`, `master-password`) |
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
# http://127.0.0.1:5050 → Add server: host "firebird", user SYSDBA,
# password from the firebird-secret Secret, database /var/lib/firebird/data/demo.fdb
```

Change `root-password` in `deploy/k8s/firebird.yaml` before applying it anywhere real.
