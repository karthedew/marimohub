# MarimoSession CRD & Controller Design

Replaces `process_manager.py`'s subprocess model with a pod-per-session architecture where the cluster is the source of truth. The backend stops managing processes and instead CRUDs `MarimoSession` custom resources; a controller reconciles them into Pods/Services/Secrets.

## Why a CRD (vs. backend creating Pods directly)

- Crash-safe: session state lives in etcd, not an in-process dict. Any API replica can route; the API becomes horizontally scalable.
- The idle reaper, restart-on-crash, and orphan cleanup become reconcile logic instead of asyncio background tasks.
- `spawn_deployment`'s lock dance disappears — CR name = deployment id, and `Create` on an existing name is a natural idempotency barrier.
- Clean audit/debug surface: `oc get marimosessions`.

## CRD

```yaml
apiVersion: apiextensions.k8s.io/v1
kind: CustomResourceDefinition
metadata:
  name: marimosessions.marimohub.io
spec:
  group: marimohub.io
  scope: Namespaced
  names:
    kind: MarimoSession
    plural: marimosessions
    singular: marimosession
    shortNames: [msess]
  versions:
    - name: v1alpha1
      served: true
      storage: true
      subresources:
        status: {}
      additionalPrinterColumns:
        - name: Mode
          type: string
          jsonPath: .spec.mode
        - name: Phase
          type: string
          jsonPath: .status.phase
        - name: Notebook
          type: string
          jsonPath: .spec.notebookId
        - name: Age
          type: date
          jsonPath: .metadata.creationTimestamp
      schema:
        openAPIV3Schema:
          type: object
          required: [spec]
          properties:
            spec:
              type: object
              required: [notebookId, mode]
              properties:
                notebookId:
                  type: string
                  format: uuid
                workspaceId:
                  type: string
                  format: uuid
                mode:
                  type: string
                  enum: [edit, run, deploy]
                creatorId:
                  type: string
                  format: uuid
                  nullable: true
                image:
                  type: string
                  description: >
                    Marimo runtime image. Defaults from controller config;
                    per-workspace override later.
                baseUrl:
                  type: string
                  description: >
                    Proxy path prefix marimo is served under
                    (e.g. /api/deployments/{slug}). Maps to --base-url.
                idleTimeoutSeconds:
                  type: integer
                  default: 600
                resources:
                  type: object
                  x-kubernetes-preserve-unknown-fields: true
                  description: corev1.ResourceRequirements passthrough
            status:
              type: object
              properties:
                phase:
                  type: string
                  enum: [Pending, Starting, Ready, Sleeping, Failed]
                serviceName:
                  type: string
                lastActivity:
                  type: string
                  format: date-time
                message:
                  type: string
                observedGeneration:
                  type: integer
```

Example CR (deployment session; CR name = deployment UUID for idempotency):

```yaml
apiVersion: marimohub.io/v1alpha1
kind: MarimoSession
metadata:
  name: 7f3e9a2c-...            # deployment id (or uuid4 for edit/run)
  namespace: marimohub-sessions
  labels:
    marimohub.io/notebook: 4c1d...
    marimohub.io/workspace: 9ab2...
    marimohub.io/mode: deploy
spec:
  notebookId: 4c1d...
  workspaceId: 9ab2...
  mode: deploy
  baseUrl: /api/deployments/plasma-dashboard
  idleTimeoutSeconds: 600
  resources:
    requests: { cpu: 250m, memory: 512Mi }
    limits:   { cpu: "1",  memory: 2Gi }
```

## What the controller creates per session

One **Secret** (marimo access token, replacing `secrets.token_urlsafe` in the tempdir flow), one **Pod**, one **Service**.

```yaml
apiVersion: v1
kind: Pod
metadata:
  name: msess-7f3e9a2c
  labels:
    app: marimo-session
    marimohub.io/session: 7f3e9a2c-...
  ownerReferences: [ MarimoSession 7f3e9a2c-... ]   # GC for free
spec:
  automountServiceAccountToken: false
  securityContext:
    runAsNonRoot: true            # restricted-v2 SCC assigns uid; don't pin one
    seccompProfile: { type: RuntimeDefault }
  initContainers:
    - name: fetch-source
      image: <marimo-runtime>
      command: ["sh", "-c"]
      args:
        - >
          curl -sf -H "Authorization: Bearer $(BOOTSTRAP_TOKEN)"
          "$API_URL/api/internal/notebooks/$(NOTEBOOK_ID)/source"
          -o /work/notebook.py
      env: [...]
      volumeMounts: [{ name: work, mountPath: /work }]
  containers:
    - name: marimo
      image: <marimo-runtime>
      args:
        - marimo
        - run                      # or: edit
        - /work/notebook.py
        - --host=0.0.0.0
        - --port=8080
        - --token-password=$(MARIMO_TOKEN)
        - --base-url=$(BASE_URL)
        - --headless
      envFrom:
        - secretRef: { name: msess-7f3e9a2c-token }
      readinessProbe:
        httpGet: { path: <baseUrl>/health, port: 8080 }
        periodSeconds: 2
      resources: { ... from spec ... }
      securityContext:
        allowPrivilegeEscalation: false
        capabilities: { drop: [ALL] }
      volumeMounts: [{ name: work, mountPath: /work }]
  volumes:
    - name: work
      emptyDir: {}
---
apiVersion: v1
kind: Service
metadata:
  name: msess-7f3e9a2c
spec:
  selector: { marimohub.io/session: 7f3e9a2c-... }
  ports: [{ port: 8080, targetPort: 8080 }]
```

