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
