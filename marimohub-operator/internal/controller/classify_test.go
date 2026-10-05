package controller

import (
	"strings"
	"testing"

	corev1 "k8s.io/api/core/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"

	"github.com/karthedew/marimohub/marimohub-operator/api/v1alpha1"
	"github.com/karthedew/marimohub/marimohub-operator/internal/session"
)

func podWith(init, main []corev1.ContainerStatus, conditions ...corev1.PodCondition) *corev1.Pod {
	return &corev1.Pod{Status: corev1.PodStatus{
		InitContainerStatuses: init,
		ContainerStatuses:     main,
		Conditions:            conditions,
	}}
}

func waiting(reason string) corev1.ContainerStatus {
	return corev1.ContainerStatus{State: corev1.ContainerState{Waiting: &corev1.ContainerStateWaiting{Reason: reason}}}
}

func terminated(exitCode int32, reason, message string) corev1.ContainerStatus {
	return corev1.ContainerStatus{State: corev1.ContainerState{Terminated: &corev1.ContainerStateTerminated{
		ExitCode: exitCode, Reason: reason, Message: message,
	}}}
}

func running(ready bool) corev1.ContainerStatus {
	return corev1.ContainerStatus{Ready: ready, State: corev1.ContainerState{Running: &corev1.ContainerStateRunning{}}}
}

func TestClassifyPodStillStartingWhenInitContainerIsWaitingBenignly(t *testing.T) {
	pod := podWith([]corev1.ContainerStatus{waiting("PodInitializing")}, nil)
	if got := classifyPod(pod); got.kind != verdictStarting {
		t.Fatalf("kind = %v, want verdictStarting", got.kind)
	}
}

func TestClassifyPodInitContainerImagePullIsTransient(t *testing.T) {
	for _, reason := range []string{"ImagePullBackOff", "ErrImagePull"} {
		pod := podWith([]corev1.ContainerStatus{waiting(reason)}, nil)
		if got := classifyPod(pod); got.kind != verdictImagePulling {
			t.Errorf("reason %q: kind = %v, want verdictImagePulling", reason, got.kind)
		}
	}
}

func TestClassifyPodInvalidImageOrCommandIsDeterministic(t *testing.T) {
	for _, reason := range []string{"InvalidImageName", "CreateContainerConfigError", "CreateContainerError"} {
		pod := podWith([]corev1.ContainerStatus{waiting(reason)}, nil)
		got := classifyPod(pod)
		if got.kind != verdictFailed || got.reason != v1alpha1.ReasonInvalidSpec {
			t.Errorf("reason %q: got %+v, want verdictFailed/InvalidSpec", reason, got)
		}
	}
}

func TestClassifyPodFetcherExitCodes(t *testing.T) {
	cases := []struct {
		exitCode   int32
		wantReason string
	}{
		{10, v1alpha1.ReasonSourceUnavailable}, // auth failure
		{11, v1alpha1.ReasonSourceUnavailable}, // not found
		{12, v1alpha1.ReasonSourceUnavailable}, // oversized response
		{13, v1alpha1.ReasonSourceUnavailable}, // TLS/CA failure
		{20, v1alpha1.ReasonSourceUnavailable}, // transient, retries exhausted
		{30, v1alpha1.ReasonSourceUnavailable}, // unexpected status
		{40, v1alpha1.ReasonInvalidSpec},       // fetcher misconfiguration
	}
	for _, c := range cases {
		pod := podWith([]corev1.ContainerStatus{terminated(c.exitCode, "Error", "boom")}, nil)
		got := classifyPod(pod)
		if got.kind != verdictFailed || got.reason != c.wantReason {
			t.Errorf("exit %d: got %+v, want verdictFailed/%s", c.exitCode, got, c.wantReason)
		}
	}
}

func TestClassifyPodInitContainerOOMKilled(t *testing.T) {
	init := corev1.ContainerStatus{State: corev1.ContainerState{Terminated: &corev1.ContainerStateTerminated{
		ExitCode: 137, Reason: containerReasonOOMKilled,
	}}}
	pod := podWith([]corev1.ContainerStatus{init}, nil)
	got := classifyPod(pod)
	if got.kind != verdictFailed || got.reason != v1alpha1.ReasonOOMKilled {
		t.Fatalf("got %+v, want verdictFailed/OOMKilled", got)
	}
}

