package controller

import (
	"errors"

	. "github.com/onsi/ginkgo/v2"
	. "github.com/onsi/gomega"

	"github.com/google/uuid"
	corev1 "k8s.io/api/core/v1"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	"k8s.io/apimachinery/pkg/api/resource"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/runtime/schema"
	clocktesting "k8s.io/utils/clock/testing"
	"sigs.k8s.io/controller-runtime/pkg/client"

	marimohubv1alpha1 "github.com/karthedew/marimohub/marimohub-operator/api/v1alpha1"
	"github.com/karthedew/marimohub/marimohub-operator/internal/runtimecontract"
	"github.com/karthedew/marimohub/marimohub-operator/internal/session"
)

// Covers state invariant 8 and the capacity/platform rows of the Failure
// Classification table, against a real API server and real ResourceQuota
// objects (only the Forbidden response itself is injected, as a stand-in
// for the classified admission error a real quota-exhausted cluster would
// return). See state_machine_coverage_test.go for the full map.
var _ = Describe("quota confirmation and platform denial", func() {
	var (
		cr *marimohubv1alpha1.MarimoSession
		r  *MarimoSessionReconciler
	)

	BeforeEach(func() {
		cr = createSession(newValidSession())
		r = newTestReconciler(clocktesting.NewFakeClock(cr.CreationTimestamp.Time))
		createValidSecret(cr)
	})

	AfterEach(func() {
		deleteAndWait(cr)
	})

	It("blocks on a confirmed quota rejection, does not retry automatically, and clears the stale Condition on an explicit retry", func() {
		quota := &corev1.ResourceQuota{
			ObjectMeta: metav1.ObjectMeta{Name: testQuotaName, Namespace: cr.Namespace},
			Status: corev1.ResourceQuotaStatus{
				Hard: corev1.ResourceList{requestsCPUName: resource.MustParse("1")},
				Used: corev1.ResourceList{requestsCPUName: resource.MustParse("900m")},
			},
		}
		wantHard := quota.Status.Hard
		wantUsed := quota.Status.Used
		Expect(k8sClient.Create(ctx, quota)).To(Succeed())
		defer func() { _ = k8sClient.Delete(ctx, quota) }()
		// Status is a subresource: Create's response decodes back into
		// quota, and since a status subresource exists the API server
		// always starts a new ResourceQuota at zero usage regardless of
		// what the create request body carried -- exactly as it would for
		// a real quota the (absent, in envtest) quota controller has not
		// synced yet. Restoring the intended values and writing them
		// through Status().Update is the fixture standing in for that
		// controller having already run.
		quota.Status.Hard = wantHard
		quota.Status.Used = wantUsed
		Expect(k8sClient.Status().Update(ctx, quota)).To(Succeed())

		pod := session.BuildPod(cr, r.Options)
		forbidden := apierrors.NewForbidden(schema.GroupResource{Resource: podsResource}, pod.Name,
			errors.New("exceeded quota: chart-quota, requested: requests.cpu=250m, used: requests.cpu=900m, limited: requests.cpu=1"))

		_, err := r.reconcileCreateForbidden(ctx, cr, pod, forbidden)
		Expect(err).NotTo(HaveOccurred())

		got := getSession(keyOf(cr))
		capCond := findCondition(got, marimohubv1alpha1.ConditionTypeCapacityAvailable)
		Expect(capCond.Status).To(Equal(metav1.ConditionFalse))
		Expect(capCond.Reason).To(Equal(marimohubv1alpha1.ReasonQuotaExceeded))

		// Blocked: no Pod exists, and reconciling again with nothing new
		// must not attempt one and must not schedule a timed retry.
		Expect(getPodIfExists(got)).To(BeNil())
		result, err := r.Reconcile(ctx, reconcileRequest(cr))
		Expect(err).NotTo(HaveOccurred())
		Expect(result.RequeueAfter).To(BeZero())
		Expect(getPodIfExists(getSession(keyOf(cr)))).To(BeNil())

		// An explicit retry (a fresh wake-request token) is required. Give
		// it room to actually succeed this time -- envtest's real
		// apiserver enforces ResourceQuota admission exactly like a
		// production cluster would, so a retry against a still-exhausted
		// quota would legitimately be rejected again -- and confirm the
		// stale QuotaExceeded Condition clears once it does.
		liveQuota := &corev1.ResourceQuota{}
		Expect(k8sClient.Get(ctx, client.ObjectKeyFromObject(quota), liveQuota)).To(Succeed())
		liveQuota.Status.Used = corev1.ResourceList{requestsCPUName: resource.MustParse("0")}
		Expect(k8sClient.Status().Update(ctx, liveQuota)).To(Succeed())

		fresh := getSession(keyOf(cr))
		fresh.Annotations = map[string]string{runtimecontract.AnnotationWakeRequest: uuid.NewString()}
		Expect(k8sClient.Update(ctx, fresh)).To(Succeed())
		_, err = r.Reconcile(ctx, reconcileRequest(fresh))
		Expect(err).NotTo(HaveOccurred())

		afterRetry := getSession(keyOf(cr))
		Expect(getPodIfExists(afterRetry)).NotTo(BeNil())
		capCondAfter := findCondition(afterRetry, marimohubv1alpha1.ConditionTypeCapacityAvailable)
		Expect(capCondAfter.Status).To(Equal(metav1.ConditionTrue))
	})

	It("does not confirm quota exhaustion from Forbidden text alone when no live quota agrees", func() {
		pod := session.BuildPod(cr, r.Options)
		forbidden := apierrors.NewForbidden(schema.GroupResource{Resource: podsResource}, pod.Name,
			errors.New("exceeded quota: chart-quota, requested: requests.cpu=250m, used: requests.cpu=100m, limited: requests.cpu=10"))

		_, err := r.reconcileCreateForbidden(ctx, cr, pod, forbidden)
		Expect(err).NotTo(HaveOccurred())

		got := getSession(keyOf(cr))
		Expect(got.Status.Phase).To(Equal(marimohubv1alpha1.RuntimePhaseFailed))
		readyCond := findCondition(got, marimohubv1alpha1.ConditionTypeReady)
		Expect(readyCond.Reason).To(Equal(marimohubv1alpha1.ReasonPlatformDenied))
	})

	It("fails PlatformDenied on a non-quota Forbidden response (RBAC/SCC/webhook) without ever mapping it to QuotaExceeded", func() {
		pod := session.BuildPod(cr, r.Options)
		forbidden := apierrors.NewForbidden(schema.GroupResource{Resource: podsResource}, pod.Name,
			errors.New("violates PodSecurity \"restricted:latest\""))

		_, err := r.reconcileCreateForbidden(ctx, cr, pod, forbidden)
		Expect(err).NotTo(HaveOccurred())

		got := getSession(keyOf(cr))
		Expect(got.Status.Phase).To(Equal(marimohubv1alpha1.RuntimePhaseFailed))
		readyCond := findCondition(got, marimohubv1alpha1.ConditionTypeReady)
		Expect(readyCond.Reason).To(Equal(marimohubv1alpha1.ReasonPlatformDenied))
		Expect(readyCond.Message).NotTo(ContainSubstring("is forbidden:"))
	})
})
