package controller

import (
	"context"

	corev1 "k8s.io/api/core/v1"
	apiequality "k8s.io/apimachinery/pkg/api/equality"
	apimeta "k8s.io/apimachinery/pkg/api/meta"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/client-go/util/retry"
	"sigs.k8s.io/controller-runtime/pkg/client"

	"github.com/karthedew/marimohub/marimohub-operator/api/v1alpha1"
)

// maxStatusMessageLength mirrors the CRD's own MaxLength marker on
// status.message; keeping both in one place would need the generated
// marker to be readable at runtime, which it is not, so this is the
// second, deliberately-matched half of that single bound.
const maxStatusMessageLength = 512

// statusPatch describes the status fields one transition wants to change.
// Every field is a pointer (or, for conditions, a slice) so "unset" and
// "explicitly cleared to the zero value" are distinguishable: clearing
// status.podName to "" is a meaningful, common transition (idle sleep,
// deterministic failure), not an accidental no-op.
type statusPatch struct {
	phase                     *v1alpha1.RuntimePhase
	conditions                []metav1.Condition
	podName                   *string
	serviceName               *string
	lastActivity              *metav1.Time
	observedWakeRequest       *string
	observedActivity          *string
	podTemplateHash           *string
	attempt                   *int32
	advanceObservedGeneration bool
}

func (p statusPatch) applyTo(cr *v1alpha1.MarimoSession, now metav1.Time) {
	if p.phase != nil {
		cr.Status.Phase = *p.phase
	}
	for _, c := range p.conditions {
		c.Message = truncateMessage(c.Message, maxStatusMessageLength)
		c.ObservedGeneration = cr.Generation
		if c.LastTransitionTime.IsZero() {
			c.LastTransitionTime = now
		}
		apimeta.SetStatusCondition(&cr.Status.Conditions, c)
	}
	if p.podName != nil {
		cr.Status.PodName = *p.podName
	}
	if p.serviceName != nil {
		cr.Status.ServiceName = *p.serviceName
	}
	if p.lastActivity != nil {
		cr.Status.LastActivity = p.lastActivity
	}
	if p.observedWakeRequest != nil {
		cr.Status.ObservedWakeRequest = *p.observedWakeRequest
	}
	if p.observedActivity != nil {
		cr.Status.ObservedActivity = *p.observedActivity
	}
	if p.podTemplateHash != nil {
		cr.Status.PodTemplateHash = *p.podTemplateHash
	}
	if p.attempt != nil {
		cr.Status.Attempt = *p.attempt
	}
	if p.advanceObservedGeneration {
		cr.Status.ObservedGeneration = cr.Generation
	}
	cr.Status.Message = deriveMessage(cr.Status.Conditions)
}

// deriveMessage summarizes the CR's current authoritative Conditions into
// the one bounded, sanitized string exposed as status.message. It is
// recomputed from cr.Status.Conditions on every status write instead of
// being a value individual call sites remember to pass, so it can never
// drift from the Conditions that are the real source of truth: the same
// write that resolves a blocking Condition automatically clears the message
// that described it, and the same write that introduces one automatically
// populates it.
func deriveMessage(conditions []metav1.Condition) string {
	ready := apimeta.FindStatusCondition(conditions, v1alpha1.ConditionTypeReady)
	reconciled := apimeta.FindStatusCondition(conditions, v1alpha1.ConditionTypeReconciled)
	if ready != nil && ready.Status == metav1.ConditionTrue && (reconciled == nil || reconciled.Status == metav1.ConditionTrue) {
		return ""
	}

	// Report the most specific blocking cause first: credentials and
	// capacity are preconditions to even attempting a Pod, so a problem
	// there explains more than a generic Ready=False ever could.
	for _, condType := range []string{
		v1alpha1.ConditionTypeCredentialsAvailable,
		v1alpha1.ConditionTypeCapacityAvailable,
		v1alpha1.ConditionTypeReady,
		v1alpha1.ConditionTypeReconciled,
	} {
		if c := apimeta.FindStatusCondition(conditions, condType); c != nil && c.Status != metav1.ConditionTrue {
			return c.Message
		}
	}
	return ""
}

