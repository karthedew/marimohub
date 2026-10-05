# deploy/

Cluster-side manifests that exist *before* the Helm release does. These are the checked-in
contract between the platform team that provisions the cluster and everything the
`marimohub` Helm chart (`charts/marimohub/`) later installs into it. `/IMPLEMENTATION_PLAN.md`
is the authoritative source for the Runtime contract, reconcile behavior, and credential model;
`DESIGN.md` DT-8 is retained only as historical context for the design that preceded it.

## Contents

- `crd/marimosession.yaml` — the `MarimoSession` CustomResourceDefinition (group
  `marimohub.io`, version `v1alpha1`). Cluster-scoped; apply once per cluster. Generated:
  `make generate` produces the canonical CRD from `marimohub-operator`'s Go types and copies it
  here byte-for-byte (and into `charts/marimohub/crds/`), and `make verify-generated` fails on
  drift. Do not hand-edit this file. The Helm chart's own `crds/` copy only ever installs this on
  a first `helm install`; an upgrade that changes the CRD still needs this file applied with
  `kubectl apply --server-side` first, the same as any other cluster-scoped object Helm does not
  manage across upgrades.
- `../marimohub-operator/config/policy/marimosession_label_identity.yaml` — a
  `ValidatingAdmissionPolicy`/`ValidatingAdmissionPolicyBinding` pair enforcing that the
  `marimohub.io/notebook`, `marimohub.io/workspace`, and `marimohub.io/mode` labels agree with the
  identically named spec fields. It is not CRD schema CEL because the CRD validation CEL
  environment cannot see `metadata.labels`; apply it alongside the CRD. Cluster-scoped, like the
  CRD, so it stays an installer step here rather than a chart template.
