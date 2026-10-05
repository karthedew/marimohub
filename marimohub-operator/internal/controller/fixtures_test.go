package controller

import (
	"slices"

	. "github.com/onsi/ginkgo/v2"
	. "github.com/onsi/gomega"

	corev1 "k8s.io/api/core/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/types"
	"k8s.io/utils/ptr"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/controller/controllerutil"
	"sigs.k8s.io/controller-runtime/pkg/reconcile"

	marimohubv1alpha1 "github.com/karthedew/marimohub/marimohub-operator/api/v1alpha1"
	"github.com/karthedew/marimohub/marimohub-operator/internal/runtimecontract"
)

// mainContainerName mirrors the marimo container name the Pod builder
// assigns; fixtures that patch container status need to name it consistently.
const mainContainerName = "marimo"

// createSession creates s and returns it re-read from the API server, so
// callers get a populated UID/resourceVersion.
func createSession(s *marimohubv1alpha1.MarimoSession) *marimohubv1alpha1.MarimoSession {
	Expect(k8sClient.Create(ctx, s)).To(Succeed())
	return getSession(keyOf(s))
}

func getSession(key types.NamespacedName) *marimohubv1alpha1.MarimoSession {
	got := &marimohubv1alpha1.MarimoSession{}
	Expect(k8sClient.Get(ctx, key, got)).To(Succeed())
	return got
}

func reconcileRequest(s *marimohubv1alpha1.MarimoSession) reconcile.Request {
	return reconcile.Request{NamespacedName: keyOf(s)}
}

// newCredentialSecret builds a valid, immutable, correctly-owned credential
// Secret for cr. cr must already have a live UID (i.e. have been created).
func newCredentialSecret(cr *marimohubv1alpha1.MarimoSession) *corev1.Secret {
	return &corev1.Secret{
		ObjectMeta: metav1.ObjectMeta{
			Name:      runtimecontract.SecretName(cr.Name),
			Namespace: cr.Namespace,
			OwnerReferences: []metav1.OwnerReference{{
				APIVersion:         marimohubv1alpha1.GroupVersion.String(),
				Kind:               "MarimoSession",
				Name:               cr.Name,
				UID:                cr.UID,
				Controller:         ptr.To(true),
				BlockOwnerDeletion: ptr.To(true),
			}},
		},
		Type:      runtimecontract.SecretType,
		Immutable: ptr.To(true),
		Data: map[string][]byte{
			runtimecontract.SecretKeyMarimoToken:       []byte("token-value"),
			runtimecontract.SecretKeyRuntimeCredential: []byte("credential-value"),
		},
	}
}

// createValidSecret creates and returns a valid owned credential Secret for
// cr, the common precondition for every scenario past Pending.
func createValidSecret(cr *marimohubv1alpha1.MarimoSession) *corev1.Secret {
	secret := newCredentialSecret(cr)
	Expect(k8sClient.Create(ctx, secret)).To(Succeed())
	return secret
}

func getPod(cr *marimohubv1alpha1.MarimoSession) *corev1.Pod {
	pod := &corev1.Pod{}
	key := client.ObjectKey{Namespace: cr.Namespace, Name: runtimecontract.ChildName(cr.Name)}
	Expect(k8sClient.Get(ctx, key, pod)).To(Succeed())
	return pod
}

func getPodIfExists(cr *marimohubv1alpha1.MarimoSession) *corev1.Pod {
	pod := &corev1.Pod{}
	key := client.ObjectKey{Namespace: cr.Namespace, Name: runtimecontract.ChildName(cr.Name)}
	if err := k8sClient.Get(ctx, key, pod); err != nil {
		return nil
	}
	return pod
}

// markInitSucceeded sets pod's init container status as the fetcher would
// report after a successful fetch: envtest has no kubelet to do this on its
// own, so every scenario past "Pod created" drives it explicitly, exactly
// as a real kubelet's reporting is the thing under test's control, not this
// controller's own decision logic.
func markInitSucceeded(pod *corev1.Pod) {
	pod.Status.InitContainerStatuses = []corev1.ContainerStatus{{
		Name:  "source-fetcher",
		State: corev1.ContainerState{Terminated: &corev1.ContainerStateTerminated{ExitCode: 0, Reason: "Completed"}},
	}}
	Expect(k8sClient.Status().Update(ctx, pod)).To(Succeed())
}

func markInitFailed(pod *corev1.Pod, exitCode int32, reason, message string) {
	pod.Status.InitContainerStatuses = []corev1.ContainerStatus{{
		Name:  "source-fetcher",
		State: corev1.ContainerState{Terminated: &corev1.ContainerStateTerminated{ExitCode: exitCode, Reason: reason, Message: message}},
	}}
	Expect(k8sClient.Status().Update(ctx, pod)).To(Succeed())
}

func markMainReady(pod *corev1.Pod) {
	markInitSucceeded(pod)
	pod.Status.Phase = corev1.PodRunning
	pod.Status.ContainerStatuses = []corev1.ContainerStatus{{
		Name:  mainContainerName,
		Ready: true,
		State: corev1.ContainerState{Running: &corev1.ContainerStateRunning{}},
	}}
	pod.Status.Conditions = []corev1.PodCondition{{Type: corev1.PodReady, Status: corev1.ConditionTrue}}
	Expect(k8sClient.Status().Update(ctx, pod)).To(Succeed())
}

func markMainNotReady(pod *corev1.Pod) {
	pod.Status.Phase = corev1.PodRunning
	pod.Status.ContainerStatuses = []corev1.ContainerStatus{{
		Name:  mainContainerName,
		Ready: false,
		State: corev1.ContainerState{Running: &corev1.ContainerStateRunning{}},
	}}
	pod.Status.Conditions = []corev1.PodCondition{{Type: corev1.PodReady, Status: corev1.ConditionFalse}}
	Expect(k8sClient.Status().Update(ctx, pod)).To(Succeed())
}

