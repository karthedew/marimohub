package session

import (
	corev1 "k8s.io/api/core/v1"
	"k8s.io/apimachinery/pkg/api/resource"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/util/intstr"
	"k8s.io/utils/ptr"

	"github.com/karthedew/marimohub/marimohub-operator/api/v1alpha1"
	"github.com/karthedew/marimohub/marimohub-operator/internal/runtimecontract"
)

const (
	fetcherContainerName = "source-fetcher"
	runtimeContainerName = "marimo"

	workVolumeName    = "work"
	tmpVolumeName     = "tmp"
	homeVolumeName    = "home"
	cacheVolumeName   = "cache"
	secretsVolumeName = "marimohub-secrets"

	workMountPath    = "/work"
	tmpMountPath     = "/tmp"
	homeMountPath    = "/home/marimo"
	cacheMountPath   = "/cache"
	secretsMountPath = "/var/run/secrets/marimohub"

	credentialFileName = "credential"
	caCertFileName     = "ca.crt"

	notebookSourcePath = workMountPath + "/notebook.py"

	// marimoSessionKind is the CR's Kind, used both for the owner reference
	// this package sets on every child object and by this package's own
	// tests, which assert against the same constant rather than a second
	// hand-typed copy of the string.
	marimoSessionKind = "MarimoSession"

	// modeCommandEdit and modeCommandRun are the marimo subcommands
	// modeCommand maps a Runtime mode to; named constants because tests
	// assert against the same values modeCommand returns.
	modeCommandEdit = "edit"
	modeCommandRun  = "run"

	// terminationGracePeriodSeconds gives marimo's kernel and any in-flight
	// cell execution a bounded window to shut down cleanly. It is
	// deliberately short: restartPolicy is Never, so nothing about this
	// value can trigger a restart loop, and the reconciler (not this
	// package) owns deciding whether a terminated Pod is ever replaced.
	terminationGracePeriodSeconds = int64(30)
)

// Chart-wide emptyDir bounds. These are not Options fields because there is
// no chart yet to source per-deployment overrides from (workVolumeSizeLimit
// below anticipates the chart value of the same name); when the chart
// exists, promoting them to Options is a mechanical change that does not
// touch the security or mount-topology decisions made here.
var (
	workVolumeSizeLimit  = resource.MustParse("512Mi")
	tmpVolumeSizeLimit   = resource.MustParse("64Mi")
	homeVolumeSizeLimit  = resource.MustParse("32Mi")
	cacheVolumeSizeLimit = resource.MustParse("256Mi")
)

// modeCommand maps a Runtime mode to the marimo subcommand that serves it.
// Deploy mode reuses "run": both serve a read-only application from a fixed
// source file, differing only in idle semantics (sleep vs. delete) and
// source-revision binding, which are reconciler and internal-API concerns,
// not a different marimo invocation.
func modeCommand(mode v1alpha1.RuntimeMode) string {
	if mode == v1alpha1.RuntimeModeEdit {
		return modeCommandEdit
	}
	return modeCommandRun
}

// readinessProbeScript is executed with `python3 -c` inside the marimo
// container. It uses only the standard library's urllib -- not curl or
// wget, which these images do not carry -- because the container already
// has the one thing a probe needs (Python), and adding a second HTTP client
// just to check liveness would contradict the "no extra packages" intent of
// the Runtime images.
//
// It authenticates with the same bearer token marimo itself accepts and
// requests GET /api/version under the mode-appropriate base path. That
// endpoint, not the root asset route, is the probe target: marimo's root
// route replies to a bad or missing token with a 303 redirect to /login
// rather than an error, and /login itself always returns 200, so a client
// that follows redirects (as urlopen does by default) cannot tell a correct
// token from a wrong one there. /api/version is declared
// `@requires("read")`, the scope every authenticated user gets in both edit
// and run mode (deploy reuses the run command), and marimo's auth layer
// rejects a failed check with a direct 4xx rather than a redirect -- so it
// discriminates "ready and correctly credentialed" from everything else,
// including no credential at all, across every mode this builder produces.
//
// The custom redirect handler is defense in depth on top of that: it makes
// any 3xx response fail the probe outright instead of silently following
// it, so a future marimo change that turned an auth failure back into a
// redirect could not resurrect the exact bug this script was built to
// avoid.
const readinessProbeScript = `import os, sys, urllib.request

class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None

base = os.environ.get("BASE_URL", "").rstrip("/")
url = "http://127.0.0.1:8080" + base + "/api/version"
req = urllib.request.Request(url, headers={"Authorization": "Bearer " + os.environ["MARIMO_TOKEN"]})
opener = urllib.request.build_opener(NoRedirect)
try:
    with opener.open(req, timeout=3) as resp:
        sys.exit(0 if resp.status == 200 else 1)
except Exception:
    sys.exit(1)
`

