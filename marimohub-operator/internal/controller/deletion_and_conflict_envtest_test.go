package controller

import (
	"sync"

	. "github.com/onsi/ginkgo/v2"
	. "github.com/onsi/gomega"

	clocktesting "k8s.io/utils/clock/testing"
	"k8s.io/utils/ptr"

	marimohubv1alpha1 "github.com/karthedew/marimohub/marimohub-operator/api/v1alpha1"
)

// Covers state invariant 13 (CR deletion returns immediately; there is no
// finalizer and no cleanup write) and the "status conflicts" and "manager
// restart idempotence" required scenarios. See state_machine_coverage_test.go
// for the full map.
var _ = Describe("CR deletion and status write conflicts", func() {
	It("returns immediately for a CR with a deletionTimestamp, performing no status write", func() {
		cr := createSession(newValidSession())
		createValidSecret(cr)
		r := newTestReconciler(clocktesting.NewFakeClock(cr.CreationTimestamp.Time))

		// A finalizer is required to observe a live deletionTimestamp at
		// all -- without one the object simply disappears -- so this adds
		// one purely as a test probe, never a controller behavior: no
		// production code path in this package ever writes a finalizer.
		fresh := getSession(keyOf(cr))
		fresh.Finalizers = []string{"marimohub.io/test-probe"}
		Expect(k8sClient.Update(ctx, fresh)).To(Succeed())
		Expect(k8sClient.Delete(ctx, fresh)).To(Succeed())

		beforeGen := getSession(keyOf(cr)).ResourceVersion
		_, err := r.Reconcile(ctx, reconcileRequest(cr))
		Expect(err).NotTo(HaveOccurred())

		afterGen := getSession(keyOf(cr)).ResourceVersion
		Expect(afterGen).To(Equal(beforeGen), "a terminating CR must receive no status write at all")

		withoutFinalizer := getSession(keyOf(cr))
		withoutFinalizer.Finalizers = nil
		Expect(k8sClient.Update(ctx, withoutFinalizer)).To(Succeed())
		Eventually(func() bool {
			return apierrorsIsNotFound(k8sClient.Get(ctx, keyOf(cr), &marimohubv1alpha1.MarimoSession{}))
		}).Should(BeTrue())
	})

	It("retries a status write through a conflicting concurrent update and lands both changes", func() {
		cr := createSession(newValidSession())
		r := newTestReconciler(clocktesting.NewFakeClock(cr.CreationTimestamp.Time))

		// Two goroutines calling applyStatus on copies of the same object
		// at the same time reliably produce at least one real
		// ResourceVersion conflict against a live API server: each starts
		// from the same resourceVersion, and only one Status().Update can
		// win before the other's copy is stale. retry.RetryOnConflict
		// inside applyStatus is what makes both patches land anyway.
		var wg sync.WaitGroup
		errs := make(chan error, 2)
		wg.Add(2)
		go func() {
			defer wg.Done()
			errs <- r.applyStatus(ctx, cr.DeepCopy(), statusPatch{podName: ptr.To("race-a")})
		}()
		go func() {
			defer wg.Done()
			errs <- r.applyStatus(ctx, cr.DeepCopy(), statusPatch{serviceName: ptr.To("race-b")})
		}()
		wg.Wait()
		close(errs)
		for err := range errs {
			Expect(err).NotTo(HaveOccurred())
		}

		got := getSession(keyOf(cr))
		Expect(got.Status.PodName).To(Equal("race-a"))
		Expect(got.Status.ServiceName).To(Equal("race-b"))

		deleteAndWait(cr)
	})
})
