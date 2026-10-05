// Package managerconfig turns the manager's command-line flags into a
// validated startup Config, or a single error describing every problem
// found. The reconciler logic that actually consumes FetcherImage,
// InternalAPIURL, DefaultResources, and IdleTimeout lives elsewhere, but
// refusing to start with a missing or malformed flag is this package's job:
// a manager that silently ran with an empty internal API URL or a
// zero-second idle timeout would fail in a much harder place to diagnose
// than its own startup.
package managerconfig

import (
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"net/url"
	"path"
	"regexp"
	"slices"
	"strconv"
	"strings"
	"time"

	corev1 "k8s.io/api/core/v1"
	"k8s.io/apimachinery/pkg/util/validation"

	"github.com/karthedew/marimohub/marimohub-operator/api/v1alpha1"
	"github.com/karthedew/marimohub/marimohub-operator/internal/session"
)

// MinIdleTimeoutSeconds mirrors the CRD's spec.idleTimeoutSeconds floor. The
// operator's own per-mode defaults must satisfy the same floor it enforces
// on the CR override, or the two would disagree about the shortest Runtime
// lifetime the platform allows.
const MinIdleTimeoutSeconds = 30

// defaultIdleGracePeriodSeconds mirrors controller.DefaultIdleGracePeriod so
// a manager started without --idle-grace-period-seconds keeps behaving
// exactly as it did before the flag existed.
const defaultIdleGracePeriodSeconds = 20

// defaultWorkspaceMountPath puts the Workspace directory beside the fetched
// notebook.py, so notebook code can reach it with a relative path from the
// Runtime image's /work working directory.
const defaultWorkspaceMountPath = "/work/workspace"

// maxGroupID is the largest group ID Kubernetes accepts in a Pod's
// supplementalGroups.
const maxGroupID = 2147483647

// imageDigestPattern reuses the exact rule the CRD applies to spec.image, so
// the manager's own --fetcher-image flag and the API server's admission
// check can never disagree about what "digest-qualified" means.
var imageDigestPattern = regexp.MustCompile(v1alpha1.ImageDigestPattern)

// requiredResourceKeys are the resource names the chart's documented default
// shape always sets; requiring them here catches a values.yaml typo at
// manager startup instead of at the first Pod-create ResourceQuota surprise.
var requiredResourceKeys = []corev1.ResourceName{
	corev1.ResourceCPU,
	corev1.ResourceMemory,
	corev1.ResourceEphemeralStorage,
}

// Config is the manager's validated startup configuration. The only way to
// obtain one is FlagSet.Resolve, which has already checked every invariant
// below.
type Config struct {
	// RuntimeNamespace is the single namespace the manager watches and
	// caches. Never empty: an empty value would otherwise read as "no
	// namespace restriction" to some client-go call sites, which must never
	// happen for a controller with this workload's blast radius.
	RuntimeNamespace string

	// FetcherImage is the digest-qualified source-fetcher image injected into
	// every Runtime Pod's init container.
	FetcherImage string

	// InternalAPIURL is the base URL of the unrouted internal API the
	// source-fetcher and marimo container call.
	InternalAPIURL string

	// InternalAPICA is the namespace-wide Secret/key every Runtime Pod
	// validates the internal API's TLS certificate against.
	InternalAPICA session.CABundle

	// DefaultResources are the chart-wide Runtime resource defaults applied
	// when a MarimoSession does not set spec.resources.
	DefaultResources corev1.ResourceRequirements

	// FetcherResources are the fixed source-fetcher init container
	// resources, never overridable by a MarimoSession.
	FetcherResources corev1.ResourceRequirements

	// ImagePullPolicy applies to both the fetcher and Runtime containers.
	ImagePullPolicy corev1.PullPolicy

	// ServiceAccountName is the Runtime ServiceAccount, if the chart
	// provisions one; empty leaves Runtime Pods on the namespace default.
	ServiceAccountName string

	// ImagePullSecrets are attached to every Runtime Pod directly.
	ImagePullSecrets []corev1.LocalObjectReference

	// IdleTimeout holds the per-mode idle defaults applied when a
	// MarimoSession does not set spec.idleTimeoutSeconds.
	IdleTimeout session.IdleTimeoutDefaults

	// IdleGracePeriod is added on top of the effective idle timeout before
	// any idle action, absorbing the backend's own activity-signal interval
	// so a throttled activity write is never mistaken for real idleness.
	// This is the one reconciler deadline promoted to a flag: the chart's
	// documented Runtime policy values already name it
	// (activityGraceSeconds), unlike the image-pull/unhealthy/node-loss
	// deadlines, which stay internal constants until something actually
	// needs to configure them.
	IdleGracePeriod time.Duration

	// WorkspaceStorage mounts each Workspace's durable directory into its
	// edit and run Runtimes; the zero value disables it.
	WorkspaceStorage session.WorkspaceStorage

	// SharedVolumes are the administrator-provided data volumes mounted
	// into Runtimes, already defaulted and validated.
	SharedVolumes []session.SharedVolume
}

