/*
Copyright 2026.

Licensed under the Apache License, Version 2.0 (the "License");
you may not use this file except in compliance with the License.
You may obtain a copy of the License at

    http://www.apache.org/licenses/LICENSE-2.0

Unless required by applicable law or agreed to in writing, software
distributed under the License is distributed on an "AS IS" BASIS,
WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
See the License for the specific language governing permissions and
limitations under the License.
*/

package v1alpha1

import (
	corev1 "k8s.io/api/core/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/runtime"
)

// RuntimeMode selects which marimo invocation a Runtime Pod runs and, through
// the CEL rule on MarimoSessionSpec, whether deploymentRevision is required.
// +kubebuilder:validation:Enum=edit;run;deploy
type RuntimeMode string

const (
	// RuntimeModeEdit serves an interactive editing session for one Notebook.
	RuntimeModeEdit RuntimeMode = "edit"
	// RuntimeModeRun serves a read-only, non-editable execution of one Notebook.
	RuntimeModeRun RuntimeMode = "run"
	// RuntimeModeDeploy serves a Deployment's immutable deploy-time snapshot and
	// may sleep between requests instead of being deleted when idle.
	RuntimeModeDeploy RuntimeMode = "deploy"
)

// RuntimePhase is the human-readable summary of a MarimoSession's lifecycle.
// Automation must key off Conditions, never off Phase or status.message text.
// +kubebuilder:validation:Enum=Pending;Starting;Ready;Sleeping;Failed
type RuntimePhase string

const (
	// RuntimePhasePending is set before a valid credential Secret is observed.
	RuntimePhasePending RuntimePhase = "Pending"
	// RuntimePhaseStarting covers every point between a credentialed attempt
	// and the Pod passing authenticated readiness.
	RuntimePhaseStarting RuntimePhase = "Starting"
	// RuntimePhaseReady means the Pod is currently routable.
	RuntimePhaseReady RuntimePhase = "Ready"
	// RuntimePhaseSleeping applies only to deploy mode: the Pod is deleted for
	// idleness while the CR, Service, and Secret are retained.
	RuntimePhaseSleeping RuntimePhase = "Sleeping"
	// RuntimePhaseFailed is terminal until an authorized redeploy or explicit
	// retry; the operator never recreates a Pod from this phase on its own.
	RuntimePhaseFailed RuntimePhase = "Failed"
)

// Condition types the controller sets on MarimoSession.status.conditions.
// These, not Phase or status.message, are the contract external callers key
// reconciliation and routing decisions off of.
const (
	// ConditionTypeCredentialsAvailable reports whether the owned credential
	// Secret currently satisfies the operator's precondition checks.
	ConditionTypeCredentialsAvailable = "CredentialsAvailable"
	// ConditionTypeCapacityAvailable reports whether the last Pod-create
	// attempt was admitted rather than rejected by ResourceQuota.
	ConditionTypeCapacityAvailable = "CapacityAvailable"
	// ConditionTypeReady mirrors Pod authenticated readiness and gates routing.
	ConditionTypeReady = "Ready"
	// ConditionTypeReconciled reports whether the last reconcile attempt for
	// the current generation completed without an unresolved error.
	ConditionTypeReconciled = "Reconciled"
)

// Stable condition Reasons. Callers may match on these; they must not change
// meaning once released, and status.message must never be parsed instead.
// This set is deliberately open-ended: new Reasons may be added for a
// classification that does not yet have a stable name, as long as no
// existing Reason ever changes what it means.
const (
	ReasonCredentialsMissing = "CredentialsMissing"
	ReasonCredentialsInvalid = "CredentialsInvalid"
	ReasonQuotaExceeded      = "QuotaExceeded"
	ReasonResourceConflict   = "ResourceConflict"
	ReasonInvalidSpec        = "InvalidSpec"
	ReasonImageUnavailable   = "ImageUnavailable"
	ReasonSourceUnavailable  = "SourceUnavailable"
	ReasonRuntimeExited      = "RuntimeExited"
	ReasonOOMKilled          = "OOMKilled"
	ReasonInfrastructureLost = "InfrastructureLost"
	ReasonReconcileSucceeded = "ReconcileSucceeded"

	// ReasonPlatformDenied covers a Pod-create Forbidden response the
	// controller could not confirm as a ResourceQuota rejection (RBAC, SCC,
	// LimitRange, Pod Security admission, or an arbitrary validating
	// webhook). It is distinct from ReasonQuotaExceeded on purpose: only a
	// confirmed quota rejection is retried by a fresh wake request or a
	// resources change, while a platform/configuration denial fails the
	// Runtime outright because no amount of waiting or retrying resolves it
	// without an administrator changing cluster policy.
	ReasonPlatformDenied = "PlatformDenied"

	// ReasonStarting marks the Ready condition False while a Pod attempt is
	// in flight (initial start, wake, retry, or resources replacement). It
	// carries no failure meaning by itself; it exists so a caller can tell
	// "actively starting" apart from every other reason Ready is False.
	ReasonStarting = "Starting"

	// ReasonUnhealthy marks the Ready condition False for a Pod that was
	// previously passing its authenticated readiness check and has now
	// started failing it while still Running. It is a waiting state, not a
	// terminal one: if the Pod recovers within the unhealthy grace period
	// the condition returns to True; if the grace period elapses first, the
	// Runtime fails with ReasonRuntimeExited instead.
	ReasonUnhealthy = "Unhealthy"
)