// TestClassifyPodWorkspaceInitFailureIsNotASourceFailure pins that a failed
// workspace-init is reported as a provisioning problem even when its exit
// code collides with one of the fetcher's own documented codes.
func TestClassifyPodWorkspaceInitFailureIsNotASourceFailure(t *testing.T) {
	fetcher := terminated(0, "Completed", "")
	for _, exitCode := range []int32{1, 11, 20} {
		workspaceInit := terminated(exitCode, "Error", "mkdir: cannot create directory: Permission denied")
		workspaceInit.Name = session.WorkspaceInitContainerName
		pod := podWith([]corev1.ContainerStatus{fetcher, workspaceInit}, nil)
		got := classifyPod(pod)
		if got.kind != verdictFailed || got.reason != v1alpha1.ReasonInvalidSpec {
			t.Errorf("exit %d: got %+v, want verdictFailed/%s", exitCode, got, v1alpha1.ReasonInvalidSpec)
		}
		if !strings.Contains(got.message, "workspace directory") {
			t.Errorf("exit %d: message %q does not name the workspace directory", exitCode, got.message)
		}
	}
}

func TestClassifyPodInitSuccessMovesOnToMainContainer(t *testing.T) {
	init := terminated(0, "Completed", "")
	pod := podWith([]corev1.ContainerStatus{init}, []corev1.ContainerStatus{running(true)})
	if got := classifyPod(pod); got.kind != verdictReady {
		t.Fatalf("kind = %v, want verdictReady", got.kind)
	}
}

func TestClassifyPodMainContainerNonZeroExitIsDeterministic(t *testing.T) {
	init := terminated(0, "Completed", "")
	main := terminated(1, "Error", "")
	pod := podWith([]corev1.ContainerStatus{init}, []corev1.ContainerStatus{main})
	got := classifyPod(pod)
	if got.kind != verdictFailed || got.reason != v1alpha1.ReasonRuntimeExited {
		t.Fatalf("got %+v, want verdictFailed/RuntimeExited", got)
	}
}

func TestClassifyPodMainContainerZeroExitBeforeServingIsDeterministic(t *testing.T) {
	init := terminated(0, "Completed", "")
	main := terminated(0, "Completed", "")
	pod := podWith([]corev1.ContainerStatus{init}, []corev1.ContainerStatus{main})
	got := classifyPod(pod)
	if got.kind != verdictFailed || got.reason != v1alpha1.ReasonRuntimeExited {
		t.Fatalf("got %+v, want verdictFailed/RuntimeExited (zero exit is still a workload exit)", got)
	}
}

func TestClassifyPodMainContainerOOMKilled(t *testing.T) {
	init := terminated(0, "Completed", "")
	main := corev1.ContainerStatus{State: corev1.ContainerState{Terminated: &corev1.ContainerStateTerminated{
		ExitCode: 137, Reason: containerReasonOOMKilled,
	}}}
	pod := podWith([]corev1.ContainerStatus{init}, []corev1.ContainerStatus{main})
	got := classifyPod(pod)
	if got.kind != verdictFailed || got.reason != v1alpha1.ReasonOOMKilled {
		t.Fatalf("got %+v, want verdictFailed/OOMKilled", got)
	}
}

func TestClassifyPodMainContainerNotReady(t *testing.T) {
	init := terminated(0, "Completed", "")
	main := running(false)
	pod := podWith([]corev1.ContainerStatus{init}, []corev1.ContainerStatus{main})
	if got := classifyPod(pod); got.kind != verdictNotReady {
		t.Fatalf("kind = %v, want verdictNotReady", got.kind)
	}
}

func TestClassifyPodNodeUnknownTakesPriorityOverContainerReady(t *testing.T) {
	init := terminated(0, "Completed", "")
	main := running(false)
	pod := podWith([]corev1.ContainerStatus{init}, []corev1.ContainerStatus{main},
		corev1.PodCondition{Type: corev1.PodReady, Status: corev1.ConditionUnknown})
	if got := classifyPod(pod); got.kind != verdictNodeUnknown {
		t.Fatalf("kind = %v, want verdictNodeUnknown", got.kind)
	}
}

// beingDeleted gives pod a deletionTimestamp: it still exists while the
// kubelet stops its containers and publishes how they exited.
func beingDeleted(pod *corev1.Pod) *corev1.Pod {
	now := metav1.Now()
	pod.DeletionTimestamp = &now
	return pod
}

