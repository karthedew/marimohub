// Package session builds the desired Pod and Service for one MarimoSession
// Runtime. Every exported function here is a pure transformation of a
// MarimoSession and this package's Options into a desired-state object:
// there are no client calls, no watches, and no reconcile loop. A later
// reconciler is the only caller that ever talks to the API server; it diffs
// these desired objects against live cluster state and decides what to
// create, patch, or leave alone.
package session

import (
	corev1 "k8s.io/api/core/v1"

	"github.com/karthedew/marimohub/marimohub-operator/api/v1alpha1"
)

// CABundle names the Secret and key the internal API's TLS certificate is
// validated against. Unlike the per-Runtime credential Secret (named from
// the CR itself), this is chart-managed and namespace-wide: every Runtime
// Pod validates the same internal API using the same CA, so it is named
// once in Options rather than derived from each CR.
type CABundle struct {
	SecretName string
	SecretKey  string
}

// IdleTimeoutDefaults are the operator's per-mode idle budgets applied when
// a MarimoSession does not set spec.idleTimeoutSeconds. Neither builder in
// this package reads these fields -- idle evaluation is reconcile-loop
// behavior, not Pod/Service shape -- but they travel with the rest of the
// chart-wide policy so the reconciler and these builders are configured
// from exactly one shared shape instead of two that could drift apart.
type IdleTimeoutDefaults struct {
	EditSeconds   int32
	RunSeconds    int32
	DeploySeconds int32
}

// Options is the chart-wide policy every Runtime Pod is built from. A
// MarimoSession supplies identity and the few fields a Runtime requester
// may override (spec.resources, spec.idleTimeoutSeconds); Options supplies
// everything else -- the platform's policy, not the requester's.
//
// The Runtime port is deliberately not a field here: runtimecontract.
// RuntimePort documents why it is a fixed constant rather than something
// any caller, including this options struct, could set differently from
// what the Service and readiness probe assume.
type Options struct {
	// FetcherImage is the digest-qualified source-fetcher image run as the
	// Pod's init container. The public backend resolves the configured
	// Runtime image flavor to a digest for spec.image; there is no
	// equivalent resolution step here because there is only ever one
	// fetcher image.
	FetcherImage string

	// InternalAPIURL is the base URL the fetcher -- and, for the internal
	// Notebook data endpoints prebuilt Notebook code calls, the marimo
	// container itself -- uses to reach the unrouted internal API.
	InternalAPIURL string

	// InternalAPICA is the namespace-wide Secret/key the fetcher validates
	// the internal API's TLS certificate against.
	InternalAPICA CABundle

	// IdleTimeout holds the per-mode idle defaults the reconciler consumes.
	// See the type's own doc comment for why the builders in this package
	// do not use it directly.
	IdleTimeout IdleTimeoutDefaults

	// Resources are the chart-wide Runtime container defaults applied when
	// a MarimoSession does not set spec.resources.
	Resources corev1.ResourceRequirements

	// FetcherResources are the fixed source-fetcher init container
	// resources. Unlike Resources, a MarimoSession can never override
	// these: spec.resources is documented as sizing the Runtime container,
	// and letting a Runtime requester size an init container the platform
	// controls would just be a second, unreviewed way to size the same Pod.
	FetcherResources corev1.ResourceRequirements

	// ImagePullPolicy applies to both containers. Digest-qualified images
	// make "Always" mostly academic -- a digest can never resolve to
	// different bytes -- but an explicit policy is still cheaper than
	// leaving Kubernetes' per-tag heuristic to decide.
	ImagePullPolicy corev1.PullPolicy

	// ServiceAccountName is the Runtime ServiceAccount, if the chart
	// provisions one. Empty leaves the Pod on the namespace's default
	// ServiceAccount; automountServiceAccountToken is false either way, so
	// this only matters for imagePullSecrets attached to the ServiceAccount
	// itself rather than to the Pod.
	ServiceAccountName string

	// ImagePullSecrets are attached to the Pod directly so a private
	// registry works without relying on a ServiceAccount-level grant.
	ImagePullSecrets []corev1.LocalObjectReference

	// WorkspaceStorage mounts each Workspace's durable directory into its
	// edit and run Runtimes. The zero value disables it, leaving every
	// Runtime with only its ephemeral scratch volumes.
	WorkspaceStorage WorkspaceStorage

	// SharedVolumes are administrator-provided data volumes, typically NFS
	// exports such as a team's datasets, mounted into every Runtime whose
	// mode each one lists. Unlike WorkspaceStorage, every Workspace sees
	// the same files.
	SharedVolumes []SharedVolume
}

// SharedVolume is one administrator-provided claim mounted into Runtimes.
// The platform creates the claim (an NFS PersistentVolume in production);
// the operator only ever references it.
type SharedVolume struct {
	// Name identifies the volume; the Pod volume is named "shared-<Name>".
	Name string

	// ClaimName is the PersistentVolumeClaim in the Runtime namespace.
	ClaimName string

	// SubPath is an optional existing directory inside the claim. It must
	// already exist: a read-only mount cannot create it.
	SubPath string

	// MountPath is where the marimo container sees the volume.
	MountPath string

	// ReadOnly mounts the volume, and the claim, read-only.
	ReadOnly bool

	// Modes lists the Runtime modes that mount the volume.
	Modes []v1alpha1.RuntimeMode

	// SupplementalGroups are added to every Pod that mounts this volume,
	// for exports whose files are group-owned.
	SupplementalGroups []int64
}

// WorkspaceStorage names the one administrator-provisioned ReadWriteMany
// claim (an NFS export in production) every Workspace directory lives on.
// The claim's root must already contain a `workspaces/` directory the
// Runtime Pods' group can write to; each Runtime then sees only
// `workspaces/<workspaceId>`, never a sibling Workspace's directory.
type WorkspaceStorage struct {
	// ClaimName is the PersistentVolumeClaim in the Runtime namespace.
	// Empty disables Workspace storage entirely.
	ClaimName string

	// MountPath is where the marimo container sees its Workspace directory.
	MountPath string

	// SupplementalGroups are added to every Pod that mounts the claim, so a
	// share whose `workspaces/` directory is group-owned (the usual shape of
	// an NFS export, which honors neither fsGroup nor a Pod's arbitrary UID)
	// stays writable without pinning a UID.
	SupplementalGroups []int64
}

// Enabled reports whether Runtimes get a Workspace directory at all.
func (w WorkspaceStorage) Enabled() bool {
	return w.ClaimName != ""
}