// BuildPod returns the desired Runtime Pod for cr. It is a pure function:
// calling it twice with the same arguments returns equal objects, and it
// never contacts the API server.
func BuildPod(cr *v1alpha1.MarimoSession, opts Options) *corev1.Pod {
	args := []string{
		modeCommand(cr.Spec.Mode),
		notebookSourcePath,
		"--host=0.0.0.0",
		"--port=8080",
		"--token-password=$(MARIMO_TOKEN)",
		"--base-url=$(BASE_URL)",
		"--headless",
	}
	if cr.Spec.Mode == v1alpha1.RuntimeModeEdit {
		args = append(args, "--skip-update-check")
	}

	return &corev1.Pod{
		ObjectMeta: metav1.ObjectMeta{
			Name:            runtimecontract.ChildName(cr.Name),
			Namespace:       cr.Namespace,
			Labels:          childLabels(cr),
			OwnerReferences: []metav1.OwnerReference{ownerReference(cr)},
		},
		Spec: corev1.PodSpec{
			RestartPolicy:                 corev1.RestartPolicyNever,
			AutomountServiceAccountToken:  ptr.To(false),
			EnableServiceLinks:            ptr.To(false),
			ServiceAccountName:            opts.ServiceAccountName,
			ImagePullSecrets:              opts.ImagePullSecrets,
			TerminationGracePeriodSeconds: ptr.To(terminationGracePeriodSeconds),
			SecurityContext: &corev1.PodSecurityContext{
				RunAsNonRoot:   ptr.To(true),
				SeccompProfile: &corev1.SeccompProfile{Type: corev1.SeccompProfileTypeRuntimeDefault},
			},
			Volumes:        buildVolumes(cr, opts),
			InitContainers: []corev1.Container{buildFetcherContainer(cr, opts)},
			Containers:     []corev1.Container{buildRuntimeContainer(cr, opts, args)},
		},
	}
}

// childLabels are applied to every child Pod/Service. LabelSession lets the
// operator and the Service selector find a Runtime's children without
// depending on the fixed naming scheme; the other three exactly mirror the
// CR's own identity labels, which the platform's admission policy already
// requires to agree with spec.notebookId/workspaceId/mode.
func childLabels(cr *v1alpha1.MarimoSession) map[string]string {
	return map[string]string{
		runtimecontract.LabelNotebook:  cr.Spec.NotebookID,
		runtimecontract.LabelWorkspace: cr.Spec.WorkspaceID,
		runtimecontract.LabelMode:      string(cr.Spec.Mode),
		runtimecontract.LabelSession:   cr.Name,
	}
}

// ownerReference builds a controller owner reference by hand rather than
// through controllerutil.SetControllerReference: that helper needs a scheme
// to look up the CR's GroupVersionKind and returns an error, both of which
// would make this package's builders stop being pure, argument-only
// functions for no benefit -- the GVK of a MarimoSession is fixed and known
// at compile time.
func ownerReference(cr *v1alpha1.MarimoSession) metav1.OwnerReference {
	return metav1.OwnerReference{
		APIVersion:         v1alpha1.GroupVersion.String(),
		Kind:               marimoSessionKind,
		Name:               cr.Name,
		UID:                cr.UID,
		Controller:         ptr.To(true),
		BlockOwnerDeletion: ptr.To(true),
	}
}