// flagValues holds the raw, unvalidated flag destinations. Keeping this
// separate from Config means a caller can never observe a half-validated
// Config: Resolve either returns one that already passed every check, or an
// error.
type flagValues struct {
	runtimeNamespace     string
	fetcherImage         string
	internalAPIURL       string
	caSecretName         string
	caSecretKey          string
	defaultResourcesJSON string
	fetcherResourcesJSON string
	imagePullPolicy      string
	serviceAccountName   string
	imagePullSecrets     string
	idleTimeoutEdit      int
	idleTimeoutRun       int
	idleTimeoutDeploy    int
	idleGracePeriod      int
	workspaceClaimName   string
	workspaceMountPath   string
	workspaceGroups      string
	sharedVolumesJSON    string
}

// FlagSet is the handle RegisterFlags returns; call Resolve after the
// wrapped flag.FlagSet has parsed its arguments.
type FlagSet struct {
	values *flagValues
}

// RegisterFlags binds every startup configuration flag onto fs and returns a
// handle Resolve can later turn into a validated Config. Separating
// registration from resolution lets a test call fs.Parse against arbitrary
// arguments without duplicating flag definitions.
func RegisterFlags(fs *flag.FlagSet) *FlagSet {
	v := &flagValues{}
	fs.StringVar(&v.runtimeNamespace, "runtime-namespace", "",
		"Namespace the manager watches and caches MarimoSession, Pod, Service, and Secret metadata in. Required.")
	fs.StringVar(&v.fetcherImage, "fetcher-image", "",
		"Digest-qualified source-fetcher image injected into every Runtime Pod. Required.")
	fs.StringVar(&v.internalAPIURL, "internal-api-url", "",
		"Base URL of the internal API the source-fetcher and Runtime call. Required.")
	fs.StringVar(&v.caSecretName, "internal-api-ca-secret-name", "",
		"Name of the namespace-wide Secret containing the internal API's CA certificate. Required.")
	fs.StringVar(&v.caSecretKey, "internal-api-ca-secret-key", "",
		"Key within --internal-api-ca-secret-name holding the CA certificate. Required.")
	fs.StringVar(&v.defaultResourcesJSON, "default-resources", "",
		`JSON-encoded corev1.ResourceRequirements applied when a MarimoSession omits spec.resources, e.g. `+
			`{"requests":{"cpu":"250m","memory":"512Mi","ephemeral-storage":"256Mi"},`+
			`"limits":{"cpu":"1","memory":"2Gi","ephemeral-storage":"1Gi"}}. Required.`)
	fs.StringVar(&v.fetcherResourcesJSON, "fetcher-resources", "",
		`JSON-encoded corev1.ResourceRequirements for the source-fetcher init container. Required.`)
	fs.StringVar(&v.imagePullPolicy, "image-pull-policy", "",
		"Image pull policy applied to the fetcher and Runtime containers (Always, IfNotPresent, or Never). Required.")
	fs.StringVar(&v.serviceAccountName, "runtime-service-account", "",
		"ServiceAccount every Runtime Pod runs under. Optional; empty leaves Pods on the namespace default.")
	fs.StringVar(&v.imagePullSecrets, "image-pull-secrets", "",
		"Comma-separated imagePullSecrets attached to every Runtime Pod. Optional.")
	fs.IntVar(&v.idleTimeoutEdit, "idle-timeout-edit-seconds", 0,
		"Default idle timeout for edit-mode Runtimes, in seconds. Required, minimum 30.")
	fs.IntVar(&v.idleTimeoutRun, "idle-timeout-run-seconds", 0,
		"Default idle timeout for run-mode Runtimes, in seconds. Required, minimum 30.")
	fs.IntVar(&v.idleTimeoutDeploy, "idle-timeout-deploy-seconds", 0,
		"Default idle timeout for deploy-mode Runtimes, in seconds. Required, minimum 30.")
	fs.IntVar(&v.idleGracePeriod, "idle-grace-period-seconds", defaultIdleGracePeriodSeconds,
		"Additional grace period added on top of the effective idle timeout before any idle action, "+
			"absorbing the backend's own activity-signal interval. Must stay comfortably larger than that interval.")
	fs.StringVar(&v.workspaceClaimName, "workspace-claim-name", "",
		"ReadWriteMany PersistentVolumeClaim in --runtime-namespace holding every Workspace directory under "+
			"workspaces/<workspaceId>. Optional; empty disables Workspace storage.")
	fs.StringVar(&v.workspaceMountPath, "workspace-mount-path", defaultWorkspaceMountPath,
		"Absolute path where edit and run Runtimes see their Workspace directory.")
	fs.StringVar(&v.workspaceGroups, "workspace-supplemental-groups", "",
		"Comma-separated group IDs added to every Pod that mounts --workspace-claim-name. Optional.")
	fs.StringVar(&v.sharedVolumesJSON, "shared-volumes", "",
		`JSON list of administrator-provided volumes to mount into Runtimes, e.g. `+
			`[{"name":"datasets","claimName":"nfs-datasets","mountPath":"/mnt/datasets"}]. Each entry may `+
			`also set subPath, readOnly (default true), modes (default ["edit","run"]; "deploy" only when `+
			`read-only) and supplementalGroups. Optional.`)
	return &FlagSet{values: v}
}

