package controller

import (
	"time"

	. "github.com/onsi/ginkgo/v2"
	. "github.com/onsi/gomega"

	corev1 "k8s.io/api/core/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/util/intstr"
	clocktesting "k8s.io/utils/clock/testing"
	"sigs.k8s.io/controller-runtime/pkg/client"

	marimohubv1alpha1 "github.com/karthedew/marimohub/marimohub-operator/api/v1alpha1"
	"github.com/karthedew/marimohub/marimohub-operator/internal/runtimecontract"
)

// Covers the foreign-collision half of state invariant 2/3.3 (Pod/Service
// drift) and the Service-before-Pod-readiness ordering. See
// state_machine_coverage_test.go for the full map.
var _ = Describe("foreign Pod/Service collisions and Service drift", func() {
	var (
		cr *marimohubv1alpha1.MarimoSession
		r  *MarimoSessionReconciler
	)

	BeforeEach(func() {
		cr = createSession(newValidSession())
		r = newTestReconciler(clocktesting.NewFakeClock(time.Now()))
	})

	AfterEach(func() {
		deleteAndWait(cr)
	})

	It("reports ResourceConflict and never adopts a foreign Service with the expected name", func() {
		foreign := &corev1.Service{
			ObjectMeta: metav1.ObjectMeta{Name: runtimecontract.ChildName(cr.Name), Namespace: cr.Namespace},
			Spec: corev1.ServiceSpec{
				Ports: []corev1.ServicePort{{Port: 9999, TargetPort: intstr.FromInt32(9999)}},
			},
		}
		Expect(k8sClient.Create(ctx, foreign)).To(Succeed())
		createValidSecret(cr)

		_, err := r.Reconcile(ctx, reconcileRequest(cr))
		Expect(err).NotTo(HaveOccurred())

		got := getSession(keyOf(cr))
		cond := findCondition(got, marimohubv1alpha1.ConditionTypeReconciled)
		Expect(cond).NotTo(BeNil())
		Expect(cond.Status).To(Equal(metav1.ConditionFalse))
		Expect(cond.Reason).To(Equal(marimohubv1alpha1.ReasonResourceConflict))

		// The foreign Service must be untouched: still exactly the object we created.
		live := &corev1.Service{}
		Expect(k8sClient.Get(ctx, client.ObjectKeyFromObject(foreign), live)).To(Succeed())
		Expect(live.Spec.Ports[0].Port).To(Equal(int32(9999)))
		Expect(live.OwnerReferences).To(BeEmpty())

		Expect(k8sClient.Delete(ctx, foreign)).To(Succeed())
	})

	It("reports ResourceConflict and never adopts a foreign Pod with the expected name", func() {
		createValidSecret(cr)
		foreign := &corev1.Pod{
			ObjectMeta: metav1.ObjectMeta{Name: runtimecontract.ChildName(cr.Name), Namespace: cr.Namespace},
			Spec: corev1.PodSpec{
				Containers: []corev1.Container{{Name: "occupied", Image: "busybox"}},
			},
		}
		Expect(k8sClient.Create(ctx, foreign)).To(Succeed())

		_, err := r.Reconcile(ctx, reconcileRequest(cr))
		Expect(err).NotTo(HaveOccurred())

		got := getSession(keyOf(cr))
		cond := findCondition(got, marimohubv1alpha1.ConditionTypeReconciled)
		Expect(cond).NotTo(BeNil())
		Expect(cond.Reason).To(Equal(marimohubv1alpha1.ReasonResourceConflict))
		Expect(got.Status.PodName).To(BeEmpty())

		live := &corev1.Pod{}
		Expect(k8sClient.Get(ctx, client.ObjectKeyFromObject(foreign), live)).To(Succeed())
		Expect(live.Spec.Containers[0].Name).To(Equal("occupied"))

		Expect(k8sClient.Delete(ctx, foreign)).To(Succeed())
	})

	It("corrects Service selector/port drift while preserving the ClusterIP", func() {
		createValidSecret(cr)
		_, err := r.Reconcile(ctx, reconcileRequest(cr))
		Expect(err).NotTo(HaveOccurred())

		svc := &corev1.Service{}
		svcKey := client.ObjectKey{Name: runtimecontract.ChildName(cr.Name), Namespace: cr.Namespace}
		Expect(k8sClient.Get(ctx, svcKey, svc)).To(Succeed())
		originalClusterIP := svc.Spec.ClusterIP

		svc.Spec.Selector = map[string]string{"drifted": "true"}
		Expect(k8sClient.Update(ctx, svc)).To(Succeed())

		_, err = r.Reconcile(ctx, reconcileRequest(cr))
		Expect(err).NotTo(HaveOccurred())

		fixed := &corev1.Service{}
		Expect(k8sClient.Get(ctx, svcKey, fixed)).To(Succeed())
		Expect(fixed.Spec.Selector).To(Equal(map[string]string{runtimecontract.LabelSession: cr.Name}))
		Expect(fixed.Spec.ClusterIP).To(Equal(originalClusterIP))
	})
})