// ImageDigestPattern requires a fully resolved, registry-agnostic image
// reference pinned by digest. It intentionally rejects a bare tag: a tag
// alone is mutable at the registry, and the operator's Pod spec and the
// backend's recorded Deployment snapshot must reference the exact bytes that
// were validated. The manager's own --fetcher-image startup flag is checked
// against this same pattern (see internal/managerconfig) so both the CRD
// admission rule and the manager's fail-fast startup check agree on what
// "digest-qualified" means; keep the Pattern marker on Spec.Image below in
// sync with this string if either changes.
const ImageDigestPattern = `^[a-z0-9]+(?:[._-][a-z0-9]+)*(?::[0-9]+)?(?:/[a-z0-9]+(?:[._-][a-z0-9]+)*)*@sha256:[0-9a-f]{64}$`

// MarimoSessionSpec is the durable launch intent for one Runtime. The public
// backend is the sole writer; the operator only ever reads it. Every field
// here except idleTimeoutSeconds and resources identifies *which* workload to
// run rather than *how much of it to run*, which is exactly the split the
// CEL immutability rules below encode: changing identity or launch fields
// means a different Runtime, so it must go through delete-and-recreate, while
// idleTimeoutSeconds and resources may be tuned on the live object.
// +kubebuilder:validation:XValidation:rule="self.mode == 'deploy' ? has(self.deploymentRevision) : !has(self.deploymentRevision)",message="deploymentRevision is required when mode is deploy and must be omitted otherwise"
// +kubebuilder:validation:XValidation:rule="has(oldSelf.creatorId) == has(self.creatorId) && (!has(self.creatorId) || self.creatorId == oldSelf.creatorId)",message="creatorId is immutable after creation"
type MarimoSessionSpec struct {
	// notebookId is the Notebook this Runtime serves. Immutable: a different
	// Notebook is a different Runtime.
	// +required
	// +kubebuilder:validation:Format=uuid
	// +kubebuilder:validation:XValidation:rule="self == oldSelf",message="notebookId is immutable after creation"
	NotebookID string `json:"notebookId"`

	// workspaceId is the owning Workspace, kept redundantly with notebookId so
	// the operator and admission rules never need a Notebook lookup to
	// enforce namespace-wide policy or Workspace-scoped cleanup. Immutable.
	// +required
	// +kubebuilder:validation:Format=uuid
	// +kubebuilder:validation:XValidation:rule="self == oldSelf",message="workspaceId is immutable after creation"
	WorkspaceID string `json:"workspaceId"`

	// creatorId attributes an edit/run Session to the User who started it.
	// Nullable and absent for deploy mode and for Sessions started without a
	// specific actor. Immutable, but its immutability rule lives on the
	// struct (see the XValidation marker above), not here: a field-level
	// "self == oldSelf" rule is only reachable once the field already has a
	// value in both the old and new object, so it silently permits the one
	// transition that actually matters for an optional field -- going from
	// absent to present on update. The struct-level rule uses has() to
	// compare presence as well as value.
	// +optional
	// +nullable
	// +kubebuilder:validation:Format=uuid
	CreatorID *string `json:"creatorId,omitempty"`

	// mode selects edit, run, or deploy behavior. Immutable: switching modes
	// is a new Runtime, never an in-place transition.
	// +required
	// +kubebuilder:validation:XValidation:rule="self == oldSelf",message="mode is immutable after creation"
	Mode RuntimeMode `json:"mode"`

	// image is a fully resolved, digest-qualified Runtime image reference.
	// Release charts always supply a digest; a mutable tag is rejected so the
	// Pod the operator creates can never silently diverge from what was
	// validated at admission time. Immutable.
	// +required
	// +kubebuilder:validation:MaxLength=512
	// +kubebuilder:validation:Pattern=`^[a-z0-9]+(?:[._-][a-z0-9]+)*(?::[0-9]+)?(?:/[a-z0-9]+(?:[._-][a-z0-9]+)*)*@sha256:[0-9a-f]{64}$`
	// +kubebuilder:validation:XValidation:rule="self == oldSelf",message="image is immutable after creation"
	Image string `json:"image"`

	// baseUrl is the proxy path prefix marimo is served under (maps to
	// --base-url). It must be a bounded, rooted path with no query string,
	// fragment, or parent-directory traversal, since it becomes part of the
	// externally routed URL. Immutable.
	// +required
	// +kubebuilder:validation:MaxLength=256
	// +kubebuilder:validation:Pattern=`^/[A-Za-z0-9._~!$&'()*+,;=:@/-]*$`
	// +kubebuilder:validation:XValidation:rule="!self.contains('..')",message="baseUrl must not contain parent-directory traversal"
	// +kubebuilder:validation:XValidation:rule="self == oldSelf",message="baseUrl is immutable after creation"
	BaseURL string `json:"baseUrl"`

	// deploymentRevision binds a deploy-mode Runtime to the exact backend
	// Deployment snapshot it must fetch; the source-fetcher and internal API
	// use it to reject a stale or since-redeployed request. Required only for
	// deploy mode (enforced by the XValidation rule on the struct) and
	// immutable once set: a new revision is always a new Runtime.
	// +optional
	// +kubebuilder:validation:Minimum=0
	// +kubebuilder:validation:XValidation:rule="self == oldSelf",message="deploymentRevision is immutable after creation"
	DeploymentRevision *int64 `json:"deploymentRevision,omitempty"`

	// idleTimeoutSeconds overrides the operator's per-mode chart default. It
	// is a minimum idle duration, not an exact shutdown deadline, and is the
	// one launch-adjacent field that is deliberately mutable: tightening or
	// loosening the idle budget does not change what Runtime is running, so
	// it never triggers Pod replacement. 30s is the floor because it must
	// stay comfortably above the backend's activity-annotation coalescing
	// window, or throttled activity signals could be mistaken for idleness.
	// +optional
	// +kubebuilder:validation:Minimum=30
	IdleTimeoutSeconds *int32 `json:"idleTimeoutSeconds,omitempty"`

	// resources overrides the operator's chart-wide Runtime resource
	// defaults. Deliberately mutable: the operator replaces the Pod when this
	// changes (a resize needs a new container, not an in-place patch), and
	// status.observedGeneration only advances once that replacement Pod is
	// fully reconciled.
	// +optional
	Resources *corev1.ResourceRequirements `json:"resources,omitempty"`
}