// Resolve validates the parsed flag values and returns a Config, or every
// validation failure joined into one error so a misconfigured manager
// reports its whole problem list on one failed startup rather than one flag
// at a time across repeated restarts.
func (f *FlagSet) Resolve() (Config, error) {
	v := f.values
	cfg := Config{
		RuntimeNamespace: v.runtimeNamespace,
		FetcherImage:     v.fetcherImage,
		InternalAPIURL:   v.internalAPIURL,
		IdleTimeout: session.IdleTimeoutDefaults{
			EditSeconds:   int32(v.idleTimeoutEdit),
			RunSeconds:    int32(v.idleTimeoutRun),
			DeploySeconds: int32(v.idleTimeoutDeploy),
		},
		IdleGracePeriod: time.Duration(v.idleGracePeriod) * time.Second,
	}

	var errs []error

	if v.runtimeNamespace == "" {
		errs = append(errs, errors.New("--runtime-namespace is required"))
	}

	if v.fetcherImage == "" {
		errs = append(errs, errors.New("--fetcher-image is required"))
	} else if !imageDigestPattern.MatchString(v.fetcherImage) {
		errs = append(errs, fmt.Errorf(
			"--fetcher-image %q must be a digest-qualified image reference (repository@sha256:<64 hex>)", v.fetcherImage))
	}

	if v.internalAPIURL == "" {
		errs = append(errs, errors.New("--internal-api-url is required"))
	} else if err := validateInternalAPIURL(v.internalAPIURL); err != nil {
		errs = append(errs, fmt.Errorf("--internal-api-url: %w", err))
	}

	if v.caSecretName == "" {
		errs = append(errs, errors.New("--internal-api-ca-secret-name is required"))
	} else {
		cfg.InternalAPICA.SecretName = v.caSecretName
	}
	if v.caSecretKey == "" {
		errs = append(errs, errors.New("--internal-api-ca-secret-key is required"))
	} else {
		cfg.InternalAPICA.SecretKey = v.caSecretKey
	}

	if v.defaultResourcesJSON == "" {
		errs = append(errs, errors.New("--default-resources is required"))
	} else if resources, err := parseResourceRequirements(v.defaultResourcesJSON); err != nil {
		errs = append(errs, fmt.Errorf("--default-resources: %w", err))
	} else {
		cfg.DefaultResources = resources
	}

	if v.fetcherResourcesJSON == "" {
		errs = append(errs, errors.New("--fetcher-resources is required"))
	} else if resources, err := parseResourceRequirements(v.fetcherResourcesJSON); err != nil {
		errs = append(errs, fmt.Errorf("--fetcher-resources: %w", err))
	} else {
		cfg.FetcherResources = resources
	}

	switch corev1.PullPolicy(v.imagePullPolicy) {
	case corev1.PullAlways, corev1.PullIfNotPresent, corev1.PullNever:
		cfg.ImagePullPolicy = corev1.PullPolicy(v.imagePullPolicy)
	default:
		errs = append(errs, fmt.Errorf(
			"--image-pull-policy must be one of Always, IfNotPresent, or Never, got %q", v.imagePullPolicy))
	}

	cfg.ServiceAccountName = v.serviceAccountName
	cfg.ImagePullSecrets = parseImagePullSecrets(v.imagePullSecrets)

	for _, timeout := range []struct {
		flag  string
		value int
	}{
		{"--idle-timeout-edit-seconds", v.idleTimeoutEdit},
		{"--idle-timeout-run-seconds", v.idleTimeoutRun},
		{"--idle-timeout-deploy-seconds", v.idleTimeoutDeploy},
	} {
		if timeout.value < MinIdleTimeoutSeconds {
			errs = append(errs, fmt.Errorf("%s must be at least %d, got %d", timeout.flag, MinIdleTimeoutSeconds, timeout.value))
		}
	}

	if v.idleGracePeriod < 0 {
		errs = append(errs, fmt.Errorf("--idle-grace-period-seconds must not be negative, got %d", v.idleGracePeriod))
	}

	if storage, storageErrs := resolveWorkspaceStorage(v); len(storageErrs) > 0 {
		errs = append(errs, storageErrs...)
	} else {
		cfg.WorkspaceStorage = storage
	}

	if shared, sharedErrs := resolveSharedVolumes(v.sharedVolumesJSON, cfg.WorkspaceStorage); len(sharedErrs) > 0 {
		errs = append(errs, sharedErrs...)
	} else {
		cfg.SharedVolumes = shared
	}

	if len(errs) > 0 {
		return Config{}, errors.Join(errs...)
	}
	return cfg, nil
}

