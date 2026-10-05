package controller

import (
	"time"

	"github.com/google/uuid"
	. "github.com/onsi/ginkgo/v2"
	. "github.com/onsi/gomega"

	corev1 "k8s.io/api/core/v1"
	"k8s.io/apimachinery/pkg/api/resource"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/types"
	clocktesting "k8s.io/utils/clock/testing"
	"sigs.k8s.io/controller-runtime/pkg/client"

	marimohubv1alpha1 "github.com/karthedew/marimohub/marimohub-operator/api/v1alpha1"
	"github.com/karthedew/marimohub/marimohub-operator/internal/runtimecontract"
	"github.com/karthedew/marimohub/marimohub-operator/internal/session"
)

// deployRuntime is newValidSession retargeted at deploy mode, in the shape
// DescribeTable entries take.
func deployRuntime() *marimohubv1alpha1.MarimoSession {
	return asDeploy(newValidSession(), 1)
}

// Covers state invariant 10 while the lost Pod still exists (deleted out of
// band and held Terminating, or evicted by the kubelet), the wake that
// collides with a slept Pod still terminating, and the guards that keep the
// controller's own deletions (idle sleep, credential revocation, resources
// replacement) ending where they always have. That Terminating window is
// where the kubelet publishes the stopped container's exit code, so every
// deleted Pod here is held in it with a finalizer. See
// state_machine_coverage_test.go for the full map.
var _ = Describe("Pods that go away outside the workload", func() {
	var (
		fakeClock *clocktesting.FakeClock
		r         *MarimoSessionReconciler
	)

	BeforeEach(func() {
		fakeClock = clocktesting.NewFakeClock(time.Now())
		r = newTestReconciler(fakeClock)
		r.Options.IdleTimeout = session.IdleTimeoutDefaults{EditSeconds: 30, RunSeconds: 30, DeploySeconds: 30}
	})

	// newReadySession creates a CR from newCR, brings it to Ready, and
	// deletes it when the spec ends.
	newReadySession := func(newCR func() *marimohubv1alpha1.MarimoSession) *marimohubv1alpha1.MarimoSession {
		cr := createSession(newCR())
		DeferCleanup(func() { deleteAndWait(cr) })
		bringToReady(r, cr)
		return cr
	}

	// reconcileWhilePodGoesAway reconciles cr and asserts what must hold
	// while its lost Pod still exists: no Failed phase, Ready cleared as
	// infrastructure loss, and a requeue that brings the controller back.
	reconcileWhilePodGoesAway := func(cr *marimohubv1alpha1.MarimoSession) {
		result, err := r.Reconcile(ctx, reconcileRequest(cr))
		Expect(err).NotTo(HaveOccurred())
		got := getSession(keyOf(cr))
		Expect(got.Status.Phase).NotTo(Equal(marimohubv1alpha1.RuntimePhaseFailed), got.Status.Message)
		readyCond := findCondition(got, marimohubv1alpha1.ConditionTypeReady)
		Expect(readyCond.Status).To(Equal(metav1.ConditionFalse))
		Expect(readyCond.Reason).To(Equal(marimohubv1alpha1.ReasonInfrastructureLost))
		Expect(result.RequeueAfter).To(BeNumerically(">", 0))
	}

	// expectRecreated steps past the infrastructure-loss backoff and
	// asserts the next reconcile starts a new Pod in place of lostPod.
	expectRecreated := func(cr *marimohubv1alpha1.MarimoSession, lostPod types.UID) {
		fakeClock.Step(r.Backoff.Max + time.Second)
		_, err := r.Reconcile(ctx, reconcileRequest(cr))
		Expect(err).NotTo(HaveOccurred())
		got := getSession(keyOf(cr))
		Expect(got.Status.Phase).To(Equal(marimohubv1alpha1.RuntimePhaseStarting))
		Expect(got.Status.PodName).NotTo(BeEmpty())
		Expect(getPod(cr).UID).NotTo(Equal(lostPod))
	}

	DescribeTable("waits out a Pod deleted out of band, without failing on its stopped container, then recreates it",
		func(newCR func() *marimohubv1alpha1.MarimoSession, exitCode int32, reason string) {
			cr := newReadySession(newCR)
			lost := getPod(cr)

			// kubectl delete pod, a drain, or a preemption: the Pod gets a
			// deletionTimestamp, the kubelet stops marimo, and it publishes
			// the container's exit before the Pod object itself goes away.
			holdPod(cr)
			Expect(k8sClient.Delete(ctx, getPod(cr))).To(Succeed())
			markMainExited(getPod(cr), exitCode, reason)

			reconcileWhilePodGoesAway(cr)
			reconcileWhilePodGoesAway(cr)
			Expect(getPod(cr).UID).To(Equal(lost.UID), "a terminating Pod is waited out, never replaced early")

			releasePod(cr)
			expectRecreated(cr, lost.UID)
		},
		Entry("deploy Runtime, marimo exits 143 on SIGTERM", deployRuntime, int32(143), "Error"),
		Entry("deploy Runtime, marimo exits 0 on SIGTERM", deployRuntime, int32(0), "Completed"),
		Entry("edit Session, marimo exits 143 on SIGTERM", newValidSession, int32(143), "Error"),
	)

	It("deletes a Pod the kubelet evicted, without failing on its killed container, then recreates it", func() {
		cr := newReadySession(deployRuntime)
		lost := getPod(cr)

		markEvicted(getPod(cr))
		reconcileWhilePodGoesAway(cr)
		// Nothing else ever deletes an evicted Pod, so the controller must.
		Expect(getPodIfExists(cr)).To(BeNil())

		expectRecreated(cr, lost.UID)
	})

	It("stops routing to a live Pod marked for disruption without deleting it, and serves from it again if the disruption is abandoned", func() {
		cr := newReadySession(deployRuntime)
		marked := getPod(cr)

		// Preemption, the eviction API, and the taint manager mark the Pod
		// and then delete it themselves.
		markDisruptionTarget(getPod(cr), corev1.ConditionTrue)
		reconcileWhilePodGoesAway(cr)
		Expect(getPod(cr).UID).To(Equal(marked.UID), "deleting a live Pod is left to whatever disrupted it")

		markDisruptionTarget(getPod(cr), corev1.ConditionFalse)
		_, err := r.Reconcile(ctx, reconcileRequest(cr))
		Expect(err).NotTo(HaveOccurred())
		got := getSession(keyOf(cr))
		Expect(got.Status.Phase).To(Equal(marimohubv1alpha1.RuntimePhaseReady))
		Expect(findCondition(got, marimohubv1alpha1.ConditionTypeReady).Status).To(Equal(metav1.ConditionTrue))
		Expect(getPod(cr).UID).To(Equal(marked.UID))
	})

	It("requeues a wake whose Pod create collides with the slept Pod still terminating, and starts the woken Pod once that one is gone", func() {
		cr := newReadySession(deployRuntime)
		slept := getPod(cr)
		holdPod(cr)

		fakeClock.Step(31 * time.Second)
		_, err := r.Reconcile(ctx, reconcileRequest(cr))
		Expect(err).NotTo(HaveOccurred())
		Expect(getSession(keyOf(cr)).Status.Phase).To(Equal(marimohubv1alpha1.RuntimePhaseSleeping))
		Expect(getPod(cr).DeletionTimestamp).NotTo(BeNil())

		token := uuid.NewString()
		woken := getSession(keyOf(cr))
		woken.Annotations = map[string]string{runtimecontract.AnnotationWakeRequest: token}
		Expect(k8sClient.Update(ctx, woken)).To(Succeed())

		result, err := r.Reconcile(ctx, reconcileRequest(cr))
		Expect(err).NotTo(HaveOccurred())
		got := getSession(keyOf(cr))
		Expect(got.Status.Phase).To(Equal(marimohubv1alpha1.RuntimePhaseStarting))
		Expect(got.Status.ObservedWakeRequest).To(Equal(token))
		Expect(getPod(cr).UID).To(Equal(slept.UID), "the create hit AlreadyExists on the slept Pod")
		Expect(result.RequeueAfter).To(BeNumerically(">", 0), "nothing else guarantees the controller comes back to finish this wake")

		// The slept Pod's marimo then exits on SIGTERM. That is its own
		// shutdown, not the woken attempt failing.
		markMainExited(getPod(cr), 143, "Error")
		reconcileWhilePodGoesAway(cr)

		releasePod(cr)
		_, err = r.Reconcile(ctx, reconcileRequest(cr))
		Expect(err).NotTo(HaveOccurred())
		got = getSession(keyOf(cr))
		Expect(got.Status.Phase).To(Equal(marimohubv1alpha1.RuntimePhaseStarting))
		Expect(got.Status.Attempt).To(Equal(int32(1)), "finishing the woken attempt is not a new attempt")
		Expect(got.Status.PodName).NotTo(BeEmpty())
		Expect(getPod(cr).UID).NotTo(Equal(slept.UID))
	})

	It("leaves an idle-slept deploy Runtime Sleeping, untouched and with no new Pod, while and after its Pod terminates", func() {
		cr := newReadySession(deployRuntime)
		holdPod(cr)

		fakeClock.Step(31 * time.Second)
		_, err := r.Reconcile(ctx, reconcileRequest(cr))
		Expect(err).NotTo(HaveOccurred())
		slept := getSession(keyOf(cr))
		Expect(slept.Status.Phase).To(Equal(marimohubv1alpha1.RuntimePhaseSleeping))
		Expect(slept.Status.PodName).To(BeEmpty())
		Expect(getPod(cr).DeletionTimestamp).NotTo(BeNil())

		markMainExited(getPod(cr), 143, "Error")
		_, err = r.Reconcile(ctx, reconcileRequest(cr))
		Expect(err).NotTo(HaveOccurred())
		Expect(getSession(keyOf(cr)).ResourceVersion).To(Equal(slept.ResourceVersion), "the slept Pod's exit is no news to a Sleeping Runtime")

		releasePod(cr)
		_, err = r.Reconcile(ctx, reconcileRequest(cr))
		Expect(err).NotTo(HaveOccurred())
		Expect(getSession(keyOf(cr)).ResourceVersion).To(Equal(slept.ResourceVersion))
		Expect(getPodIfExists(cr)).To(BeNil())
	})

	It("keeps a Session whose credentials were revoked Pending while its Pod terminates, and starts a new Pod only once the Secret is repaired and the old Pod is gone", func() {
		cr := newReadySession(newValidSession)
		lost := getPod(cr)
		holdPod(cr)

		secret := &corev1.Secret{}
		Expect(k8sClient.Get(ctx, client.ObjectKey{Namespace: cr.Namespace, Name: runtimecontract.SecretName(cr.Name)}, secret)).To(Succeed())
		Expect(k8sClient.Delete(ctx, secret)).To(Succeed())
		_, err := r.Reconcile(ctx, reconcileRequest(cr))
		Expect(err).NotTo(HaveOccurred())
		revoked := getSession(keyOf(cr))
		Expect(revoked.Status.Phase).To(Equal(marimohubv1alpha1.RuntimePhasePending))
		Expect(getPod(cr).DeletionTimestamp).NotTo(BeNil())

		markMainExited(getPod(cr), 143, "Error")
		_, err = r.Reconcile(ctx, reconcileRequest(cr))
		Expect(err).NotTo(HaveOccurred())
		Expect(getSession(keyOf(cr)).ResourceVersion).To(Equal(revoked.ResourceVersion))

		// Repairing the Secret before the old Pod is gone must neither fail
		// the Session on that Pod's exit nor start a second Pod beside it.
		createValidSecret(cr)
		reconcileWhilePodGoesAway(cr)
		Expect(getPod(cr).UID).To(Equal(lost.UID))

		releasePod(cr)
		_, err = r.Reconcile(ctx, reconcileRequest(cr))
		Expect(err).NotTo(HaveOccurred())
		Expect(getSession(keyOf(cr)).Status.Phase).To(Equal(marimohubv1alpha1.RuntimePhaseStarting))
		Expect(getPod(cr).UID).NotTo(Equal(lost.UID))
	})

	It("still replaces the Pod for a spec.resources change while the old Pod terminates, without calling that infrastructure loss", func() {
		cr := newReadySession(newValidSession)
		replaced := getPod(cr)
		holdPod(cr)

		resized := getSession(keyOf(cr))
		resized.Spec.Resources = &corev1.ResourceRequirements{
			Requests: corev1.ResourceList{corev1.ResourceCPU: resource.MustParse("500m")},
			Limits:   corev1.ResourceList{corev1.ResourceCPU: resource.MustParse("2")},
		}
		Expect(k8sClient.Update(ctx, resized)).To(Succeed())
		_, err := r.Reconcile(ctx, reconcileRequest(cr))
		Expect(err).NotTo(HaveOccurred())
		Expect(getPod(cr).DeletionTimestamp).NotTo(BeNil())

		markMainExited(getPod(cr), 143, "Error")
		_, err = r.Reconcile(ctx, reconcileRequest(cr))
		Expect(err).NotTo(HaveOccurred())
		replacing := getSession(keyOf(cr))
		Expect(replacing.Status.Phase).To(Equal(marimohubv1alpha1.RuntimePhaseStarting))
		Expect(findCondition(replacing, marimohubv1alpha1.ConditionTypeReady).Reason).To(Equal(marimohubv1alpha1.ReasonStarting))

		releasePod(cr)
		_, err = r.Reconcile(ctx, reconcileRequest(cr))
		Expect(err).NotTo(HaveOccurred())
		got := getSession(keyOf(cr))
		Expect(got.Status.PodName).NotTo(BeEmpty())
		Expect(got.Status.PodTemplateHash).To(Equal(podTemplateHash(got, r.Options)))
		Expect(getPod(cr).UID).NotTo(Equal(replaced.UID))
	})
})
