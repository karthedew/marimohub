package controller

import (
	"context"
	"errors"
	"testing"

	corev1 "k8s.io/api/core/v1"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	"k8s.io/apimachinery/pkg/api/resource"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/runtime/schema"
	"sigs.k8s.io/controller-runtime/pkg/client/fake"
)

// Shared across this file and quota_envtest_test.go: both exercise the same
// confirmQuotaExhaustion/effectivePodRequest machinery against slightly
// different fixtures (a fake client here, a real envtest API server there).
const (
	testQuotaName   = "chart-quota"
	requestsCPUName = "requests.cpu"
	podsResource    = "pods"
)

func testPod(cpuRequest, memRequest string) *corev1.Pod {
	return &corev1.Pod{
		Spec: corev1.PodSpec{
			InitContainers: []corev1.Container{{
				Resources: corev1.ResourceRequirements{
					Requests: corev1.ResourceList{
						corev1.ResourceCPU:    resource.MustParse("25m"),
						corev1.ResourceMemory: resource.MustParse("64Mi"),
					},
				},
			}},
			Containers: []corev1.Container{{
				Resources: corev1.ResourceRequirements{
					Requests: corev1.ResourceList{
						corev1.ResourceCPU:    resource.MustParse(cpuRequest),
						corev1.ResourceMemory: resource.MustParse(memRequest),
					},
					Limits: corev1.ResourceList{
						corev1.ResourceCPU:    resource.MustParse("1"),
						corev1.ResourceMemory: resource.MustParse("2Gi"),
					},
				},
			}},
		},
	}
}

func TestEffectivePodRequestUsesMaxOfInitAndSumOfContainers(t *testing.T) {
	pod := testPod("250m", "512Mi")
	got := effectivePodRequest(pod)

	// The Runtime container's 250m/512Mi requests dominate the fetcher's
	// 25m/64Mi, so the effective request equals the sum here (there is only
	// one regular container), not sum(containers) + max(init).
	wantCPU := resource.MustParse("250m")
	if v := got[corev1.ResourceName(requestsCPUName)]; v.Cmp(wantCPU) != 0 {
		t.Errorf("requests.cpu = %v, want %v", v, wantCPU)
	}
	wantMem := resource.MustParse("512Mi")
	if v := got[corev1.ResourceName("requests.memory")]; v.Cmp(wantMem) != 0 {
		t.Errorf("requests.memory = %v, want %v", v, wantMem)
	}
	wantLimitCPU := resource.MustParse("1")
	if v := got[corev1.ResourceName("limits.cpu")]; v.Cmp(wantLimitCPU) != 0 {
		t.Errorf("limits.cpu = %v, want %v", v, wantLimitCPU)
	}
	if v := got[corev1.ResourcePods]; v.Cmp(resource.MustParse("1")) != 0 {
		t.Errorf("pods = %v, want 1", v)
	}
}

func TestEffectivePodRequestInitContainerDominates(t *testing.T) {
	pod := &corev1.Pod{
		Spec: corev1.PodSpec{
			InitContainers: []corev1.Container{{
				Resources: corev1.ResourceRequirements{
					Requests: corev1.ResourceList{corev1.ResourceCPU: resource.MustParse("2")},
				},
			}},
			Containers: []corev1.Container{{
				Resources: corev1.ResourceRequirements{
					Requests: corev1.ResourceList{corev1.ResourceCPU: resource.MustParse("100m")},
				},
			}},
		},
	}
	got := effectivePodRequest(pod)
	want := resource.MustParse("2")
	if v := got[corev1.ResourceName(requestsCPUName)]; v.Cmp(want) != 0 {
		t.Errorf("requests.cpu = %v, want %v (init container's larger request should dominate)", v, want)
	}
}

func TestConfirmQuotaExhaustionRequiresBothTextAndLiveConfirmation(t *testing.T) {
	quota := &corev1.ResourceQuota{
		ObjectMeta: metav1.ObjectMeta{Name: testQuotaName, Namespace: "ns"},
		Status: corev1.ResourceQuotaStatus{
			Hard: corev1.ResourceList{requestsCPUName: resource.MustParse("1")},
			Used: corev1.ResourceList{requestsCPUName: resource.MustParse("900m")},
		},
	}
	r := &MarimoSessionReconciler{Client: fake.NewClientBuilder().WithObjects(quota).Build()}
	pod := testPod("250m", "512Mi") // requests.cpu 250m; used 900m + 250m > hard 1 -> exceeded

	t.Run("confirmed when both text and live quota agree", func(t *testing.T) {
		err := apierrors.NewForbidden(schema.GroupResource{Resource: podsResource}, "msess-x",
			errors.New("exceeded quota: chart-quota, requested: requests.cpu=250m, used: requests.cpu=900m, limited: requests.cpu=1"))
		confirmed, listErr := r.confirmQuotaExhaustion(context.Background(), "ns", pod, err)
		if listErr != nil {
			t.Fatalf("confirmQuotaExhaustion() error = %v", listErr)
		}
		if !confirmed {
			t.Error("confirmed = false, want true")
		}
	})

	t.Run("not confirmed without the canonical quota admission text", func(t *testing.T) {
		err := apierrors.NewForbidden(schema.GroupResource{Resource: podsResource}, "msess-x", errors.New("pods is forbidden by a webhook"))
		confirmed, listErr := r.confirmQuotaExhaustion(context.Background(), "ns", pod, err)
		if listErr != nil {
			t.Fatalf("confirmQuotaExhaustion() error = %v", listErr)
		}
		if confirmed {
			t.Error("confirmed = true, want false (no canonical quota text)")
		}
	})
}

func TestConfirmQuotaExhaustionNotConfirmedWhenQuotaHasHeadroom(t *testing.T) {
	quota := &corev1.ResourceQuota{
		ObjectMeta: metav1.ObjectMeta{Name: testQuotaName, Namespace: "ns"},
		Status: corev1.ResourceQuotaStatus{
			Hard: corev1.ResourceList{requestsCPUName: resource.MustParse("10")},
			Used: corev1.ResourceList{requestsCPUName: resource.MustParse("100m")},
		},
	}
	r := &MarimoSessionReconciler{Client: fake.NewClientBuilder().WithObjects(quota).Build()}
	pod := testPod("250m", "512Mi")

	err := apierrors.NewForbidden(schema.GroupResource{Resource: podsResource}, "msess-x",
		errors.New("exceeded quota: chart-quota, requested: requests.cpu=250m, used: requests.cpu=100m, limited: requests.cpu=10"))
	confirmed, listErr := r.confirmQuotaExhaustion(context.Background(), "ns", pod, err)
	if listErr != nil {
		t.Fatalf("confirmQuotaExhaustion() error = %v", listErr)
	}
	if confirmed {
		t.Error("confirmed = true, want false (quota has ample headroom; text alone must not be enough)")
	}
}

func TestSanitizeAdmissionErrorStripsProtocolPrefix(t *testing.T) {
	err := apierrors.NewForbidden(schema.GroupResource{Resource: podsResource}, "msess-x", errors.New("exceeded quota: chart-quota"))
	got := sanitizeAdmissionError(err)
	if got != "exceeded quota: chart-quota" {
		t.Errorf("sanitizeAdmissionError() = %q, want the message with the forbidden-prefix stripped", got)
	}
}

func TestSanitizeAdmissionErrorFallsBackToFullMessage(t *testing.T) {
	err := errors.New("some other error entirely")
	if got := sanitizeAdmissionError(err); got != "some other error entirely" {
		t.Errorf("sanitizeAdmissionError() = %q", got)
	}
}