// resolveWorkspaceStorage validates the three Workspace storage flags
// together. With no claim configured the other two are ignored, so the
// chart can always pass the mount path default without enabling anything.
func resolveWorkspaceStorage(v *flagValues) (session.WorkspaceStorage, []error) {
	if v.workspaceClaimName == "" {
		return session.WorkspaceStorage{}, nil
	}
	var errs []error
	for _, msg := range validation.IsDNS1123Subdomain(v.workspaceClaimName) {
		errs = append(errs, fmt.Errorf("--workspace-claim-name %q: %s", v.workspaceClaimName, msg))
	}
	if err := validateRuntimeMountPath(v.workspaceMountPath); err != nil {
		errs = append(errs, fmt.Errorf("--workspace-mount-path: %w", err))
	}
	groups, err := parseSupplementalGroups(v.workspaceGroups)
	if err != nil {
		errs = append(errs, fmt.Errorf("--workspace-supplemental-groups: %w", err))
	}
	return session.WorkspaceStorage{
		ClaimName:          v.workspaceClaimName,
		MountPath:          v.workspaceMountPath,
		SupplementalGroups: groups,
	}, errs
}

// validateRuntimeMountPath refuses a Workspace or shared-volume mount path
// that would shadow one of the Runtime's own mounts or land inside the
// read-only credential projection. Nesting under a scratch mount (the
// /work/workspace default) is fine and intended: it puts the Workspace
// directory next to notebook.py.
func validateRuntimeMountPath(p string) error {
	if !path.IsAbs(p) || path.Clean(p) != p {
		return fmt.Errorf("%q must be a clean absolute path", p)
	}
	reserved := session.ReservedMountPaths()
	if slices.Contains(reserved, p) {
		return fmt.Errorf("%q collides with a mount every Runtime already has (%s)", p, strings.Join(reserved[1:], ", "))
	}
	secrets := reserved[len(reserved)-1]
	if strings.HasPrefix(p, secrets+"/") {
		return fmt.Errorf("%q must not be inside the read-only credential mount %s", p, secrets)
	}
	return nil
}

