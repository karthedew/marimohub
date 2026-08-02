# MarimoHub Kubernetes Runtime Platform Implementation Plan

## Status

Planned. This is the active implementation plan. The completed frontend plan and the reviewed
operator draft are retained under `docs/plans/`.

## Goal

Deliver MarimoHub as a production-ready, serverless notebook platform for Red Hat OpenShift, with a
portable Kubernetes profile for development and integration testing. A Go operator reconciles the
technical `MarimoSession` resource into hardened notebook Runtime Pods and Services. The backend,
operator, images, Helm chart, and lifecycle protocols are delivered and verified together.

The result must:

- scale idle notebook workloads to zero compute;
- preserve durable Deployment intent and immutable deploy-time source snapshots;
- automatically recreate Runtime Pods only after classified infrastructure loss; startup dependency,
  capacity, policy, image, and workload failures never trigger automatic Pod recreation;
- fail closed around credentials, ownership, RBAC, and network access;
- run under OpenShift `restricted-v2` with arbitrary non-root UIDs;
- provide a versioned, STIG-aligned security evidence trail without claiming system certification;
- install as one coordinated MarimoHub release through an OCI Helm chart;
- pass deterministic unit/envtest suites, a real full-stack kind smoke, and a live OpenShift release
  gate.

## Authority And Supersession

Use these sources in order when implementation details conflict:

1. `CONTEXT.md` for domain language and product relationships.
2. This plan for the agreed target behavior and delivery sequence.
3. The generated Go API types and generated CRD after Phase 1.
4. Accepted ADRs in `docs/adr/`.
5. Existing code and older design documents as evidence of current behavior, not target authority.

This plan supersedes the controller protocol in `DESIGN.md` DT-8,
`marimosession-crd-spec.md`, and `docs/plans/operator-plan-draft.md` where they disagree. Phase 0
updates those documents so conflicting guidance does not remain active.

There is no persisted production `MarimoSession` state to migrate. Do not add conversion webhooks,
legacy annotation handling, or compatibility branches for the reviewed draft.

## Scope

- A Go/Kubebuilder operator in `marimohub-operator/`.
- A generated `marimohub.io/v1alpha1` `MarimoSession` CRD.
- Backend lifecycle, compensation, concurrency, activity, authentication, and source-snapshot fixes.
- A separately deployed internal backend API.
- Ubuntu and UBI notebook Runtime images.
- A UBI source-fetcher image and UBI operator, backend, and frontend production images.
- A full Helm chart with OpenShift and portable Kubernetes profiles.
- Three pre-created namespace trust boundaries.
- External PostgreSQL/pgvector integration and a migration Job.
- CI, release automation, image signing, SBOMs, vulnerability gates, STIG evidence, kind tests, and
  OpenShift qualification.
- Documentation for install, upgrade, operations, security boundaries, and ephemeral storage.

## Non-Goals

- No bundled PostgreSQL, database operator, backup system, or restore system.
- No dynamic `marimo --sandbox` dependency installation or public package-registry egress.
- No user-facing CPU, memory, storage, image, or network controls.
- No per-Workspace Runtime images or policies in this phase.
- No durable Runtime filesystem, Workspace PVC, or Deployment PVC.
- No Deployment revision history or rollback UI; only the current deployed snapshot is retained.
- No automatic retry of quota-rejected attempts or deterministic workload failures.
- No public access to `/api/internal/*`.
- No formal DISA certification claim and no STIG claim for the portable Kubernetes profile.
- No compatibility with existing expiring `SESSION_TOKEN` values or the old wake/activity annotations.

## Domain Model

- A Session is an ephemeral edit or run environment for a Notebook.
- A Runtime is the shared execution concept. A Session has one ephemeral Runtime; a Deployment has
  one durable Runtime identity that may sleep.
- `MarimoSession` remains the Kubernetes resource name for both kinds of Runtime.
- A Deployment runs an immutable source and Runtime-image snapshot captured at deploy time. Notebook
  edits do not affect it until an Editor or Owner redeploys.
- A Runtime filesystem is ephemeral. Only Notebook source and explicitly stored Notebook data survive
  Pod replacement or scale-to-zero.
- Archiving a Workspace stops all of its Sessions and Deployments.

## Target Architecture

