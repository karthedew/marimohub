package controller

import (
	"time"

	. "github.com/onsi/ginkgo/v2"
	. "github.com/onsi/gomega"

	corev1 "k8s.io/api/core/v1"
	"k8s.io/apimachinery/pkg/api/resource"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	clocktesting "k8s.io/utils/clock/testing"

	marimohubv1alpha1 "github.com/karthedew/marimohub/marimohub-operator/api/v1alpha1"
)

// startedSession creates cr with a valid Secret and drives it to a live,
// owned Pod, returning that Pod.
func startedSession(r *MarimoSessionReconciler, cr *marimohubv1alpha1.MarimoSession) {
	createValidSecret(cr)
	Expect(reconcileUntil(r, cr, func(s *marimohubv1alpha1.MarimoSession) bool { return s.Status.PodName != "" })).To(Succeed())
}

// Covers state invariants 9, 10, and 12, and the Failure Classification
// rows for source/init/main exits, OOM, and image pull. See
// state_machine_coverage_test.go for the full map.
var _ = Describe("failure classification and recovery", func() {
	var (
		cr        *marimohubv1alpha1.MarimoSession
		fakeClock *clocktesting.FakeClock
		r         *MarimoSessionReconciler
	)

	BeforeEach(func() {
		cr = createSession(newValidSession())
		fakeClock = clocktesting.NewFakeClock(cr.CreationTimestamp.Time)
		r = newTestReconciler(fakeClock)
	})

	AfterEach(func() {
		deleteAndWait(cr)
	})

	assertFailed := func(reason string) *marimohubv1alpha1.MarimoSession {
		_, err := r.Reconcile(ctx, reconcileRequest(cr))
		Expect(err).NotTo(HaveOccurred())
		got := getSession(keyOf(cr))
		Expect(got.Status.Phase).To(Equal(marimohubv1alpha1.RuntimePhaseFailed))
		Expect(got.Status.PodName).To(BeEmpty())
		readyCond := findCondition(got, marimohubv1alpha1.ConditionTypeReady)
		Expect(readyCond.Reason).To(Equal(reason))
		Expect(getPodIfExists(got)).To(BeNil())
		return got
	}

	It("fails SourceUnavailable on a fetcher auth failure and removes the Pod", func() {
		startedSession(r, cr)
		markInitFailed(getPod(cr), 10, "Error", "authentication failed (HTTP 401)")
		assertFailed(marimohubv1alpha1.ReasonSourceUnavailable)
	})

	It("fails RuntimeExited on a non-zero marimo exit and never recreates the Pod afterward", func() {
		startedSession(r, cr)
		markMainExited(getPod(cr), 1, "Error")
		assertFailed(marimohubv1alpha1.ReasonRuntimeExited)

		// Guard: reconciling a Failed CR again must never create a Pod.
		_, err := r.Reconcile(ctx, reconcileRequest(cr))
		Expect(err).NotTo(HaveOccurred())
		Expect(getPodIfExists(getSession(keyOf(cr)))).To(BeNil())
		Expect(getSession(keyOf(cr)).Status.Phase).To(Equal(marimohubv1alpha1.RuntimePhaseFailed))
	})

	It("fails RuntimeExited on a zero exit before ever serving", func() {
		startedSession(r, cr)
		markMainExited(getPod(cr), 0, "Completed")
		assertFailed(marimohubv1alpha1.ReasonRuntimeExited)
	})

	It("fails OOMKilled on the main container", func() {
		startedSession(r, cr)
		markMainExited(getPod(cr), 137, containerReasonOOMKilled)
		assertFailed(marimohubv1alpha1.ReasonOOMKilled)
	})

	It("fails ImageUnavailable once a Pod stuck pulling its image exceeds the startup deadline", func() {
		startedSession(r, cr)
		markMainWaiting(getPod(cr), "ImagePullBackOff")

		result, err := r.Reconcile(ctx, reconcileRequest(cr))
		Expect(err).NotTo(HaveOccurred())
		Expect(result.RequeueAfter).To(BeNumerically(">", 0))
		Expect(getSession(keyOf(cr)).Status.Phase).NotTo(Equal(marimohubv1alpha1.RuntimePhaseFailed))

		fakeClock.Step(r.ImagePullDeadline + time.Second)
		assertFailed(marimohubv1alpha1.ReasonImageUnavailable)
	})

	It("clears Ready immediately on readiness loss, then fails after the unhealthy timeout without recreating the Pod", func() {
		startedSession(r, cr)
		markMainReady(getPod(cr))
		_, err := r.Reconcile(ctx, reconcileRequest(cr))
		Expect(err).NotTo(HaveOccurred())
		Expect(getSession(keyOf(cr)).Status.Phase).To(Equal(marimohubv1alpha1.RuntimePhaseReady))

		markMainNotReady(getPod(cr))
		result, err := r.Reconcile(ctx, reconcileRequest(cr))
		Expect(err).NotTo(HaveOccurred())
		got := getSession(keyOf(cr))
		readyCond := findCondition(got, marimohubv1alpha1.ConditionTypeReady)
		Expect(readyCond.Status).To(Equal(metav1.ConditionFalse))
		Expect(readyCond.Reason).To(Equal(marimohubv1alpha1.ReasonUnhealthy))
		Expect(result.RequeueAfter).To(BeNumerically(">", 0))

		fakeClock.Step(r.UnhealthyTimeout + time.Second)
		assertFailed(marimohubv1alpha1.ReasonRuntimeExited)
	})

	It("recovers readiness within the grace period without ever failing", func() {
		startedSession(r, cr)
		markMainReady(getPod(cr))
		_, err := r.Reconcile(ctx, reconcileRequest(cr))
		Expect(err).NotTo(HaveOccurred())

		markMainNotReady(getPod(cr))
		_, err = r.Reconcile(ctx, reconcileRequest(cr))
		Expect(err).NotTo(HaveOccurred())

		fakeClock.Step(r.UnhealthyTimeout / 2)
		markMainReady(getPod(cr))
		_, err = r.Reconcile(ctx, reconcileRequest(cr))
		Expect(err).NotTo(HaveOccurred())

		got := getSession(keyOf(cr))
		Expect(got.Status.Phase).To(Equal(marimohubv1alpha1.RuntimePhaseReady))
		readyCond := findCondition(got, marimohubv1alpha1.ConditionTypeReady)
		Expect(readyCond.Status).To(Equal(metav1.ConditionTrue))
	})

	It("classifies a node that stops reporting as Unknown and recovers infrastructure rather than failing", func() {
		startedSession(r, cr)
		markMainReady(getPod(cr))
		_, err := r.Reconcile(ctx, reconcileRequest(cr))
		Expect(err).NotTo(HaveOccurred())

		markNodeUnknown(getPod(cr))
		result, err := r.Reconcile(ctx, reconcileRequest(cr))
		Expect(err).NotTo(HaveOccurred())
		got := getSession(keyOf(cr))
		readyCond := findCondition(got, marimohubv1alpha1.ConditionTypeReady)
		Expect(readyCond.Status).To(Equal(metav1.ConditionFalse))
		Expect(readyCond.Reason).To(Equal(marimohubv1alpha1.ReasonInfrastructureLost))
		Expect(result.RequeueAfter).To(BeNumerically(">", 0))
		Expect(got.Status.Phase).NotTo(Equal(marimohubv1alpha1.RuntimePhaseFailed))

		// Past the node-loss deadline, the controller forces deletion of
		// the unresponsive Pod and, once it is gone, recreates it through
		// the ordinary infrastructure-recovery path -- it never fails the
		// Runtime for a node-loss verdict.
		fakeClock.Step(r.NodeLossDeadline + time.Second)
		_, err = r.Reconcile(ctx, reconcileRequest(cr))
		Expect(err).NotTo(HaveOccurred())
		Eventually(func() bool { return getPodIfExists(getSession(keyOf(cr))) == nil }).Should(BeTrue())

		fakeClock.Step(r.Backoff.Max + time.Second)
		_, err = r.Reconcile(ctx, reconcileRequest(cr))
		Expect(err).NotTo(HaveOccurred())
		Expect(getPodIfExists(getSession(keyOf(cr)))).NotTo(BeNil())
		Expect(getSession(keyOf(cr)).Status.Phase).NotTo(Equal(marimohubv1alpha1.RuntimePhaseFailed))
	})

	It("recreates a Pod lost to infrastructure with bounded backoff, waiting for NotFound first", func() {
		startedSession(r, cr)
		markMainReady(getPod(cr))
		_, err := r.Reconcile(ctx, reconcileRequest(cr))
		Expect(err).NotTo(HaveOccurred())

		// Simulate the Pod being deleted out-of-band (manual deletion, node
		// loss, eviction, or preemption all look identical from here: the
		// Pod this controller remembers creating is simply gone).
		Expect(k8sClient.Delete(ctx, getPod(cr))).To(Succeed())
		Eventually(func() bool { return getPodIfExists(getSession(keyOf(cr))) == nil }).Should(BeTrue())

		result, err := r.Reconcile(ctx, reconcileRequest(cr))
		Expect(err).NotTo(HaveOccurred())
		got := getSession(keyOf(cr))
		readyCond := findCondition(got, marimohubv1alpha1.ConditionTypeReady)
		Expect(readyCond.Reason).To(Equal(marimohubv1alpha1.ReasonInfrastructureLost))
		Expect(result.RequeueAfter).To(BeNumerically(">", 0))
		Expect(getPodIfExists(got)).To(BeNil(), "must not create a replacement before its backoff window elapses")

		fakeClock.Step(result.RequeueAfter + time.Second)
		_, err = r.Reconcile(ctx, reconcileRequest(cr))
		Expect(err).NotTo(HaveOccurred())
		Expect(getPodIfExists(getSession(keyOf(cr)))).NotTo(BeNil())
	})

	It("replaces the Pod on a spec.resources change and reports progress through Conditions", func() {
		startedSession(r, cr)
		markMainReady(getPod(cr))
		_, err := r.Reconcile(ctx, reconcileRequest(cr))
		Expect(err).NotTo(HaveOccurred())
		before := getSession(keyOf(cr))
		Expect(before.Status.ObservedGeneration).To(Equal(before.Generation))

		fresh := getSession(keyOf(cr))
		fresh.Spec.Resources = &corev1.ResourceRequirements{
			Requests: corev1.ResourceList{corev1.ResourceCPU: resource.MustParse("500m")},
			Limits:   corev1.ResourceList{corev1.ResourceCPU: resource.MustParse("2")},
		}
		Expect(k8sClient.Update(ctx, fresh)).To(Succeed())

		_, err = r.Reconcile(ctx, reconcileRequest(fresh))
		Expect(err).NotTo(HaveOccurred())
		got := getSession(keyOf(cr))
		Expect(got.Status.Phase).To(Equal(marimohubv1alpha1.RuntimePhaseStarting))
		Expect(got.Status.PodName).To(BeEmpty())
		readyCond := findCondition(got, marimohubv1alpha1.ConditionTypeReady)
		Expect(readyCond.Status).To(Equal(metav1.ConditionFalse))
		Expect(readyCond.Reason).To(Equal(marimohubv1alpha1.ReasonStarting))
		// observedGeneration must lag the new generation until the
		// replacement Pod is actually reconciled to Ready.
		Expect(got.Status.ObservedGeneration).To(BeNumerically("<", got.Generation))

		Eventually(func() bool { return getPodIfExists(got) == nil }).Should(BeTrue())
		_, err = r.Reconcile(ctx, reconcileRequest(got))
		Expect(err).NotTo(HaveOccurred())
		Expect(getSession(keyOf(cr)).Status.PodName).NotTo(BeEmpty())
	})

	It("does not replace the Pod when only idleTimeoutSeconds changes", func() {
		startedSession(r, cr)
		markMainReady(getPod(cr))
		_, err := r.Reconcile(ctx, reconcileRequest(cr))
		Expect(err).NotTo(HaveOccurred())
		podBefore := getPod(cr).UID

		fresh := getSession(keyOf(cr))
		timeout := int32(60)
		fresh.Spec.IdleTimeoutSeconds = &timeout
		Expect(k8sClient.Update(ctx, fresh)).To(Succeed())

		_, err = r.Reconcile(ctx, reconcileRequest(fresh))
		Expect(err).NotTo(HaveOccurred())

		got := getSession(keyOf(cr))
		Expect(got.Status.Phase).To(Equal(marimohubv1alpha1.RuntimePhaseReady))
		Expect(getPod(got).UID).To(Equal(podBefore), "an idle-timeout-only change must never replace the Pod")
	})
})