// sharedVolumeSpec is one --shared-volumes entry as given. Pointers and
// nil slices mark fields left to their defaults.
type sharedVolumeSpec struct {
	Name               string                 `json:"name"`
	ClaimName          string                 `json:"claimName"`
	SubPath            string                 `json:"subPath"`
	MountPath          string                 `json:"mountPath"`
	ReadOnly           *bool                  `json:"readOnly"`
	Modes              []v1alpha1.RuntimeMode `json:"modes"`
	SupplementalGroups []int64                `json:"supplementalGroups"`
}

// maxSharedVolumeNameLength keeps "shared-<name>" within a Pod volume
// name's DNS-label limit of 63 characters.
const maxSharedVolumeNameLength = 63 - len("shared-")

// resolveSharedVolumes parses, defaults and validates --shared-volumes. A
// share defaults to read-only, edit and run Runtimes, and /mnt/<name>.
// Deploy Runtimes are public applications, so a share may reach them only
// read-only. Mount paths may not collide with a Runtime's own mounts, the
// Workspace directory, or each other.
func resolveSharedVolumes(raw string, workspace session.WorkspaceStorage) ([]session.SharedVolume, []error) {
	if strings.TrimSpace(raw) == "" {
		return nil, nil
	}
	decoder := json.NewDecoder(strings.NewReader(raw))
	decoder.DisallowUnknownFields()
	var specs []sharedVolumeSpec
	if err := decoder.Decode(&specs); err != nil {
		return nil, []error{fmt.Errorf("--shared-volumes: invalid JSON: %w", err)}
	}

	type claimedPath struct{ path, owner string }
	var errs []error
	var claimed []claimedPath
	if workspace.Enabled() {
		claimed = append(claimed, claimedPath{workspace.MountPath, "the Workspace directory"})
	}
	seen := map[string]bool{}
	volumes := make([]session.SharedVolume, 0, len(specs))
	for i, spec := range specs {
		label := fmt.Sprintf("--shared-volumes[%d]", i)
		if spec.Name != "" {
			label = fmt.Sprintf("--shared-volumes %q", spec.Name)
		}
		volume, specErrs := resolveSharedVolume(spec, label)
		errs = append(errs, specErrs...)
		if seen[volume.Name] {
			errs = append(errs, fmt.Errorf("%s: name is used more than once", label))
		}
		seen[volume.Name] = true
		if volume.MountPath != "" {
			for _, other := range claimed {
				if pathsOverlap(volume.MountPath, other.path) {
					errs = append(errs, fmt.Errorf("%s: mountPath %q overlaps %s at %q",
						label, volume.MountPath, other.owner, other.path))
				}
			}
			claimed = append(claimed, claimedPath{volume.MountPath, label})
		}
		volumes = append(volumes, volume)
	}
	return volumes, errs
}

func resolveSharedVolume(spec sharedVolumeSpec, label string) (session.SharedVolume, []error) {
	var errs []error
	volume := session.SharedVolume{
		Name:               spec.Name,
		ClaimName:          spec.ClaimName,
		SubPath:            spec.SubPath,
		MountPath:          spec.MountPath,
		ReadOnly:           spec.ReadOnly == nil || *spec.ReadOnly,
		Modes:              spec.Modes,
		SupplementalGroups: spec.SupplementalGroups,
	}

	if msgs := validation.IsDNS1123Label(spec.Name); len(msgs) > 0 || len(spec.Name) > maxSharedVolumeNameLength {
		errs = append(errs, fmt.Errorf("%s: name must be a DNS label of at most %d characters", label, maxSharedVolumeNameLength))
	}
	for _, msg := range validation.IsDNS1123Subdomain(spec.ClaimName) {
		errs = append(errs, fmt.Errorf("%s: claimName %q: %s", label, spec.ClaimName, msg))
	}
	if spec.SubPath != "" && (path.IsAbs(spec.SubPath) || path.Clean(spec.SubPath) != spec.SubPath ||
		spec.SubPath == ".." || strings.HasPrefix(spec.SubPath, "../")) {
		errs = append(errs, fmt.Errorf("%s: subPath %q must be a clean relative path inside the claim", label, spec.SubPath))
	}
	if volume.MountPath == "" && spec.Name != "" {
		volume.MountPath = "/mnt/" + spec.Name
	}
	if err := validateRuntimeMountPath(volume.MountPath); err != nil {
		errs = append(errs, fmt.Errorf("%s: mountPath: %w", label, err))
	}
	if volume.Modes == nil {
		volume.Modes = []v1alpha1.RuntimeMode{v1alpha1.RuntimeModeEdit, v1alpha1.RuntimeModeRun}
	}
	errs = append(errs, validateSharedVolumeModes(volume, label)...)
	for _, gid := range spec.SupplementalGroups {
		if gid < 1 || gid > maxGroupID {
			errs = append(errs, fmt.Errorf("%s: supplementalGroups: %d is not a group ID between 1 and %d", label, gid, maxGroupID))
		}
	}
	return volume, errs
}