func markNodeUnknown(pod *corev1.Pod) {
	pod.Status.Phase = corev1.PodRunning
	pod.Status.ContainerStatuses = []corev1.ContainerStatus{{
		Name:  mainContainerName,
		Ready: false,
		State: corev1.ContainerState{Running: &corev1.ContainerStateRunning{}},
	}}
	pod.Status.Conditions = []corev1.PodCondition{{Type: corev1.PodReady, Status: corev1.ConditionUnknown}}
	Expect(k8sClient.Status().Update(ctx, pod)).To(Succeed())
}

func markMainExited(pod *corev1.Pod, exitCode int32, reason string) {
	markInitSucceeded(pod)
	pod.Status.Phase = corev1.PodFailed
	pod.Status.ContainerStatuses = []corev1.ContainerStatus{{
		Name:  mainContainerName,
		State: corev1.ContainerState{Terminated: &corev1.ContainerStateTerminated{ExitCode: exitCode, Reason: reason}},
	}}
	Expect(k8sClient.Status().Update(ctx, pod)).To(Succeed())
}

// markEvicted reports what the kubelet leaves behind after a node-pressure
// eviction: a Failed Pod with reason Evicted and a DisruptionTarget
// condition, its marimo container killed, and no deletionTimestamp. Nothing
// deletes such a Pod until someone does.
func markEvicted(pod *corev1.Pod) {
	markInitSucceeded(pod)
	pod.Status.Phase = corev1.PodFailed
	pod.Status.Reason = podReasonEvicted
	pod.Status.Message = "The node was low on resource: memory."
	pod.Status.Conditions = []corev1.PodCondition{{
		Type:   corev1.DisruptionTarget,
		Status: corev1.ConditionTrue,
		Reason: corev1.PodReasonTerminationByKubelet,
	}}
	pod.Status.ContainerStatuses = []corev1.ContainerStatus{{
		Name:  mainContainerName,
		State: corev1.ContainerState{Terminated: &corev1.ContainerStateTerminated{ExitCode: 137, Reason: "Error"}},
	}}
	Expect(k8sClient.Status().Update(ctx, pod)).To(Succeed())
}

// markDisruptionTarget sets pod's DisruptionTarget condition to status: True
// as the scheduler, the eviction API, or the taint manager does right before
// deleting a Pod, and False as the disruption controller does when it resets
// a disruption that never went ahead.
func markDisruptionTarget(pod *corev1.Pod, status corev1.ConditionStatus) {
	cond := corev1.PodCondition{Type: corev1.DisruptionTarget, Status: status, Reason: corev1.PodReasonPreemptionByScheduler}
	if i := slices.IndexFunc(pod.Status.Conditions, func(c corev1.PodCondition) bool { return c.Type == corev1.DisruptionTarget }); i >= 0 {
		pod.Status.Conditions[i] = cond
	} else {
		pod.Status.Conditions = append(pod.Status.Conditions, cond)
	}
	Expect(k8sClient.Status().Update(ctx, pod)).To(Succeed())
}

// testPodFinalizer holds a deleted Pod in Terminating. envtest has no
// kubelet, so a deleted Pod otherwise disappears at once, and the window
// the controller has to survive never opens: the Pod still exists, with a
// deletionTimestamp, while its containers report how they were stopped. No
// production code path writes a finalizer.
const testPodFinalizer = "marimohub.io/test-hold"

// holdPod adds testPodFinalizer to cr's live Pod, so deleting it leaves it
// Terminating until releasePod. The cleanup releases it if a spec stops
// early, so a failed spec never leaks a Pod that can't be deleted.
func holdPod(cr *marimohubv1alpha1.MarimoSession) {
	pod := getPod(cr)
	controllerutil.AddFinalizer(pod, testPodFinalizer)
	Expect(k8sClient.Update(ctx, pod)).To(Succeed())
	DeferCleanup(func() {
		if pod := getPodIfExists(cr); pod != nil && controllerutil.RemoveFinalizer(pod, testPodFinalizer) {
			Expect(k8sClient.Update(ctx, pod)).To(Succeed())
		}
	})
}

// releasePod removes testPodFinalizer from cr's held, already-deleted Pod
// and waits until the Pod is gone.
func releasePod(cr *marimohubv1alpha1.MarimoSession) {
	pod := getPod(cr)
	Expect(controllerutil.RemoveFinalizer(pod, testPodFinalizer)).To(BeTrue())
	Expect(k8sClient.Update(ctx, pod)).To(Succeed())
	Eventually(func() bool { return getPodIfExists(cr) == nil }).Should(BeTrue())
}

func markMainWaiting(pod *corev1.Pod, reason string) {
	markInitSucceeded(pod)
	pod.Status.Phase = corev1.PodPending
	pod.Status.ContainerStatuses = []corev1.ContainerStatus{{
		Name:  mainContainerName,
		State: corev1.ContainerState{Waiting: &corev1.ContainerStateWaiting{Reason: reason}},
	}}
	Expect(k8sClient.Status().Update(ctx, pod)).To(Succeed())
}

func deleteAndWait(obj client.Object) {
	key := client.ObjectKeyFromObject(obj)
	_ = k8sClient.Delete(ctx, obj)
	Eventually(func() bool {
		return apierrorsIsNotFound(k8sClient.Get(ctx, key, obj))
	}).Should(BeTrue())
}

func apierrorsIsNotFound(err error) bool {
	return err != nil && client.IgnoreNotFound(err) == nil
}