// buildVolumes returns the Pod's four bounded scratch volumes plus one
// projected Secret volume. The projected volume merges two different
// Secrets (the per-Runtime credential and the namespace-wide internal API
// CA bundle) into one mount, which is what lets both files land as direct
// siblings at /var/run/secrets/marimohub -- exactly the layout the fetcher
// script's default paths assume -- without either Secret needing to know
// about the other.
func buildVolumes(cr *v1alpha1.MarimoSession, opts Options) []corev1.Volume {
	return []corev1.Volume{
		emptyDirVolume(workVolumeName, workVolumeSizeLimit),
		emptyDirVolume(tmpVolumeName, tmpVolumeSizeLimit),
		emptyDirVolume(homeVolumeName, homeVolumeSizeLimit),
		emptyDirVolume(cacheVolumeName, cacheVolumeSizeLimit),
		{
			Name: secretsVolumeName,
			VolumeSource: corev1.VolumeSource{
				Projected: &corev1.ProjectedVolumeSource{
					DefaultMode: ptr.To(int32(0o440)),
					Sources: []corev1.VolumeProjection{
						{
							Secret: &corev1.SecretProjection{
								LocalObjectReference: corev1.LocalObjectReference{Name: runtimecontract.SecretName(cr.Name)},
								Items: []corev1.KeyToPath{
									{Key: runtimecontract.SecretKeyRuntimeCredential, Path: credentialFileName},
								},
							},
						},
						{
							Secret: &corev1.SecretProjection{
								LocalObjectReference: corev1.LocalObjectReference{Name: opts.InternalAPICA.SecretName},
								Items: []corev1.KeyToPath{
									{Key: opts.InternalAPICA.SecretKey, Path: caCertFileName},
								},
							},
						},
					},
				},
			},
		},
	}
}

func emptyDirVolume(name string, sizeLimit resource.Quantity) corev1.Volume {
	return corev1.Volume{
		Name: name,
		VolumeSource: corev1.VolumeSource{
			EmptyDir: &corev1.EmptyDirVolumeSource{SizeLimit: ptr.To(sizeLimit)},
		},
	}
}

// commonVolumeMounts is shared by the fetcher and marimo containers: both
// need the same writable scratch paths and the same read-only credential/CA
// projection, since prebuilt Notebook code running in the marimo container
// is documented to call the internal API using the same credential the
// fetcher used to retrieve the source in the first place.
func commonVolumeMounts() []corev1.VolumeMount {
	return []corev1.VolumeMount{
		{Name: workVolumeName, MountPath: workMountPath},
		{Name: tmpVolumeName, MountPath: tmpMountPath},
		{Name: homeVolumeName, MountPath: homeMountPath},
		{Name: cacheVolumeName, MountPath: cacheMountPath},
		{Name: secretsVolumeName, MountPath: secretsMountPath, ReadOnly: true},
	}
}

// commonEnv is shared by the fetcher and marimo containers.
func commonEnv(cr *v1alpha1.MarimoSession, opts Options) []corev1.EnvVar {
	return []corev1.EnvVar{
		{Name: "HOME", Value: homeMountPath},
		{Name: "XDG_CACHE_HOME", Value: cacheMountPath},
		{Name: "INTERNAL_API_URL", Value: opts.InternalAPIURL},
		{Name: "RUNTIME_ID", Value: cr.Name},
	}
}

func buildFetcherContainer(cr *v1alpha1.MarimoSession, opts Options) corev1.Container {
	env := append(commonEnv(cr, opts),
		corev1.EnvVar{Name: "OUTPUT_FILE", Value: notebookSourcePath},
		corev1.EnvVar{Name: "RUNTIME_CREDENTIAL_FILE", Value: secretsMountPath + "/" + credentialFileName},
		corev1.EnvVar{Name: "CA_CERT_FILE", Value: secretsMountPath + "/" + caCertFileName},
	)
	return corev1.Container{
		Name:            fetcherContainerName,
		Image:           opts.FetcherImage,
		ImagePullPolicy: opts.ImagePullPolicy,
		Env:             env,
		VolumeMounts:    commonVolumeMounts(),
		Resources:       opts.FetcherResources,
		SecurityContext: restrictedContainerSecurityContext(),
	}
}

