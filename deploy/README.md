# deploy/

Cluster-side manifests for MarimoHub's Kubernetes-native session Runtime. These are the
checked-in contract between the backend, the operator that reconciles `MarimoSession` resources,
and the platform team that provisions the namespace. `/IMPLEMENTATION_PLAN.md` is the
authoritative source for the Runtime contract, reconcile behavior, and credential model;
`DESIGN.md` DT-8 is retained only as historical context for the design that preceded it.

## Contents

- `crd/marimosession.yaml` — the `MarimoSession` CustomResourceDefinition (group
  `marimohub.io`, version `v1alpha1`). Cluster-scoped; apply once per cluster. Generated:
  `make generate` produces the canonical CRD from `marimohub-operator`'s Go types and copies it
  here byte-for-byte, and `make verify-generated` fails on drift. Do not hand-edit this file.
- `../marimohub-operator/config/policy/marimosession_label_identity.yaml` — a
  `ValidatingAdmissionPolicy`/`ValidatingAdmissionPolicyBinding` pair enforcing that the
  `marimohub.io/notebook`, `marimohub.io/workspace`, and `marimohub.io/mode` labels agree with the
  identically named spec fields. It is not CRD schema CEL because the CRD validation CEL
  environment cannot see `metadata.labels`; apply it alongside the CRD.
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
kubectl apply -f marimohub-operator/config/policy/marimosession_label_identity.yaml
kubectl apply -f deploy/namespace/namespace.yaml
kubectl apply -f deploy/namespace/networkpolicy.yaml
kubectl apply -f deploy/namespace/resourcequota.yaml
kubectl apply -f deploy/namespace/limitrange.yaml
```

The NetworkPolicy's ingress/egress rules select the API's namespace by its
auto-assigned `kubernetes.io/metadata.name: marimohub` label and pods labelled
`app: marimohub-api` — that namespace and Deployment are provisioned separately, outside this
directory.

## What's not here

The `MarimoSession` controller (operator) lives in `marimohub-operator/`, out of `backend/app`
and out of this directory. `/IMPLEMENTATION_PLAN.md` specifies it in full:

- **Phase 1** (done) — the generated CRD, typed spec/status, and CEL/`ValidatingAdmissionPolicy`
  validation that replace the handwritten schema this directory used to carry.
- **Phase 2** — the Pod/Service builders: security context, fetcher init container, probes,
  labels, and the immutable `RUNTIME_CREDENTIAL` Secret projection.
- **Phase 3** — the reconcile state machine: Condition-based phase transitions, idle/wake
  handling, capacity and failure classification, all without a finalizer.
- **Phase 5** — the credential model: an opaque, live-resource-bound `RUNTIME_CREDENTIAL` verified
  by a separate internal API, replacing any expiring signed token.
