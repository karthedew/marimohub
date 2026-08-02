package controller

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"fmt"

	corev1 "k8s.io/api/core/v1"
	apiequality "k8s.io/apimachinery/pkg/api/equality"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	"sigs.k8s.io/controller-runtime/pkg/client"

	"github.com/karthedew/marimohub/marimohub-operator/api/v1alpha1"
	"github.com/karthedew/marimohub/marimohub-operator/internal/session"
)

// reconcileService ensures cr's Runtime Service exists and matches the
// builder's desired shape, and reports false without touching anything if a
// same-name Service already exists but is not owned by cr. This runs
// unconditionally before any Pod readiness is ever consulted: a Pod cannot
// usefully become "Ready" for routing purposes if the Service in front of
// it does not yet select it.
func (r *MarimoSessionReconciler) reconcileService(ctx context.Context, cr *v1alpha1.MarimoSession) (ok bool, err error) {
	desired := session.BuildService(cr)
	var existing corev1.Service
	getErr := r.Get(ctx, client.ObjectKeyFromObject(desired), &existing)
	if apierrors.IsNotFound(getErr) {
		if createErr := r.Create(ctx, desired); createErr != nil {
			if apierrors.IsAlreadyExists(createErr) {
				return true, nil
			}
			return false, createErr
		}
		return true, nil
	}
	if getErr != nil {
		return false, getErr
	}
	if !ownedByCR(cr, existing.OwnerReferences) {
		return false, nil
	}

	updated := existing.DeepCopy()
	updated.Labels = desired.Labels
	updated.Spec.Selector = desired.Spec.Selector
	updated.Spec.Ports = desired.Spec.Ports
	if apiequality.Semantic.DeepEqual(updated.Labels, existing.Labels) &&
		apiequality.Semantic.DeepEqual(updated.Spec, existing.Spec) {
		return true, nil
	}
	return true, r.Update(ctx, updated)
}

// podTemplateHash content-hashes the builder's desired PodSpec for cr. It is
// the one place resources drift or "someone hand-edited the live Pod"
// becomes a comparable value: BuildPod is a pure function of cr and opts,
// so equal inputs always hash identically and the only spec field that can
// change it post-creation is spec.resources.
func podTemplateHash(cr *v1alpha1.MarimoSession, opts session.Options) string {
	pod := session.BuildPod(cr, opts)
	data, err := json.Marshal(pod.Spec)
	if err != nil {
		// BuildPod's output is a plain corev1.PodSpec built entirely from
		// strings, maps, and slices; a marshal failure here would mean the
		// builder itself produced something JSON cannot represent, which is
		// a programming error to fix, not a runtime condition to recover
		// from.
		panic(fmt.Sprintf("marshal desired Pod spec: %v", err))
	}
	sum := sha256.Sum256(data)
	return hex.EncodeToString(sum[:])
}