The port-range allocator, `_reserved_ports`, and `MARIMO_PORT_RANGE` all disappear: every session listens on 8080 behind its own Service, and the gateway proxies to `http://msess-{id}.marimohub-sessions.svc:8080`.

## Reconcile loop

```
Reconcile(session):
  if deletionTimestamp set:
      # owned Pod/Service/Secret GC'd via ownerRefs; remove finalizer
      return

  ensure Secret exists (generate token once)
  ensure Service exists

  pod = get Pod
  if pod missing:
      if spec.mode == deploy and status.phase == Sleeping and not wakeRequested:
          return                      # stay asleep until API bumps annotation
      create Pod; status.phase = Starting; return requeue(2s)

  if pod Ready:            status.phase = Ready; status.serviceName = ...
  if pod Failed/CrashLoop: status.phase = Failed; status.message = tail of logs
                           # NotebookStartupError equivalent: don't retry
                           # deterministic failures (exit before ready)

  # idle handling
  idle = now - status.lastActivity
  if idle > spec.idleTimeoutSeconds:
      if spec.mode == deploy:
          delete Pod; status.phase = Sleeping     # CR persists; wake on demand
      else:
          delete MarimoSession                    # edit/run sessions are ephemeral

  requeue(30s)
```

Activity tracking: the existing gateway (`marimo_proxy.py`, kept) PATCHes `status.lastActivity` — or cheaper, an annotation the controller copies — on proxied traffic, replacing `mark_active()`. Wake-on-request: gateway sees `phase: Sleeping`, sets `marimohub.io/wake: "now"` annotation, polls status until `Ready` (bounded by the same 30s ready-timeout semantics you have today), then proxies.

Controller stack: **kubebuilder/controller-runtime (Go)** is the natural fit given your Go work, or **kopf (Python)** to stay monorepo-simple. Given Cosma and your prior marimo-operator experience, Go + kubebuilder is my recommendation — you also get the CRD schema generated from types.

## Backend changes

`ProcessManager` → `KubeSessionManager` implementing the same seam:

| today | after |
|---|---|
| `spawn(notebook, mode)` → subprocess | `create(MarimoSession)`; watch status until Ready/Failed |
| `_sessions: dict` | `list/get MarimoSession` by label (no local state) |
| `SessionTarget(http_base_url, ws_base_url, token)` | built from `status.serviceName` + token Secret |
| `IdleDeploymentReaper` | deleted — controller owns idleness |
| `mark_running_deployments_sleeping` on boot | deleted — state survives API restarts |
| `deployments.port` column | dropped (see schema doc) |

Add one internal endpoint: `GET /api/internal/notebooks/{id}/source` (service-account-token auth, NetworkPolicy-restricted) for the init container, replacing the tempdir write.

Deployment status in Postgres becomes a read-model cache updated from a controller watch (or simply read through to the CR — start with read-through, add the cache if listing gets hot).

## OpenShift specifics

- Sessions run in a dedicated namespace (`marimohub-sessions`) under the default `restricted-v2` SCC — the pod spec above is already compliant (non-root, no caps, no privilege escalation, RuntimeDefault seccomp).
- **NetworkPolicy**: session pods accept ingress only from the API/gateway pods; egress limited to the API (source fetch) + your internal PyPI if notebooks install deps. No Route per session — only the gateway gets a Route.
- ResourceQuota + LimitRange on the sessions namespace replaces `MAX_CONCURRENT_SESSIONS` (or keep a soft cap in the API for nicer 429s — your `SessionCapacityError` maps to quota-exceeded).
- Image: one marimo runtime image per marimo version; the per-user uv-venv question from your earlier MarimoHub design maps to per-workspace images or an image + shared PVC later — out of scope for v1alpha1.

## Migration path

1. Ship `KubeSessionManager` behind a `SESSION_BACKEND=subprocess|kube` setting (the `command_factory` seam shows this codebase already likes injectable backends).
2. Controller + CRD deploy first; run deployments through kube while edit/run stays subprocess, then flip everything.
3. Delete `process_manager.py` (609 lines) and `deployment_lifecycle.py` once stable.