// MarimoSessionStatus is owned exclusively by the operator. The public and
// internal backends and end users only ever read it.
type MarimoSessionStatus struct {
	// phase is the human-readable lifecycle summary. It exists for kubectl
	// and dashboards; Conditions are the machine-readable contract.
	// +optional
	Phase RuntimePhase `json:"phase,omitempty"`

	// conditions carries the machine-readable Reasons callers key behavior
	// off of. Merged by type so repeated status patches never duplicate an
	// entry.
	// +optional
	// +patchMergeKey=type
	// +patchStrategy=merge
	// +listType=map
	// +listMapKey=type
	Conditions []metav1.Condition `json:"conditions,omitempty" patchStrategy:"merge" patchMergeKey:"type"`

	// podName is the current Runtime Pod's name, cleared before the Pod is
	// deleted for an idle deploy sleep so a crash between those writes can
	// never be mistaken for a recoverable infrastructure loss.
	// +optional
	PodName string `json:"podName,omitempty"`

	// serviceName is the current Runtime Service's name.
	// +optional
	ServiceName string `json:"serviceName,omitempty"`

	// lastActivity is the controller's own clock reading of the last time
	// this Runtime was known active; it is never taken from client-supplied
	// timestamps.
	// +optional
	LastActivity *metav1.Time `json:"lastActivity,omitempty"`

	// observedWakeRequest is the last marimohub.io/wake-request annotation
	// value the controller has acknowledged, so a repeated reconcile of the
	// same request never starts a second Pod attempt.
	// +optional
	ObservedWakeRequest string `json:"observedWakeRequest,omitempty"`

	// observedActivity is the last marimohub.io/activity annotation value the
	// controller has acknowledged.
	// +optional
	ObservedActivity string `json:"observedActivity,omitempty"`

	// observedGeneration advances only after metadata.generation's spec has
	// been fully reconciled, so callers can detect an in-flight resources
	// replacement rather than reading a stale Ready condition.
	// +optional
	ObservedGeneration int64 `json:"observedGeneration,omitempty"`

	// attempt counts start attempts for the current generation; it is
	// consumed (not retried) by a quota rejection until an unobserved wake
	// request or a resources change starts a new attempt.
	// +optional
	Attempt int32 `json:"attempt,omitempty"`

	// podTemplateHash is a content hash of the desired Pod this controller
	// last built for the current attempt. It exists to distinguish a
	// deliberate spec.resources change (the only mutable field that alters
	// the desired Pod's shape) from any other reason the live Pod might be
	// absent or need replacing: idleTimeoutSeconds changes are invisible to
	// this hash by construction, since the Pod/Service builders never read
	// it, so tuning idle behavior can never look like a resources change and
	// trigger a Pod replacement or an capacity retry it was never meant to.
	// +optional
	PodTemplateHash string `json:"podTemplateHash,omitempty"`

	// message is bounded, sanitized diagnostic text. It must never contain
	// credentials, raw Pod logs, admission payloads, or a parseable protocol
	// prefix; Conditions carry the machine-readable meaning.
	// +optional
	// +kubebuilder:validation:MaxLength=512
	Message string `json:"message,omitempty"`
}

