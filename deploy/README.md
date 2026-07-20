# deploy/

Cluster-side manifests for MarimoHub's Kubernetes-native session runtime. These are the
checked-in contract between the backend (`KubeSessionManager`), the operator that reconciles
`MarimoSession` resources, and the platform team that provisions the namespace. Full design and
rationale: `DESIGN.md`, DT-8 ("MarimoSession CRD, controller, and internal source endpoint").

## Contents

- `crd/marimosession.yaml` — the `MarimoSession` CustomResourceDefinition (group
  `marimohub.io`, version `v1alpha1`). Cluster-scoped; apply once per cluster.
- `namespace/namespace.yaml` — the `marimohub-sessions` Namespace that session Pods/Services/
  Secrets live in.
- `namespace/networkpolicy.yaml` — default-deny isolation for session pods: ingress only from
  the API/gateway on 8080, egress only to the API on 8000 plus DNS.
- `namespace/resourcequota.yaml` — namespace-wide pod/CPU/memory ceiling (replaces the old
  in-process `MAX_CONCURRENT_SESSIONS` gate).
- `namespace/limitrange.yaml` — per-container CPU/memory defaults and ceilings.

## Apply order

The CRD must exist before any `MarimoSession` object (including the controller's own
manifests) can be created; the namespace must exist before the namespace-scoped guardrails.

```sh
kubectl apply -f deploy/crd/marimosession.yaml
kubectl apply -f deploy/namespace/namespace.yaml
kubectl apply -f deploy/namespace/networkpolicy.yaml
kubectl apply -f deploy/namespace/resourcequota.yaml
kubectl apply -f deploy/namespace/limitrange.yaml
```

The NetworkPolicy's ingress/egress rules select the API's namespace by its
auto-assigned `kubernetes.io/metadata.name: marimohub` label and pods labelled
`app: marimohub-api` — that namespace and Deployment are provisioned separately (outside this
directory) and are not part of M10's scope.

## What's not here

The `MarimoSession` controller (operator) itself is a separate component, out of
`backend/app` and out of this directory. It is not implemented as part of this repo's backend
plan — only specified. Its implementer should start from DESIGN.md's DT-8 Design section:

- **"Controller component layout (outside `backend/app`)"** — recommended repo layout
  (kubebuilder/Go), RBAC scope.
- **"Reconcile loop (create / ready / fail / idle / wake / delete)"** — the reconciler's
  pseudocode: phase transitions, the Secret precondition, idle-sleep and wake-on-annotation
  handling, deterministic failure behavior.
- **"Per-session manifests (restricted-v2 SCC compliant)"** — the Pod/Service/Secret shapes the
  controller must render from a `MarimoSession` CR, including which fields the backend authors
  (the Secret) versus which the controller owns (Pod, Service).
- **"Service-token auth contract"** — how the per-session bearer token the Secret carries is
  minted, delivered, and verified; the controller only gates Pod creation on the Secret's
  presence and never mints or reads the token's claims.
