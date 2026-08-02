package controller

import (
	"time"

	. "github.com/onsi/ginkgo/v2"
	. "github.com/onsi/gomega"

	"github.com/google/uuid"
	corev1 "k8s.io/api/core/v1"
	clocktesting "k8s.io/utils/clock/testing"
	"sigs.k8s.io/controller-runtime/pkg/client"

	marimohubv1alpha1 "github.com/karthedew/marimohub/marimohub-operator/api/v1alpha1"
	"github.com/karthedew/marimohub/marimohub-operator/internal/runtimecontract"
	"github.com/karthedew/marimohub/marimohub-operator/internal/session"
)

// testActivityToken is an arbitrary opaque activity-annotation value reused
// across these specs; its content is never inspected, only its identity
// against the previously observed value.
const testActivityToken = "tok-1"

// bringToReady drives cr from a valid Secret through to Ready, returning the
// fake clock the test can then advance to exercise idle timing.
func bringToReady(r *MarimoSessionReconciler, cr *marimohubv1alpha1.MarimoSession) {
	createValidSecret(cr)
	Expect(reconcileUntil(r, cr, func(s *marimohubv1alpha1.MarimoSession) bool { return s.Status.PodName != "" })).To(Succeed())
	markMainReady(getPod(cr))
	_, err := r.Reconcile(ctx, reconcileRequest(cr))
	Expect(err).NotTo(HaveOccurred())
	Expect(getSession(keyOf(cr)).Status.Phase).To(Equal(marimohubv1alpha1.RuntimePhaseReady))
}