// condition builds a metav1.Condition ready to hand to applyStatus.
// LastTransitionTime and ObservedGeneration are deliberately left zero here:
// applyStatus stamps both from the controller's own clock and the live
// object's generation immediately before the merge, so every condition this
// package produces goes through exactly one place that decides "when".
func condition(condType string, status metav1.ConditionStatus, reason, message string) metav1.Condition {
	return metav1.Condition{Type: condType, Status: status, Reason: reason, Message: message}
}

func truncateMessage(msg string, max int) string {
	if len(msg) <= max {
		return msg
	}
	const suffix = "...(truncated)"
	if max <= len(suffix) {
		return msg[:max]
	}
	return msg[:max-len(suffix)] + suffix
}

// applyStatus is the single place that ever writes to a MarimoSession's
// status subresource. It re-reads the live object on every attempt (so a
// conflict retry always merges onto the latest resourceVersion, never a
// stale local copy), merges conditions with standard metav1.Condition
// semantics, skips the write entirely when nothing actually changed, and
// otherwise records the transition as an Event/metric before returning. On
// success *cr is updated in place so the caller can keep using it without a
// second Get.
func (r *MarimoSessionReconciler) applyStatus(ctx context.Context, cr *v1alpha1.MarimoSession, patch statusPatch) error {
	key := client.ObjectKeyFromObject(cr)
	now := metav1.NewTime(r.Clock.Now())
	return retry.RetryOnConflict(retry.DefaultBackoff, func() error {
		live := &v1alpha1.MarimoSession{}
		if err := r.Get(ctx, key, live); err != nil {
			return err
		}
		before := live.Status.DeepCopy()
		patch.applyTo(live, now)
		if apiequality.Semantic.DeepEqual(*before, live.Status) {
			*cr = *live
			return nil
		}
		if err := r.Status().Update(ctx, live); err != nil {
			return err
		}
		r.recordTransition(*before, live.Status, live)
		*cr = *live
		return nil
	})
}

// recordTransition emits transition-only Events and metrics: it compares
// the status this write replaced against the status it wrote, so a
// reconcile that leaves a phase or condition unchanged never re-announces
// it, no matter how often that reconcile itself re-runs.
func (r *MarimoSessionReconciler) recordTransition(before, after v1alpha1.MarimoSessionStatus, cr *v1alpha1.MarimoSession) {
	if before.Phase != after.Phase {
		r.Metrics.observePhaseChange(before.Phase, after.Phase)
		if r.Recorder != nil {
			r.Recorder.Eventf(cr, corev1.EventTypeNormal, "PhaseChanged", "phase changed from %q to %q", before.Phase, after.Phase)
		}
	}
	for _, c := range after.Conditions {
		old := apimeta.FindStatusCondition(before.Conditions, c.Type)
		if old != nil && old.Status == c.Status && old.Reason == c.Reason {
			continue
		}
		eventType := corev1.EventTypeNormal
		degraded := c.Status != metav1.ConditionTrue &&
			(c.Type == v1alpha1.ConditionTypeReady ||
				c.Type == v1alpha1.ConditionTypeCredentialsAvailable ||
				c.Type == v1alpha1.ConditionTypeCapacityAvailable ||
				c.Type == v1alpha1.ConditionTypeReconciled)
		if degraded {
			eventType = corev1.EventTypeWarning
		}
		if r.Recorder != nil {
			r.Recorder.Event(cr, eventType, c.Reason, c.Message)
		}
	}
}

func isConditionFalseWithReason(cr *v1alpha1.MarimoSession, condType, reason string) bool {
	c := apimeta.FindStatusCondition(cr.Status.Conditions, condType)
	return c != nil && c.Status == metav1.ConditionFalse && c.Reason == reason
}

func wasReady(cr *v1alpha1.MarimoSession) bool {
	return cr.Status.Phase == v1alpha1.RuntimePhaseReady
}