```text
public client
    |
    | TLS, one hostname
    v
OpenShift Routes
    |-- /api/* -> public backend -> Runtime Service:8080
    `-- /*     -> frontend

Runtime Pod -> internal API Service (TLS, no Route) -> PostgreSQL
     |                 |
     |                 `-> read-only Runtime CR/Secret validation
     `-> no Kubernetes ServiceAccount token

public backend -> create/get/patch/delete MarimoSession + create/get Secret
operator       -> watch MarimoSession/Secret metadata/Pod/Service, own status/Pod/Service
```

### Namespace Boundaries

| Namespace | Workloads | Trust boundary |
|---|---|---|
| `marimohub` | frontend, public backend, internal API, migration Job | application code and data access |
| `marimohub-controller` | two operator replicas with leader election | Runtime control plane |
| `marimohub-sessions` | untrusted notebook Runtime Pods, Services, credential Secrets, CRs | arbitrary Notebook execution |

The platform pre-creates and owns all three namespaces. A versioned release preflight validates them;
Helm uses but never creates or deletes them.

### Resource Ownership

| Resource/field | Writer | Readers |
|---|---|---|
| Deployment row, source snapshot, desired state | public backend | public/internal backend |
| `MarimoSession` metadata/spec | public backend; operator may delete for idle/orphan cleanup | operator, internal backend |
| wake/activity request annotations | public backend gateway | operator |
| `MarimoSession.status` | operator only | public/internal backend, users |
| credential Secret | public backend only | public/internal backend, operator precondition |
| Runtime Pod and Service | operator only | Kubernetes, public backend |
| namespace quotas, limits, default resources, idle defaults | Helm/platform | operator, admission controllers |

The operator never creates, changes, or adopts credential Secrets. No finalizer is used. Owner
references provide child garbage collection; Events are best-effort and never block deletion.

## Runtime Contract

### Names And Shared Constants

| Contract | Value |
|---|---|
| API | `marimohub.io/v1alpha1`, plural `marimosessions` |
| CR name | Runtime UUID; Deployment Runtime uses Deployment UUID |
| labels | `marimohub.io/notebook`, `marimohub.io/workspace`, `marimohub.io/mode` |
| wake request | `marimohub.io/wake-request`, unique opaque value |
| activity signal | `marimohub.io/activity`, unique opaque value |
| child label | `marimohub.io/session: <runtime-id>` |
| Secret | `msess-<runtime-id>-env` |
| Secret keys | `MARIMO_TOKEN`, `RUNTIME_CREDENTIAL` |
| Pod/Service | `msess-<runtime-id>` |
| Runtime port | fixed `8080` |

One repository conformance test reads both the Python and Go constants and fails on drift. Do not test
either implementation against a third duplicated constants table.

### Spec

The generated CRD has a typed spec:

- `notebookId`, `workspaceId`, `mode`, `image`, and `baseUrl` are required.
- `creatorId` is nullable.
- `mode` is `edit | run | deploy`.
- `image` is a fully resolved immutable image reference; release charts use digests.
- `deploymentRevision` is required only for `deploy` and binds source fetch to the current snapshot.
- `idleTimeoutSeconds` is optional, minimum 30, and overrides the operator's per-mode chart default.
- `resources` is optional typed `corev1.ResourceRequirements`.
- metadata.name is validated as a UUID.
- required labels must exactly match immutable spec identity, `image` must be digest-qualified, and
  `baseUrl` must be a bounded rooted path without query, fragment, or parent traversal.
- identity and launch fields are immutable through CEL.
- `idleTimeoutSeconds` and `resources` are mutable.

Changing resources deliberately replaces the Pod. `status.observedGeneration` advances only after the
new generation is fully reconciled. Changing idle timeout takes effect without replacing the Pod.

Representative type shape:

```go
type MarimoSessionSpec struct {
	NotebookID           string                       `json:"notebookId"`
	WorkspaceID          string                       `json:"workspaceId"`
	CreatorID            *string                      `json:"creatorId,omitempty"`
	Mode                 RuntimeMode                  `json:"mode"`
	Image                string                       `json:"image"`
	BaseURL              string                       `json:"baseUrl"`
	DeploymentRevision   *int64                       `json:"deploymentRevision,omitempty"`
	IdleTimeoutSeconds   *int32                       `json:"idleTimeoutSeconds,omitempty"`
	Resources            *corev1.ResourceRequirements `json:"resources,omitempty"`
}

type MarimoSessionStatus struct {
	Phase                RuntimePhase      `json:"phase,omitempty"`
	Conditions           []metav1.Condition `json:"conditions,omitempty"`
	PodName              string            `json:"podName,omitempty"`
	ServiceName          string            `json:"serviceName,omitempty"`
	LastActivity         *metav1.Time      `json:"lastActivity,omitempty"`
	ObservedWakeRequest  string            `json:"observedWakeRequest,omitempty"`
	ObservedActivity     string            `json:"observedActivity,omitempty"`
	ObservedGeneration   int64             `json:"observedGeneration,omitempty"`
	Attempt              int32             `json:"attempt,omitempty"`
	Message              string            `json:"message,omitempty"`
}
```

The exact markers and generated schema, not this illustrative snippet, are authoritative after Phase
1.

### Phases And Conditions

Keep the human-readable phases `Pending`, `Starting`, `Ready`, `Sleeping`, and `Failed`. Machine
behavior uses Conditions, never message prefixes.

Required condition types:

- `CredentialsAvailable`
- `CapacityAvailable`
- `Ready`
- `Reconciled`

Stable Reasons include `CredentialsMissing`, `CredentialsInvalid`, `QuotaExceeded`, `ResourceConflict`,
`InvalidSpec`, `ImageUnavailable`, `SourceUnavailable`, `RuntimeExited`, `OOMKilled`, `InfrastructureLost`,
and `ReconcileSucceeded`.

`status.message` is bounded, sanitized diagnostic text. It must not include credentials, raw Pod logs,
arbitrary admission payloads, or an API protocol prefix. Clear stale messages and Conditions whenever
their cause is resolved.

### State Invariants

1. New CR + missing/invalid Secret -> `Pending`, `CredentialsAvailable=False`.
2. Valid Secret -> create/reconcile Service, record a start attempt, enter `Starting`, create Pod.
3. Pod passes startup and authenticated readiness -> `Ready`; set `lastActivity=now` and do not run
   idle evaluation in the same reconcile.
4. New activity token -> acknowledge it in status and set `lastActivity` from the controller clock.
5. Idle edit/run -> delete the CR; owner-reference GC removes all children.
6. Idle deploy -> first persist `Sleeping` and clear `podName`, then delete the Pod; retain CR, Service,
   and Secret. A crash between those writes cannot recreate the Pod as infrastructure recovery.
7. Sleeping deploy + unobserved wake request -> persist `Starting`, acknowledge the request, then create
   the Pod. A crash after acknowledgement cannot lose intent because `Starting` with no Pod resumes.
8. Quota rejection -> set `CapacityAvailable=False/QuotaExceeded`, consume that attempt, retain a
   zero-compute state, and do not schedule a timed retry. Only an unobserved request token or a change
   to `spec.resources` starts a new Pod attempt; an idle-timeout-only change does not.
9. Deterministic workload failure -> persist `Failed`, remove the Pod, and never recreate it. Backend
   compensation deletes edit/run CRs; a Deployment remains diagnosable until authorized redeploy/stop.
10. Infrastructure Pod loss -> set `InfrastructureLost`, apply bounded backoff, and recreate unless the
    CR is Sleeping, Failed, quota-blocked, or credentials-invalid.
11. Secret deletion/mutation -> first persist non-Ready credential-invalid state, then remove a live
    Pod. Edit/run is cleaned up; Deployment waits for backend repair plus a new wake request.
12. A previously Ready Pod that loses readiness is immediately removed from routing. If readiness does
    not recover within a bounded grace, a still-running but unhealthy workload fails without automatic
    Pod recreation; an Unknown/node-loss verdict follows infrastructure recovery instead.
13. CR deletion -> return immediately. There is no finalizer and no controller cleanup write.

### Idle Semantics

- Chart defaults: edit 30 minutes, run 10 minutes, deploy 5 minutes.
- `idleTimeoutSeconds` is a minimum idle duration, not an exact shutdown deadline.
- The chart configures a backend activity signal interval and a larger controller grace interval.
- The gateway keeps an activity lease alive while any proxied HTTP request or WebSocket is open and
  signals request/response/frame edges.
- Backend activity writes are coalesced; a failed PATCH does not advance the local throttle.
- The controller timestamps activity itself, schedules the next reconcile at the effective deadline,
  and re-reads current metadata before deleting.
- Every successful initial start, wake, resource replacement, or infrastructure recovery resets the
  clock when the Pod becomes Ready.

### Failure Classification

| Event | Classification | Automatic action |
|---|---|---|
| Pod deleted, node lost, eviction, preemption | infrastructure | bounded recreate |
| source API/network 5xx or timeout | startup dependency | bounded fetch retries in the existing Pod, then fail `SourceUnavailable`; no Pod recreation |
| quota admission denial | capacity | block until explicit retry |
| RBAC/SCC/admission policy denial | platform/configuration | fail, Event/metric, no quota mapping |
| image pull unavailable | transient until startup deadline | fail `ImageUnavailable` after deadline |
| invalid image/command, source 401/403/404 | deterministic | fail, no retry |
| init/main non-zero exit | deterministic workload | fail, no retry |
| OOMKilled | deterministic resource failure | fail, no retry |
| zero exit before serving | deterministic workload | fail, no retry |

Runtime Pods use `restartPolicy: Never`. The operator reads termination reasons and bounded termination
messages, not log tails. Cluster logging retains raw logs outside CR status.

## Security Baseline

- OpenShift is the production and STIG-aligned profile; portable Kubernetes has no STIG claim.
- Select and pin the current applicable DISA Kubernetes STIG release in Phase 0.
- Map each applicable control to automated evidence or an explicit platform prerequisite.
- Runtime Pods use arbitrary non-root UIDs, read-only roots, RuntimeDefault seccomp, no privilege
  escalation, all capabilities dropped, no host namespaces, and no ServiceAccount token.
- All workloads set resource requests/limits, bounded ephemeral storage, and bounded `emptyDir` sizes.
- Default-deny ingress and egress apply in all three namespaces.
- Runtime egress permits only DNS and the internal API by default. Administrators may add chart-managed
  CIDR/namespace/port allowlists; users cannot change egress.
- The internal API has no Route and uses TLS. Public Routes use re-encrypt TLS.
- The OpenShift profile requires verified TLS to PostgreSQL and every external dependency.
- Sensitive application configuration references existing Secrets and never appears in Helm values or
  Helm release history.
- Workload ServiceAccounts receive namespaced least privilege. No application or operator ClusterRole
  is permitted; cluster-scoped CRD/namespace installation is an installer responsibility.
- Release artifacts are digest-pinned, signed keylessly through GitHub OIDC, scanned, and accompanied by
  SPDX/CycloneDX SBOM and provenance.
- Release blocks on any known Critical or fixable High vulnerability unless a checked-in exception has
  an owner, rationale, and expiration.

## Conventions And Gates

The root Makefile becomes the single command surface. Local and CI commands use exact versions from a
checked-in tool manifest.

```text
make bootstrap-tools       # install repository-owned tools into .bin/
make verify-tools          # print and enforce pinned versions
make generate              # deepcopy, CRD, RBAC, generated contract copies
make verify-generated      # fail if generate changes the worktree
make operator-check        # gofmt, go vet, static analysis, unit tests
make operator-test         # envtest with pinned Kubernetes assets
make backend-check         # formatter, type checker, unit/integration tests
make frontend-check        # check, unit tests, build
make images                # all production images
make helm-check            # lint, schema, render, policy checks
make kind-smoke            # full portable Kubernetes integration
make openshift-smoke       # protected live-cluster release gate
make verify                # all non-cluster checks
```

Do not install floating `latest` tools. Do not rely on Homebrew. The current workspace has Go 1.22.2
and none of kubectl, kind, Podman, Helm, or `oc`; Phase 0 must resolve this before scaffolding.

Each phase leaves its own tests green and does not defer correctness tests to a later phase.

---

## Phase 0: Contract Reset And Reproducible Toolchain

**Intent:** remove contradictory guidance and establish a repeatable build environment.

### 0.1 Pin The Compatibility Matrix

Create `hack/tools/versions.env` and bootstrap/verification scripts. Pin:

- Go 1.26.x;
- Kubebuilder go/v4 plugin and controller-runtime;
- controller-gen, kustomize, setup-envtest, and envtest Kubernetes assets;
- kubectl and kind against one supported Kubernetes minor;
- Helm, `oc`, the kind NetworkPolicy CNI, Syft, Grype/Trivy, Cosign, and manifest linters.

Record the Kubernetes/controller-runtime compatibility rationale. All placeholders must be replaced by
exact versions and checksums before Phase 1.

```sh
# hack/tools/versions.env
GO_VERSION=1.26.1
KUBERNETES_MINOR=1.35
KUBEBUILDER_VERSION=<exact-compatible-version>
CONTROLLER_RUNTIME_VERSION=<exact-compatible-version>
KIND_VERSION=<exact-version>
HELM_VERSION=<exact-version>
```

Podman is a host prerequisite because nested container engines are not hidden inside the bootstrap.

### 0.2 Align Documentation

- Update `DESIGN.md`, `marimosession-crd-spec.md`, `deploy/README.md`, root `README.md`, and `.env.example`
  to use Runtime terminology and point to this plan.
- Remove the old JWT, message-prefix, finalizer, auto-retry, current-source Deployment, and handwritten
  CRD claims.
- Keep `docs/plans/frontend-implementation-plan.md` as completed history.
- Keep `docs/plans/operator-plan-draft.md` explicitly superseded.
- Keep ADR `docs/adr/0001-runtime-credentials-and-internal-api.md` accepted.

### 0.3 Add Root Build Targets

Extend the root Makefile without breaking existing local compose commands. Component Makefiles remain
usable directly, but CI calls the root gates.

### Verification

- A clean machine can run `make bootstrap-tools && make verify-tools`.
- No active documentation instructs implementers to parse `QuotaExceeded:`, clear wake annotations,
  mint expiring Runtime JWTs, or treat current Notebook source as Deployment source.
- `git grep` finds old terms only in superseded historical plans or migration notes.

**Status:** pending

## Phase 1: Generated API And Operator Foundation

**Intent:** establish one generated API contract before implementing behavior.

### 1.1 Scaffold Correctly

Create `marimohub-operator/` with a pinned Kubebuilder toolchain:

```sh
kubebuilder init \
  --domain io \
  --repo github.com/karthedew/marimohub/marimohub-operator \
  --plugins go/v4

kubebuilder create api \
  --group marimohub \
  --version v1alpha1 \
  --kind MarimoSession \
  --resource=true \
  --controller=true
```

`group=marimohub` plus `domain=io` intentionally produces `marimohub.io`. Remove generated webhook and
secure-metrics authorization scaffolding that would require cluster-scoped TokenReview/SAR grants.

### 1.2 Define Types And Validation

- Implement typed spec/status, deepcopy generation, short name `msess`, status subresource, and printer
  columns.
- Add CEL rules for UUID names, deploy-only revision requirements, and immutable launch fields.
- Validate exact label/spec identity agreement, digest-qualified images, and bounded safe base paths.
- Generate a structural `ResourceRequirements` schema; do not retain schemaless
  `x-kubernetes-preserve-unknown-fields` behavior.
- Add status list-map markers for Conditions.
- Add admission tests proving required fields, defaults/optional fields, minimum timeout, mode/revision
  coupling, and immutability.

### 1.3 Make Generated Output Canonical

`make generate` produces one canonical CRD and copies it byte-for-byte to:

- `marimohub-operator/config/crd/bases/marimohub.io_marimosessions.yaml`;
- `deploy/crd/marimosession.yaml`;
- `charts/marimohub/crds/marimohub.io_marimosessions.yaml` once the chart exists.

`make verify-generated` regenerates and fails on any diff. There is no semantic allowlist and no
hand-edited CRD copy.

### 1.4 Establish Manager Boundaries

- Watch only the configured Runtime namespace.
- Validate required image/API URL/resource/idle flags at startup.
- Add leader election, `/healthz`, `/readyz`, structured logging, and graceful shutdown.
- Bind metrics without TokenReview/SAR cluster permissions; OpenShift TLS/ServiceMonitor integration is
  completed in Phase 6.

### Verification

- `go build ./...`, `gofmt -l .`, `go vet ./...`, and unit tests pass.
- envtest proves API-server validation and status-subresource isolation.
- Generated CRD copies are exact and drift detection fails when one is edited.
- The manager cannot cache or list resources outside the configured Runtime namespace.

**Status:** pending

## Phase 2: Hardened Images And Resource Builders

**Intent:** define the exact workload interface before writing reconciliation.

### 2.1 Build The Image Set

Create:

- `images/marimo-runtime/Containerfile.ubuntu`;
- `images/marimo-runtime/Containerfile.ubi`;
- `images/source-fetcher/Containerfile` based on UBI 9 minimal;
- `marimohub-operator/Containerfile` based on UBI 9 minimal.

Pin all base images by digest and pin marimo to the repository-tested release, initially 0.23.10 unless
Phase 2 compatibility tests justify an upgrade. Runtime images include Python, marimo, and approved
prebuilt dependencies only. They do not install packages at startup.

Both Runtime flavors and the fetcher:

- default to the same numeric non-root UID for vanilla Kubernetes;
- remain compatible with arbitrary OpenShift-assigned UIDs through root-group writable image paths;
- contain no fixed-UID assumption in Pod manifests;
- work with a read-only root filesystem;
- write only to mounted `/work`, `/tmp`, and bounded cache/home paths.

### 2.2 Implement The Dedicated Fetcher

The fetcher reads `RUNTIME_CREDENTIAL` from a mounted file and downloads atomically from
`/api/internal/runtimes/<id>/source`. It applies connect/total timeouts, bounded retries, TLS CA
validation, output size limits, and distinct exit codes for auth/not-found/transient failures.

```sh
tmp=/work/notebook.py.tmp
curl --fail --show-error --silent \
  --connect-timeout 5 --max-time 30 \
  --retry 5 --retry-all-errors \
  --cacert /var/run/secrets/marimohub/ca.crt \
  -H "Authorization: Bearer $(cat /var/run/secrets/marimohub/credential)" \
  "$INTERNAL_API_URL/api/internal/runtimes/$RUNTIME_ID/source" \
  --output "$tmp"
chmod 0660 "$tmp"
mv "$tmp" /work/notebook.py
```

The checked-in script implements explicit HTTP-to-exit-code mapping rather than relying only on
curl's aggregate exit status.

### 2.3 Build Pod And Service Builders

Create pure builders under `marimohub-operator/internal/session/` with an options struct containing
the fetcher image, internal API URL/CA mount, fixed Runtime port, per-mode idle defaults, chart-wide
Runtime resource defaults, image-pull policy, and optional Runtime ServiceAccount/imagePullSecrets.
The public backend, not the operator, resolves the configured Runtime image flavor to the required
digest-qualified `spec.image`.

The Pod contract includes:

- controller owner reference and exact labels;
- `restartPolicy: Never`, `automountServiceAccountToken: false`, and `enableServiceLinks: false`;
- pod/container restricted security contexts;
- one fetcher init container and one marimo container;
- immutable Secret projection for `RUNTIME_CREDENTIAL` and token environment for marimo;
- explicit `command: ["marimo"]` and mode-derived args;
- `--skip-update-check` for edit mode only; no `--sandbox` in any mode;
- bounded `emptyDir` volumes for work/tmp/home/cache;
- explicit resources for init and main containers;
- TCP startup probe and Python-based authenticated HTTP readiness probe;
- termination grace and lifecycle settings that do not trigger workload restart loops.

```go
args := []string{
	modeCommand(cr.Spec.Mode),
	"/work/notebook.py",
	"--host=0.0.0.0",
	"--port=8080",
	"--token-password=$(MARIMO_TOKEN)",
	"--base-url=$(BASE_URL)",
	"--headless",
}
if cr.Spec.Mode == v1alpha1.RuntimeModeEdit {
	args = append(args, "--skip-update-check")
}
```

The Service is ClusterIP, owner-referenced, fixed at 8080, and selects only
`marimohub.io/session=<runtime-id>`. Reconciliation may update selector/ports while preserving
clusterIP. A foreign same-name object is never adopted.

### 2.4 Pin Builders With Table Tests

Test both Runtime image flavors and every mode. Assert all security fields, writable mounts, Secret
projection, CA mount, commands, probes, labels, owner UID, image pull behavior, resources, and
Service shape. Run containers locally under their default UID and an arbitrary UID with read-only
roots.

### Verification

- Both Runtime images serve a valid Notebook under their hardened filesystem/user settings.
- Fetcher succeeds against TLS, rejects an untrusted CA, and maps auth/not-found/transient errors.
- Image history contains no credentials and every image has a pinned base.
- Builder unit tests, Go checks, and container structure tests pass.

**Status:** pending

## Phase 3: Reconcile State Machine And Envtest

**Intent:** implement the lifecycle as an idempotent, conflict-safe controller.

### 3.1 Centralize Status Transitions

Create one transition helper that patches the status subresource with optimistic conflict retry,
sets/clears Conditions consistently, truncates messages, and advances `observedGeneration` only after
success. Inject a clock and backoff policy into the reconciler.

```go
func (r *Reconciler) transition(
	ctx context.Context,
	session *v1alpha1.MarimoSession,
	phase v1alpha1.RuntimePhase,
	conditions ...metav1.Condition,
) error {
	// Patch only status, merge conditions by type, clear stale fields, retry conflicts.
}
```

### 3.2 Implement Event-Driven Watches

- Watch CR spec and wake/activity annotation changes even when generation is unchanged.
- Watch owned Pods and Services.
- Watch Secret metadata through a mapper from `msess-<id>-env` to CR; do not place Secret data in the
  shared controller cache. Use controller-runtime metadata watches/`PartialObjectMetadata`, while the
  direct reader fetches data only for a named precondition check.
- Read Secret contents with a direct API reader only when validating the precondition.
- Suppress reconcile loops caused only by the controller's own status writes.

### 3.3 Reconcile Credentials And Children

- Validate Secret name, immutable flag, type, owner API/kind/name/UID, and both non-empty keys.
- Treat foreign/partial/mutable Secrets as invalid and fail closed.
- Reconcile Service drift before Pod readiness can be advertised.
- Add a desired-Pod-template hash and detect mutation/spec resource changes.
- Reject foreign Pod/Service collisions with a stable Condition rather than adopting or deleting them.

### 3.4 Implement Attempts, Wake, And Capacity

Persist `Starting` and `observedWakeRequest` before Pod creation. A missing Pod in `Starting` resumes
without needing the annotation again. A quota-blocked attempt remains blocked across reconciles. Only
an unobserved request token or a change to `spec.resources` starts a new Pod attempt;
`idleTimeoutSeconds` changes never retry capacity.

ResourceQuota admission does not provide a reliable structured reason. After a Pod-create Forbidden,
confirm quota exhaustion against every live chart-managed ResourceQuota's hard/used values and the
effective Pod request, and require the canonical quota admission text to agree. If quota cannot be
confirmed, classify it as RBAC, SCC, LimitRange, Pod Security, or webhook/platform denial instead.

### 3.5 Implement Readiness, Failure, And Recovery

- Inspect init and main container status separately.
- Apply the failure matrix and source-fetch exit-code contract.
- Let Kubernetes image-pull backoff operate until a configured startup deadline, then fail.
- Persist deterministic failure before deleting the Pod.
- Guard `Failed` before every create path.
- Recreate infrastructure loss with bounded exponential backoff and jitter.
- Delete an old terminal infrastructure Pod and wait for NotFound before creating its replacement.
- Clear Ready/routing immediately when a Ready Pod becomes unready; apply separate bounded deadlines
  for readiness loss and Unknown/node-loss classification.

### 3.6 Implement Idle And Policy Updates

- Acknowledge new activity tokens using controller time.
- Keep activity monotonic and reset at every Ready transition.
- Compute the effective per-mode idle timeout and chart-wide resource defaults from manager
  configuration, applying `spec.idleTimeoutSeconds` and `spec.resources` as CR overrides.
- Schedule reconcile for the exact timeout-plus-grace deadline.
- Re-read metadata before idle action so concurrent activity wins.
- Replace the Pod on a resources generation change and report progress through Conditions.

### 3.7 Add Events And Metrics

Emit transition-only Events and Prometheus metrics for phase counts, startup/wake duration, idle
shutdown, attempts, capacity rejection, failures by Reason, and reconcile errors. Never label metrics
with Runtime, Notebook, User, or Workspace IDs.

### 3.8 Test Every Branch

Use unit tests for pure classification/builders and envtest for real watches/status/admission. Include:

- Secret arrival, missing keys, wrong UID, mutation, and deletion;
- Service/Pod foreign collisions and drift;
- Ready startup, null activity, token activity, open-lease cadence, idle edit/run, idle deploy;
- wake crash points, stale wake values, concurrent annotations, and immediate re-sleep prevention;
- quota block, no timed retry, explicit retry, and stale Condition clearing;
- non-quota Forbidden responses and live multi-dimensional quota confirmation;
- init/main exits, OOM, source failures, image pull timeout, zero exit, and Failed Pod deletion;
- readiness loss, recovery grace, unhealthy timeout, and Unknown/node-loss deadline;
- eviction/node loss/manual Pod deletion recovery and Failed no-recreate;
- resource update replacement, idle update without replacement, and observedGeneration timing;
- status conflicts, manager restart idempotence, namespace isolation, and event-driven enqueue behavior.

envtest has no kubelet, quota controller, or GC. Patch Pod statuses, inject classified API errors, and
assert owner references there; real behavior is covered in Phases 8 and 9.

### Verification

- `make operator-check operator-test` passes with pinned envtest assets.
- A state-transition coverage table maps every invariant/failure row to at least one test.
- No reconcile path creates a Pod from `Failed`, quota-blocked, Sleeping-without-new-wake, or invalid-
  credentials state.
- No controller RBAC grants Secret write or Pod log access.

**Status:** pending

## Phase 4: Backend Runtime Lifecycle And Deployment Snapshots

**Intent:** make backend intent, cleanup, and concurrency agree with the controller.

### 4.1 Persist Deployment Intent And Snapshot

Add an Alembic migration and model fields:

- `desired_state: active | stopped` as durable owner intent;
- `source_snapshot` containing the current deployed source;
- `source_sha256`;
- `runtime_image` as an immutable digest reference;
- monotonically increasing `revision`.

Migrate every existing Deployment row to `stopped` because it has no valid snapshot/image revision;
an Editor or Owner must redeploy it. Initialize revision to zero and use constraints requiring a
non-null snapshot (an empty Notebook is valid), non-empty hash/digest, and positive revision whenever
desired state is `active`.

Public API status becomes `running | sleeping | failed | stopped` and is projected from durable intent
plus live Runtime state. Authorized Notebook management responses include a stable sanitized failure
Reason/message; anonymous Deployment traffic receives only a generic unavailable response.

Deploy/redeploy reads Notebook source through `NotebookStorageService`, resolves the configured Runtime
image digest, and commits the new snapshot atomically. Do not expose source snapshots in API schemas.

### 4.2 Serialize Stop, Wake, And Redeploy

Use fixed lock order `Workspace` then `Deployment`. Persist durable intent/revision before external
waits, perform foreground deletion/waits outside database locks, then reacquire and recheck the same
intent/revision before any short create/wake mutation. Never hold a database lock while waiting for CR
deletion, child GC, or Ready.

```python
async with db.begin():
    workspace = await lock_active_workspace(db, workspace_id)
    deployment = await lock_deployment(db, deployment_id)
    expected_revision = deployment.revision

await remove_stale_runtime_if_revision_differs(deployment_id, expected_revision)

async with db.begin():
    await lock_active_workspace(db, workspace.id)
    deployment = await lock_deployment(db, deployment_id)
    require_active_revision(deployment, expected_revision)
    request = await manager.request_wake(deployment)  # short cluster mutation only

return await manager.wait_ready(request)  # no database lock held
```

- Stop commits `stopped` first, then foreground-deletes/waits outside the lock. A later wake recheck
  cannot resurrect it.
- Redeploy commits the new snapshot revision, foreground-deletes/waits for the old CR and all fixed-name
  children, then rechecks the revision before creating. Concurrent first requests share this protocol.
- Wake rechecks `active` and the expected revision while locked, performs only the short request
  mutation, releases, then polls.
- Detect `deletionTimestamp` and wait for NotFound; never route or reuse a terminating CR.
- Public target resolution compares the CR's `deploymentRevision` with the locked database revision and
  returns unavailable while they differ; it never routes the stale Ready CR during redeploy.
- `delete_runtime_and_wait` uses foreground propagation with blocking controller owner references and
  waits for CR NotFound, which must occur only after dependents are gone; real-cluster tests verify this
  before same-ID recreation without granting the backend Pod/Service reads.
- Tests force concurrent stop/wake, stop/create, redeploy/request, archive/create, process-crash, and
  backend-replica races.

### 4.3 Replace Message Parsing With Conditions

Update Python CR types and manager behavior to consume stable Condition types/Reasons. Remove
`_QUOTA_MESSAGE_PREFIX`. Require `status.observedGeneration == metadata.generation` before routing a
Ready Runtime.

- quota -> `SessionCapacityError`/429;
- deterministic failure -> sanitized `NotebookStartupError`;
- transient unavailability/timeout -> 503;
- terminating/missing -> not found or retry according to caller context.

### 4.4 Make Creation Compensating

CR creation, Secret creation, and readiness polling form one compensated operation for edit/run:

```python
created = await create_runtime_cr(...)
try:
    await create_runtime_secret(created)
    return await wait_ready(created.id)
except BaseException:
    await asyncio.shield(delete_runtime_and_wait(created.id))
    raise
```

Cleanup is bounded, preserves the original exception, and applies to Secret-create failure, quota,
deterministic startup failure, timeout, and caller cancellation. The controller also deletes
credentialless/failed edit/run CRs after a short chart-configured orphan TTL as a crash safety net.
Deployment CRs are retained. A missing owned Secret is repairable; an existing invalid/foreign Secret
requires authorized redeploy or administrator remediation.

### 4.5 Implement Request/Activity Protocols

- Wake writes a cryptographically random request token; the operator acknowledges it in status.
- Activity writes a random token only after a successful PATCH updates local throttle state.
- Add an activity-lease helper used for the full lifetime of HTTP streams and WebSockets.
- Bound per-replica throttle/lease bookkeeping with TTL/LRU eviction.
- The chart injects the same signal interval and a safely larger controller grace.
- Capacity retry writes a new wake request only for a fresh incoming/manual request; no background task
  retries it.

### 4.6 Stop All Workspace Runtimes On Archive

Every Runtime create/wake/redeploy first locks and rechecks the owning Workspace. Archive locks the
Workspace, commits `archived_at` and all Deployment desired states as stopped, then foreground-deletes
and waits for every Workspace-labeled Runtime outside the lock. The fixed lock order is Workspace then
Deployment. Once archive intent commits, no new Runtime mutation may pass the active-Workspace recheck.

Use the same lifecycle cleanup for permanent Notebook deletion, Workspace Purge, and any User deletion
that removes the last owning relationship. Add a retryable maintenance command so a process crash after
durable stop/archive intent cannot leave compute indefinitely; it removes CRs whose Workspace is
Archived/missing, whose Deployment is stopped/missing/revision-stale, or whose Notebook is missing.
Phase 6 schedules it.

### Verification

- Backend unit/integration tests cover snapshots, digests, revisions, status projection, compensation,
  condition mapping, activity leases, and all concurrency races.
- A Notebook source edit does not alter an existing Deployment snapshot; redeploy does.
- Capacity/startup/timeouts leave no unreachable edit/run CR or Secret.
- Archive leaves no Runtime CR for the Workspace.
- Archive/create, Notebook-delete, Workspace-Purge, and crash-point race tests leave no live Runtime.
- Existing subprocess backend tests remain green; new Kube behavior does not leak into that seam.

**Status:** pending

## Phase 5: Runtime Credentials And Internal API Isolation

**Intent:** deliver the accepted workload-identity boundary from ADR 0001.

### 5.1 Replace Session JWTs

Remove `SESSION_TOKEN_TTL_SECONDS`, JWT mint/decode helpers, and `SESSION_TOKEN`. Generate a token such
as `mh_rt_v1.<runtime-uuid>.<32+ random bytes>` and store it only in the immutable owned Secret as
`RUNTIME_CREDENTIAL`.

Do not log, hash into Pod annotations, place in CR status, or return the credential to clients.
The backend may recreate a missing owned Secret for a zero-compute Deployment and then issue a new wake
request. It never adopts or mutates an invalid/foreign same-name Secret; authorized redeploy
foreground-deletes the old resource graph, while an administrator resolves a foreign collision.

### 5.2 Validate Live Runtime Identity

Create a narrow credential verifier in the internal backend:

```python
async def verify_runtime_credential(token: str) -> RuntimePrincipal:
    runtime_id, _random = parse_runtime_token(token)
    cr, secret = await read_runtime_and_secret(runtime_id)
    require_live_uid_bound_owner(cr, secret)
    expected = decode_secret(secret, "RUNTIME_CREDENTIAL")
    if not secrets.compare_digest(token, expected):
        raise Unauthenticated("Invalid runtime credential")
    return RuntimePrincipal.from_cr(cr)
```

Validation fails closed on Kubernetes errors, terminating CRs, wrong owner UID, malformed tokens,
missing keys, wrong Notebook binding, or stale deployment revision. Initial implementation performs
live reads. Any later cache requires a separate decision and bounded revocation tests.

The Runtime image-pull Secret, when required, is referenced through a tokenless Runtime ServiceAccount
or Pod `imagePullSecrets` and is used only by kubelet; it is never mounted into Runtime containers.
Trusted backend/operator Secret-read permissions and this residual namespace exposure are documented
in the security matrix.

### 5.3 Split Public And Internal Applications

- Remove the internal router from `app.main`.
- Add `app.internal_main` mounting health plus `/api/internal/*` only.
- Run public and internal APIs as separate Deployments and ServiceAccounts using the same backend image.
- Give the internal API only get access to named Runtime CRs/Secrets; never create/list/patch/delete.
- Serve the internal API over OpenShift service-certificate TLS with no Route.

### 5.4 Make Source Runtime-Addressed

Replace the Notebook source path with:

```text
GET /api/internal/runtimes/{runtime_id}/source
Authorization: Bearer <RUNTIME_CREDENTIAL>
```

After credential verification:

- edit/run returns current source through `NotebookStorageService`;
- deploy requires matching `deploymentRevision` and returns `Deployment.source_snapshot`;
- absent source is a valid empty Python document;
- mismatched/deleted/stopped resources fail closed.

Keep internal Notebook data endpoints, but authenticate through the Runtime principal and confirm its
Notebook ID matches the path. Document environment variables/file paths that prebuilt Notebook code
uses to call them.

### 5.5 Test Revocation And Isolation

- Token valid only while exact CR + Secret UID ownership exists.
- CR deletion revokes even while GC has not deleted the Secret.
- Secret deletion/mutation revokes and causes Pod shutdown.
- Recreated same-name CR with a new UID cannot use the old Secret/token.
- Missing Deployment Secret can be recreated explicitly; invalid/foreign collisions cannot be adopted.
- Public app returns 404 for every internal path.
- Internal app has no public/auth/workspace/notebook management routes.
- Tokens never appear in logs, metrics, Events, errors, or snapshots.

### Verification

- Backend security, API, Kube fake, and database tests pass.
- `git grep` finds no active `SESSION_TOKEN`, session JWT TTL, or internal router on the public app.
- RBAC tests prove public and internal backend ServiceAccounts have distinct permissions.

**Status:** pending

## Phase 6: Production Images And Full Helm Chart

**Intent:** package the complete platform for OpenShift and portable Kubernetes.

### 6.1 Harden Application Images

Replace production backend/frontend images with pinned UBI 9 Python/Node multi-stage builds. Remove
`--reload` and Alembic startup migration from backend CMD. Add dedicated public/internal commands,
native TLS entrypoints, health endpoints, read-only roots, arbitrary-UID compatibility, bounded tmp
volumes, and signal-correct shutdown.

Backend TLS entrypoints watch certificate files and perform a graceful Uvicorn restart on rotation;
the frontend's custom HTTPS server reloads its secure context without dropping the process. The Go
operator uses controller-runtime's certificate watcher for metrics TLS. Test rotation, not just initial
mounting. Bake the pinned embedding model into the backend image so production startup needs no model
download.

The local development compose profile may retain live reload, but it must not be the production image
entrypoint.

### 6.2 Create The Chart

Create:

```text
charts/marimohub/
  Chart.yaml
  values.yaml
  values.schema.json
  crds/
  templates/
    frontend/
    backend-public/
    backend-internal/
    operator/
    migration/
    maintenance/
    uninstall-drain/
    rbac/
    networkpolicy/
    routes/
    monitoring/
    tests/
  values-kind.yaml
  values-openshift.yaml
```

The chart deploys the frontend, both backend apps, migration Job, operator, cross-namespace RBAC,
guardrails, maintenance/Purge jobs, uninstall drain, Routes/Ingress, monitoring, and Runtime policy.
It does not deploy PostgreSQL or own namespaces. A versioned preflight command, not Helm `lookup`,
validates namespace existence/labels and external dependencies before install; offline `helm template`
remains deterministic.

### 6.3 Configure External Dependencies Safely

- Require an existing application Secret mapping `DATABASE_URL`, signing/auth settings, and any OIDC
  credentials.
- Require external PostgreSQL with pgvector already available.
- Require existing image-pull Secrets when registry policy needs them.
- Require verified PostgreSQL TLS and an existing CA mount in the OpenShift profile.
- Require existing internal TLS certificate/CA Secrets in the portable profile. In OpenShift, annotate
  Services for service-serving certificates, inject the service CA, and template the re-encrypt Route's
  destination CA correctly.
- Keep sensitive values out of `values.yaml`, NOTES, rendered manifests, and Helm tests.
- Run one pre-install/pre-upgrade Alembic Job with hook-weighted ServiceAccount/RBAC creation and hook
  cleanup, and block rollout on failure.
- Use expand-contract migrations compatible with the previous released application. Rollback tests run
  the N-1 image against the upgraded schema; destructive contract cleanup occurs only in a later release.
- Route GitLab import through an administrator-supplied egress proxy/allowlist with HTTPS host,
  redirect, and SSRF validation, or disable it explicitly. Standard NetworkPolicy never pretends to
  allow an FQDN directly.

### 6.4 Add Runtime Policy Values

```yaml
runtime:
  defaultImageFlavor: ubi
  images:
    ubi: ghcr.io/karthedew/marimohub-runtime-ubi@sha256:...
    ubuntu: ghcr.io/karthedew/marimohub-runtime-ubuntu@sha256:...
  idleTimeoutSeconds:
    edit: 1800
    run: 600
    deploy: 300
  activitySignalIntervalSeconds: 10
  activityGraceSeconds: 20
  resources:
    requests: { cpu: 250m, memory: 512Mi, ephemeral-storage: 256Mi }
    limits: { cpu: "1", memory: 2Gi, ephemeral-storage: 1Gi }
  fetcherResources:
    requests: { cpu: 25m, memory: 64Mi, ephemeral-storage: 32Mi }
    limits: { cpu: 100m, memory: 128Mi, ephemeral-storage: 128Mi }
  quota:
    hard:
      pods: "50"
      requests.cpu: "12500m"
      requests.memory: "25Gi"
      requests.ephemeral-storage: "12800Mi"
      limits.cpu: "50"
      limits.memory: "100Gi"
      limits.ephemeral-storage: "50Gi"
  workVolumeSizeLimit: 512Mi
  maxSourceBytes: 10485760
  egressAllowlist: []
```

The chart injects the image digest map/default flavor into the public backend so it can author required
`spec.image`; the operator receives only fetcher and policy defaults. Values configure operator
resource/idle defaults and namespace LimitRange/ResourceQuota, not backend product authorization.
Validation checks explicit quota hard values against main/init effective Pod requests using Kubernetes
quota semantics; it does not infer capacity from Pod count alone.

### 6.5 Add Least-Privilege RBAC

- Public backend: create/get/list/patch/delete CR main resource; create/get Secrets; no status writes.
- Internal backend: get CRs and Secrets only.
- Operator: CR main resource get/list/watch/delete; CR status get/patch/update; Pod/Service get/list/
  watch/create/update/patch/delete; Secret get/list/watch through metadata watches plus direct named
  reads; ResourceQuota get/list; Event create/patch.
- Operator leader election: Lease access only in controller namespace.
- No workload receives Secret mutation beyond public backend Secret creation. No workload receives Pod
  logs, TokenReview, SAR, node access, namespace mutation, or cluster-wide list/watch.

Add positive and negative `kubectl auth can-i --as=...` tests.

### 6.6 Add Network And TLS Boundaries

- Default deny all three namespaces.
- Router ingress reaches public frontend/backend TLS Services only.
- Public backend reaches PostgreSQL, Runtime port 8080, DNS, Kubernetes API as required, and configured
  OIDC/GitLab egress proxy or explicit external CIDRs only.
- Internal backend receives only Runtime source/data traffic and reaches DNS, PostgreSQL, and the
  Kubernetes API.
- Migration and maintenance Jobs reach DNS, PostgreSQL, and the Kubernetes API only as their command
  requires; the migration Job normally needs only DNS/PostgreSQL.
- Operator reaches DNS and explicitly configured Kubernetes API CIDRs/ports only.
- When monitoring is enabled, only configured monitoring namespace/pod selectors reach TLS metrics.
- Runtime reaches DNS and internal API TLS only, plus administrator allowlists.
- Cross-Runtime traffic, Runtime Kubernetes API, public-to-internal API, and public internet egress are
  denied.

OpenShift profile creates same-host path Routes (`/api` and `/`) with re-encrypt TLS and service-serving
certificates. Portable profile requires explicit internal TLS inputs and uses conditional Ingress. The
chart exposes concrete DNS, Kubernetes API, database, router, monitoring, and external proxy CIDR/port
values because standard NetworkPolicy cannot portably select post-DNAT API endpoints or FQDNs. Preflight
fails when required production policy inputs are absent.

### 6.7 Add HA And Operations Defaults

- Two replicas each for frontend, public backend, internal backend, and operator.
- Leader election for the controller.
- Rolling updates, topology spread/anti-affinity, PodDisruptionBudgets, probes, priorities, and resource
  limits.
- One-replica development values for constrained clusters.
- Internal TLS metrics Service and optional ServiceMonitor.
- Hardened single-concurrency CronJobs for archived-Workspace/stopped-Deployment Runtime cleanup and
  the existing Workspace purge command, with distinct least-privilege ServiceAccounts.
- Helm NOTES with only non-sensitive endpoints and required follow-up checks.

### 6.8 Handle CRD Upgrades Explicitly

Helm `crds/` supports first install but does not upgrade CRDs. Release artifacts include the canonical
CRD and an explicit server-side apply step before `helm upgrade`. CI tests install, schema upgrade, app
upgrade, N-1 rollback behavior, and uninstall. A pre-delete drain hook foreground-deletes all
`MarimoSession` CRs and waits for their Pods, Services, and credential Secrets before Helm removes the
operator or NetworkPolicies; timeout aborts uninstall. Give that hook a dedicated ServiceAccount/Role
for CR/Pod/Service/Secret get/list/watch and CR delete, plus API-server/DNS egress under default deny.
Production uninstall retains platform
namespaces, externally supplied Secrets/database, and cluster CRD. An explicit documented purge removes
the CRD only after all CRs are gone.

### Verification

- `helm lint`, JSON schema validation, OpenShift and kind renders, and manifest policy checks pass.
- No rendered workload violates the security baseline or references mutable image tags.
- Migration runs once and application Pods never execute Alembic.
- Certificate rotation reloads/restarts serving processes without serving expired credentials.
- Preflight catches absent namespaces, TLS/database inputs, and policy CIDRs without making render
  cluster-dependent.
- All Routes/Ingress, Service ports, certificates, RBAC, NetworkPolicies, quotas, and image flavors are
  represented in chart tests.
- Production and development profiles install without editing templates.

**Status:** pending

## Phase 7: CI, Supply Chain, And STIG Evidence Foundation

**Intent:** make security and release gates enforceable rather than documentary.

### 7.1 Add GitHub Actions

Create immediately runnable workflows for:

- pull-request component checks and generated-file drift;
- image build/test/scan without publication.

Create reusable, non-triggered release jobs for signing/attestation and artifact promotion, but do not
enable kind, tagged release, or OpenShift workflows until their executable harnesses exist in Phases 8
and 9.

Use concurrency cancellation, least-privilege `GITHUB_TOKEN`, pinned action commit SHAs, artifact
retention, and no long-lived registry signing key.

```yaml
permissions:
  contents: read
  packages: write
  id-token: write

steps:
  - name: Sign immutable image
    run: cosign sign --yes "${IMAGE}@${DIGEST}"
```

### 7.2 Define A Coordinated Release Candidate

Define and locally test one pipeline that builds separate artifacts:

- backend;
- frontend;
- operator;
- source fetcher;
- Runtime Ubuntu;
- Runtime UBI;
- OCI Helm chart.

The eventual tagged workflow publishes signed immutable commit-SHA image candidates so qualification
clusters can pull them, captures digests, injects those digests into one packaged chart candidate, and
stores that exact `.tgz` as a workflow artifact. It does not publish a versioned chart or release tags
until OpenShift qualifies the exact blob in Phase 9. Never rebuild between qualification and promotion.

### 7.3 Add Security Artifacts

- Generate SPDX and CycloneDX SBOMs for each image and chart bundle.
- Generate SLSA-compatible provenance where supported.
- Scan first-party and chart-referenced third-party images.
- Fail on Critical or fixable High findings.
- Add `docs/security/vulnerability-exceptions.yaml` with schema requiring owner/rationale/expiry.
- Provide reusable signature/attestation verification consumed by Phase 9's release workflow and
  OpenShift smoke.

### 7.4 Add STIG Control Matrix

Create `docs/security/stig-control-matrix.md` pinned to the selected DISA release. For each applicable
control record:

- control identifier and requirement;
- MarimoHub evidence/test;
- chart value or manifest implementing it;
- platform-owned prerequisite where not component-controlled;
- status and review date.

Include image provenance, non-root/arbitrary UID, privilege/capability, audit/event, RBAC, Secret,
network, TLS, resource, logging, update, and vulnerability-management controls. Explicitly state that
the matrix is alignment evidence, not an authorization/certification package.

### Verification

- Pull requests cannot merge with generated drift, failing tests, unsafe manifests, or blocked CVEs.
- No enabled workflow references a cluster harness or Make target that does not yet exist.
- Candidate pipeline tests prove every candidate digest has a signature, SBOM, provenance, and matching
  packaged-chart reference.
- No workflow exposes cluster credentials to untrusted pull requests.

**Status:** pending

## Phase 8: Kind Component And Full-Stack Integration

**Intent:** prove the portable profile and cross-component lifecycle on every change.

### 8.1 Build A Safe Harness

Create an idempotent script under `hack/smoke/kind/` that:

- uses `set -Eeuo pipefail`;
- creates a uniquely named cluster and dedicated temporary kubeconfig;
- refuses to delete a cluster it did not create;
- installs a pinned NetworkPolicy-capable CNI with default kind networking disabled;
- loads exact local image digests;
- traps cleanup of clusters, port-forwards, and processes;
- uses bounded waits and emits diagnostics/log bundles before teardown;
- supports `KEEP_CLUSTER=1` for debugging.

After the harness passes locally, enable a pull-request kind workflow that invokes only the checked-in
`make kind-smoke` target and uploads failure diagnostics.

### 8.2 Keep A Fast Operator Component Tier

Use a TLS mock internal API and manually authored CR/Secret fixtures to isolate controller transitions,
real kubelet status, ResourceQuota, owner-reference GC, Pod security, both Runtime images, and failure
classification. This is not called a full MarimoHub lifecycle test.

### 8.3 Install The Real Full Stack

Provision a disposable PostgreSQL/pgvector fixture outside the chart, pre-create the three namespaces
and existing Secrets, then install the portable chart. Drive the real public backend to:

1. register a User and create a Workspace and Notebook;
2. start edit and run Sessions with both Runtime image flavors;
3. verify authenticated readiness and autosave persistence;
4. create a Deployment snapshot, edit the Notebook, sleep/wake, and prove the old snapshot still runs;
5. redeploy and prove the new snapshot/revision runs;
6. hold HTTP/WebSocket connections open past idle and prove no shutdown;
7. close them and prove edit/run deletion and Deployment zero-compute Sleeping;
8. inject quota exhaustion, observe 429/Condition/no background retry, free capacity, and manually retry;
9. inject deterministic init/main/OOM/image failures and prove no restart loop;
10. delete/evict a healthy Pod and prove infrastructure recovery;
11. delete/mutate credentials and prove immediate fail-closed shutdown/revocation;
12. exercise concurrent stop/wake/redeploy requests and stale-revision routing denial;
13. Archive a Workspace while racing Session create/Deployment wake and prove all Runtime CRs disappear;
14. permanently delete a Notebook, Purge a Workspace, and run maintenance cleanup;
15. expose authorized Deployment failure diagnostics without leaking detail to anonymous visitors;
16. stop/delete live resources and prove real foreground/background GC behavior.

Add one Playwright journey for a public Deployment cold start, capacity Retry, Ready iframe, and
snapshot behavior.

### 8.4 Prove NetworkPolicy

Execute positive and negative probes for every allowed/denied edge. In particular, prove a Runtime
cannot reach another Runtime, Kubernetes API, public backend management routes, public internet, or
the external database, while DNS and internal API TLS continue to work.

### 8.5 Exercise Install Lifecycle

- Fresh install with canonical CRD.
- Upgrade from the previous test chart revision with CRD pre-apply.
- Failed migration blocks rollout.
- Operator leader failover.
- Chart uninstall first drains every Runtime resource, then leaves pre-created namespaces, externally
  supplied Secrets, database, and CRD intact.
- Explicit test purge removes the disposable CRD and cluster.

### Verification

- `make kind-smoke` passes unattended on a clean supported Podman host.
- Every state transition, failure class, security edge, image flavor, and cleanup rule is asserted.
- The GitHub kind workflow is required and green before Phase 9 release automation is enabled.
- The harness always tears down its own cluster unless `KEEP_CLUSTER=1` is set.

**Status:** pending

## Phase 9: OpenShift Release Qualification

**Intent:** verify the behavior that kind cannot establish and approve the coordinated release.

### 9.1 Prepare Platform Prerequisites

In a protected, disposable OpenShift project set:

- pre-created/labeled app, controller, and Runtime namespaces;
- external PostgreSQL/pgvector and existing application/image-pull Secrets;
- DNS/public hostname and Route permissions;
- OpenShift service CA and cluster monitoring integration;
- platform audit, log retention, image policy, and STIG controls identified as prerequisites.

### 9.2 Install The Signed Release Candidate

- Publish signed immutable commit-SHA image candidates, but no version release tags or chart.
- Verify image signatures, attestations, SBOMs, and digests before install.
- Download the exact packaged chart workflow artifact, record its checksum, server-side apply its
  canonical CRD, and install that local artifact. Do not rebuild or substitute an OCI chart.
- Confirm every Pod is admitted by `restricted-v2` without a custom SCC.
- Confirm arbitrary assigned UIDs, read-only roots, seccomp, dropped capabilities, and disabled Runtime
  ServiceAccount tokens at runtime.

### 9.3 Repeat Full Lifecycle And Security Tests

Repeat Phase 8 against real Routes, re-encrypt/service TLS, real ResourceQuota/LimitRange, OpenShift
NetworkPolicy, owner-reference GC, leader election, both Runtime flavors, HA rollouts, and node
disruption. Include positive and negative `oc auth can-i` assertions for each ServiceAccount.

Validate that sleeping Deployments have no Pods and no CPU/memory workload while retaining only their
CR, Service, and immutable Secret.

### 9.4 Complete Security Evidence

- Attach OpenShift test output and manifest snapshots to the release.
- Complete every component-owned STIG matrix row.
- Record platform-owned prerequisites as verified or explicitly blocking.
- Run vulnerability scans against pulled registry digests, not only local build layers.
- Confirm logs/Events/metrics contain no credentials or high-cardinality identity labels.

### 9.5 Publish And Document

After all gates pass:

- promote the already-qualified image manifests with coordinated version tags and publish the exact
  qualified chart blob as the OCI chart version;
- publish install, prerequisite, upgrade, rollback, uninstall, purge, backup-boundary, and troubleshooting
  documentation;
- document cold starts, idle defaults, capacity Retry, Deployment snapshots, ephemeral filesystems,
  admin egress policy, and failure/redeploy behavior;
- record the tested OpenShift/Kubernetes version matrix.

If no live OpenShift cluster is available, the implementation remains incomplete. Chart rendering and
kind success cannot waive this gate.

### Verification

- `make openshift-smoke` passes against the protected release environment.
- The published chart checksum and image manifest digests exactly match the qualified candidates.
- The release workflow publishes only after the OpenShift environment approves.
- Install and operation docs can reproduce the qualified topology without undocumented permissions or
  secret values.

**Status:** pending

---

## Required Test Layers

| Layer | Owns |
|---|---|
| Go unit tests | builders, names, status helpers, failure classification, clocks/backoff |
| envtest | CRD admission, watches, status conflicts, deterministic reconcile transitions |
| backend unit tests | token parsing, condition mapping, compensation, activity lease, snapshot logic |
| backend integration tests | migrations, row locks, concurrent lifecycle, archive cleanup |
| container tests | arbitrary UID, read-only root, writable mounts, marimo readiness, fetch TLS/errors |
| Helm render/policy tests | profiles, RBAC, NetworkPolicy, TLS, quotas, HA, Secret references |
| kind component smoke | real kubelet/quota/GC/controller with mock internal API |
| kind full-stack smoke | real backend/operator/chart/database fixture/browser lifecycle |
| OpenShift release smoke | SCC, Routes, service TLS, NetworkPolicy, RBAC, HA, signed artifacts |

## Risk Register

1. **Full chart scope is large.** Keep phases independently green and do not begin chart polish before
   API/controller/backend contracts are tested.
2. **OpenShift access can block completion.** Treat cluster credentials/environment as an early project
   dependency, not a final-week concern.
3. **CRD and chart release ordering can drift.** One generated CRD plus pre-upgrade server-side apply is
   mandatory.
4. **Opaque credential verification adds Kubernetes reads.** Start correct and uncached; measure before
   adding a bounded cache.
5. **Activity leases can increase metadata writes.** Coalesce per backend replica, avoid identity metric
   labels, and load-test API-server write volume.
6. **Re-encrypt TLS and certificate renewal are platform-sensitive.** Test service certificate rotation
   and Pod rollout/reload behavior on the supported OpenShift matrix.
7. **Arbitrary UID behavior differs across images and volumes.** Test every image under kind default UID
   and OpenShift-assigned UID before release.
8. **External database and egress differ by deployment.** Make required endpoints explicit values and
   fail chart validation on missing policy inputs.
9. **Notebook source can approach storage/response limits.** Enforce source-fetch size/time limits and
   document the supported maximum.
10. **Quota is multi-dimensional.** Derive smoke pressure from actual CPU/memory/ephemeral requests,
    not only `pods` count.

## Definition Of Done

- Phases 0 through 9 are complete and every phase gate is green.
- `CONTEXT.md`, active design docs, generated CRD, Python/Go conformance tests, Helm chart, and release
  docs agree.
- Idle edit/run Sessions delete all Runtime resources; idle Deployments have zero workload compute and
  wake through a crash-safe explicit request.
- Deterministic failures never loop; infrastructure losses recover with bounded backoff.
- Capacity exhaustion returns machine-readable 429 behavior without background orphan creation.
- Deployment wakes always run the immutable deploy-time source/image snapshot until redeploy.
- Runtime credentials are live-resource-bound, revocable, absent from public routes/logs/status, and
  inaccessible as Kubernetes credentials from Runtime Pods.
- Both Runtime image flavors and all UBI production images pass arbitrary-UID/read-only-root tests,
  vulnerability policy, SBOM, provenance, and signature verification.
- The full chart installs against external PostgreSQL using existing Secrets, three pre-created
  namespaces, HA defaults, re-encrypt TLS, least-privilege RBAC, default-deny networking, and
  chart-controlled Runtime policy.
- The real full-stack kind smoke and mandatory OpenShift release smoke pass end to end.
- The coordinated GHCR image/chart release points only to qualified immutable digests.
