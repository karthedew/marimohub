package controller

import (
	"testing"

	corev1 "k8s.io/api/core/v1"

	"github.com/karthedew/marimohub/marimohub-operator/api/v1alpha1"
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

func TestClassifyPodNoContainerStatusesYetIsStarting(t *testing.T) {
	pod := podWith(nil, nil)
	if got := classifyPod(pod); got.kind != verdictStarting {
		t.Fatalf("kind = %v, want verdictStarting", got.kind)
	}
}
