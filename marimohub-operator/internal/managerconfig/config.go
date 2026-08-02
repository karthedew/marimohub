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
	"regexp"
	"strings"

	corev1 "k8s.io/api/core/v1"

	"github.com/karthedew/marimohub/marimohub-operator/api/v1alpha1"
	"github.com/karthedew/marimohub/marimohub-operator/internal/session"
)

// MinIdleTimeoutSeconds mirrors the CRD's spec.idleTimeoutSeconds floor. The
// operator's own per-mode defaults must satisfy the same floor it enforces
// on the CR override, or the two would disagree about the shortest Runtime
// lifetime the platform allows.
const MinIdleTimeoutSeconds = 30

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

	if len(errs) > 0 {
		return Config{}, errors.Join(errs...)
	}
	return cfg, nil
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
