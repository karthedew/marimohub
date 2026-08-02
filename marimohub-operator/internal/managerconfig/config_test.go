package managerconfig

import (
	"flag"
	"strings"
	"testing"

	corev1 "k8s.io/api/core/v1"
	"k8s.io/apimachinery/pkg/api/resource"

	"github.com/karthedew/marimohub/marimohub-operator/internal/session"
)

const validResources = `{"requests":{"cpu":"250m","memory":"512Mi","ephemeral-storage":"256Mi"},` +
	`"limits":{"cpu":"1","memory":"2Gi","ephemeral-storage":"1Gi"}}`

const validFetcherResources = `{"requests":{"cpu":"25m","memory":"64Mi","ephemeral-storage":"32Mi"},` +
	`"limits":{"cpu":"100m","memory":"128Mi","ephemeral-storage":"128Mi"}}`

func resolve(t *testing.T, args ...string) (Config, error) {
	t.Helper()
	fs := flag.NewFlagSet("test", flag.ContinueOnError)
	handle := RegisterFlags(fs)
	if err := fs.Parse(args); err != nil {
		t.Fatalf("fs.Parse() error = %v", err)
	}
	return handle.Resolve()
}

func validArgs() []string {
	return []string{
		"--runtime-namespace=marimohub-sessions",
		"--fetcher-image=ghcr.io/karthedew/marimohub-fetcher@sha256:" + strings.Repeat("a", 64),
		"--internal-api-url=https://marimohub-internal.marimohub.svc:8443",
		"--internal-api-ca-secret-name=marimohub-internal-ca",
		"--internal-api-ca-secret-key=ca.crt",
		"--default-resources=" + validResources,
		"--fetcher-resources=" + validFetcherResources,
		"--image-pull-policy=IfNotPresent",
		"--idle-timeout-edit-seconds=1800",
		"--idle-timeout-run-seconds=600",
		"--idle-timeout-deploy-seconds=300",
	}
}

func TestResolveAcceptsFullyPopulatedFlags(t *testing.T) {
	cfg, err := resolve(t, validArgs()...)
	if err != nil {
		t.Fatalf("Resolve() error = %v", err)
	}
	if cfg.RuntimeNamespace != "marimohub-sessions" {
		t.Errorf("RuntimeNamespace = %q", cfg.RuntimeNamespace)
	}
	if cfg.IdleTimeout != (session.IdleTimeoutDefaults{EditSeconds: 1800, RunSeconds: 600, DeploySeconds: 300}) {
		t.Errorf("IdleTimeout = %+v", cfg.IdleTimeout)
	}
	wantCPU := resource.MustParse("250m")
	if got := cfg.DefaultResources.Requests[corev1.ResourceCPU]; got.Cmp(wantCPU) != 0 {
		t.Errorf("DefaultResources.Requests[cpu] = %v, want %v", got, wantCPU)
	}
	wantFetcherCPU := resource.MustParse("25m")
	if got := cfg.FetcherResources.Requests[corev1.ResourceCPU]; got.Cmp(wantFetcherCPU) != 0 {
		t.Errorf("FetcherResources.Requests[cpu] = %v, want %v", got, wantFetcherCPU)
	}
	if cfg.InternalAPICA.SecretName != "marimohub-internal-ca" || cfg.InternalAPICA.SecretKey != "ca.crt" {
		t.Errorf("InternalAPICA = %+v", cfg.InternalAPICA)
	}
	if cfg.ImagePullPolicy != corev1.PullIfNotPresent {
		t.Errorf("ImagePullPolicy = %v, want IfNotPresent", cfg.ImagePullPolicy)
	}
}

func TestResolveRejectsMissingFlags(t *testing.T) {
	_, err := resolve(t)
	if err == nil {
		t.Fatal("Resolve() with no flags: want error, got nil")
	}
	for _, want := range []string{
		"--runtime-namespace is required",
		"--fetcher-image is required",
		"--internal-api-url is required",
		"--internal-api-ca-secret-name is required",
		"--internal-api-ca-secret-key is required",
		"--default-resources is required",
		"--fetcher-resources is required",
		"--image-pull-policy must be one of",
		"--idle-timeout-edit-seconds must be at least 30",
		"--idle-timeout-run-seconds must be at least 30",
		"--idle-timeout-deploy-seconds must be at least 30",
	} {
		if !strings.Contains(err.Error(), want) {
			t.Errorf("Resolve() error missing %q; got: %v", want, err)
		}
	}
}

func TestResolveRejectsMutableTagFetcherImage(t *testing.T) {
	args := validArgs()
	for i, a := range args {
		if strings.HasPrefix(a, "--fetcher-image=") {
			args[i] = "--fetcher-image=ghcr.io/karthedew/marimohub-fetcher:latest"
		}
	}
	_, err := resolve(t, args...)
	if err == nil || !strings.Contains(err.Error(), "digest-qualified") {
		t.Fatalf("Resolve() error = %v, want digest-qualified complaint", err)
	}
}

func TestResolveRejectsNonHTTPInternalAPIURL(t *testing.T) {
	args := validArgs()
	for i, a := range args {
		if strings.HasPrefix(a, "--internal-api-url=") {
			args[i] = "--internal-api-url=ftp://example.com"
		}
	}
	_, err := resolve(t, args...)
	if err == nil || !strings.Contains(err.Error(), "scheme must be http or https") {
		t.Fatalf("Resolve() error = %v, want scheme complaint", err)
	}
}

func TestResolveRejectsMalformedInternalAPIURL(t *testing.T) {
	args := validArgs()
	for i, a := range args {
		if strings.HasPrefix(a, "--internal-api-url=") {
			args[i] = "--internal-api-url=%zz"
		}
	}
	_, err := resolve(t, args...)
	if err == nil {
		t.Fatal("Resolve() with malformed URL: want error, got nil")
	}
}

func TestResolveRejectsResourcesMissingRequiredKeys(t *testing.T) {
	for _, tc := range []struct {
		name string
		json string
		want string
	}{
		{"invalid JSON", "{not json", "invalid JSON"},
		{"missing requests.memory", `{"requests":{"cpu":"250m"},"limits":{"cpu":"1","memory":"2Gi","ephemeral-storage":"1Gi"}}`, "requests.memory is required"},
		{"missing limits.ephemeral-storage", `{"requests":{"cpu":"250m","memory":"512Mi","ephemeral-storage":"256Mi"},"limits":{"cpu":"1","memory":"2Gi"}}`, "limits.ephemeral-storage is required"},
	} {
		t.Run(tc.name, func(t *testing.T) {
			args := validArgs()
			for i, a := range args {
				if strings.HasPrefix(a, "--default-resources=") {
					args[i] = "--default-resources=" + tc.json
				}
			}
			_, err := resolve(t, args...)
			if err == nil || !strings.Contains(err.Error(), tc.want) {
				t.Fatalf("Resolve() error = %v, want to contain %q", err, tc.want)
			}
		})
	}
}

func TestResolveRejectsBelowFloorIdleTimeout(t *testing.T) {
	args := validArgs()
	for i, a := range args {
		if strings.HasPrefix(a, "--idle-timeout-run-seconds=") {
			args[i] = "--idle-timeout-run-seconds=29"
		}
	}
	_, err := resolve(t, args...)
	if err == nil || !strings.Contains(err.Error(), "--idle-timeout-run-seconds must be at least 30, got 29") {
		t.Fatalf("Resolve() error = %v, want floor complaint", err)
	}
}