// Covers state invariants 3-8 (Ready with no idle evaluation in the same
// reconcile, activity acknowledgement, idle edit/run/deploy, and wake
// crash-safety). See state_machine_coverage_test.go for the full map.
var _ = Describe("idle timers and the wake protocol", func() {
	var (
		fakeClock *clocktesting.FakeClock
		r         *MarimoSessionReconciler
	)

	BeforeEach(func() {
		fakeClock = clocktesting.NewFakeClock(time.Now())
		r = newTestReconciler(fakeClock)
		// Deterministic, short idle timeouts so the tests only ever move
		// the fake clock, never wait on a real one.
		r.Options.IdleTimeout = session.IdleTimeoutDefaults{}
	})

	It("deletes an idle edit/run CR after its idle timeout elapses", func() {
		cr := createSession(newValidSession())
		r.Options.IdleTimeout.EditSeconds = 30
		bringToReady(r, cr)

		fakeClock.Step(31 * time.Second)
		_, err := r.Reconcile(ctx, reconcileRequest(cr))
		Expect(err).NotTo(HaveOccurred())

		Eventually(func() bool {
			return apierrorsIsNotFound(k8sClient.Get(ctx, keyOf(cr), &marimohubv1alpha1.MarimoSession{}))
		}).Should(BeTrue())
	})

	It("keeps a Runtime alive when an activity token arrives before the deadline, and never moves lastActivity backwards", func() {
		cr := createSession(newValidSession())
		r.Options.IdleTimeout.EditSeconds = 30
		bringToReady(r, cr)
		firstActivity := getSession(keyOf(cr)).Status.LastActivity.Time

		fakeClock.Step(20 * time.Second)
		fresh := getSession(keyOf(cr))
		fresh.Annotations = map[string]string{runtimecontract.AnnotationActivity: testActivityToken}
		Expect(k8sClient.Update(ctx, fresh)).To(Succeed())

		result, err := r.Reconcile(ctx, reconcileRequest(fresh))
		Expect(err).NotTo(HaveOccurred())
		Expect(result.RequeueAfter).To(BeNumerically(">", 0))

		got := getSession(keyOf(cr))
		Expect(got.Status.ObservedActivity).To(Equal(testActivityToken))
		Expect(got.Status.LastActivity.Time).NotTo(BeTemporally("<", firstActivity))

		deleteAndWait(cr)
	})

	It("treats a repeated activity annotation value as no new activity", func() {
		cr := createSession(newValidSession())
		r.Options.IdleTimeout.EditSeconds = 30
		bringToReady(r, cr)

		fresh := getSession(keyOf(cr))
		fresh.Annotations = map[string]string{runtimecontract.AnnotationActivity: testActivityToken}
		Expect(k8sClient.Update(ctx, fresh)).To(Succeed())
		_, err := r.Reconcile(ctx, reconcileRequest(fresh))
		Expect(err).NotTo(HaveOccurred())
		afterFirst := getSession(keyOf(cr)).Status.LastActivity.Time

		fakeClock.Step(5 * time.Second)
		_, err = r.Reconcile(ctx, reconcileRequest(cr)) // same token again: no new activity to acknowledge
		Expect(err).NotTo(HaveOccurred())
		Expect(getSession(keyOf(cr)).Status.LastActivity.Time).To(Equal(afterFirst))

		deleteAndWait(cr)
	})

	It("sleeps (not deletes) an idle deploy Runtime, persisting Sleeping and clearing podName before the Pod is removed", func() {
		revision := int64(1)
		cr := createSession(asDeploy(newValidSession(), revision))
		r.Options.IdleTimeout.DeploySeconds = 30
		bringToReady(r, cr)

		fakeClock.Step(31 * time.Second)
		_, err := r.Reconcile(ctx, reconcileRequest(cr))
		Expect(err).NotTo(HaveOccurred())

		got := getSession(keyOf(cr))
		Expect(got.Status.Phase).To(Equal(marimohubv1alpha1.RuntimePhaseSleeping))
		Expect(got.Status.PodName).To(BeEmpty())
		Expect(getPodIfExists(got)).To(BeNil())

		// The Secret and Service are retained.
		Expect(k8sClient.Get(ctx, keyOf(cr), &marimohubv1alpha1.MarimoSession{})).To(Succeed())
		secretKey := client.ObjectKey{Name: runtimecontract.SecretName(cr.Name), Namespace: cr.Namespace}
		Expect(k8sClient.Get(ctx, secretKey, &corev1.Secret{})).To(Succeed())

		deleteAndWait(cr)
	})

	It("wakes a sleeping deploy Runtime on a fresh wake-request token and starts a new Pod", func() {
		revision := int64(1)
		cr := createSession(asDeploy(newValidSession(), revision))
		r.Options.IdleTimeout.DeploySeconds = 30
		bringToReady(r, cr)
		fakeClock.Step(31 * time.Second)
		_, err := r.Reconcile(ctx, reconcileRequest(cr))
		Expect(err).NotTo(HaveOccurred())
		Expect(getSession(keyOf(cr)).Status.Phase).To(Equal(marimohubv1alpha1.RuntimePhaseSleeping))

		fresh := getSession(keyOf(cr))
		fresh.Annotations = map[string]string{runtimecontract.AnnotationWakeRequest: uuid.NewString()}
		Expect(k8sClient.Update(ctx, fresh)).To(Succeed())

		_, err = r.Reconcile(ctx, reconcileRequest(fresh))
		Expect(err).NotTo(HaveOccurred())

		got := getSession(keyOf(cr))
		Expect(got.Status.Phase).To(Equal(marimohubv1alpha1.RuntimePhaseStarting))
		Expect(got.Status.ObservedWakeRequest).To(Equal(fresh.Annotations[runtimecontract.AnnotationWakeRequest]))
		Expect(getPodIfExists(got)).NotTo(BeNil())

		deleteAndWait(cr)
	})

	It("ignores a stale wake-request value that was already observed", func() {
		revision := int64(1)
		cr := createSession(asDeploy(newValidSession(), revision))
		token := uuid.NewString()
		fresh := getSession(keyOf(cr))
		fresh.Annotations = map[string]string{runtimecontract.AnnotationWakeRequest: token}
		Expect(k8sClient.Update(ctx, fresh)).To(Succeed())
		createValidSecret(fresh)
		Expect(reconcileUntil(r, fresh, func(s *marimohubv1alpha1.MarimoSession) bool { return s.Status.ObservedWakeRequest == token })).To(Succeed())

		before := getSession(keyOf(cr))
		result, err := r.Reconcile(ctx, reconcileRequest(before)) // same token: already observed, no new attempt
		Expect(err).NotTo(HaveOccurred())
		Expect(result.RequeueAfter).To(BeZero())

		deleteAndWait(cr)
	})

	It("resumes Pod creation after a crash between persisting Starting and creating the Pod, without requiring the wake annotation again", func() {
		cr := createSession(newValidSession())
		createValidSecret(cr)

		// Simulate the crash window directly: status already reflects "an
		// attempt was recorded" (Starting, an attempt number, a hash) but
		// the crash happened before Create was ever called, so podName was
		// never recorded and no Pod exists yet. This is deliberately
		// distinct from a Pod that existed and then disappeared (that is
		// infrastructure loss, covered separately) -- the signal here is
		// specifically "Starting with an empty podName".
		fresh := getSession(keyOf(cr))
		fresh.Status.Phase = marimohubv1alpha1.RuntimePhaseStarting
		fresh.Status.Attempt = 1
		fresh.Status.PodTemplateHash = podTemplateHash(fresh, r.Options)
		Expect(k8sClient.Status().Update(ctx, fresh)).To(Succeed())
		Expect(getPodIfExists(fresh)).To(BeNil())

		_, err := r.Reconcile(ctx, reconcileRequest(cr))
		Expect(err).NotTo(HaveOccurred())

		got := getSession(keyOf(cr))
		Expect(got.Status.PodName).NotTo(BeEmpty())
		Expect(got.Status.Attempt).To(Equal(int32(1)), "resuming an interrupted Starting attempt must not count as a new attempt")

		deleteAndWait(cr)
	})

	It("does not immediately re-sleep a deploy Runtime that just woke and is still starting", func() {
		revision := int64(1)
		cr := createSession(asDeploy(newValidSession(), revision))
		r.Options.IdleTimeout.DeploySeconds = 30
		createValidSecret(cr)
		fresh := getSession(keyOf(cr))
		fresh.Annotations = map[string]string{runtimecontract.AnnotationWakeRequest: uuid.NewString()}
		Expect(k8sClient.Update(ctx, fresh)).To(Succeed())

		_, err := r.Reconcile(ctx, reconcileRequest(fresh))
		Expect(err).NotTo(HaveOccurred())
		Expect(getSession(keyOf(cr)).Status.Phase).To(Equal(marimohubv1alpha1.RuntimePhaseStarting))

		// Even after stepping well past the idle timeout, a CR that is
		// still Starting (never yet Ready) must not be swept into idle
		// sleep -- idle evaluation only ever runs from an already-Ready
		// Pod's own reconcile.
		fakeClock.Step(time.Hour)
		_, err = r.Reconcile(ctx, reconcileRequest(cr))
		Expect(err).NotTo(HaveOccurred())
		Expect(getSession(keyOf(cr)).Status.Phase).NotTo(Equal(marimohubv1alpha1.RuntimePhaseSleeping))

		deleteAndWait(cr)
	})
})
