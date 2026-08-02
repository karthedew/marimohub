package controller

import (
	"context"
	"fmt"

	corev1 "k8s.io/api/core/v1"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"sigs.k8s.io/controller-runtime/pkg/client"

	"github.com/karthedew/marimohub/marimohub-operator/api/v1alpha1"
	"github.com/karthedew/marimohub/marimohub-operator/internal/runtimecontract"
)

// marimoSessionKind is the CR's Kind, used wherever this package checks an
// owner reference back to a MarimoSession. It must equal the Kind
// controller-gen registers for v1alpha1.MarimoSession; a conformance check
// would be redundant with the ownership tests already exercising it end to
// end against a real API server.
const marimoSessionKind = "MarimoSession"

// credentialResult is the outcome of validating a Runtime's owned
// credential Secret against every precondition the operator fails closed
// on: existence, name, type, immutability, ownership, and key contents.
type credentialResult struct {
	available bool
	reason    string
	message   string
	secret    *corev1.Secret
}

// checkCredentials reads the owned credential Secret directly from the API
// server rather than the shared informer cache. The cache only ever holds
// Secret metadata (see the Secret watch wiring in SetupWithManager) so that
// a compromised or merely curious reconcile path can never enumerate
// Runtime credentials cluster-wide from an in-memory store; the one place
// that legitimately needs the Secret's data reads it here, scoped to the
// single named object this Runtime is entitled to.
func (r *MarimoSessionReconciler) checkCredentials(ctx context.Context, cr *v1alpha1.MarimoSession) (credentialResult, error) {
	var secret corev1.Secret
	key := client.ObjectKey{Namespace: cr.Namespace, Name: runtimecontract.SecretName(cr.Name)}
	if err := r.APIReader.Get(ctx, key, &secret); err != nil {
		if apierrors.IsNotFound(err) {
			return credentialResult{
				reason:  v1alpha1.ReasonCredentialsMissing,
				message: fmt.Sprintf("credential Secret %s does not exist", key.Name),
			}, nil
		}
		return credentialResult{}, err
	}
	if problem := validateOwnedSecret(cr, &secret); problem != "" {
		return credentialResult{reason: v1alpha1.ReasonCredentialsInvalid, message: problem}, nil
	}
	return credentialResult{available: true, secret: &secret}, nil
}

// validateOwnedSecret returns a non-empty, sanitized problem description if
// secret cannot be trusted as this Runtime's credential source, or "" if it
// passes every check. Content mutation is deliberately not checked
// separately from the immutable-flag check: a Secret created with
// immutable: true cannot have its data or stringData changed by any client
// afterward -- the API server itself rejects that -- so requiring the flag
// is what makes "never accept mutation of a Secret's content as
// re-validating it" true by construction rather than something this
// function has to detect after the fact by comparing against a
// previously-seen version.
func validateOwnedSecret(cr *v1alpha1.MarimoSession, secret *corev1.Secret) string {
	if secret.Type != runtimecontract.SecretType {
		return fmt.Sprintf("credential Secret has type %q, want %q", secret.Type, runtimecontract.SecretType)
	}
	if secret.Immutable == nil || !*secret.Immutable {
		return "credential Secret must set immutable: true"
	}
	if !ownedByCR(cr, secret.OwnerReferences) {
		return "credential Secret is not owned by this MarimoSession"
	}
	for _, key := range []string{runtimecontract.SecretKeyMarimoToken, runtimecontract.SecretKeyRuntimeCredential} {
		if len(secret.Data[key]) == 0 {
			return fmt.Sprintf("credential Secret is missing a non-empty %q key", key)
		}
	}
	return ""
}

// ownedByCR reports whether refs contains a controller owner reference
// pointing at exactly this CR: right APIVersion, right Kind, right Name,
// and -- the check that actually matters against a recreated same-name
// object -- the exact live UID. A Secret or Pod/Service that matches on
// name alone but not UID is a foreign object, not a stale one.
func ownedByCR(cr *v1alpha1.MarimoSession, refs []metav1.OwnerReference) bool {
	for _, ref := range refs {
		if ref.Controller != nil && *ref.Controller &&
			ref.APIVersion == v1alpha1.GroupVersion.String() &&
			ref.Kind == marimoSessionKind &&
			ref.Name == cr.Name &&
			ref.UID == cr.UID {
			return true
		}
	}
	return false
}

// getOwnedPod fetches the Runtime's conventionally-named Pod, if any, and
// reports whether an object with that name exists but belongs to something
// else. Every caller must check foreign before treating pod as this
// Runtime's own: creating, deleting, or reading routing decisions from a
// same-name object this controller does not own would be exactly the kind
// of collision the state machine is required to reject instead.
func (r *MarimoSessionReconciler) getOwnedPod(ctx context.Context, cr *v1alpha1.MarimoSession) (pod *corev1.Pod, foreign bool, err error) {
	var p corev1.Pod
	key := client.ObjectKey{Namespace: cr.Namespace, Name: runtimecontract.ChildName(cr.Name)}
	if err := r.Get(ctx, key, &p); err != nil {
		if apierrors.IsNotFound(err) {
			return nil, false, nil
		}
		return nil, false, err
	}
	if !ownedByCR(cr, p.OwnerReferences) {
		return &p, true, nil
	}
	return &p, false, nil
}
