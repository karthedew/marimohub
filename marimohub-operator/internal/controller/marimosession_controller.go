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

// Package controller implements the MarimoSession reconcile loop: the
// lifecycle state machine that turns a validated CR and its owned Secret
// into a running, routable Runtime Pod and Service, and back down again on
// idle, failure, or credential loss. Every exported and unexported function
// here is either the one entry point (Reconcile) or a small piece of that
// state machine factored out for its own tests; there is deliberately no
// second copy of the desired-state logic already implemented as pure
// builders in internal/session, and no client call anywhere outside this
// package's own files.
package controller

import (
	"context"
	"fmt"
	"time"

	corev1 "k8s.io/api/core/v1"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	apimeta "k8s.io/apimachinery/pkg/api/meta"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/runtime"
	"k8s.io/client-go/tools/record"
	clock "k8s.io/utils/clock"
	"k8s.io/utils/ptr"
	ctrl "sigs.k8s.io/controller-runtime"
	"sigs.k8s.io/controller-runtime/pkg/builder"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/handler"
	logf "sigs.k8s.io/controller-runtime/pkg/log"

	marimohubv1alpha1 "github.com/karthedew/marimohub/marimohub-operator/api/v1alpha1"
	"github.com/karthedew/marimohub/marimohub-operator/internal/runtimecontract"
	"github.com/karthedew/marimohub/marimohub-operator/internal/session"
)

// Default deadlines the manager wires in unless a test overrides them.
// DefaultImagePullDeadline, DefaultUnhealthyTimeout, and DefaultNodeLossDeadline
// are not yet manager flags: nothing in the chart's documented value shape
// anticipates configuring them the way it already does for idle timeouts and
// resource defaults, so promoting them to flags is a mechanical follow-up
// whenever that need arises, not a decision this package should preempt.
// DefaultIdleGracePeriod is the one exception: it mirrors the value
// managerconfig falls back to when --idle-grace-period-seconds is omitted,
// since the chart's Runtime policy values do name this one
// (activityGraceSeconds) and main.go wires the resolved flag value, not this
// constant, into the reconciler.
const (
	DefaultImagePullDeadline = 10 * time.Minute
	DefaultUnhealthyTimeout  = 2 * time.Minute
	DefaultNodeLossDeadline  = 5 * time.Minute
	DefaultIdleGracePeriod   = 20 * time.Second

	// foreignCollisionRetryInterval is how often the controller re-checks a
	// same-name Pod or Service it does not own. A foreign object carries no
	// owner reference back to this CR, so there is no watch event to wake
	// up on if an administrator later removes it; a bounded poll is the
	// only way this ever self-resolves.
	foreignCollisionRetryInterval = 30 * time.Second
)

// MarimoSessionReconciler reconciles a MarimoSession object.
type MarimoSessionReconciler struct {
	client.Client

	// APIReader is a direct, uncached read path used only to fetch the
	// owned credential Secret's contents. See checkCredentials for why the
	// shared cache must never hold Secret data.
	APIReader client.Reader

	Scheme   *runtime.Scheme
	Recorder record.EventRecorder
	Metrics  *Metrics

	// Clock and Backoff are injected so tests can control time and jitter
	// deterministically instead of racing real wall-clock delays.
	Clock   clock.Clock
	Backoff BackoffPolicy

	// Options is the chart-wide policy the session builders turn a CR into
	// a desired Pod/Service from.
	Options session.Options

	// ImagePullDeadline bounds how long a Pod may sit waiting on its image
	// before the controller stops trusting Kubernetes' own pull backoff and
	// fails it ImageUnavailable.
	ImagePullDeadline time.Duration
	// UnhealthyTimeout bounds how long a previously-Ready Pod may keep
	// failing its readiness check before the controller fails it rather
	// than continuing to wait for recovery.
	UnhealthyTimeout time.Duration
	// NodeLossDeadline bounds how long the controller waits on a Pod whose
	// Ready condition is Unknown (kubelet not reporting) before treating it
	// as confirmed infrastructure loss and proceeding to recreate it.
	NodeLossDeadline time.Duration
	// IdleGracePeriod is added on top of the effective idle timeout before
	// any idle action, absorbing the backend's own activity-signal interval
	// so a throttled activity write is never mistaken for real idleness.
	IdleGracePeriod time.Duration
}