// TestClassifyPodBeingDeletedIsInfrastructureLossWhateverItsContainersReport
// pins that a Pod with a deletionTimestamp is never judged by how its
// containers were stopped: marimo exits 143 on SIGTERM, or 0 if it shuts
// down cleanly, and a fetcher stopped mid-fetch exits too.
func TestClassifyPodBeingDeletedIsInfrastructureLossWhateverItsContainersReport(t *testing.T) {
	fetched := terminated(0, "Completed", "")
	cases := map[string]*corev1.Pod{
		"marimo exited 143 on SIGTERM": podWith([]corev1.ContainerStatus{fetched}, []corev1.ContainerStatus{terminated(143, "Error", "")}),
		"marimo exited 0 on SIGTERM":   podWith([]corev1.ContainerStatus{fetched}, []corev1.ContainerStatus{terminated(0, "Completed", "")}),
		"marimo still serving":         podWith([]corev1.ContainerStatus{fetched}, []corev1.ContainerStatus{running(true)}),
		"fetcher stopped mid-fetch":    podWith([]corev1.ContainerStatus{terminated(143, "Error", "")}, nil),
	}
	for name, pod := range cases {
		if got := classifyPod(beingDeleted(pod)); got.kind != verdictInfrastructureLost {
			t.Errorf("%s: got %+v, want verdictInfrastructureLost", name, got)
		}
	}
}

// TestClassifyPodEvictedIsInfrastructureLoss pins the Pod a kubelet leaves
// behind after a node-pressure eviction: Failed, reason Evicted, its
// container killed, and no deletionTimestamp. The reason alone is enough,
// so a kubelet that sets no DisruptionTarget condition is covered too.
func TestClassifyPodEvictedIsInfrastructureLoss(t *testing.T) {
	pod := podWith([]corev1.ContainerStatus{terminated(0, "Completed", "")}, []corev1.ContainerStatus{terminated(137, "Error", "")})
	pod.Status.Phase = corev1.PodFailed
	pod.Status.Reason = podReasonEvicted
	if got := classifyPod(pod); got.kind != verdictInfrastructureLost {
		t.Fatalf("got %+v, want verdictInfrastructureLost", got)
	}
}

// TestClassifyPodDisruptionTargetIsInfrastructureLoss covers a Pod carrying
// only a True DisruptionTarget condition: one the kubelet terminated (node
// shutdown, its own preemption), or one a scheduler preemption is about to
// delete while it still serves.
func TestClassifyPodDisruptionTargetIsInfrastructureLoss(t *testing.T) {
	fetched := terminated(0, "Completed", "")
	for _, reason := range []string{corev1.PodReasonTerminationByKubelet, corev1.PodReasonPreemptionByScheduler} {
		disrupted := corev1.PodCondition{Type: corev1.DisruptionTarget, Status: corev1.ConditionTrue, Reason: reason}
		for name, main := range map[string]corev1.ContainerStatus{"stopped": terminated(143, "Error", ""), "still serving": running(true)} {
			pod := podWith([]corev1.ContainerStatus{fetched}, []corev1.ContainerStatus{main}, disrupted)
			if got := classifyPod(pod); got.kind != verdictInfrastructureLost {
				t.Errorf("%s, %s: got %+v, want verdictInfrastructureLost", reason, name, got)
			}
		}
	}
}

// TestClassifyPodAbandonedDisruptionIsClassifiedAsUsual pins that only a
// True DisruptionTarget counts: the disruption controller resets one that
// never went ahead to False, and the Pod's own state decides again.
func TestClassifyPodAbandonedDisruptionIsClassifiedAsUsual(t *testing.T) {
	fetched := terminated(0, "Completed", "")
	abandoned := corev1.PodCondition{Type: corev1.DisruptionTarget, Status: corev1.ConditionFalse}
	serving := podWith([]corev1.ContainerStatus{fetched}, []corev1.ContainerStatus{running(true)}, abandoned)
	if got := classifyPod(serving); got.kind != verdictReady {
		t.Errorf("serving Pod: got %+v, want verdictReady", got)
	}
	exited := podWith([]corev1.ContainerStatus{fetched}, []corev1.ContainerStatus{terminated(1, "Error", "")}, abandoned)
	if got := classifyPod(exited); got.kind != verdictFailed || got.reason != v1alpha1.ReasonRuntimeExited {
		t.Errorf("exited Pod: got %+v, want verdictFailed/RuntimeExited", got)
	}
}

func TestClassifyPodNoContainerStatusesYetIsStarting(t *testing.T) {
	pod := podWith(nil, nil)
	if got := classifyPod(pod); got.kind != verdictStarting {
		t.Fatalf("kind = %v, want verdictStarting", got.kind)
	}
}