// +kubebuilder:object:root=true
// +kubebuilder:subresource:status
// +kubebuilder:resource:shortName=msess
// +kubebuilder:printcolumn:name="Notebook",type=string,JSONPath=`.spec.notebookId`
// +kubebuilder:printcolumn:name="Mode",type=string,JSONPath=`.spec.mode`
// +kubebuilder:printcolumn:name="Phase",type=string,JSONPath=`.status.phase`
// +kubebuilder:printcolumn:name="Ready",type=string,JSONPath=`.status.conditions[?(@.type=="Ready")].status`
// +kubebuilder:printcolumn:name="Age",type=date,JSONPath=`.metadata.creationTimestamp`
// +kubebuilder:validation:XValidation:rule="self.metadata.name.matches('^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$')",message="metadata.name must be the Runtime UUID"

// MarimoSession is the technical resource for both a Session's ephemeral
// Runtime and a Deployment's durable Runtime. See the Domain Model: a Session
// has one ephemeral Runtime, while a Deployment has one durable Runtime that
// may sleep.
//
// Label/spec identity agreement (marimohub.io/notebook, marimohub.io/
// workspace, and marimohub.io/mode must equal the identically named spec
// fields) is deliberately not expressed here as a CRD schema CEL rule: the
// CRD validation CEL environment only exposes metadata.name and
// metadata.generateName, not metadata.labels, so the API server rejects any
// x-kubernetes-validations rule that reaches into self.metadata.labels at
// CRD-install time. That agreement is instead enforced by the
// ValidatingAdmissionPolicy in config/policy, which runs inside the API
// server (no webhook process, no TLS material, no TokenReview/SAR) and does
// have the full object, including labels, available to its CEL rules.
type MarimoSession struct {
	metav1.TypeMeta `json:",inline"`

	// metadata is a standard object metadata
	// +optional
	metav1.ObjectMeta `json:"metadata,omitzero"`

	// spec defines the desired state of MarimoSession
	// +required
	Spec MarimoSessionSpec `json:"spec"`

	// status defines the observed state of MarimoSession
	// +optional
	Status MarimoSessionStatus `json:"status,omitzero"`
}

// +kubebuilder:object:root=true

// MarimoSessionList contains a list of MarimoSession
type MarimoSessionList struct {
	metav1.TypeMeta `json:",inline"`
	metav1.ListMeta `json:"metadata,omitzero"`
	Items           []MarimoSession `json:"items"`
}

func init() {
	SchemeBuilder.Register(func(s *runtime.Scheme) error {
		s.AddKnownTypes(SchemeGroupVersion, &MarimoSession{}, &MarimoSessionList{})
		return nil
	})
}