// The operator never creates or mutates MarimoSession spec/metadata (the
// public backend is the sole writer; see Resource Ownership) and uses no
// finalizer, so the main resource only needs read and delete verbs here --
// delete is for idle edit/run cleanup, never create. Status is the one
// field the operator owns outright. Every rule below is scoped to the
// single configured Runtime namespace rather than generated as a
// ClusterRole: no operator workload is permitted cluster-scoped access.
// Secret access is get/list/watch only -- list/watch back the metadata-only
// cache the Secret watch mapper uses, and every actual content read goes
// through APIReader instead -- and never create/update/delete: the operator
// never creates, changes, or adopts credential Secrets. There is no Pod
// logs or exec access at all.
//
// +kubebuilder:rbac:groups=marimohub.io,namespace=marimohub-sessions,resources=marimosessions,verbs=get;list;watch;delete
// +kubebuilder:rbac:groups=marimohub.io,namespace=marimohub-sessions,resources=marimosessions/status,verbs=get;update;patch
// +kubebuilder:rbac:groups="",namespace=marimohub-sessions,resources=pods,verbs=get;list;watch;create;update;patch;delete
// +kubebuilder:rbac:groups="",namespace=marimohub-sessions,resources=services,verbs=get;list;watch;create;update;patch;delete
// +kubebuilder:rbac:groups="",namespace=marimohub-sessions,resources=secrets,verbs=get;list;watch
// +kubebuilder:rbac:groups="",namespace=marimohub-sessions,resources=resourcequotas,verbs=get;list
// +kubebuilder:rbac:groups="",namespace=marimohub-sessions,resources=events,verbs=create;patch

// Reconcile is the entry point for the MarimoSession control loop.
func (r *MarimoSessionReconciler) Reconcile(ctx context.Context, req ctrl.Request) (ctrl.Result, error) {
	log := logf.FromContext(ctx)

	var cr marimohubv1alpha1.MarimoSession
	if err := r.Get(ctx, req.NamespacedName, &cr); err != nil {
		if apierrors.IsNotFound(err) {
			return ctrl.Result{}, nil
		}
		return ctrl.Result{}, err
	}

	// No finalizer is used anywhere in this controller: a MarimoSession
	// carrying a deletionTimestamp has nothing left for the reconciler to
	// do. Returning immediately here, rather than falling through to the
	// state machine below, is what makes that true structurally instead of
	// by convention.
	if !cr.DeletionTimestamp.IsZero() {
		return ctrl.Result{}, nil
	}

	// Failed is fully terminal: it must never be re-evaluated against
	// credentials, quota, or anything else that could otherwise read as
	// permission to try again. Checking it before any other state, rather
	// than as one branch among several, is what makes "never create a Pod
	// from Failed" a property of the control flow rather than something
	// every other branch has to remember to respect.
	if cr.Status.Phase == marimohubv1alpha1.RuntimePhaseFailed {
		if err := r.reconcileFailedPhase(ctx, &cr); err != nil {
			r.Metrics.observeReconcileError()
			return ctrl.Result{}, err
		}
		return ctrl.Result{}, nil
	}

	result, err := r.reconcile(ctx, &cr)
	if err != nil {
		log.Error(err, "reconcile failed")
		r.Metrics.observeReconcileError()
	}
	return result, err
}

func (r *MarimoSessionReconciler) reconcile(ctx context.Context, cr *marimohubv1alpha1.MarimoSession) (ctrl.Result, error) {
	cred, err := r.checkCredentials(ctx, cr)
	if err != nil {
		return ctrl.Result{}, err
	}
	if !cred.available {
		return r.reconcileCredentialsInvalid(ctx, cr, cred)
	}

	// Re-assert CredentialsAvailable=True on every reconcile where the
	// Secret checks out, not just the first one: a Runtime that once had a
	// missing or invalid Secret must not keep reporting that stale verdict
	// forever once the Secret is repaired. applyStatus no-ops when nothing
	// actually changed, so this costs nothing once the condition is already
	// True.
	if err := r.applyStatus(ctx, cr, statusPatch{
		conditions: []metav1.Condition{
			condition(marimohubv1alpha1.ConditionTypeCredentialsAvailable, metav1.ConditionTrue, marimohubv1alpha1.ReasonReconcileSucceeded, "credential Secret satisfies all preconditions"),
		},
	}); err != nil {
		return ctrl.Result{}, err
	}

	serviceOK, err := r.reconcileService(ctx, cr)
	if err != nil {
		return ctrl.Result{}, err
	}
	if !serviceOK {
		return r.reconcileForeignCollision(ctx, cr, "Service")
	}
	if cr.Status.ServiceName == "" {
		name := runtimecontract.ChildName(cr.Name)
		if err := r.applyStatus(ctx, cr, statusPatch{serviceName: &name}); err != nil {
			return ctrl.Result{}, err
		}
	}

	if cr.Status.Phase == marimohubv1alpha1.RuntimePhaseSleeping {
		return r.reconcileSleepingPhase(ctx, cr)
	}
	return r.reconcileActivePhase(ctx, cr)
}

