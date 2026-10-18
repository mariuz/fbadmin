# fbadmin with the cloudnative-firebird operator

This example runs fbadmin next to a Firebird cluster managed by
[cloudnative-firebird](https://github.com/mariuz/cloudnative-firebird):

- **The operator** creates the cluster from the `FirebirdCluster` resource: a StatefulSet, a
  volume, the primary Service `fbcluster` and the database `demo.fdb`.
- **fbadmin** registers that cluster at startup from a ConfigMap (`FBADMIN_SERVERS_FILE`). It
  reads the password from the cluster's superuser Secret, so nothing has to be typed into the UI.

```bash
# 1. the operator (CRDs + Deployment), from a checkout of cloudnative-firebird
kubectl apply --server-side -f config/crds/
kubectl apply -f config/deploy/namespace.yaml -f config/deploy/serviceaccount.yaml \
              -f config/deploy/rbac.yaml -f config/deploy/webhook.yaml -f config/deploy/deployment.yaml

# 2. this example (change the password in firebird-secret.yaml first)
kubectl apply -k deploy/k8s-operator
kubectl -n fbadmin-demo wait pod/fbcluster-0 --for=condition=Ready --timeout=300s

# 3. open fbadmin: "fbcluster (primary)" and its demo database are already registered
kubectl -n fbadmin-demo port-forward svc/fbadmin 5050:80   # http://127.0.0.1:5050
```

To use your own cluster, change `host` in the `fbadmin-servers` ConfigMap to the cluster's
Service name (`<cluster name>`, or `<cluster name>.<namespace>.svc` from another namespace),
and change the Secret in `FIREBIRD_PASSWORD` to the cluster's `superuserSecret`. You can
register the read-only Service `<cluster name>-replica` as a second server with
`read_only: true`. With the Helm chart, put the same values in `firebirdServers`.

How it's tested:

- **CI, end to end:** `.github/workflows/k8s-operator.yml` runs on a kind cluster. It builds the
  operator at a pinned commit and fbadmin from this repository, applies this directory, waits
  for the operator to bring the cluster up, then uses fbadmin's API to connect, create a table
  and read it back.
- **Manifests:** server-side dry run against the operator's CRDs. The API server accepts the
  manifests and rejects invalid values (`instances: 0`) and unknown fields.
- **fbadmin side, locally:** the image runs read-only with the ConfigMap's `servers.yaml`
  against a Firebird 5 reachable as `fbcluster`. It registers the server, connects, and runs
  DDL and queries.
