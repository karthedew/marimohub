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

package controller

import (
	"fmt"

	. "github.com/onsi/ginkgo/v2"
	. "github.com/onsi/gomega"

	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	clocktesting "k8s.io/utils/clock/testing"

	marimohubv1alpha1 "github.com/karthedew/marimohub/marimohub-operator/api/v1alpha1"
	"github.com/karthedew/marimohub/marimohub-operator/internal/runtimecontract"
)

// Covers state invariant 1 (new CR + missing Secret) and the front half of
// invariant 2 (valid Secret -> Service, Starting, Pod). See
// state_machine_coverage_test.go for the full invariant/test map.
var _ = Describe("MarimoSession Controller", func() {
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

	It("stays Pending with CredentialsAvailable=False when the Secret is missing", func() {
		_, err := r.Reconcile(ctx, reconcileRequest(cr))
		Expect(err).NotTo(HaveOccurred())

		got := getSession(keyOf(cr))
		Expect(got.Status.Phase).To(Equal(marimohubv1alpha1.RuntimePhasePending))
		cond := findCondition(got, marimohubv1alpha1.ConditionTypeCredentialsAvailable)
		Expect(cond).NotTo(BeNil())
		Expect(cond.Status).To(Equal(metav1.ConditionFalse))
		Expect(cond.Reason).To(Equal(marimohubv1alpha1.ReasonCredentialsMissing))
		Expect(got.Status.Message).To(Equal(cond.Message))
	})

	It("creates the Service and Pod, entering Starting, once the Secret is valid", func() {
		createValidSecret(cr)

		_, err := r.Reconcile(ctx, reconcileRequest(cr))
		Expect(err).NotTo(HaveOccurred())

		got := getSession(keyOf(cr))
		Expect(got.Status.Phase).To(Equal(marimohubv1alpha1.RuntimePhaseStarting))
		Expect(got.Status.ServiceName).To(Equal(runtimecontract.ChildName(cr.Name)))
		Expect(got.Status.PodName).To(Equal(runtimecontract.ChildName(cr.Name)))
		Expect(got.Status.Attempt).To(Equal(int32(1)))

		pod := getPod(got)
		Expect(pod.OwnerReferences).To(HaveLen(1))
		Expect(pod.OwnerReferences[0].UID).To(Equal(got.UID))
	})

	It("becomes Ready once the Pod passes authenticated readiness, and does not idle-evaluate in that same reconcile", func() {
		createValidSecret(cr)
		Expect(reconcileUntil(r, cr, func(s *marimohubv1alpha1.MarimoSession) bool {
			return s.Status.PodName != ""
		})).To(Succeed())

		pod := getPod(getSession(keyOf(cr)))
		markMainReady(pod)

		result, err := r.Reconcile(ctx, reconcileRequest(cr))
		Expect(err).NotTo(HaveOccurred())

		got := getSession(keyOf(cr))
		Expect(got.Status.Phase).To(Equal(marimohubv1alpha1.RuntimePhaseReady))
		Expect(got.Status.LastActivity).NotTo(BeNil())
		Expect(got.Status.ObservedGeneration).To(Equal(got.Generation))
		readyCond := findCondition(got, marimohubv1alpha1.ConditionTypeReady)
		Expect(readyCond.Status).To(Equal(metav1.ConditionTrue))
		// Reaching Ready with no active failure or blocking Condition must
		// clear any diagnostic message a prior Pending/Starting state left
		// behind.
		Expect(got.Status.Message).To(BeEmpty())
		// The Ready transition itself must not also perform idle
		// evaluation: a zero RequeueAfter here means the next reconcile is
		// purely event-driven, not "immediately re-checked as if idle."
		Expect(result.RequeueAfter).To(BeZero())
	})
})

func findCondition(cr *marimohubv1alpha1.MarimoSession, condType string) *metav1.Condition {
	for i := range cr.Status.Conditions {
		if cr.Status.Conditions[i].Type == condType {
			return &cr.Status.Conditions[i]
		}
	}
	return nil
}

// reconcileUntil calls Reconcile repeatedly (bounded) until check passes,
// re-reading cr's latest status each time. It exists because a single
// reconcile only ever performs one state transition at a time by design
// (for example: persist Starting, then a later reconcile notices the Pod
// and classifies it) so driving a scenario to a specific point is a small
// fixed loop, not a single call.
func reconcileUntil(r *MarimoSessionReconciler, cr *marimohubv1alpha1.MarimoSession, check func(*marimohubv1alpha1.MarimoSession) bool) error {
	const maxAttempts = 10
	for range maxAttempts {
		if _, err := r.Reconcile(ctx, reconcileRequest(cr)); err != nil {
			return err
		}
		got := getSession(keyOf(cr))
		if check(got) {
			return nil
		}
	}
	return fmt.Errorf("condition not met after %d reconciles", maxAttempts)
}