// reconcileFailedPhase is defensive only: Failed never creates or
// recreates a Pod. Every path that transitions a CR into Failed already
// deletes its Pod before doing so, so finding one here would mean
// something outside this controller's own lifecycle left it behind; clean
// it up without ever treating its presence, or absence, as a signal to act.
func (r *MarimoSessionReconciler) reconcileFailedPhase(ctx context.Context, cr *marimohubv1alpha1.MarimoSession) error {
	pod, foreign, err := r.getOwnedPod(ctx, cr)
	if err != nil {
		return err
	}
	if pod == nil || foreign {
		return nil
	}
	if err := r.Delete(ctx, pod); err != nil && !apierrors.IsNotFound(err) {
		return err
	}
	return nil
}

// reconcileCredentialsInvalid persists a non-Ready, credential-invalid
// status before ever touching a live Pod, so a crash between the two
// writes always leaves the CR correctly describing itself as
// credential-less rather than silently still claiming a Pod that is about
// to be removed.
func (r *MarimoSessionReconciler) reconcileCredentialsInvalid(ctx context.Context, cr *marimohubv1alpha1.MarimoSession, cred credentialResult) (ctrl.Result, error) {
	pod, foreign, err := r.getOwnedPod(ctx, cr)
	if err != nil {
		return ctrl.Result{}, err
	}

	phase := marimohubv1alpha1.RuntimePhasePending
	if cr.Spec.Mode == marimohubv1alpha1.RuntimeModeDeploy {
		switch cr.Status.Phase {
		case marimohubv1alpha1.RuntimePhaseReady, marimohubv1alpha1.RuntimePhaseStarting, marimohubv1alpha1.RuntimePhaseSleeping:
			// This Deployment has already been running, or was already
			// sleeping, before its credentials were lost -- it is not a
			// brand-new CR that has simply never had a valid Secret yet.
			// Sleeping is the only phase reconcileSleepingPhase's
			// unobserved-wake-token gate applies to, so routing every such
			// case there, rather than through the missing-Pod path's
			// unconditional restart from Pending, is what keeps a Secret
			// repair from silently restarting the Pod on its own, without a
			// fresh wake request, the moment the backend fixes it.
			phase = marimohubv1alpha1.RuntimePhaseSleeping
		}
	}

	if err := r.applyStatus(ctx, cr, statusPatch{
		phase:   &phase,
		podName: ptr.To(""),
		conditions: []metav1.Condition{
			condition(marimohubv1alpha1.ConditionTypeCredentialsAvailable, metav1.ConditionFalse, cred.reason, cred.message),
			condition(marimohubv1alpha1.ConditionTypeReady, metav1.ConditionFalse, cred.reason, "credentials unavailable"),
			condition(marimohubv1alpha1.ConditionTypeReconciled, metav1.ConditionFalse, cred.reason, cred.message),
		},
	}); err != nil {
		return ctrl.Result{}, err
	}

	if pod != nil && !foreign {
		if err := r.Delete(ctx, pod); err != nil && !apierrors.IsNotFound(err) {
			return ctrl.Result{}, err
		}
	}
	return ctrl.Result{}, nil
}

