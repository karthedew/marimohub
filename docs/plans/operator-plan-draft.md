# marimohub-operator — Implementation Plan

> Superseded draft. Its review produced `/IMPLEMENTATION_PLAN.md`; do not implement this plan as written.

## Context

The backend redesign (complete at `8f80f38`) made the cluster the source of truth for session
runtime state: the API CRUDs `MarimoSession` CRs and `deploy/crd/marimosession.yaml` is the
checked-in contract, but nothing reconciles those CRs into running pods. This plan builds the
missing controller: a Go/kubebuilder operator living at `marimohub-operator/` in this monorepo,
implementing DESIGN.md DT-8's reconcile loop. Verification depth: envtest for the reconcile
logic plus a kind-on-podman cluster smoke driving the full lifecycle with a real (minimal)
marimo runtime image — which this plan also builds, since production needs it anyway.

Authoritative sources, in precedence order:
1. `deploy/crd/marimosession.yaml` — the CRD contract. If generated output disagrees, fix the
   Go types/markers, never the checked-in CRD (risk register #4 of IMPLEMENTATION_PLAN.md).
2. `DESIGN.md` DT-8 — reconcile pseudocode, per-session Pod/Service manifests, RBAC,
   phase-transition invariants, namespace guardrails.
3. `backend/app/services/kube_session_manager.py` — the other side of every string contract
   (label/annotation/secret-name literals, group/version/plural, poll semantics). The operator
   must match it byte-for-byte on shared names.

## Cross-component string contracts (must match the backend exactly)

| Contract | Value |
|---|---|
| Group/version/plural | `marimohub.io/v1alpha1`, `marimosessions` |
| CR name | bare session id (uuid4 for edit/run; deployment id for deploy) |
| Labels | `marimohub.io/notebook`, `marimohub.io/workspace`, `marimohub.io/mode` |
| Wake annotation | `marimohub.io/wake` (backend sets rfc3339; controller consumes AND clears) |
| Activity annotation | `marimohub.io/last-activity` (backend PATCHes, coalesced ≤1/15s; controller promotes to `status.lastActivity` each reconcile — idle checks tolerate ≤15s lag, safe because CRD floors `idleTimeoutSeconds` at 30) |
| Secret | `msess-<id>-env`, keys `MARIMO_TOKEN` + `SESSION_TOKEN` — **authored by the backend, read-only to the controller**; absence → phase `Pending` "awaiting session credentials" |
| Capacity sentinel | on a quota-Forbidden Pod create: `status.message = "QuotaExceeded: <detail>"`, phase stays `Pending`/`Starting` (retriable, never `Failed`) — the backend's poll parses this prefix into its 429 |
| Pod/Service names | `msess-<id>`, Service port 8080→8080, selector `marimohub.io/session: <id>` |
| Finalizer | `marimohub.io/session-cleanup` (gates only a terminal Event; ownerRefs do the GC) |

Phase invariants (DT-8): `Pending → Starting → Ready`; `Ready → Sleeping` only for deploy on
idle (Pod deleted; CR/Secret/Service retained); `Sleeping → Starting` only on wake annotation;
any → `Failed` on deterministic non-zero exit / CrashLoopBackOff with NO auto-recreate;
edit/run idle → the CR itself is deleted. Transient pod loss (eviction/node death) without a
`Failed` verdict reconciles back to `Starting` by re-creating the missing Pod.

## Conventions

- Toolchain present: Go 1.26.1, kind v0.27 (podman provider), kubectl 1.35. Install
  `kubebuilder` via `brew install kubebuilder` in O1; envtest binaries via
  `setup-envtest` (Makefile target).
- Module path `github.com/karthedew/marimohub/marimohub-operator`; layout per DT-8's sketch
  (`api/v1alpha1/`, `internal/controller/`, `internal/session/`, `config/{crd,rbac,samples,manager}`).
- `FMT` = `gofmt -l .` empty; `VET` = `go vet ./...` clean; `TEST` = `go test ./...` green.
  All from `marimohub-operator/`. (golangci-lint optional; do not add config churn for it.)
- Backend suite must stay untouched: no file outside `marimohub-operator/`, `images/`, and
  `deploy/README.md` may change in this plan.

---

## O1 — Scaffold + API types matching the checked-in CRD

**Intent**: kubebuilder project whose generated CRD is semantically identical to the contract.

- `brew install kubebuilder`; `kubebuilder init --domain marimohub.io`; `kubebuilder create api
  --group '' --version v1alpha1 --kind MarimoSession` (group handling so the served group is
  exactly `marimohub.io` — match the contract, adjust domain/group flags accordingly).
- Write `api/v1alpha1/marimosession_types.go` with markers reproducing the contract: spec
  required `[notebookId, workspaceId, mode]`; `creatorId` nullable; `mode` enum edit/run/deploy;
  `image`/`baseUrl` strings with the contract's descriptions; `idleTimeoutSeconds` default 600
  minimum 30; `resources` as `corev1.ResourceRequirements` with preserve-unknown semantics
  (or `runtime.RawExtension`-style passthrough — whichever controller-gen renders to match);
  full status block (phase enum incl. `Pending`, serviceName, podName, lastActivity date-time,
  message, observedGeneration); status subresource; shortName `msess`; the five printer columns.
- `make manifests` and reconcile the generated CRD against `deploy/crd/marimosession.yaml`
  with a YAML-normalizing comparison (key order/format-insensitive). Iterate markers until the
  only differences are benign (controller-gen boilerplate annotations). If controller-gen
  cannot express something the contract requires, STOP and report — do not change the contract.
- **Gates**: `go build ./...`; `FMT`; `VET`; the normalized CRD diff is empty/benign-only
  (check the comparison script/command into `hack/`).
- **Status**: pending

## O2 — Session builders (`internal/session/`)

**Intent**: pure, unit-testable construction of the per-session resources per DT-8's manifest
sketch.

- `pod.go` — `BuildPod(cr, defaultImage)`: name/labels/ownerRef; `automountServiceAccountToken:
  false`; pod security context runAsNonRoot **without** a pinned UID (SCC assigns it),
  seccomp RuntimeDefault; init container `fetch-source` (image = spec.image else defaultImage;
  `curl -sf --retry 5 --retry-connrefused -H "Authorization: Bearer $SESSION_TOKEN"
  "$API_URL/api/internal/notebooks/$NOTEBOOK_ID/source" -o /work/notebook.py`; env `API_URL`
  (manager flag, default `http://marimohub-api.marimohub.svc:8000`) + `NOTEBOOK_ID`; envFrom
  the session Secret; hardened securityContext: no-priv-escalation, drop ALL, RO rootfs); main
  container `marimo run|edit /work/notebook.py --host=0.0.0.0 --port=8080
  --token-password=$(MARIMO_TOKEN) --base-url=$(BASE_URL) --headless` with `BASE_URL` from
  spec.baseUrl, envFrom Secret, **TCP readiness probe :8080** (period 2, failureThreshold 3 —
  marimo's HTTP routes are token-gated, so httpGet would read 403 as not-ready), resources from
  spec, same hardened securityContext; single `emptyDir` volume at `/work`.
- `service.go` — `BuildService(cr)`: `msess-<id>`, selector on the session label, 8080→8080,
  ownerRef.
- `secret.go` — precondition lookup only (never create/mutate).
- `names.go` (or similar) — every shared string literal from the contracts table in one place,
  with a test that cross-checks the values against the table (guards drift; the backend's
  literals were read and matched during planning — re-verify against
  `backend/app/services/kube_session_manager.py` during implementation).
- **Gates**: table-driven unit tests pinning every SCC field, the init command/env, the TCP
  probe, ownerRefs, image default fallback (spec.image empty → defaultImage), resources
  passthrough, and mode→args (run vs edit; deploy runs `marimo run`); `FMT`; `VET`; `TEST`.
- **Status**: pending

## O3 — Reconcile loop + envtest

**Intent**: DT-8's pseudocode, faithfully, with the capacity sentinel.

- `internal/controller/marimosession_controller.go`:
  deletionTimestamp → emit terminal Event, remove finalizer, done; ensure finalizer (requeue);
  `status.observedGeneration = generation`; promote `marimohub.io/last-activity` →
  `status.lastActivity`; Secret precondition (absent → `Pending` + message, requeue 2s); ensure
  Service + `status.serviceName`; wake gate per pseudocode (pod nil + deploy + Sleeping + no
  wake → stay asleep, NO requeue; wake present → clear annotation, create Pod, `Starting`,
  requeue 2s); Pod create rejected with a quota `Forbidden` → set the `QuotaExceeded:` sentinel
  in `status.message`, keep phase, requeue (retriable); pod Ready → `Ready`; terminated
  non-zero or CrashLoopBackOff → `Failed` + message tail, done (no recreate); else `Starting`
  requeue 2s; idle (`now - lastActivity > idleTimeoutSeconds`, only meaningful when Ready) →
  deploy: delete Pod, `Sleeping`, clear podName; edit/run: delete the CR; else requeue 30s.
  Missing-not-Failed pod (eviction) → recreate path falls out of the pseudocode naturally.
- Manager flags: `--default-session-image`, `--api-url`, namespace to watch
  (`marimohub-sessions`), leader election.
- envtest suite (`setup-envtest` Makefile target; no kubelet, so pod phase/readiness is set by
  the tests): secret-absent → Pending; full create → Service+Pod, Starting; readiness patched →
  Ready; terminated non-zero → Failed + no recreate; activity-annotation promotion; idle deploy
  → Pod deleted + Sleeping (drive with a short idleTimeoutSeconds ≥30 and backdated
  lastActivity); wake annotation → Pod recreated + annotation cleared + Starting; edit idle →
  CR deleted; delete → finalizer removed, ownerRef'd children present until GC (envtest runs no
  GC controller — assert ownerRefs instead of actual cascade). Quota-Forbidden sentinel: envtest
  runs no quota controller, so pin it with a controller-runtime fake/interceptor client
  injecting `Forbidden` on Pod create — assert the exact `QuotaExceeded:` message prefix and
  the retained phase.
- **Gates**: `FMT`; `VET`; `TEST` (envtest included); the sentinel-prefix test exists and
  matches the backend's parse (`QuotaExceeded:` — read the backend constant, assert equality).
- **Status**: pending

## O4 — RBAC, manager manifests, operator image

**Intent**: the deployable artifact.

- `config/rbac/`: ServiceAccount (namespace `marimohub-controller`), namespaced Role in
  `marimohub-sessions` exactly per DT-8 — marimosessions + status (all verbs); pods, services
  (get/list/watch/create/delete); secrets (get/list/watch — NO write); events (create) — plus
  RoleBinding. Leader-election Role in the controller namespace. No cluster-scoped grants.
- `config/manager/`: Deployment (1 replica, leader election on, flags from O3), the
  `marimohub-controller` namespace manifest.
- `Containerfile` (multi-stage: golang build → distroless static) buildable with podman.
- `marimohub-operator/README.md`: build/run/deploy instructions + pointer to DESIGN.md DT-8;
  update `deploy/README.md` to note the operator now exists and where.
- **Gates**: `kubectl apply --dry-run=client` accepts every manifest; `podman build` of the
  Containerfile succeeds; `FMT`/`VET`/`TEST` still green.
- **Status**: pending

## O5 — Runtime image + kind smoke

**Intent**: prove the real lifecycle on a real (local) cluster, and produce the session runtime
image production needs anyway.

- **Create `images/marimo-runtime/Containerfile`**: `python:3.12-slim` + `pip install marimo` +
  curl, non-root-friendly (no fixed UID assumptions, works with a read-only rootfs given the
  `/work` emptyDir). Build with podman; `kind load` it into the cluster.
- **Mock API**: a one-Deployment stub in namespace `marimohub` — Service named `marimohub-api`
  on 8000, pod labeled `app: marimohub-api`, serving a static valid `notebook.py` at
  `/api/internal/notebooks/<id>/source` (auth header ignored — the smoke tests the operator,
  not the backend). A `python -m http.server`-style pod with the right path layout suffices.
  Check smoke assets into `marimohub-operator/hack/smoke/`.
- **Smoke sequence** (scripted in `hack/smoke/smoke.sh`, teardown included; kind via
  `KIND_EXPERIMENTAL_PROVIDER=podman kind create cluster` — note the podman machine has
  ~3.7 GB, sufficient for a single-node kind):
  1. Apply `deploy/crd/` + `deploy/namespace/` + the mock API; run the operator locally
     (`make run`) against the kubeconfig.
  2. Create a `MarimoSession` (mode deploy, `idleTimeoutSeconds: 30`, image = the runtime
     image) WITHOUT its Secret → observe `Pending` "awaiting session credentials".
  3. Create `msess-<id>-env` (dummy tokens) → observe `Starting` → init fetch from mock →
     `Ready`; Service resolves; `kubectl port-forward` + curl proves marimo answers on 8080.
  4. Idle 30s+ with no activity → Pod deleted, phase `Sleeping`, CR/Secret/Service intact.
  5. Patch `marimohub.io/wake` → Pod recreated, annotation cleared, back to `Ready`.
  6. Quota: apply a `pods: 1`-tight ResourceQuota variant, create a second session with its
     Secret → `status.message` gains the `QuotaExceeded:` prefix while phase stays retriable;
     delete the first session → second proceeds to `Ready` (sentinel cleared on success).
  7. Create an edit-mode session, let it idle → the CR itself is deleted.
  8. Delete a live session CR → Pod/Service/Secret GC via ownerRefs (real GC exists in kind).
  9. Failure leg: a session whose image/entry deterministically exits non-zero → `Failed` with
     message, and NO recreate loop.
  - Known non-goals of the smoke: NetworkPolicy semantics (kind's default CNI doesn't enforce
    them) and the backend's real internal API (mocked). Record both in the smoke script header.
- **Gates**: `smoke.sh` runs clean end-to-end on a fresh kind cluster; every phase transition
  and the sentinel observed; teardown leaves no cluster.
- **Status**: pending

---

## Risks / accepted gaps

1. **envtest has no kubelet/GC/quota controllers** — pod phases are test-set, GC asserted via
   ownerRefs, quota via injected Forbidden. The kind smoke covers all three for real.
2. **NetworkPolicy unenforced in the smoke** (kindnet) — the policy manifests are validated
   syntactically and by real-cluster apply, not semantically. OpenShift/prod validation stays
   open.
3. **CRD expressiveness** — if controller-gen can't reproduce a contract detail (e.g., the
   `resources` preserve-unknown shape), the milestone stops and reports rather than drifting
   the contract.
4. **Backend↔operator drift** — mitigated by the O2 string-constants test and the O3
   sentinel-prefix equality check against backend source; a shared-constants extraction is
   deliberately not attempted across languages.
5. **kind-on-podman resources** — 3.7 GB machine is adequate for one-node kind + a couple of
   session pods with the LimitRange defaults; the smoke uses small resource requests.

## Definition of done

- O1–O5 `complete`; `gofmt`/`go vet`/`go test ./...` clean from `marimohub-operator/`.
- Generated CRD ≡ `deploy/crd/marimosession.yaml` (normalized comparison checked into `hack/`).
- The kind smoke passes every step incl. the `QuotaExceeded:` sentinel and the no-recreate
  `Failed` leg.
- Operator image and marimo runtime image both build with podman.
- Nothing outside `marimohub-operator/`, `images/`, and `deploy/README.md` changed; backend
  suite still green (`cd backend && uv run pytest -q`).
