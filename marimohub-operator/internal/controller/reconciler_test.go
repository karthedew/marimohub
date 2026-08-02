package controller

import (
	"time"

	corev1 "k8s.io/api/core/v1"
	"k8s.io/apimachinery/pkg/api/resource"
	"k8s.io/client-go/tools/record"
	clocktesting "k8s.io/utils/clock/testing"

	"github.com/karthedew/marimohub/marimohub-operator/internal/session"
)

// testSessionOptions mirrors what a chart would inject, at values small
// enough that every builder output stays cheap to construct and hash in a
// tight test loop.
func testSessionOptions() session.Options {
	return session.Options{
		FetcherImage:   "ghcr.io/karthedew/marimohub-fetcher@sha256:" + repeat64("a"),
		InternalAPIURL: "https://marimohub-internal.marimohub.svc:8443",
		InternalAPICA:  session.CABundle{SecretName: "marimohub-internal-ca", SecretKey: "ca.crt"},
		IdleTimeout:    session.IdleTimeoutDefaults{EditSeconds: 1800, RunSeconds: 600, DeploySeconds: 300},
		Resources: corev1.ResourceRequirements{
			Requests: corev1.ResourceList{
				corev1.ResourceCPU:    resource.MustParse("250m"),
				corev1.ResourceMemory: resource.MustParse("512Mi"),
			},
			Limits: corev1.ResourceList{
				corev1.ResourceCPU:    resource.MustParse("1"),
				corev1.ResourceMemory: resource.MustParse("2Gi"),
			},
		},
		FetcherResources: corev1.ResourceRequirements{
			Requests: corev1.ResourceList{
				corev1.ResourceCPU:    resource.MustParse("25m"),
				corev1.ResourceMemory: resource.MustParse("64Mi"),
			},
			Limits: corev1.ResourceList{
				corev1.ResourceCPU:    resource.MustParse("100m"),
				corev1.ResourceMemory: resource.MustParse("128Mi"),
			},
		},
		ImagePullPolicy: corev1.PullIfNotPresent,
	}
}

func repeat64(s string) string {
	out := ""
	for len(out) < 64 {
		out += s
	}
	return out[:64]
}

// newTestReconciler builds a reconciler wired against the shared envtest
// API server, with a fake clock the test controls explicitly and short
// deadlines so waiting out a real timeout never means a slow test suite.
func newTestReconciler(fakeClock *clocktesting.FakeClock) *MarimoSessionReconciler {
	return &MarimoSessionReconciler{
		Client:    k8sClient,
		APIReader: k8sClient,
		Scheme:    k8sClient.Scheme(),
		Recorder:  record.NewFakeRecorder(100),
		Metrics:   NewMetrics(),
		Clock:     fakeClock,
		Backoff:   BackoffPolicy{Base: 10 * time.Millisecond, Max: 100 * time.Millisecond, Jitter: 0},
		Options:   testSessionOptions(),

		ImagePullDeadline: 200 * time.Millisecond,
		UnhealthyTimeout:  200 * time.Millisecond,
		NodeLossDeadline:  200 * time.Millisecond,
		IdleGracePeriod:   0,
	}
}
