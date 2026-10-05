package managerconfig

import (
	"flag"
	"reflect"
	"slices"
	"strings"
	"testing"

	corev1 "k8s.io/api/core/v1"
	"k8s.io/apimachinery/pkg/api/resource"

	"github.com/karthedew/marimohub/marimohub-operator/api/v1alpha1"
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

func TestResolveLeavesWorkspaceStorageDisabledWithoutClaim(t *testing.T) {
	cfg, err := resolve(t, append(validArgs(), "--workspace-supplemental-groups=not-a-number")...)
	if err != nil {
		t.Fatalf("Resolve() error = %v; the other storage flags must be ignored without a claim", err)
	}
	if cfg.WorkspaceStorage.Enabled() {
		t.Errorf("WorkspaceStorage = %+v, want disabled", cfg.WorkspaceStorage)
	}
}

func TestResolveAcceptsWorkspaceStorage(t *testing.T) {
	cfg, err := resolve(t, append(validArgs(),
		"--workspace-claim-name=marimohub-workspaces",
		"--workspace-supplemental-groups=1000, 2000",
	)...)
	if err != nil {
		t.Fatalf("Resolve() error = %v", err)
	}
	want := session.WorkspaceStorage{
		ClaimName:          "marimohub-workspaces",
		MountPath:          "/work/workspace",
		SupplementalGroups: []int64{1000, 2000},
	}
	if cfg.WorkspaceStorage.ClaimName != want.ClaimName || cfg.WorkspaceStorage.MountPath != want.MountPath ||
		!slices.Equal(cfg.WorkspaceStorage.SupplementalGroups, want.SupplementalGroups) {
		t.Errorf("WorkspaceStorage = %+v, want %+v", cfg.WorkspaceStorage, want)
	}
}

func TestResolveRejectsInvalidWorkspaceStorage(t *testing.T) {
	const (
		claimFlag  = "--workspace-claim-name"
		mountFlag  = "--workspace-mount-path"
		groupsFlag = "--workspace-supplemental-groups"
	)
	cases := []struct{ flag, value string }{
		{claimFlag, "Not_A_Name"},
		{mountFlag, "relative/path"},
		{mountFlag, "/work/../etc"},
		{mountFlag, "/work"},
		{mountFlag, "/home/marimo"},
		{mountFlag, "/var/run/secrets/marimohub/x"},
		{groupsFlag, "0"},
		{groupsFlag, "abc"},
	}
	for _, c := range cases {
		args := append(validArgs(), claimFlag+"=marimohub-workspaces", c.flag+"="+c.value)
		_, err := resolve(t, args...)
		if err == nil || !strings.Contains(err.Error(), c.flag) {
			t.Errorf("%s=%s: Resolve() error = %v, want one naming %s", c.flag, c.value, err, c.flag)
		}
	}
}

func TestResolveSharedVolumesAppliesDefaults(t *testing.T) {
	cfg, err := resolve(t, append(validArgs(),
		`--shared-volumes=[{"name":"datasets","claimName":"nfs-datasets"},`+
			`{"name":"scratch","claimName":"nfs-team","subPath":"team/scratch","mountPath":"/data/scratch",`+
			`"readOnly":false,"modes":["edit"],"supplementalGroups":[2000]}]`,
	)...)
	if err != nil {
		t.Fatalf("Resolve() error = %v", err)
	}
	want := []session.SharedVolume{
		{
			Name:      "datasets",
			ClaimName: "nfs-datasets",
			MountPath: "/mnt/datasets",
			ReadOnly:  true,
			Modes:     []v1alpha1.RuntimeMode{v1alpha1.RuntimeModeEdit, v1alpha1.RuntimeModeRun},
		},
		{
			Name:               "scratch",
			ClaimName:          "nfs-team",
			SubPath:            "team/scratch",
			MountPath:          "/data/scratch",
			ReadOnly:           false,
			Modes:              []v1alpha1.RuntimeMode{v1alpha1.RuntimeModeEdit},
			SupplementalGroups: []int64{2000},
		},
	}
	if !reflect.DeepEqual(cfg.SharedVolumes, want) {
		t.Errorf("SharedVolumes = %+v, want %+v", cfg.SharedVolumes, want)
	}
}

func TestResolveSharedVolumesEmptyMeansNone(t *testing.T) {
	for _, raw := range []string{"", "[]", "  "} {
		cfg, err := resolve(t, append(validArgs(), "--shared-volumes="+raw)...)
		if err != nil {
			t.Fatalf("%q: Resolve() error = %v", raw, err)
		}
		if len(cfg.SharedVolumes) != 0 {
			t.Errorf("%q: SharedVolumes = %+v, want none", raw, cfg.SharedVolumes)
		}
	}
}

func TestResolveRejectsInvalidSharedVolumes(t *testing.T) {
	const ok = `"name":"datasets","claimName":"nfs-datasets"`
	cases := map[string]string{
		"not JSON":                      `{`,
		"chart key instead of flag key": `[{"name":"datasets","existingClaim":"nfs-datasets"}]`,
		"bad name":                      `[{"name":"Data_Sets","claimName":"nfs-datasets"}]`,
		"name too long":                 `[{"name":"` + strings.Repeat("a", 57) + `","claimName":"nfs-datasets"}]`,
		"duplicate name":                `[{` + ok + `},{` + ok + `,"mountPath":"/mnt/other"}]`,
		"bad claim":                     `[{"name":"datasets","claimName":"Bad_Claim"}]`,
		"absolute subPath":              `[{` + ok + `,"subPath":"/etc"}]`,
		"escaping subPath":              `[{` + ok + `,"subPath":"../other"}]`,
		"unclean subPath":               `[{` + ok + `,"subPath":"a//b"}]`,
		"relative mountPath":            `[{` + ok + `,"mountPath":"mnt/datasets"}]`,
		"reserved mountPath":            `[{` + ok + `,"mountPath":"/work"}]`,
		"inside the credentials":        `[{` + ok + `,"mountPath":"/var/run/secrets/marimohub/data"}]`,
		"over the Workspace":            `[{` + ok + `,"mountPath":"/work/workspace/datasets"}]`,
		"overlapping another share":     `[{` + ok + `},{"name":"inner","claimName":"nfs-inner","mountPath":"/mnt/datasets/inner"}]`,
		"writable share for deploy":     `[{` + ok + `,"readOnly":false,"modes":["edit","deploy"]}]`,
		"unknown mode":                  `[{` + ok + `,"modes":["edit","admin"]}]`,
		"no modes":                      `[{` + ok + `,"modes":[]}]`,
		"repeated mode":                 `[{` + ok + `,"modes":["edit","edit"]}]`,
		"bad supplemental group":        `[{` + ok + `,"supplementalGroups":[0]}]`,
	}
	for name, raw := range cases {
		args := append(validArgs(), "--workspace-claim-name=marimohub-workspaces", "--shared-volumes="+raw)
		if _, err := resolve(t, args...); err == nil || !strings.Contains(err.Error(), "--shared-volumes") {
			t.Errorf("%s: Resolve() error = %v, want one naming --shared-volumes", name, err)
		}
	}
}