// reconcileForeignCollision records a stable, non-terminal Condition when a
// same-name Pod or Service exists but is not owned by this CR, rather than
// ever adopting or deleting someone else's object. There is no owner
// reference from the foreign object back to this CR, so there is no watch
// event to resume on once the collision is resolved; the bounded requeue is
// the only way this ever gets re-checked.
func (r *MarimoSessionReconciler) reconcileForeignCollision(ctx context.Context, cr *marimohubv1alpha1.MarimoSession, kind string) (ctrl.Result, error) {
	message := fmt.Sprintf("a %s named %q already exists in this namespace and is not owned by this MarimoSession", kind, runtimecontract.ChildName(cr.Name))
	if err := r.applyStatus(ctx, cr, statusPatch{
		conditions: []metav1.Condition{
			condition(marimohubv1alpha1.ConditionTypeReconciled, metav1.ConditionFalse, marimohubv1alpha1.ReasonResourceConflict, message),
			condition(marimohubv1alpha1.ConditionTypeReady, metav1.ConditionFalse, marimohubv1alpha1.ReasonResourceConflict, message),
		},
	}); err != nil {
		return ctrl.Result{}, err
	}
	return ctrl.Result{RequeueAfter: foreignCollisionRetryInterval}, nil
}

// reconcileSleepingPhase implements waking a sleeping deploy Runtime. A
// Sleeping CR always has no live Pod (idleSleepDeploy clears podName in the
// same write that sets the phase), so an unobserved wake request is the
// only thing that may ever start one here.
func (r *MarimoSessionReconciler) reconcileSleepingPhase(ctx context.Context, cr *marimohubv1alpha1.MarimoSession) (ctrl.Result, error) {
	wakeToken := cr.Annotations[runtimecontract.AnnotationWakeRequest]
	if wakeToken == "" || wakeToken == cr.Status.ObservedWakeRequest {
		return ctrl.Result{}, nil
	}
	hash := podTemplateHash(cr, r.Options)
	return r.startPod(ctx, cr, hash, 1, wakeToken)
}

// reconcileActivePhase covers every phase other than Sleeping and Failed:
// Pending, Starting, Ready, and the empty phase of a brand-new CR.
func (r *MarimoSessionReconciler) reconcileActivePhase(ctx context.Context, cr *marimohubv1alpha1.MarimoSession) (ctrl.Result, error) {
	pod, foreign, err := r.getOwnedPod(ctx, cr)
	if err != nil {
		return ctrl.Result{}, err
	}
	if foreign {
		return r.reconcileForeignCollision(ctx, cr, "Pod")
	}
	if pod != nil {
		return r.reconcileExistingPod(ctx, cr, pod)
	}
	return r.reconcileMissingPod(ctx, cr)
}

// reconcileExistingPod classifies a live, owned Pod and acts on its
// verdict. A spec.resources change is checked first and takes priority over
// whatever the Pod's current container status says: replacing the Pod for
// a deliberate resize is not something classifyPod needs to know about.
func (r *MarimoSessionReconciler) reconcileExistingPod(ctx context.Context, cr *marimohubv1alpha1.MarimoSession, pod *corev1.Pod) (ctrl.Result, error) {
	desiredHash := podTemplateHash(cr, r.Options)
	if cr.Status.PodTemplateHash != "" && cr.Status.PodTemplateHash != desiredHash {
		return r.replacePod(ctx, cr, pod)
	}

	verdict := classifyPod(pod)
	switch verdict.kind {
	case verdictImagePulling:
		return r.handleImagePulling(ctx, cr, pod)
	case verdictNodeUnknown:
		return r.handleNodeUnknown(ctx, cr, pod)
	case verdictNotReady:
		if wasReady(cr) {
			return r.handleReadinessLoss(ctx, cr, pod)
		}
		return ctrl.Result{}, nil
	case verdictReady:
		return r.handlePodReady(ctx, cr, pod)
	case verdictFailed:
		return r.failAndRemovePod(ctx, cr, pod, verdict.reason, verdict.message)
	default: // verdictStarting
		return ctrl.Result{}, nil
	}
}

// replacePod implements a deliberate resources-change replacement: persist
// progress through Conditions, clear podName, and delete the old Pod. The
// desired hash is deliberately left untouched here (it is only ever
// advanced once a Pod actually exists again) so the very next reconcile
// still sees "podName empty, hash differs from desired" and proceeds
// straight into starting the replacement through the ordinary missing-Pod
// path, rather than this function needing a second, parallel create step of
// its own to keep in sync with it.
func (r *MarimoSessionReconciler) replacePod(ctx context.Context, cr *marimohubv1alpha1.MarimoSession, pod *corev1.Pod) (ctrl.Result, error) {
	if err := r.applyStatus(ctx, cr, statusPatch{
		phase:   ptr.To(marimohubv1alpha1.RuntimePhaseStarting),
		podName: ptr.To(""),
		conditions: []metav1.Condition{
			condition(marimohubv1alpha1.ConditionTypeReady, metav1.ConditionFalse, marimohubv1alpha1.ReasonStarting, "replacing the Pod for an updated resources spec"),
		},
	}); err != nil {
		return ctrl.Result{}, err
	}
	if err := r.Delete(ctx, pod); err != nil && !apierrors.IsNotFound(err) {
		return ctrl.Result{}, err
	}
	return ctrl.Result{}, nil
}