func validateSharedVolumeModes(volume session.SharedVolume, label string) []error {
	var errs []error
	if len(volume.Modes) == 0 {
		errs = append(errs, fmt.Errorf("%s: modes must name at least one of edit, run, deploy", label))
	}
	seen := map[v1alpha1.RuntimeMode]bool{}
	for _, mode := range volume.Modes {
		switch mode {
		case v1alpha1.RuntimeModeEdit, v1alpha1.RuntimeModeRun:
		case v1alpha1.RuntimeModeDeploy:
			if !volume.ReadOnly {
				errs = append(errs, fmt.Errorf("%s: deploy Runtimes are public, so only a read-only volume may list mode deploy", label))
			}
		default:
			errs = append(errs, fmt.Errorf("%s: unknown mode %q", label, mode))
		}
		if seen[mode] {
			errs = append(errs, fmt.Errorf("%s: mode %q is listed more than once", label, mode))
		}
		seen[mode] = true
	}
	return errs
}

// pathsOverlap reports whether one path equals the other or lies under it;
// two mounts that overlap would shadow one another.
func pathsOverlap(a, b string) bool {
	return a == b || strings.HasPrefix(a, b+"/") || strings.HasPrefix(b, a+"/")
}

// parseSupplementalGroups accepts a comma-separated list of positive group
// IDs. Group 0 is refused: Runtime images already run with it as their
// primary group, so naming it here could only ever be a mistake.
func parseSupplementalGroups(raw string) ([]int64, error) {
	var groups []int64
	for field := range strings.SplitSeq(raw, ",") {
		field = strings.TrimSpace(field)
		if field == "" {
			continue
		}
		gid, err := strconv.ParseInt(field, 10, 64)
		if err != nil || gid < 1 || gid > maxGroupID {
			return nil, fmt.Errorf("%q is not a group ID between 1 and %d", field, maxGroupID)
		}
		groups = append(groups, gid)
	}
	return groups, nil
}

func validateInternalAPIURL(raw string) error {
	u, err := url.Parse(raw)
	if err != nil {
		return fmt.Errorf("not a valid URL: %w", err)
	}
	if u.Scheme != "https" && u.Scheme != "http" {
		return fmt.Errorf("scheme must be http or https, got %q", u.Scheme)
	}
	if u.Host == "" {
		return errors.New("must include a host")
	}
	return nil
}

// parseResourceRequirements backs both --default-resources and
// --fetcher-resources: the two flags accept the identical JSON shape and
// enforce the identical "every dimension the chart documents must be
// explicit" rule, so there is exactly one parser to keep in sync rather than
// two copies that could silently diverge.
func parseResourceRequirements(raw string) (corev1.ResourceRequirements, error) {
	var resources corev1.ResourceRequirements
	if err := json.Unmarshal([]byte(raw), &resources); err != nil {
		return corev1.ResourceRequirements{}, fmt.Errorf("invalid JSON: %w", err)
	}
	for _, key := range requiredResourceKeys {
		if _, ok := resources.Requests[key]; !ok {
			return corev1.ResourceRequirements{}, fmt.Errorf("requests.%s is required", key)
		}
		if _, ok := resources.Limits[key]; !ok {
			return corev1.ResourceRequirements{}, fmt.Errorf("limits.%s is required", key)
		}
	}
	return resources, nil
}

// parseImagePullSecrets splits a comma-separated flag value into
// LocalObjectReferences, trimming whitespace and dropping empty entries so
// both "" and a trailing comma resolve to no secrets rather than one named "".
func parseImagePullSecrets(raw string) []corev1.LocalObjectReference {
	var refs []corev1.LocalObjectReference
	for name := range strings.SplitSeq(raw, ",") {
		name = strings.TrimSpace(name)
		if name == "" {
			continue
		}
		refs = append(refs, corev1.LocalObjectReference{Name: name})
	}
	return refs
}
