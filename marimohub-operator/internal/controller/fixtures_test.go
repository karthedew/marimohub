package controller

import (
	. "github.com/onsi/gomega"

	corev1 "k8s.io/api/core/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/types"
	"k8s.io/utils/ptr"
	"sigs.k8s.io/controller-runtime/pkg/client"
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