// reconcileMissingPod decides what an absent Pod means and what, if
// anything, to do about it. Each branch corresponds to exactly one reason a
// Pod can legitimately be missing; the switch's ordering is itself the
// guard against starting a Pod from a state that must never start one.
func (r *MarimoSessionReconciler) reconcileMissingPod(ctx context.Context, cr *marimohubv1alpha1.MarimoSession) (ctrl.Result, error) {
	desiredHash := podTemplateHash(cr, r.Options)
	wakeToken := cr.Annotations[runtimecontract.AnnotationWakeRequest]
	unobservedWake := wakeToken != "" && wakeToken != cr.Status.ObservedWakeRequest
	resourcesChanged := cr.Status.PodTemplateHash != "" && cr.Status.PodTemplateHash != desiredHash
	quotaBlocked := isConditionFalseWithReason(cr, marimohubv1alpha1.ConditionTypeCapacityAvailable, marimohubv1alpha1.ReasonQuotaExceeded)

	switch {
	case quotaBlocked && !unobservedWake && !resourcesChanged:
		// A quota-blocked attempt stays blocked across reconciles: only an
		// explicit retry (a fresh wake token) or a resources change may try
		// again. There is no timed retry to schedule here at all.
		return ctrl.Result{}, nil

	case cr.Status.PodName != "":
		// The controller previously recorded a live Pod by this name and it
		// is gone now without this reconciler having deleted it for any
		// classified reason (deterministic failure and idle sleep both
		// clear podName before deleting, and a quota block never creates a
		// Pod at all) -- so its disappearance can only be infrastructure
		// loss: deleted out-of-band, its node lost, evicted, or it lost a
		// preemption race.
		return r.recoverFromInfrastructureLoss(ctx, cr, desiredHash, wakeToken)

	case cr.Status.Phase == marimohubv1alpha1.RuntimePhaseStarting && !resourcesChanged && !unobservedWake:
		// Interrupted before the Pod was ever created (for example, a
		// manager restart between persisting Starting and calling Create).
		// Resuming the same attempt needs no new wake acknowledgement and
		// must not count as a new attempt.
		return r.startPod(ctx, cr, desiredHash, cr.Status.Attempt, wakeToken)

	default:
		// First-ever start, or an explicit retry (fresh wake token or a
		// resources change) out of a blocked or otherwise stalled state.
		return r.startPod(ctx, cr, desiredHash, cr.Status.Attempt+1, wakeToken)
	}
}

// recoverFromInfrastructureLoss applies bounded exponential backoff with
// jitter before recreating a Pod lost to infrastructure. The Ready
// condition's own LastTransitionTime is the loss clock: the first call
// after a loss sets Ready False/InfrastructureLost and returns without
// creating anything, and only once that condition has been in place for at
// least the backoff window does this actually attempt a new Pod.
func (r *MarimoSessionReconciler) recoverFromInfrastructureLoss(ctx context.Context, cr *marimohubv1alpha1.MarimoSession, desiredHash, wakeToken string) (ctrl.Result, error) {
	readyCond := apimeta.FindStatusCondition(cr.Status.Conditions, marimohubv1alpha1.ConditionTypeReady)
	if readyCond == nil || readyCond.Status != metav1.ConditionFalse || readyCond.Reason != marimohubv1alpha1.ReasonInfrastructureLost {
		if err := r.applyStatus(ctx, cr, statusPatch{
			conditions: []metav1.Condition{
				condition(marimohubv1alpha1.ConditionTypeReady, metav1.ConditionFalse, marimohubv1alpha1.ReasonInfrastructureLost, "Pod is missing outside of any controller-initiated deletion"),
			},
		}); err != nil {
			return ctrl.Result{}, err
		}
		return ctrl.Result{RequeueAfter: r.Backoff.Duration(cr.Status.Attempt + 1)}, nil
	}

	wait := r.Backoff.Duration(cr.Status.Attempt + 1)
	elapsed := r.Clock.Now().Sub(readyCond.LastTransitionTime.Time)
	if elapsed < wait {
		return ctrl.Result{RequeueAfter: wait - elapsed}, nil
	}
	return r.startPod(ctx, cr, desiredHash, cr.Status.Attempt+1, wakeToken)
}

