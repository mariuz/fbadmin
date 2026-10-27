# Kubernetes: Firebird clusters of the operator

When fbadmin runs in a Kubernetes cluster, or has a kubeconfig, the **⎈ Kubernetes** button in the top bar
lists the Firebird clusters of the
[cloudnative-firebird](https://github.com/mariuz/cloudnative-firebird) operator
(`FirebirdCluster` resources) and manages them. Only administrators see the page. fbadmin acts
with its own Kubernetes credentials, so what it can do is set by the RBAC you give its service
account. Every action is written to the [audit log](SECURITY.md) as `-- kubernetes: ...`.

## What the page shows

For each cluster:

- the phase, the ready instances, the image, the primary, the replication mode, and whether it
  is hibernated or has fenced instances;
- the **instances** (pods): role, readiness, node, restarts, start time;
- the **backups** (`FirebirdBackup`, newest first) and the **scheduled backups**;
- the **databases** (`FirebirdDatabase`) and **users** (`FirebirdUser`);
- the recent Kubernetes **events** and the cluster's **conditions**.

## Actions

| Action | What fbadmin changes |
|---|---|
| Scale (1–32 instances) | `spec.instances` |
| Hibernate / wake up | `spec.hibernated`; the instances stop and the volumes are kept |
| Rolling restart | the StatefulSet's `kubectl.kubernetes.io/restartedAt` annotation; the operator restarts the primary last |
| Restart one instance | deletes that pod; the StatefulSet recreates it |
| Promote (switchover) | the `targetPrimary` annotation; needs replication |
| Fence / unfence | the `fencedInstances` annotation; the database is shut down, the pod keeps running |
| Back up now | a `FirebirdBackup`: logical (gbak) or physical (nbackup level 0), to the cluster's S3 destination when it has one, else into the primary's data directory |
| Restore | a `FirebirdRestore` from a completed backup of the same cluster, **into a new file** on the primary (default `restore-<name>.fdb`). The cluster database is never replaced |
| Logs | the last lines of an instance's `firebird` container |
| Register in fbadmin | adds (or updates) the server `k8s-<namespace>-<cluster>` in the group *Kubernetes*: host `<cluster>.<namespace>.svc`, the cluster database and its `FirebirdDatabase`s, and the SYSDBA password from the cluster's superuser Secret (or typed in, when fbadmin may not read Secrets). In server mode it can be a shared server |

When Kubernetes refuses an action (RBAC), the page shows the refusal and everything else keeps
working, so a read-only Role gives a read-only page.

## Credentials

fbadmin looks for credentials in this order:

1. `FBADMIN_KUBECONFIG=/path/to/kubeconfig` (and `FBADMIN_KUBE_CONTEXT` for a context other
   than the current one);
2. the pod's service account, when fbadmin runs in a cluster (the token is mounted);
3. with `FBADMIN_KUBERNETES=on`: `$KUBECONFIG` or `~/.kube/config`.

`FBADMIN_KUBERNETES=off` turns the integration off. Kubeconfigs that authenticate with `exec`
or `auth-provider` plugins (cloud CLIs) are not supported: use a token, a client certificate,
or a service account token.

`FBADMIN_K8S_NAMESPACES` lists the namespaces to look in (comma-separated); `*` means all of
them. The default is fbadmin's own namespace (in a cluster) or the kubeconfig context's.

## RBAC

The example `deploy/k8s-operator/rbac.yaml` has the full set for one namespace:

| Resource | Verbs | For |
|---|---|---|
| `firebirdclusters` | get, list, patch | the list; scale, hibernate, switchover, fence |
| `firebirdbackups`, `firebirdrestores` | get, list, create | backups and restores |
| `firebirdscheduledbackups`, `firebirddatabases`, `firebirdusers` | get, list | the cluster view |
| `pods` | get, list, delete | instances; restarting one instance |
| `pods/log`, `events` | get, list | logs and events |
| `statefulsets` (apps) | get, patch | rolling restart |
| `secrets` | get (best limited with `resourceNames`) | Register in fbadmin reads the superuser password |

With the Helm chart:

```bash
helm install fbadmin deploy/helm/fbadmin -n databases \
  --set kubernetes.enabled=true \
  --set kubernetes.readSecrets=true          # optional: read superuser Secrets
# other namespaces:      --set 'kubernetes.namespaces={team-a,team-b}'   (a Role in each)
# every namespace:       --set 'kubernetes.namespaces={*}'               (a ClusterRole)
# list and inspect only: --set kubernetes.manage=false
```

`kubernetes.enabled` mounts the service account token (the chart doesn't by default) and
creates the Role and RoleBinding.

## API

All endpoints need an administrator.

| Method and path | |
|---|---|
| `GET /api/kubernetes/status` | credentials found, API server, namespaces, operator installed |
| `GET /api/kubernetes/clusters` | the clusters |
| `GET /api/kubernetes/clusters/{ns}/{name}` | one cluster with pods, backups, databases, users, events |
| `POST …/scale` `{instances}` | |
| `POST …/hibernate` `{on}` | |
| `POST …/restart` `{instance?}` | no instance: rolling restart |
| `POST …/switchover` `{target}` | pod name or ordinal |
| `POST …/fence` `{on, instance}` | pod name, ordinal or `*` |
| `POST …/backups` `{type, level?, database?, name?}` | 201, the `FirebirdBackup` name |
| `POST …/restores` `{backup, target?}` | 201, the `FirebirdRestore` name and the file |
| `GET …/logs?pod=&tail=` | `{pod, container, text}` |
| `POST …/register` `{host?, port?, password?, shared?}` | the fbadmin server id and databases |

Errors: 409 when no credentials are configured; Kubernetes' own 403, 404, 409 and 422 are passed
through; other API server errors are 502.

## How it's tested

- `backend/tests/test_kubernetes.py` runs every endpoint against a small fake API server
  (merge patches, label and field selectors, RBAC refusals, token checks).
- The kind workflow `.github/workflows/k8s-operator.yml` runs the real operator: it lists and
  registers the cluster, takes a logical backup and waits for it to complete, restores it into a
  new file and reads the data back through fbadmin, and hibernates and wakes the cluster.