func buildRuntimeContainer(cr *v1alpha1.MarimoSession, opts Options, args []string) corev1.Container {
	resources := opts.Resources
	if cr.Spec.Resources != nil {
		resources = *cr.Spec.Resources
	}

	env := append(commonEnv(cr, opts),
		corev1.EnvVar{Name: "BASE_URL", Value: cr.Spec.BaseURL},
		corev1.EnvVar{
			Name: "MARIMO_TOKEN",
			ValueFrom: &corev1.EnvVarSource{
				SecretKeyRef: &corev1.SecretKeySelector{
					LocalObjectReference: corev1.LocalObjectReference{Name: runtimecontract.SecretName(cr.Name)},
					Key:                  runtimecontract.SecretKeyMarimoToken,
				},
			},
		},
	)

	return corev1.Container{
		Name:            runtimeContainerName,
		Image:           cr.Spec.Image,
		ImagePullPolicy: opts.ImagePullPolicy,
		Command:         []string{"marimo"},
		Args:            args,
		Env:             env,
		Ports: []corev1.ContainerPort{
			{Name: "http", ContainerPort: runtimecontract.RuntimePort, Protocol: corev1.ProtocolTCP},
		},
		VolumeMounts:    commonVolumeMounts(),
		Resources:       resources,
		SecurityContext: restrictedContainerSecurityContext(),
		StartupProbe:    tcpStartupProbe(),
		ReadinessProbe:  authenticatedReadinessProbe(),
	}
}

// restrictedContainerSecurityContext matches OpenShift's restricted-v2 SCC:
// no privilege escalation, every Linux capability dropped, RuntimeDefault
// seccomp (set at the Pod level; repeating it here would be redundant), and
// non-root. RunAsUser is deliberately never set, at either the Pod or
// container level: leaving it unset is what lets the same Pod spec run
// under an image's default UID on vanilla Kubernetes and under an arbitrary
// OpenShift-assigned UID without this package encoding either assumption.
func restrictedContainerSecurityContext() *corev1.SecurityContext {
	return &corev1.SecurityContext{
		AllowPrivilegeEscalation: ptr.To(false),
		ReadOnlyRootFilesystem:   ptr.To(true),
		RunAsNonRoot:             ptr.To(true),
		Privileged:               ptr.To(false),
		Capabilities:             &corev1.Capabilities{Drop: []corev1.Capability{"ALL"}},
	}
}

// tcpStartupProbe gates the readiness probe behind a plain TCP connect so a
// slow image pull or interpreter start is never misreported as a failed
// authenticated check. The failure threshold allows roughly five minutes
// (60 attempts x 5s) before Kubernetes gives up on startup, comfortably
// inside the image-pull deadline the reconciler enforces on top of it.
func tcpStartupProbe() *corev1.Probe {
	return &corev1.Probe{
		ProbeHandler: corev1.ProbeHandler{
			TCPSocket: &corev1.TCPSocketAction{Port: intstr.FromInt32(runtimecontract.RuntimePort)},
		},
		PeriodSeconds:    5,
		FailureThreshold: 60,
	}
}

// authenticatedReadinessProbe confirms marimo is not just accepting TCP
// connections but actually serving authenticated requests with the token
// this exact Pod was issued. See readinessProbeScript's doc comment for why
// the check targets /api/version and never follows a redirect.
func authenticatedReadinessProbe() *corev1.Probe {
	return &corev1.Probe{
		ProbeHandler: corev1.ProbeHandler{
			Exec: &corev1.ExecAction{Command: []string{"python3", "-c", readinessProbeScript}},
		},
		PeriodSeconds:    10,
		TimeoutSeconds:   5,
		FailureThreshold: 3,
	}
}