// handleImagePulling waits out Kubernetes' own image-pull backoff until
// ImagePullDeadline, anchored on the Pod's creation time, then fails it.
func (r *MarimoSessionReconciler) handleImagePulling(ctx context.Context, cr *marimohubv1alpha1.MarimoSession, pod *corev1.Pod) (ctrl.Result, error) {
	elapsed := r.Clock.Now().Sub(pod.CreationTimestamp.Time)
	if elapsed < r.ImagePullDeadline {
		return ctrl.Result{RequeueAfter: r.ImagePullDeadline - elapsed}, nil
	}
	return r.failAndRemovePod(ctx, cr, pod, marimohubv1alpha1.ReasonImageUnavailable, "image pull did not complete within the startup deadline")
}

// handleNodeUnknown waits out NodeLossDeadline, anchored on the CR's own
// Ready condition, before forcing the Pod's deletion. It never needs to
// create anything itself: once the Pod is actually gone, the ordinary
// missing-Pod path (recoverFromInfrastructureLoss) takes over and applies
// its own backoff before recreating.
func (r *MarimoSessionReconciler) handleNodeUnknown(ctx context.Context, cr *marimohubv1alpha1.MarimoSession, pod *corev1.Pod) (ctrl.Result, error) {
	readyCond := apimeta.FindStatusCondition(cr.Status.Conditions, marimohubv1alpha1.ConditionTypeReady)
	if readyCond == nil || readyCond.Status != metav1.ConditionFalse || readyCond.Reason != marimohubv1alpha1.ReasonInfrastructureLost {
		if err := r.applyStatus(ctx, cr, statusPatch{
			conditions: []metav1.Condition{
				condition(marimohubv1alpha1.ConditionTypeReady, metav1.ConditionFalse, marimohubv1alpha1.ReasonInfrastructureLost, "node is not reporting this Pod's status"),
			},
		}); err != nil {
			return ctrl.Result{}, err
		}
		return ctrl.Result{RequeueAfter: r.NodeLossDeadline}, nil
	}

	elapsed := r.Clock.Now().Sub(readyCond.LastTransitionTime.Time)
	if elapsed < r.NodeLossDeadline {
		return ctrl.Result{RequeueAfter: r.NodeLossDeadline - elapsed}, nil
	}
	if err := r.Delete(ctx, pod); err != nil && !apierrors.IsNotFound(err) {
		return ctrl.Result{}, err
	}
	return ctrl.Result{}, nil
}

// handleReadinessLoss implements the "readiness loss -> unhealthy timeout ->
// Failed" half of a Ready Pod becoming not-Ready. Routing is cleared the
// moment this is first observed (the Ready condition flips False
// immediately); only a sustained failure past UnhealthyTimeout ends the
// Runtime.
func (r *MarimoSessionReconciler) handleReadinessLoss(ctx context.Context, cr *marimohubv1alpha1.MarimoSession, pod *corev1.Pod) (ctrl.Result, error) {
	readyCond := apimeta.FindStatusCondition(cr.Status.Conditions, marimohubv1alpha1.ConditionTypeReady)
	if readyCond == nil || readyCond.Status != metav1.ConditionFalse || readyCond.Reason != marimohubv1alpha1.ReasonUnhealthy {
		if err := r.applyStatus(ctx, cr, statusPatch{
			conditions: []metav1.Condition{
				condition(marimohubv1alpha1.ConditionTypeReady, metav1.ConditionFalse, marimohubv1alpha1.ReasonUnhealthy, "Pod is running but failing its authenticated readiness check"),
			},
		}); err != nil {
			return ctrl.Result{}, err
		}
		return ctrl.Result{RequeueAfter: r.UnhealthyTimeout}, nil
	}

	elapsed := r.Clock.Now().Sub(readyCond.LastTransitionTime.Time)
	if elapsed < r.UnhealthyTimeout {
		return ctrl.Result{RequeueAfter: r.UnhealthyTimeout - elapsed}, nil
	}
	return r.failAndRemovePod(ctx, cr, pod, marimohubv1alpha1.ReasonRuntimeExited, "Pod did not recover readiness within the unhealthy timeout")
}

