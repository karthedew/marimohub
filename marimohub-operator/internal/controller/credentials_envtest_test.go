package controller

import (
	"github.com/google/uuid"
	. "github.com/onsi/ginkgo/v2"
	. "github.com/onsi/gomega"

	corev1 "k8s.io/api/core/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/types"
	clocktesting "k8s.io/utils/clock/testing"
	"k8s.io/utils/ptr"

	marimohubv1alpha1 "github.com/karthedew/marimohub/marimohub-operator/api/v1alpha1"
	"github.com/karthedew/marimohub/marimohub-operator/internal/runtimecontract"
)

// Covers state invariants 1 and 11: a foreign, partial, or mutable Secret
// must fail closed exactly like a missing one, and a Ready Runtime whose
// credentials become invalid must clear Ready and remove its Pod before
// anything else. See state_machine_coverage_test.go for the full map.
var _ = Describe("credential Secret validation", func() {
	var (
		cr *marimohubv1alpha1.MarimoSession
		r  *MarimoSessionReconciler
	)

	BeforeEach(func() {
		cr = createSession(newValidSession())
		r = newTestReconciler(clocktesting.NewFakeClock(cr.CreationTimestamp.Time))
	})

	AfterEach(func() {
		deleteAndWait(cr)
	})

	credentialsInvalidReason := func() string {
		_, err := r.Reconcile(ctx, reconcileRequest(cr))
		Expect(err).NotTo(HaveOccurred())
		got := getSession(keyOf(cr))
		Expect(got.Status.Phase).To(Equal(marimohubv1alpha1.RuntimePhasePending))
		cond := findCondition(got, marimohubv1alpha1.ConditionTypeCredentialsAvailable)
		Expect(cond).NotTo(BeNil())
		Expect(cond.Status).To(Equal(metav1.ConditionFalse))
		return cond.Reason
	}

	It("rejects a Secret owned by a different UID as invalid, not missing", func() {
		secret := newCredentialSecret(cr)
		secret.OwnerReferences[0].UID = types.UID("11111111-1111-1111-1111-111111111111")
		Expect(k8sClient.Create(ctx, secret)).To(Succeed())

		Expect(credentialsInvalidReason()).To(Equal(marimohubv1alpha1.ReasonCredentialsInvalid))
	})

	It("rejects a Secret missing the RUNTIME_CREDENTIAL key", func() {
		secret := newCredentialSecret(cr)
		delete(secret.Data, "RUNTIME_CREDENTIAL")
		Expect(k8sClient.Create(ctx, secret)).To(Succeed())

		Expect(credentialsInvalidReason()).To(Equal(marimohubv1alpha1.ReasonCredentialsInvalid))
	})

	It("rejects a Secret missing the MARIMO_TOKEN key", func() {
		secret := newCredentialSecret(cr)
		delete(secret.Data, "MARIMO_TOKEN")
		Expect(k8sClient.Create(ctx, secret)).To(Succeed())

		Expect(credentialsInvalidReason()).To(Equal(marimohubv1alpha1.ReasonCredentialsInvalid))
	})

	It("rejects a Secret that is not marked immutable", func() {
		secret := newCredentialSecret(cr)
		secret.Immutable = ptr.To(false)
		Expect(k8sClient.Create(ctx, secret)).To(Succeed())

		Expect(credentialsInvalidReason()).To(Equal(marimohubv1alpha1.ReasonCredentialsInvalid))
	})

	It("rejects a Secret of the wrong type", func() {
		secret := newCredentialSecret(cr)
		secret.Type = corev1.SecretTypeDockerConfigJson
		secret.Data = map[string][]byte{".dockerconfigjson": []byte("{}")}
		Expect(k8sClient.Create(ctx, secret)).To(Succeed())

		Expect(credentialsInvalidReason()).To(Equal(marimohubv1alpha1.ReasonCredentialsInvalid))
	})

	It("clears Ready and removes the Pod, in that order, when the Secret is deleted after the Runtime was Ready", func() {
		secret := createValidSecret(cr)
		Expect(reconcileUntil(r, cr, func(s *marimohubv1alpha1.MarimoSession) bool { return s.Status.PodName != "" })).To(Succeed())
		markMainReady(getPod(cr))
		_, err := r.Reconcile(ctx, reconcileRequest(cr))
		Expect(err).NotTo(HaveOccurred())
		Expect(getSession(keyOf(cr)).Status.Phase).To(Equal(marimohubv1alpha1.RuntimePhaseReady))

		Expect(k8sClient.Delete(ctx, secret)).To(Succeed())
		_, err = r.Reconcile(ctx, reconcileRequest(cr))
		Expect(err).NotTo(HaveOccurred())

		got := getSession(keyOf(cr))
		Expect(got.Status.Phase).To(Equal(marimohubv1alpha1.RuntimePhasePending))
		Expect(got.Status.PodName).To(BeEmpty())
		readyCond := findCondition(got, marimohubv1alpha1.ConditionTypeReady)
		Expect(readyCond.Status).To(Equal(metav1.ConditionFalse))
		Expect(readyCond.Reason).To(Equal(marimohubv1alpha1.ReasonCredentialsMissing))

		Expect(getPodIfExists(got)).To(BeNil())
	})

	It("re-asserts CredentialsAvailable=True and a cleared message once a missing Secret is repaired", func() {
		_, err := r.Reconcile(ctx, reconcileRequest(cr))
		Expect(err).NotTo(HaveOccurred())
		pending := getSession(keyOf(cr))
		Expect(pending.Status.Phase).To(Equal(marimohubv1alpha1.RuntimePhasePending))
		pendingCond := findCondition(pending, marimohubv1alpha1.ConditionTypeCredentialsAvailable)
		Expect(pendingCond).NotTo(BeNil())
		Expect(pendingCond.Status).To(Equal(metav1.ConditionFalse))
		Expect(pending.Status.Message).To(Equal(pendingCond.Message))
		Expect(pending.Status.Message).NotTo(BeEmpty())

		createValidSecret(cr)
		_, err = r.Reconcile(ctx, reconcileRequest(cr))
		Expect(err).NotTo(HaveOccurred())

		got := getSession(keyOf(cr))
		Expect(got.Status.Phase).To(Equal(marimohubv1alpha1.RuntimePhaseStarting))
		repairedCond := findCondition(got, marimohubv1alpha1.ConditionTypeCredentialsAvailable)
		Expect(repairedCond).NotTo(BeNil())
		Expect(repairedCond.Status).To(Equal(metav1.ConditionTrue))
		Expect(repairedCond.Reason).To(Equal(marimohubv1alpha1.ReasonReconcileSucceeded))
	})

	It("keeps a Ready deploy Runtime Sleeping after Secret repair until an unobserved wake request arrives", func() {
		revision := int64(1)
		deployCR := createSession(asDeploy(newValidSession(), revision))
		defer deleteAndWait(deployCR)

		secret := createValidSecret(deployCR)
		Expect(reconcileUntil(r, deployCR, func(s *marimohubv1alpha1.MarimoSession) bool { return s.Status.PodName != "" })).To(Succeed())
		markMainReady(getPod(deployCR))
		_, err := r.Reconcile(ctx, reconcileRequest(deployCR))
		Expect(err).NotTo(HaveOccurred())
		Expect(getSession(keyOf(deployCR)).Status.Phase).To(Equal(marimohubv1alpha1.RuntimePhaseReady))

		Expect(k8sClient.Delete(ctx, secret)).To(Succeed())
		_, err = r.Reconcile(ctx, reconcileRequest(deployCR))
		Expect(err).NotTo(HaveOccurred())

		lost := getSession(keyOf(deployCR))
		Expect(lost.Status.Phase).To(Equal(marimohubv1alpha1.RuntimePhaseSleeping))
		Expect(lost.Status.PodName).To(BeEmpty())
		Expect(getPodIfExists(lost)).To(BeNil())

		// Repair the Secret but supply no fresh wake request: the Runtime
		// must stay Sleeping with no Pod instead of restarting on its own
		// the instant credentials become valid again.
		createValidSecret(lost)
		_, err = r.Reconcile(ctx, reconcileRequest(lost))
		Expect(err).NotTo(HaveOccurred())

		repaired := getSession(keyOf(lost))
		Expect(repaired.Status.Phase).To(Equal(marimohubv1alpha1.RuntimePhaseSleeping))
		Expect(repaired.Status.PodName).To(BeEmpty())
		Expect(getPodIfExists(repaired)).To(BeNil())
		credCond := findCondition(repaired, marimohubv1alpha1.ConditionTypeCredentialsAvailable)
		Expect(credCond).NotTo(BeNil())
		Expect(credCond.Status).To(Equal(metav1.ConditionTrue))

		// Only a fresh, unobserved wake-request annotation may now start a
		// new Pod.
		woke := getSession(keyOf(repaired))
		woke.Annotations = map[string]string{runtimecontract.AnnotationWakeRequest: uuid.NewString()}
		Expect(k8sClient.Update(ctx, woke)).To(Succeed())
		_, err = r.Reconcile(ctx, reconcileRequest(woke))
		Expect(err).NotTo(HaveOccurred())

		started := getSession(keyOf(woke))
		Expect(started.Status.Phase).To(Equal(marimohubv1alpha1.RuntimePhaseStarting))
		Expect(getPodIfExists(started)).NotTo(BeNil())
	})
})
