package controller

import (
	"context"
	"time"

	corev1 "k8s.io/api/core/v1"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/utils/ptr"
	ctrl "sigs.k8s.io/controller-runtime"
	"sigs.k8s.io/controller-runtime/pkg/client"

	"github.com/karthedew/marimohub/marimohub-operator/api/v1alpha1"
	"github.com/karthedew/marimohub/marimohub-operator/internal/runtimecontract"
)

// effectiveIdleTimeout applies spec.idleTimeoutSeconds as a per-CR override
// of the operator's per-mode chart default; idleTimeoutSeconds is a minimum
// idle duration, never an exact shutdown deadline; the grace period the
// caller adds on top absorbs the backend's own activity-signal interval so
// throttled activity is never mistaken for idleness.
func (r *MarimoSessionReconciler) effectiveIdleTimeout(cr *v1alpha1.MarimoSession) time.Duration {
	if cr.Spec.IdleTimeoutSeconds != nil {
		return time.Duration(*cr.Spec.IdleTimeoutSeconds) * time.Second
	}
	switch cr.Spec.Mode {
	case v1alpha1.RuntimeModeEdit:
		return time.Duration(r.Options.IdleTimeout.EditSeconds) * time.Second
	case v1alpha1.RuntimeModeRun:
		return time.Duration(r.Options.IdleTimeout.RunSeconds) * time.Second
	default:
		return time.Duration(r.Options.IdleTimeout.DeploySeconds) * time.Second
	}
}

// evaluateActivity acknowledges a new activity token using the controller's
// own clock -- never a client-supplied timestamp -- and schedules the next
// reconcile for the exact idle deadline rather than a fixed poll interval.
// It only ever runs for an already-Ready CR: entering Ready resets the clock
// and schedules the first evaluation one full idle window later (see
// handlePodReady), so a fresh Ready transition and its first idle
// evaluation are never the same reconcile.
func (r *MarimoSessionReconciler) evaluateActivity(ctx context.Context, cr *v1alpha1.MarimoSession) (ctrl.Result, error) {
	activityToken := cr.Annotations[runtimecontract.AnnotationActivity]
	if activityToken != "" && activityToken != cr.Status.ObservedActivity {
		now := metav1.NewTime(r.Clock.Now())
		if err := r.applyStatus(ctx, cr, statusPatch{
			observedActivity: &activityToken,
			lastActivity:     &now,
		}); err != nil {
			return ctrl.Result{}, err
		}
	}

	last := cr.Status.LastActivity
	if last == nil {
		now := metav1.NewTime(r.Clock.Now())
		last = &now
	}
	deadline := last.Time.Add(r.effectiveIdleTimeout(cr)).Add(r.IdleGracePeriod)
	if remaining := deadline.Sub(r.Clock.Now()); remaining > 0 {
		return ctrl.Result{RequeueAfter: remaining}, nil
	}
	return r.actOnIdle(ctx, cr)
}

// actOnIdle re-reads the CR before acting so a concurrent activity write
// that landed after this reconcile started still wins: idle shutdown must
// never race a request that is, at this exact moment, keeping the Runtime
// alive.
func (r *MarimoSessionReconciler) actOnIdle(ctx context.Context, cr *v1alpha1.MarimoSession) (ctrl.Result, error) {
	fresh := &v1alpha1.MarimoSession{}
	if err := r.Get(ctx, client.ObjectKeyFromObject(cr), fresh); err != nil {
		if apierrors.IsNotFound(err) {
			return ctrl.Result{}, nil
		}
		return ctrl.Result{}, err
	}
	*cr = *fresh

	freshToken := cr.Annotations[runtimecontract.AnnotationActivity]
	if freshToken != "" && freshToken != cr.Status.ObservedActivity {
		return r.evaluateActivity(ctx, cr)
	}
	last := cr.Status.LastActivity
	if last != nil {
		deadline := last.Time.Add(r.effectiveIdleTimeout(cr)).Add(r.IdleGracePeriod)
		if remaining := deadline.Sub(r.Clock.Now()); remaining > 0 {
			return ctrl.Result{RequeueAfter: remaining}, nil
		}
	}

	r.Metrics.observeIdleShutdown(cr.Spec.Mode)
	if cr.Spec.Mode == v1alpha1.RuntimeModeDeploy {
		return r.idleSleepDeploy(ctx, cr)
	}
	return ctrl.Result{}, r.idleDeleteCR(ctx, cr)
}

// idleDeleteCR implements idle edit/run: deleting the CR lets owner
// reference garbage collection remove its Pod and Service. There is no
// finalizer and nothing further for the controller to do or wait for.
func (r *MarimoSessionReconciler) idleDeleteCR(ctx context.Context, cr *v1alpha1.MarimoSession) error {
	if err := r.Delete(ctx, cr); err != nil && !apierrors.IsNotFound(err) {
		return err
	}
	return nil
}

// idleSleepDeploy implements idle deploy: persist Sleeping and clear
// podName first, then delete the Pod. That order is what makes a crash
// between the two writes safe -- a Sleeping CR with an already-cleared
// podName can never be mistaken, on the next reconcile, for a Pod that
// disappeared unexpectedly and needs infrastructure recovery, because that
// path only ever fires when podName is still set.
func (r *MarimoSessionReconciler) idleSleepDeploy(ctx context.Context, cr *v1alpha1.MarimoSession) (ctrl.Result, error) {
	if err := r.applyStatus(ctx, cr, statusPatch{
		phase:                     ptr.To(v1alpha1.RuntimePhaseSleeping),
		podName:                   ptr.To(""),
		advanceObservedGeneration: true,
		conditions: []metav1.Condition{
			condition(v1alpha1.ConditionTypeReady, metav1.ConditionFalse, v1alpha1.ReasonReconcileSucceeded, "sleeping after idle timeout"),
			condition(v1alpha1.ConditionTypeReconciled, metav1.ConditionTrue, v1alpha1.ReasonReconcileSucceeded, "reconciled successfully"),
		},
	}); err != nil {
		return ctrl.Result{}, err
	}

	pod := &corev1.Pod{ObjectMeta: metav1.ObjectMeta{
		Name:      runtimecontract.ChildName(cr.Name),
		Namespace: cr.Namespace,
	}}
	if err := r.Delete(ctx, pod); err != nil && !apierrors.IsNotFound(err) {
		return ctrl.Result{}, err
	}
	return ctrl.Result{}, nil
}