- `namespace/namespaces.yaml` — the three namespace trust boundaries (`marimohub`,
  `marimohub-controller`, `marimohub-sessions`) the Namespace Boundaries table describes. The
  platform pre-creates and owns all three; the Helm chart installs into them but never creates,
  labels, or deletes any of them, so this file (or an installer's equivalent) has to exist first.

## Workspace storage (optional)

Workspace Files (see `docs/adr/0002-workspace-files-on-shared-storage.md`) live on one
ReadWriteMany PersistentVolumeClaim in the sessions namespace, which the platform provisions and
the chart only references through `runtime.workspaceStorage.existingClaim`. In production this is
an NFS export:

```yaml
apiVersion: v1
kind: PersistentVolume
metadata: { name: marimohub-workspaces }
spec:
  capacity: { storage: 1Ti }
  accessModes: ["ReadWriteMany"]
  persistentVolumeReclaimPolicy: Retain
  storageClassName: ""
  claimRef: { namespace: marimohub-sessions, name: marimohub-workspaces }
  nfs: { server: nfs.example.internal, path: /exports/marimohub }
---
apiVersion: v1
kind: PersistentVolumeClaim
metadata: { name: marimohub-workspaces, namespace: marimohub-sessions }
spec:
  accessModes: ["ReadWriteMany"]
  storageClassName: ""
  volumeName: marimohub-workspaces
  resources: { requests: { storage: 1Ti } }
```

The export's root must contain a `workspaces/` directory that is setgid and writable by a
non-root group (`chown :<gid> workspaces && chmod 2775 workspaces`). Pass that group in
`runtime.workspaceStorage.supplementalGroups`, because NFS honors neither `fsGroup` nor an
arbitrary OpenShift UID. A Runtime that cannot create its directory fails fast with
`InvalidSpec` ("workspace directory could not be prepared") and is never retried.

## Shared volumes for notebooks (optional)

Administrators can mount data, such as team datasets on an NFS export, into every notebook
through `runtime.sharedVolumes` (see `docs/adr/0003-admin-shared-volumes.md`). Each share is a
ReadWriteMany PersistentVolumeClaim in the sessions namespace that the platform creates and the
chart only references. Inline `nfs:` Pod volumes are refused by the restricted Pod Security profile
and by OpenShift's `restricted-v2`, so an export becomes an `nfs:` PersistentVolume bound to that
claim:

```yaml
apiVersion: v1
kind: PersistentVolume
metadata: { name: nfs-datasets }
spec:
  capacity: { storage: 500Gi }
  accessModes: ["ReadWriteMany"]
  persistentVolumeReclaimPolicy: Retain
  storageClassName: ""
  claimRef: { namespace: marimohub-sessions, name: nfs-datasets }
  nfs: { server: nfs.example.internal, path: /exports/datasets }
  mountOptions: ["ro"]
---
apiVersion: v1
kind: PersistentVolumeClaim
metadata: { name: nfs-datasets, namespace: marimohub-sessions }
spec:
  accessModes: ["ReadWriteMany"]
  storageClassName: ""
  volumeName: nfs-datasets
  resources: { requests: { storage: 500Gi } }
```

```yaml
runtime:
  sharedVolumes:
    - name: datasets            # mounted at /mnt/datasets
      existingClaim: nfs-datasets
```

A share is read-only and mounted into edit and run Runtimes unless it says otherwise. It can set:
- `mountPath`
- `subPath`, an existing directory inside the claim, since a read-only mount cannot create one
- `readOnly: false`, together with `supplementalGroups` naming the export's group, because NFS
  honors neither `fsGroup` nor an arbitrary UID
- `modes`, which may include `deploy` only for a read-only share, since Deployments are public

Every Workspace sees the same files, so make a share writable only on purpose. Changing
`runtime.sharedVolumes` replaces running Runtime Pods the next time the operator restarts.

## Google and OpenID Connect sign-in

Sign-in through Google, or any other OpenID Connect provider, is optional and runs only in
backend-public. Its credentials go in a Secret of their own that only backend-public loads,
`backendPublic.oidc.existingSecret`. The shared `backendPublic.existingSecret` is also loaded by
backend-internal (reachable from the Runtime namespace), the migration Job, and the maintenance
CronJobs, none of which need the client secret.

1. **OAuth client.** In a Google Cloud project for this environment (Google asks for one project
   per environment), open Google Auth Platform and create a **Web application** client:
   - Under **Branding > Authorized domains**, add the registrable domain of the public host
     (`example.com` for `marimohub.apps.example.com`) before adding the redirect URI.
   - Add the authorized redirect URI `PUBLIC_API_URL` + `/api/auth/oidc/google/callback`, where
     `PUBLIC_API_URL` is the value in `backendPublic.existingSecret`, for example
     `https://marimohub.apps.example.com/api/auth/oidc/google/callback`. Google compares it
     character for character, including the scheme and the absence of a trailing slash.
   - Leave **Authorized JavaScript origins** empty, and request only the `openid`, `email`, and
     `profile` scopes. They are non-sensitive, so they need no scope verification.
   - Google expects production apps to pass brand verification, which needs homepage, privacy
     policy, and terms of service links. Until then the consent screen shows the app's domain
     instead of its name and logo.
2. **Secret.** Create it from a file of `KEY=value` lines and point the chart at it:

   ```sh
   # google-oauth.env: GOOGLE_CLIENT_ID=..., GOOGLE_CLIENT_SECRET=..., and optionally
   # GOOGLE_HOSTED_DOMAIN=example.com to accept only that Google Workspace domain
   kubectl -n marimohub create secret generic marimohub-backend-oidc --from-env-file=google-oauth.env
   hack/preflight/check-dependencies.sh marimohub marimohub-backend-oidc GOOGLE_CLIENT_ID GOOGLE_CLIENT_SECRET
   ```

   ```yaml
   backendPublic:
     oidc:
       existingSecret: marimohub-backend-oidc
   ```

   Other providers go in the same Secret as `OIDC_PROVIDERS` (JSON). backend-public reads the
   Secret only when it starts, so restart it after changing it:
   `kubectl -n marimohub rollout restart deployment/marimohub-backend-public`.
3. **Egress.** backend-public itself calls `accounts.google.com` (discovery),
   `oauth2.googleapis.com` (token exchange), and `www.googleapis.com` (signing keys) on port 443.
   NetworkPolicy matches addresses, not hostnames, and Google's addresses change often, so send
   these calls through an egress proxy that allows only those hosts: set
   `backendPublic.oidc.proxyUrl` and put the proxy's address and port in `network.externalProxy`.
   Do not set `HTTPS_PROXY` or `HTTP_PROXY` on backend-public instead, because its notebook proxy
   and WebSocket clients honor them too. Opening `network.externalProxy` to `0.0.0.0/0` on 443, as
   the kind environment does, is a development shortcut only. Without egress, sign-in fails.

Leave `backendPublic.publicAppUrl` empty unless the browser app is served from a different origin
than `/api`; behind this chart's Route or Ingress they share one host.

**Notebook content shares the app's origin.** In the kind and OpenShift profiles, that one host
also serves Runtime content: `/api/proxy/*` for edit and run Sessions and `/api/deployments/*` for
Deployments. The browser app keeps the signed-in user's MarimoHub bearer token in `localStorage`,
so JavaScript in a notebook, such as an anywidget, can read the token of any signed-in user who
opens that notebook or Deployment and act as them until the token expires. The fix is to serve
Runtime content from a separate notebooks host, so notebook script runs in a different origin from
the app. The chart cannot do that yet.

## Local kind environment

`make kind-up` stands up the whole stack on a local kind cluster with the real Kubernetes
Runtime backend, and reruns converge it after code changes:

- a local registry at `localhost:5001`, with images pushed and digest-pinned;
- a PostgreSQL/pgvector fixture container outside the chart;
- ingress-nginx serving `https://localhost`, or `https://$MARIMOHUB_HOST`, with a generated CA in
  `.kind/tls/ca.crt`; changing the host reissues the certificates that name it;
- the three namespaces, the CRD and admission policy, and every Secret listed above;
- a shared NFS stand-in: `MARIMOHUB_NFS_DIR` (default `/data1/nfs`) mounted into the kind node, bound
  as the `marimohub-nfs` claim, and mounted whole into every notebook at that same path. It is
  read-only, and also mounted into Deployments, unless `MARIMOHUB_NFS_WRITABLE=1`;
- Workspace storage on a separate host directory, `MARIMOHUB_DATA_DIR` (default `/data1/marimohub`),
  bound as the `marimohub-workspaces` claim. Each Workspace's files live in
  `workspaces/<workspace-id>` there, so the NFS stand-in never exposes them;
- the `marimohub-backend-oidc` Secret for Google sign-in, empty unless `GOOGLE_CLIENT_ID` and
  `GOOGLE_CLIENT_SECRET` (and optionally `GOOGLE_HOSTED_DOMAIN`) are passed to `make kind-up`.

The default host is `localhost` because Google refuses redirect URIs on `*.localhost` names. Google
credentials passed once are saved in `.kind/google-oauth.env` for later runs. While they are set,
the script opens `network.externalProxy` to `0.0.0.0/0` on 443 for backend-public, a
development-only shortcut, and restarts backend-public whenever they change. It prints the
redirect URI to register on the OAuth client, `https://localhost/api/auth/oidc/google/callback` by
default; the repository [README](../README.md#sign-in-with-google) walks through the Google Cloud
console.

`SKIP_BUILD=1 make kind-up` reuses the existing images. `make kind-down` deletes the cluster and
containers but never the Workspace files, and `PURGE=1` also drops the database volume and
`.kind/`, including the saved Google credentials. `uv run --project backend python
hack/kind/load_sessions.py --users 50` drives 50 concurrent users, each through an edit Session
whose notebook writes into its own Workspace directory, and reports start latency.

## What moved to the Helm chart

The per-namespace guardrails that used to live here as standalone manifests — the sessions
namespace's `ResourceQuota`/`LimitRange`, the default-deny and Runtime-scoped `NetworkPolicy`
objects, and the RBAC every workload needs — are now rendered by `charts/marimohub/templates/`
from that chart's `values.yaml` (see its `runtime.quota`, `runtime.limitRange`, and `network`
sections), not hand-applied here. Keeping one hand-maintained copy and one chart-templated copy
of the same objects is exactly the kind of drift this repository's own conventions warn against,
so there is deliberately no `namespace/resourcequota.yaml`, `namespace/limitrange.yaml`, or
`namespace/networkpolicy.yaml` any more — install or upgrade the chart to change any of them.

## Apply order

The CRD and the label-identity policy must exist before any `MarimoSession` object (including the
controller's own manifests) can be created; the three namespaces must exist before `helm install`
targets them. The two `hack/preflight/` scripts are real `kubectl` commands against the target
cluster — unlike `helm template`/`helm lint`, which stay offline-deterministic — and are meant to
run before install, never as part of chart rendering.

```sh
kubectl apply -f deploy/crd/marimosession.yaml
kubectl apply -f marimohub-operator/config/policy/marimosession_label_identity.yaml
kubectl apply -f deploy/namespace/namespaces.yaml

hack/preflight/check-namespaces.sh --platform openshift \
  --kubernetes-api-cidrs "10.0.0.1/32" --database-cidrs "10.0.1.0/24" \
  --require-nonempty "routes.host=marimohub.apps.example.com" \
  marimohub marimohub-controller marimohub-sessions
hack/preflight/check-dependencies.sh marimohub marimohub-backend-env DATABASE_URL SECRET_KEY
hack/preflight/check-dependencies.sh marimohub marimohub-frontend-tls tls.crt tls.key
hack/preflight/check-dependencies.sh marimohub marimohub-backend-public-tls tls.crt tls.key
hack/preflight/check-dependencies.sh marimohub marimohub-backend-internal-tls tls.crt tls.key
hack/preflight/check-dependencies.sh marimohub marimohub-database-ca ca.crt
hack/preflight/check-dependencies.sh marimohub-sessions marimohub-internal-api-ca ca.crt
# only with backendPublic.oidc.existingSecret (Google sign-in):
hack/preflight/check-dependencies.sh marimohub marimohub-backend-oidc GOOGLE_CLIENT_ID GOOGLE_CLIENT_SECRET

helm install marimohub charts/marimohub -f charts/marimohub/values-openshift.yaml   # or values-kind.yaml
```

Drop `--platform openshift --kubernetes-api-cidrs ... --database-cidrs ...` (and the
`values-openshift.yaml`-only Secret checks) for a portable/kind install; adjust the CIDR values,
namespace names, and Secret names to whatever the chosen values file actually configures.

## Upgrade

Helm's chart-local `crds/` directory only ever installs the CRD on a first `helm install`; it does
not update an already-installed CRD on `helm upgrade`. Whenever `deploy/crd/marimosession.yaml`
changes, apply it with server-side apply before upgrading the release:

```sh
kubectl apply --server-side -f deploy/crd/marimosession.yaml
helm upgrade marimohub charts/marimohub -f charts/marimohub/values-openshift.yaml
```

The migration Job (a `pre-install,pre-upgrade` Helm hook) then runs `alembic upgrade head` once,
before any application Deployment rolls out; a failed migration blocks the upgrade rather than
rolling forward with a stale schema.

## Uninstall

`helm uninstall` first runs a `pre-delete` hook Job that foreground-deletes every `MarimoSession`
in the sessions namespace and waits for their owned Pods, Services, and credential Secrets to be
gone; it aborts the uninstall (non-zero exit) if that does not finish within
`uninstallDrain.timeoutSeconds`. Only once that hook succeeds does Helm remove the operator,
RBAC, and NetworkPolicies. Nothing in the chart's own templates ever deletes the three
pre-created namespaces, an externally supplied Secret, the external database, or the CRD itself
(Helm's `crds/` directory is never removed by `helm uninstall`, by Helm's own design) — a
completed uninstall leaves all four exactly as they were before install.

## Purge

Removing the CRD itself (and therefore the entire `MarimoSession` API) is a separate, manual,
administrator-run step, deliberately not wired into `helm uninstall`: doing so automatically would
risk deleting the CRD out from under a still-running release, or any other MarimoHub release
sharing the same cluster. Run this only after confirming no `MarimoSession` remains anywhere in
the cluster and no release still depends on the CRD:

```sh
kubectl get marimosessions.marimohub.io --all-namespaces
kubectl delete -f deploy/crd/marimosession.yaml
kubectl delete -f marimohub-operator/config/policy/marimosession_label_identity.yaml
```

## What's not here

The `MarimoSession` controller (operator) lives in `marimohub-operator/`, out of `backend/app`
and out of this directory. `/IMPLEMENTATION_PLAN.md` specifies it in full:

- **Phase 1** (done) — the generated CRD, typed spec/status, and CEL/`ValidatingAdmissionPolicy`
  validation that replace the handwritten schema this directory used to carry.
- **Phase 2** (done) — the Pod/Service builders: security context, fetcher init container,
  probes, labels, and the immutable `RUNTIME_CREDENTIAL` Secret projection.
- **Phase 3** (done) — the reconcile state machine: Condition-based phase transitions, idle/wake
  handling, capacity and failure classification, all without a finalizer.
- **Phase 5** (done) — the credential model: an opaque, live-resource-bound `RUNTIME_CREDENTIAL`
  verified by a separate internal API, replacing any expiring signed token.
- **Phase 6** (done) — production images and the full `charts/marimohub/` Helm chart: workloads,
  RBAC, NetworkPolicy, Runtime policy, external dependency wiring (existing Secrets, the
  migration Job, GitLab-import egress allowlisting), HA defaults, monitoring, and the
  CRD-upgrade/uninstall-drain/purge handling documented above are all packaged there.
