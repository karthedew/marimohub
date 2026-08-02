package controller

import (
	"context"

	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/event"
	"sigs.k8s.io/controller-runtime/pkg/predicate"
	"sigs.k8s.io/controller-runtime/pkg/reconcile"

	marimohubv1alpha1 "github.com/karthedew/marimohub/marimohub-operator/api/v1alpha1"
	"github.com/karthedew/marimohub/marimohub-operator/internal/runtimecontract"
)

// specOrWakeActivityChanged decides which MarimoSession update events are
// worth reconciling. It has to do two things a plain
// predicate.GenerationChangedPredicate cannot do on its own:
//
//   - fire on a wake-request or activity annotation change even though
//     annotations never bump metadata.generation, since those annotations
//     are exactly how the backend gateway asks this controller to act; and
//   - never fire for an update where neither of those changed, which is
//     precisely what the controller's own status-subresource writes look
//     like from the informer's perspective (status is a subresource, but
//     the informer still delivers an Update event for it, carrying the same
//     generation and the same annotations as before).
//
// Together these two rules are what let the controller be safely
// event-driven instead of falling back to a fixed poll interval: real
// intent changes always wake it, and its own writes never wake it again.
func specOrWakeActivityChanged() predicate.Funcs {
	return predicate.Funcs{
		CreateFunc:  func(event.CreateEvent) bool { return true },
		DeleteFunc:  func(event.DeleteEvent) bool { return true },
		GenericFunc: func(event.GenericEvent) bool { return true },
		UpdateFunc: func(e event.UpdateEvent) bool {
			oldObj, ok := e.ObjectOld.(*marimohubv1alpha1.MarimoSession)
			newObj, ok2 := e.ObjectNew.(*marimohubv1alpha1.MarimoSession)
			if !ok || !ok2 {
				return true
			}
			if oldObj.Generation != newObj.Generation {
				return true
			}
			if oldObj.Annotations[runtimecontract.AnnotationWakeRequest] != newObj.Annotations[runtimecontract.AnnotationWakeRequest] {
				return true
			}
			if oldObj.Annotations[runtimecontract.AnnotationActivity] != newObj.Annotations[runtimecontract.AnnotationActivity] {
				return true
			}
			return false
		},
	}
}

// mapSecretToSession maps a Secret metadata event back to the MarimoSession
// its name identifies. obj is a metadata-only object (see the
// builder.OnlyMetadata watch in SetupWithManager) rather than a structured
// *corev1.Secret; that is exactly why this function only ever calls the
// client.Object accessors on it and never inspects Secret-specific fields --
// this controller's cache never holds Secret data at all, only the
// metadata needed to know a Secret by this name changed.
func (r *MarimoSessionReconciler) mapSecretToSession(_ context.Context, obj client.Object) []reconcile.Request {
	id, ok := runtimecontract.RuntimeIDFromSecretName(obj.GetName())
	if !ok {
		return nil
	}
	return []reconcile.Request{{NamespacedName: client.ObjectKey{Namespace: obj.GetNamespace(), Name: id}}}
}