// handlePodReady persists the Ready transition and, only once that has
// happened, moves on to idle evaluation. A fresh transition into Ready --
// initial start, wake, resources replacement, or infrastructure recovery --
// always resets the idle clock and is never evaluated for idleness in the
// same reconcile; an already-Ready Pod that is still Ready falls straight
// through to the ordinary activity/idle check.
func (r *MarimoSessionReconciler) handlePodReady(ctx context.Context, cr *marimohubv1alpha1.MarimoSession, pod *corev1.Pod) (ctrl.Result, error) {
	wasAlreadyReady := cr.Status.Phase == marimohubv1alpha1.RuntimePhaseReady
	var startedAt time.Time
	if !wasAlreadyReady {
		if c := apimeta.FindStatusCondition(cr.Status.Conditions, marimohubv1alpha1.ConditionTypeReady); c != nil {
			startedAt = c.LastTransitionTime.Time
		} else {
			startedAt = cr.CreationTimestamp.Time
		}
	}

	patch := statusPatch{
		phase:                     ptr.To(marimohubv1alpha1.RuntimePhaseReady),
		podName:                   ptr.To(pod.Name),
		attempt:                   ptr.To(int32(0)),
		advanceObservedGeneration: true,
		conditions: []metav1.Condition{
			condition(marimohubv1alpha1.ConditionTypeReady, metav1.ConditionTrue, marimohubv1alpha1.ReasonReconcileSucceeded, "Pod is serving authenticated readiness checks"),
			condition(marimohubv1alpha1.ConditionTypeReconciled, metav1.ConditionTrue, marimohubv1alpha1.ReasonReconcileSucceeded, "reconciled successfully"),
		},
	}
	if !wasAlreadyReady {
		now := metav1.NewTime(r.Clock.Now())
		patch.lastActivity = &now
	}
	if err := r.applyStatus(ctx, cr, patch); err != nil {
		return ctrl.Result{}, err
	}

	if !wasAlreadyReady {
		r.Metrics.observeStartupDuration(cr.Spec.Mode, r.Clock.Now().Sub(startedAt))
		return ctrl.Result{}, nil
	}
	return r.evaluateActivity(ctx, cr)
}

// startPod persists Starting, the desired Pod-template hash, the attempt
// number, and a wake-request acknowledgement -- in that order, before ever
// calling Create -- so a crash between the status write and the Create call
// leaves a CR that safely resumes the same attempt (see
// reconcileMissingPod) instead of one that looks like it never started.
func (r *MarimoSessionReconciler) startPod(ctx context.Context, cr *marimohubv1alpha1.MarimoSession, hash string, attempt int32, wakeToken string) (ctrl.Result, error) {
	patch := statusPatch{
		phase:           ptr.To(marimohubv1alpha1.RuntimePhaseStarting),
		podTemplateHash: &hash,
		attempt:         &attempt,
		conditions: []metav1.Condition{
			condition(marimohubv1alpha1.ConditionTypeReady, metav1.ConditionFalse, marimohubv1alpha1.ReasonStarting, "starting a new Pod attempt"),
			condition(marimohubv1alpha1.ConditionTypeCapacityAvailable, metav1.ConditionTrue, marimohubv1alpha1.ReasonReconcileSucceeded, "attempting Pod creation"),
			condition(marimohubv1alpha1.ConditionTypeReconciled, metav1.ConditionFalse, marimohubv1alpha1.ReasonStarting, "starting a new Pod attempt"),
		},
	}
	if wakeToken != "" {
		patch.observedWakeRequest = &wakeToken
	}
	if err := r.applyStatus(ctx, cr, patch); err != nil {
		return ctrl.Result{}, err
	}

	pod := session.BuildPod(cr, r.Options)
	if err := r.Create(ctx, pod); err != nil {
		if apierrors.IsAlreadyExists(err) {
			// Something created it already -- most likely this exact
			// sequence, retried after a crash right after Create but before
			// the podName write below. The next reconcile treats it like
			// any other existing, owned Pod.
			return ctrl.Result{}, nil
		}
		if apierrors.IsForbidden(err) {
			return r.reconcileCreateForbidden(ctx, cr, pod, err)
		}
		return ctrl.Result{}, err
	}
	r.Metrics.observeAttempt(cr.Spec.Mode)
	return ctrl.Result{}, r.applyStatus(ctx, cr, statusPatch{podName: ptr.To(pod.Name)})
}

// reconcileCreateForbidden classifies a Pod-create Forbidden response as
// either confirmed quota exhaustion (retry only on an explicit new wake or
// a resources change, no timed retry) or a platform/configuration denial
// (fail outright: no amount of waiting resolves an RBAC, SCC, LimitRange,
// Pod Security, or webhook rejection on its own).
func (r *MarimoSessionReconciler) reconcileCreateForbidden(ctx context.Context, cr *marimohubv1alpha1.MarimoSession, pod *corev1.Pod, createErr error) (ctrl.Result, error) {
	confirmed, err := r.confirmQuotaExhaustion(ctx, cr.Namespace, pod, createErr)
	if err != nil {
		return ctrl.Result{}, err
	}
	if confirmed {
		r.Metrics.observeCapacityRejection(cr.Spec.Mode)
		message := "Pod creation was rejected by a namespace ResourceQuota; retry requires a new wake request or a resources change"
		return ctrl.Result{}, r.applyStatus(ctx, cr, statusPatch{
			conditions: []metav1.Condition{
				condition(marimohubv1alpha1.ConditionTypeCapacityAvailable, metav1.ConditionFalse, marimohubv1alpha1.ReasonQuotaExceeded, message),
				condition(marimohubv1alpha1.ConditionTypeReady, metav1.ConditionFalse, marimohubv1alpha1.ReasonQuotaExceeded, message),
				condition(marimohubv1alpha1.ConditionTypeReconciled, metav1.ConditionFalse, marimohubv1alpha1.ReasonQuotaExceeded, message),
			},
		})
	}
	return ctrl.Result{}, r.failCR(ctx, cr, marimohubv1alpha1.ReasonPlatformDenied, sanitizeAdmissionError(createErr))
}

// failAndRemovePod persists the deterministic failure before deleting the
// Pod, never the other way around: if the controller crashes between the
// two, the CR already and correctly says why it failed, rather than an
// observer finding a live Pod with no explanation or a deleted Pod with no
// recorded cause.
func (r *MarimoSessionReconciler) failAndRemovePod(ctx context.Context, cr *marimohubv1alpha1.MarimoSession, pod *corev1.Pod, reason, message string) (ctrl.Result, error) {
	if err := r.failCR(ctx, cr, reason, message); err != nil {
		return ctrl.Result{}, err
	}
	if err := r.Delete(ctx, pod); err != nil && !apierrors.IsNotFound(err) {
		return ctrl.Result{}, err
	}
	return ctrl.Result{}, nil
}

// failCR is the single path into the Failed phase. observedGeneration
// advances here because Failed, like Ready and Sleeping, is a stable,
// fully-processed outcome for the current generation -- there is nothing
// further this controller will do on its own until an authorized redeploy
// or explicit retry replaces the CR entirely.
func (r *MarimoSessionReconciler) failCR(ctx context.Context, cr *marimohubv1alpha1.MarimoSession, reason, message string) error {
	r.Metrics.observeFailure(reason)
	return r.applyStatus(ctx, cr, statusPatch{
		phase:                     ptr.To(marimohubv1alpha1.RuntimePhaseFailed),
		podName:                   ptr.To(""),
		advanceObservedGeneration: true,
		conditions: []metav1.Condition{
			condition(marimohubv1alpha1.ConditionTypeReady, metav1.ConditionFalse, reason, message),
			condition(marimohubv1alpha1.ConditionTypeReconciled, metav1.ConditionTrue, reason, message),
		},
	})
}

// SetupWithManager sets up the controller with the Manager.
func (r *MarimoSessionReconciler) SetupWithManager(mgr ctrl.Manager) error {
	return ctrl.NewControllerManagedBy(mgr).
		For(&marimohubv1alpha1.MarimoSession{}, builder.WithPredicates(specOrWakeActivityChanged())).
		Owns(&corev1.Pod{}).
		Owns(&corev1.Service{}).
		Watches(
			&corev1.Secret{},
			handler.EnqueueRequestsFromMapFunc(r.mapSecretToSession),
			builder.OnlyMetadata,
		).
		Named("marimosession").
		Complete(r)
}
