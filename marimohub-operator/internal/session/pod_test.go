package session

import (
	"reflect"
	"strings"
	"testing"

	corev1 "k8s.io/api/core/v1"
	"k8s.io/apimachinery/pkg/api/resource"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/types"

	"github.com/karthedew/marimohub/marimohub-operator/api/v1alpha1"
	"github.com/karthedew/marimohub/marimohub-operator/internal/runtimecontract"
)

func digestImage(repo string) string {
	return repo + "@sha256:" + strings.Repeat("0123456789abcdef", 4)
}

var (
	ubuntuImage = digestImage("ghcr.io/karthedew/marimohub-runtime-ubuntu")
	ubiImage    = digestImage("ghcr.io/karthedew/marimohub-runtime-ubi")
	fetcherImg  = digestImage("ghcr.io/karthedew/marimohub-source-fetcher")
)

const (
	testName        = "11111111-1111-1111-1111-111111111111"
	testNotebookID  = "22222222-2222-2222-2222-222222222222"
	testWorkspaceID = "33333333-3333-3333-3333-333333333333"
	testUID         = types.UID("44444444-4444-4444-4444-444444444444")
)

func testOptions() Options {
	return Options{
		FetcherImage:   fetcherImg,
		InternalAPIURL: "https://internal-api.marimohub.svc:8443",
		InternalAPICA:  CABundle{SecretName: "marimohub-internal-ca", SecretKey: "ca.crt"},
		IdleTimeout:    IdleTimeoutDefaults{EditSeconds: 1800, RunSeconds: 600, DeploySeconds: 300},
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
		ImagePullPolicy:    corev1.PullIfNotPresent,
		ServiceAccountName: "marimohub-runtime",
		ImagePullSecrets:   []corev1.LocalObjectReference{{Name: "registry-creds"}},
	}
}

func testSession(mode v1alpha1.RuntimeMode, image string) *v1alpha1.MarimoSession {
	cr := &v1alpha1.MarimoSession{
		ObjectMeta: metav1.ObjectMeta{
			Name:      testName,
			Namespace: "marimohub-sessions",
			UID:       testUID,
			Labels: map[string]string{
				runtimecontract.LabelNotebook:  testNotebookID,
				runtimecontract.LabelWorkspace: testWorkspaceID,
				runtimecontract.LabelMode:      string(mode),
			},
		},
		Spec: v1alpha1.MarimoSessionSpec{
			NotebookID:  testNotebookID,
			WorkspaceID: testWorkspaceID,
			Mode:        mode,
			Image:       image,
			BaseURL:     "/api/proxy/" + testName,
		},
	}
	if mode == v1alpha1.RuntimeModeDeploy {
		revision := int64(3)
		cr.Spec.DeploymentRevision = &revision
		cr.Spec.BaseURL = "/api/deployments/" + testName
	}
	return cr
}

func envValue(env []corev1.EnvVar, name string) (corev1.EnvVar, bool) {
	for _, e := range env {
		if e.Name == name {
			return e, true
		}
	}
	return corev1.EnvVar{}, false
}

func wantArgsFor(mode v1alpha1.RuntimeMode) []string {
	args := []string{
		modeCommand(mode),
		"/work/notebook.py",
		"--host=0.0.0.0",
		"--port=8080",
		"--token-password=$(MARIMO_TOKEN)",
		"--base-url=$(BASE_URL)",
		"--headless",
	}
	if mode == v1alpha1.RuntimeModeEdit {
		args = append(args, "--skip-update-check")
	}
	return args
}

// TestBuildPod pins the Pod contract across both Runtime image flavors and
// every mode: table-driven so a regression in one combination cannot hide
// behind a passing assertion for another.
func TestBuildPod(t *testing.T) {
	opts := testOptions()
	images := map[string]string{"ubuntu": ubuntuImage, "ubi": ubiImage}
	modes := []v1alpha1.RuntimeMode{v1alpha1.RuntimeModeEdit, v1alpha1.RuntimeModeRun, v1alpha1.RuntimeModeDeploy}

	for flavor, image := range images {
		for _, mode := range modes {
			t.Run(flavor+"_"+string(mode), func(t *testing.T) {
				cr := testSession(mode, image)
				pod := BuildPod(cr, opts)

				t.Run("identity", func(t *testing.T) { checkPodIdentity(t, cr, mode, pod) })
				t.Run("owner_reference", func(t *testing.T) { checkOwnerReference(t, cr, pod.OwnerReferences) })
				t.Run("pod_security", func(t *testing.T) { checkPodSecurity(t, opts, pod) })
				t.Run("volumes", func(t *testing.T) { checkVolumes(t, cr, opts, pod.Spec.Volumes) })
				t.Run("init_container", func(t *testing.T) { checkInitContainer(t, cr, opts, pod) })
				t.Run("runtime_container", func(t *testing.T) { checkRuntimeContainer(t, cr, opts, mode, pod) })
			})
		}
	}
}

func checkPodIdentity(t *testing.T, cr *v1alpha1.MarimoSession, mode v1alpha1.RuntimeMode, pod *corev1.Pod) {
	t.Helper()
	if got, want := pod.Name, runtimecontract.ChildName(cr.Name); got != want {
		t.Errorf("Name = %q, want %q", got, want)
	}
	if pod.Namespace != cr.Namespace {
		t.Errorf("Namespace = %q, want %q", pod.Namespace, cr.Namespace)
	}
	wantLabels := map[string]string{
		runtimecontract.LabelNotebook:  testNotebookID,
		runtimecontract.LabelWorkspace: testWorkspaceID,
		runtimecontract.LabelMode:      string(mode),
		runtimecontract.LabelSession:   testName,
	}
	if !reflect.DeepEqual(pod.Labels, wantLabels) {
		t.Errorf("Labels = %v, want %v", pod.Labels, wantLabels)
	}
}

// checkOwnerReference is shared by the Pod and Service tests: both child
// objects carry an identical controller owner reference back to the CR.
func checkOwnerReference(t *testing.T, cr *v1alpha1.MarimoSession, refs []metav1.OwnerReference) {
	t.Helper()
	if len(refs) != 1 {
		t.Fatalf("OwnerReferences = %d entries, want 1", len(refs))
	}
	owner := refs[0]
	if owner.APIVersion != v1alpha1.GroupVersion.String() {
		t.Errorf("owner.APIVersion = %q, want %q", owner.APIVersion, v1alpha1.GroupVersion.String())
	}
	if owner.Kind != marimoSessionKind {
		t.Errorf("owner.Kind = %q, want %q", owner.Kind, marimoSessionKind)
	}
	if owner.Name != cr.Name {
		t.Errorf("owner.Name = %q, want %q", owner.Name, cr.Name)
	}
	if owner.UID != testUID {
		t.Errorf("owner.UID = %q, want %q", owner.UID, testUID)
	}
	if owner.Controller == nil || !*owner.Controller {
		t.Error("owner.Controller must be true")
	}
	if owner.BlockOwnerDeletion == nil || !*owner.BlockOwnerDeletion {
		t.Error("owner.BlockOwnerDeletion must be true")
	}
}

func checkPodSecurity(t *testing.T, opts Options, pod *corev1.Pod) {
	t.Helper()
	if pod.Spec.RestartPolicy != corev1.RestartPolicyNever {
		t.Errorf("RestartPolicy = %q, want Never", pod.Spec.RestartPolicy)
	}
	if pod.Spec.AutomountServiceAccountToken == nil || *pod.Spec.AutomountServiceAccountToken {
		t.Error("AutomountServiceAccountToken must be false")
	}
	if pod.Spec.EnableServiceLinks == nil || *pod.Spec.EnableServiceLinks {
		t.Error("EnableServiceLinks must be false")
	}
	if pod.Spec.ShareProcessNamespace == nil || !*pod.Spec.ShareProcessNamespace {
		t.Error("ShareProcessNamespace must be true so marimo is never PID 1 and exits promptly on SIGTERM")
	}
	if pod.Spec.ServiceAccountName != opts.ServiceAccountName {
		t.Errorf("ServiceAccountName = %q, want %q", pod.Spec.ServiceAccountName, opts.ServiceAccountName)
	}
	if !reflect.DeepEqual(pod.Spec.ImagePullSecrets, opts.ImagePullSecrets) {
		t.Errorf("ImagePullSecrets = %v, want %v", pod.Spec.ImagePullSecrets, opts.ImagePullSecrets)
	}
	if pod.Spec.TerminationGracePeriodSeconds == nil || *pod.Spec.TerminationGracePeriodSeconds != terminationGracePeriodSeconds {
		t.Errorf("TerminationGracePeriodSeconds = %v, want %d", pod.Spec.TerminationGracePeriodSeconds, terminationGracePeriodSeconds)
	}
	sc := pod.Spec.SecurityContext
	if sc == nil {
		t.Fatal("Pod SecurityContext is nil")
	}
	if sc.RunAsNonRoot == nil || !*sc.RunAsNonRoot {
		t.Error("Pod SecurityContext.RunAsNonRoot must be true")
	}
	if sc.RunAsUser != nil {
		t.Errorf("Pod SecurityContext.RunAsUser must be unset (no fixed-UID assumption), got %v", *sc.RunAsUser)
	}
	if sc.SeccompProfile == nil || sc.SeccompProfile.Type != corev1.SeccompProfileTypeRuntimeDefault {
		t.Errorf("Pod SeccompProfile = %v, want RuntimeDefault", sc.SeccompProfile)
	}
}

func checkInitContainer(t *testing.T, cr *v1alpha1.MarimoSession, opts Options, pod *corev1.Pod) {
	t.Helper()
	if len(pod.Spec.InitContainers) != 1 {
		t.Fatalf("InitContainers = %d, want 1", len(pod.Spec.InitContainers))
	}
	fetcher := pod.Spec.InitContainers[0]
	if fetcher.Name != fetcherContainerName {
		t.Errorf("init container Name = %q, want %q", fetcher.Name, fetcherContainerName)
	}
	if fetcher.Image != opts.FetcherImage {
		t.Errorf("init container Image = %q, want %q", fetcher.Image, opts.FetcherImage)
	}
	if fetcher.ImagePullPolicy != opts.ImagePullPolicy {
		t.Errorf("init container ImagePullPolicy = %q, want %q", fetcher.ImagePullPolicy, opts.ImagePullPolicy)
	}
	if !reflect.DeepEqual(fetcher.Resources, opts.FetcherResources) {
		t.Errorf("init container Resources = %v, want %v", fetcher.Resources, opts.FetcherResources)
	}
	checkRestrictedSecurityContext(t, "init container", fetcher.SecurityContext)
	checkCommonVolumeMounts(t, "init container", fetcher.VolumeMounts)

	wantEnv := map[string]string{
		"HOME":                    homeMountPath,
		"XDG_CACHE_HOME":          cacheMountPath,
		"INTERNAL_API_URL":        opts.InternalAPIURL,
		"RUNTIME_ID":              cr.Name,
		"OUTPUT_FILE":             notebookSourcePath,
		"RUNTIME_CREDENTIAL_FILE": secretsMountPath + "/" + credentialFileName,
		"CA_CERT_FILE":            secretsMountPath + "/" + caCertFileName,
	}
	for name, want := range wantEnv {
		got, ok := envValue(fetcher.Env, name)
		if !ok {
			t.Errorf("init container missing env %s", name)
			continue
		}
		if got.Value != want {
			t.Errorf("init container env %s = %q, want %q", name, got.Value, want)
		}
	}
}

func checkRuntimeContainer(t *testing.T, cr *v1alpha1.MarimoSession, opts Options, mode v1alpha1.RuntimeMode, pod *corev1.Pod) {
	t.Helper()
	if len(pod.Spec.Containers) != 1 {
		t.Fatalf("Containers = %d, want 1", len(pod.Spec.Containers))
	}
	main := pod.Spec.Containers[0]
	if main.Name != runtimeContainerName {
		t.Errorf("runtime container Name = %q, want %q", main.Name, runtimeContainerName)
	}
	if main.Image != cr.Spec.Image {
		t.Errorf("runtime container Image = %q, want %q", main.Image, cr.Spec.Image)
	}
	if main.ImagePullPolicy != opts.ImagePullPolicy {
		t.Errorf("runtime container ImagePullPolicy = %q, want %q", main.ImagePullPolicy, opts.ImagePullPolicy)
	}
	if !reflect.DeepEqual(main.Command, []string{"marimo"}) {
		t.Errorf("Command = %v, want [marimo]", main.Command)
	}
	if want := wantArgsFor(mode); !reflect.DeepEqual(main.Args, want) {
		t.Errorf("Args = %v, want %v", main.Args, want)
	}
	if !reflect.DeepEqual(main.Resources, opts.Resources) {
		t.Errorf("Resources = %v, want %v", main.Resources, opts.Resources)
	}
	if len(main.Ports) != 1 || main.Ports[0].ContainerPort != runtimecontract.RuntimePort {
		t.Errorf("Ports = %v, want single port %d", main.Ports, runtimecontract.RuntimePort)
	}
	checkRestrictedSecurityContext(t, "runtime container", main.SecurityContext)
	checkCommonVolumeMounts(t, "runtime container", main.VolumeMounts)

	if baseURL, ok := envValue(main.Env, "BASE_URL"); !ok || baseURL.Value != cr.Spec.BaseURL {
		t.Errorf("BASE_URL env = %+v, want value %q", baseURL, cr.Spec.BaseURL)
	}
	token, ok := envValue(main.Env, "MARIMO_TOKEN")
	if !ok || token.ValueFrom == nil || token.ValueFrom.SecretKeyRef == nil {
		t.Fatalf("MARIMO_TOKEN env = %+v, want a SecretKeyRef", token)
	}
	if token.ValueFrom.SecretKeyRef.Name != runtimecontract.SecretName(cr.Name) {
		t.Errorf("MARIMO_TOKEN secret name = %q, want %q", token.ValueFrom.SecretKeyRef.Name, runtimecontract.SecretName(cr.Name))
	}
	if token.ValueFrom.SecretKeyRef.Key != runtimecontract.SecretKeyMarimoToken {
		t.Errorf("MARIMO_TOKEN secret key = %q, want %q", token.ValueFrom.SecretKeyRef.Key, runtimecontract.SecretKeyMarimoToken)
	}

	if main.StartupProbe == nil || main.StartupProbe.TCPSocket == nil {
		t.Fatal("StartupProbe must be a TCP socket probe")
	}
	if main.StartupProbe.TCPSocket.Port.IntValue() != runtimecontract.RuntimePort {
		t.Errorf("StartupProbe port = %d, want %d", main.StartupProbe.TCPSocket.Port.IntValue(), runtimecontract.RuntimePort)
	}
	if main.ReadinessProbe == nil || main.ReadinessProbe.Exec == nil {
		t.Fatal("ReadinessProbe must be an exec probe")
	}
	cmd := main.ReadinessProbe.Exec.Command
	if len(cmd) != 3 || cmd[0] != "python3" || cmd[1] != "-c" {
		t.Errorf("ReadinessProbe.Exec.Command = %v, want [python3 -c <script>]", cmd)
	}
}

func checkVolumes(t *testing.T, cr *v1alpha1.MarimoSession, opts Options, volumes []corev1.Volume) {
	t.Helper()
	byName := make(map[string]corev1.Volume, len(volumes))
	for _, v := range volumes {
		byName[v.Name] = v
	}

	emptyDirs := map[string]resource.Quantity{
		workVolumeName:  workVolumeSizeLimit,
		tmpVolumeName:   tmpVolumeSizeLimit,
		homeVolumeName:  homeVolumeSizeLimit,
		cacheVolumeName: cacheVolumeSizeLimit,
	}
	for name, wantLimit := range emptyDirs {
		v, ok := byName[name]
		if !ok {
			t.Errorf("missing volume %q", name)
			continue
		}
		if v.EmptyDir == nil {
			t.Errorf("volume %q is not an emptyDir", name)
			continue
		}
		if v.EmptyDir.SizeLimit == nil || v.EmptyDir.SizeLimit.Cmp(wantLimit) != 0 {
			t.Errorf("volume %q SizeLimit = %v, want %v", name, v.EmptyDir.SizeLimit, wantLimit)
		}
	}

	secrets, ok := byName[secretsVolumeName]
	if !ok {
		t.Fatalf("missing volume %q", secretsVolumeName)
	}
	if secrets.Projected == nil || len(secrets.Projected.Sources) != 2 {
		t.Fatalf("volume %q must project exactly 2 sources, got %+v", secretsVolumeName, secrets.Projected)
	}

	credentialSource := secrets.Projected.Sources[0].Secret
	if credentialSource == nil || credentialSource.Name != runtimecontract.SecretName(cr.Name) {
		t.Errorf("credential projection Secret = %v, want %q", credentialSource, runtimecontract.SecretName(cr.Name))
	}
	if credentialSource != nil && (len(credentialSource.Items) != 1 ||
		credentialSource.Items[0].Key != runtimecontract.SecretKeyRuntimeCredential ||
		credentialSource.Items[0].Path != credentialFileName) {
		t.Errorf("credential projection Items = %v, want key %q path %q",
			credentialSource.Items, runtimecontract.SecretKeyRuntimeCredential, credentialFileName)
	}

	caSource := secrets.Projected.Sources[1].Secret
	if caSource == nil || caSource.Name != opts.InternalAPICA.SecretName {
		t.Errorf("CA projection Secret = %v, want %q", caSource, opts.InternalAPICA.SecretName)
	}
	if caSource != nil && (len(caSource.Items) != 1 ||
		caSource.Items[0].Key != opts.InternalAPICA.SecretKey ||
		caSource.Items[0].Path != caCertFileName) {
		t.Errorf("CA projection Items = %v, want key %q path %q",
			caSource.Items, opts.InternalAPICA.SecretKey, caCertFileName)
	}
}

func checkRestrictedSecurityContext(t *testing.T, label string, sc *corev1.SecurityContext) {
	t.Helper()
	if sc == nil {
		t.Fatalf("%s: SecurityContext is nil", label)
	}
	if sc.AllowPrivilegeEscalation == nil || *sc.AllowPrivilegeEscalation {
		t.Errorf("%s: AllowPrivilegeEscalation must be false", label)
	}
	if sc.ReadOnlyRootFilesystem == nil || !*sc.ReadOnlyRootFilesystem {
		t.Errorf("%s: ReadOnlyRootFilesystem must be true", label)
	}
	if sc.RunAsNonRoot == nil || !*sc.RunAsNonRoot {
		t.Errorf("%s: RunAsNonRoot must be true", label)
	}
	if sc.Privileged == nil || *sc.Privileged {
		t.Errorf("%s: Privileged must be false", label)
	}
	if sc.RunAsUser != nil {
		t.Errorf("%s: RunAsUser must be unset (no fixed-UID assumption), got %v", label, *sc.RunAsUser)
	}
	if sc.Capabilities == nil || !reflect.DeepEqual(sc.Capabilities.Drop, []corev1.Capability{"ALL"}) {
		t.Errorf("%s: Capabilities.Drop = %v, want [ALL]", label, sc.Capabilities)
	}
}

func checkCommonVolumeMounts(t *testing.T, label string, mounts []corev1.VolumeMount) {
	t.Helper()
	wantPaths := map[string]string{
		workVolumeName:    workMountPath,
		tmpVolumeName:     tmpMountPath,
		homeVolumeName:    homeMountPath,
		cacheVolumeName:   cacheMountPath,
		secretsVolumeName: secretsMountPath,
	}
	byName := make(map[string]corev1.VolumeMount, len(mounts))
	for _, m := range mounts {
		byName[m.Name] = m
	}
	for name, wantPath := range wantPaths {
		m, ok := byName[name]
		if !ok {
			t.Errorf("%s: missing volume mount %q", label, name)
			continue
		}
		if m.MountPath != wantPath {
			t.Errorf("%s: mount %q path = %q, want %q", label, name, m.MountPath, wantPath)
		}
	}
	if secrets, ok := byName[secretsVolumeName]; ok && !secrets.ReadOnly {
		t.Errorf("%s: mount %q must be read-only", label, secretsVolumeName)
	}
}

func testWorkspaceStorage() WorkspaceStorage {
	return WorkspaceStorage{
		ClaimName:          "marimohub-workspaces",
		MountPath:          "/work/workspace",
		SupplementalGroups: []int64{1000},
	}
}

func volumeMountNamed(mounts []corev1.VolumeMount, name string) (corev1.VolumeMount, bool) {
	for _, m := range mounts {
		if m.Name == name {
			return m, true
		}
	}
	return corev1.VolumeMount{}, false
}

// TestBuildPodWorkspaceStorageDisabled pins that the zero-value
// WorkspaceStorage leaves the Pod exactly as it was before Workspace
// storage existed: no claim, no extra init container, no group change.
func TestBuildPodWorkspaceStorageDisabled(t *testing.T) {
	for _, mode := range []v1alpha1.RuntimeMode{v1alpha1.RuntimeModeEdit, v1alpha1.RuntimeModeRun, v1alpha1.RuntimeModeDeploy} {
		pod := BuildPod(testSession(mode, ubiImage), testOptions())
		for _, v := range pod.Spec.Volumes {
			if v.Name == workspaceVolumeName {
				t.Errorf("%s: unexpected %q volume with storage disabled", mode, workspaceVolumeName)
			}
		}
		if len(pod.Spec.InitContainers) != 1 {
			t.Errorf("%s: InitContainers = %d, want only the fetcher", mode, len(pod.Spec.InitContainers))
		}
		if pod.Spec.SecurityContext.SupplementalGroups != nil {
			t.Errorf("%s: SupplementalGroups = %v, want unset", mode, pod.Spec.SecurityContext.SupplementalGroups)
		}
	}
}

// TestBuildPodWorkspaceStorage pins each mode's access: edit read-write,
// run read-only, deploy not mounted at all.
func TestBuildPodWorkspaceStorage(t *testing.T) {
	cases := []struct {
		mode         v1alpha1.RuntimeMode
		wantMounted  bool
		wantReadOnly bool
	}{
		{v1alpha1.RuntimeModeEdit, true, false},
		{v1alpha1.RuntimeModeRun, true, true},
		{v1alpha1.RuntimeModeDeploy, false, false},
	}
	for _, c := range cases {
		t.Run(string(c.mode), func(t *testing.T) {
			opts := testOptions()
			opts.WorkspaceStorage = testWorkspaceStorage()
			cr := testSession(c.mode, ubiImage)
			pod := BuildPod(cr, opts)

			if !c.wantMounted {
				if len(pod.Spec.InitContainers) != 1 || pod.Spec.SecurityContext.SupplementalGroups != nil {
					t.Fatalf("deploy Pod must not carry any Workspace storage: init=%d groups=%v",
						len(pod.Spec.InitContainers), pod.Spec.SecurityContext.SupplementalGroups)
				}
				if _, ok := volumeMountNamed(pod.Spec.Containers[0].VolumeMounts, workspaceVolumeName); ok {
					t.Fatal("deploy Runtime must not mount the Workspace directory")
				}
				return
			}

			var claim *corev1.PersistentVolumeClaimVolumeSource
			for _, v := range pod.Spec.Volumes {
				if v.Name == workspaceVolumeName {
					claim = v.PersistentVolumeClaim
				}
			}
			if claim == nil || claim.ClaimName != opts.WorkspaceStorage.ClaimName || claim.ReadOnly {
				t.Errorf("workspace volume claim = %+v, want writable claim %q", claim, opts.WorkspaceStorage.ClaimName)
			}
			if !reflect.DeepEqual(pod.Spec.SecurityContext.SupplementalGroups, opts.WorkspaceStorage.SupplementalGroups) {
				t.Errorf("SupplementalGroups = %v, want %v", pod.Spec.SecurityContext.SupplementalGroups, opts.WorkspaceStorage.SupplementalGroups)
			}

			if len(pod.Spec.InitContainers) != 2 || pod.Spec.InitContainers[0].Name != fetcherContainerName {
				t.Fatalf("InitContainers = %d, want the fetcher followed by %s", len(pod.Spec.InitContainers), WorkspaceInitContainerName)
			}
			initC := pod.Spec.InitContainers[1]
			if initC.Name != WorkspaceInitContainerName || initC.Image != opts.FetcherImage {
				t.Errorf("workspace init = %s/%s, want %s/%s", initC.Name, initC.Image, WorkspaceInitContainerName, opts.FetcherImage)
			}
			wantArgs := []string{"-p", "-m", workspaceDirMode, workspacesInitMountPath + "/" + testWorkspaceID}
			if !reflect.DeepEqual(initC.Command, []string{"mkdir"}) || !reflect.DeepEqual(initC.Args, wantArgs) {
				t.Errorf("workspace init command = %v %v, want [mkdir] %v", initC.Command, initC.Args, wantArgs)
			}
			wantInitMounts := []corev1.VolumeMount{{Name: workspaceVolumeName, MountPath: workspacesInitMountPath, SubPath: workspacesSubPath}}
			if !reflect.DeepEqual(initC.VolumeMounts, wantInitMounts) {
				t.Errorf("workspace init mounts = %v, want only %v", initC.VolumeMounts, wantInitMounts)
			}
			checkRestrictedSecurityContext(t, "workspace init container", initC.SecurityContext)

			main := pod.Spec.Containers[0]
			mount, ok := volumeMountNamed(main.VolumeMounts, workspaceVolumeName)
			if !ok {
				t.Fatal("runtime container does not mount the Workspace directory")
			}
			wantMount := corev1.VolumeMount{
				Name:      workspaceVolumeName,
				MountPath: opts.WorkspaceStorage.MountPath,
				SubPath:   workspacesSubPath + "/" + testWorkspaceID,
				ReadOnly:  c.wantReadOnly,
			}
			if mount != wantMount {
				t.Errorf("runtime workspace mount = %+v, want %+v", mount, wantMount)
			}
			if env, ok := envValue(main.Env, workspaceDirEnv); !ok || env.Value != opts.WorkspaceStorage.MountPath {
				t.Errorf("%s env = %+v, want %q", workspaceDirEnv, env, opts.WorkspaceStorage.MountPath)
			}
			if _, ok := volumeMountNamed(pod.Spec.InitContainers[0].VolumeMounts, workspaceVolumeName); ok {
				t.Error("the source fetcher must never mount Workspace storage")
			}
		})
	}
}

// TestBuildPodResourcesOverride pins that spec.resources overrides only the
// Runtime container's resources, never the fetcher's: fetcher sizing is
// platform-controlled policy, not something a Runtime requester's
// spec.resources is documented to reach.
func TestBuildPodResourcesOverride(t *testing.T) {
	opts := testOptions()
	cr := testSession(v1alpha1.RuntimeModeRun, ubuntuImage)
	override := corev1.ResourceRequirements{
		Requests: corev1.ResourceList{corev1.ResourceCPU: resource.MustParse("500m")},
		Limits:   corev1.ResourceList{corev1.ResourceCPU: resource.MustParse("2")},
	}
	cr.Spec.Resources = &override

	pod := BuildPod(cr, opts)
	main := pod.Spec.Containers[0]
	if !reflect.DeepEqual(main.Resources, override) {
		t.Errorf("runtime container Resources = %v, want override %v", main.Resources, override)
	}
	fetcher := pod.Spec.InitContainers[0]
	if !reflect.DeepEqual(fetcher.Resources, opts.FetcherResources) {
		t.Errorf("init container Resources = %v, want unchanged chart default %v", fetcher.Resources, opts.FetcherResources)
	}
}

// TestModeCommand pins that deploy mode -- like run -- serves a read-only
// app, and that only edit mode requests --skip-update-check.
func TestModeCommand(t *testing.T) {
	cases := []struct {
		mode v1alpha1.RuntimeMode
		want string
	}{
		{v1alpha1.RuntimeModeEdit, modeCommandEdit},
		{v1alpha1.RuntimeModeRun, modeCommandRun},
		{v1alpha1.RuntimeModeDeploy, modeCommandRun},
	}
	for _, c := range cases {
		if got := modeCommand(c.mode); got != c.want {
			t.Errorf("modeCommand(%s) = %q, want %q", c.mode, got, c.want)
		}
	}
}

const (
	testShareDatasets = "datasets"
	testShareScratch  = "scratch"
)

func testSharedVolumes() []SharedVolume {
	return []SharedVolume{
		{
			Name:               testShareDatasets,
			ClaimName:          "nfs-datasets",
			MountPath:          "/mnt/datasets",
			ReadOnly:           true,
			Modes:              []v1alpha1.RuntimeMode{v1alpha1.RuntimeModeEdit, v1alpha1.RuntimeModeRun, v1alpha1.RuntimeModeDeploy},
			SupplementalGroups: []int64{1000},
		},
		{
			Name:               testShareScratch,
			ClaimName:          "nfs-team",
			SubPath:            testShareScratch,
			MountPath:          "/mnt/scratch",
			ReadOnly:           false,
			Modes:              []v1alpha1.RuntimeMode{v1alpha1.RuntimeModeEdit},
			SupplementalGroups: []int64{2000},
		},
	}
}

// TestBuildPodSharedVolumes pins which Runtime modes mount each shared
// volume, how, and that only the marimo container ever sees one.
func TestBuildPodSharedVolumes(t *testing.T) {
	cases := []struct {
		mode       v1alpha1.RuntimeMode
		wantShared []string
		wantGroups []int64
	}{
		// Workspace storage contributes 1000 first; the datasets volume's
		// 1000 is not repeated.
		{v1alpha1.RuntimeModeEdit, []string{testShareDatasets, testShareScratch}, []int64{1000, 2000}},
		{v1alpha1.RuntimeModeRun, []string{testShareDatasets}, []int64{1000}},
		{v1alpha1.RuntimeModeDeploy, []string{testShareDatasets}, []int64{1000}},
	}
	byName := map[string]SharedVolume{}
	for _, shared := range testSharedVolumes() {
		byName[shared.Name] = shared
	}
	for _, c := range cases {
		t.Run(string(c.mode), func(t *testing.T) {
			opts := testOptions()
			opts.WorkspaceStorage = testWorkspaceStorage()
			opts.SharedVolumes = testSharedVolumes()
			pod := BuildPod(testSession(c.mode, ubiImage), opts)

			var gotShared []string
			for _, volume := range pod.Spec.Volumes {
				name, ok := strings.CutPrefix(volume.Name, sharedVolumePrefix)
				if !ok {
					continue
				}
				gotShared = append(gotShared, name)
				want := byName[name]
				if volume.PersistentVolumeClaim == nil || volume.PersistentVolumeClaim.ClaimName != want.ClaimName ||
					volume.PersistentVolumeClaim.ReadOnly != want.ReadOnly {
					t.Errorf("volume %q = %+v, want claim %q readOnly %v", volume.Name, volume.PersistentVolumeClaim, want.ClaimName, want.ReadOnly)
				}
				mount, ok := volumeMountNamed(pod.Spec.Containers[0].VolumeMounts, volume.Name)
				wantMount := corev1.VolumeMount{Name: volume.Name, MountPath: want.MountPath, SubPath: want.SubPath, ReadOnly: want.ReadOnly}
				if !ok || mount != wantMount {
					t.Errorf("runtime mount for %q = %+v, want %+v", volume.Name, mount, wantMount)
				}
				for _, initC := range pod.Spec.InitContainers {
					if _, mounted := volumeMountNamed(initC.VolumeMounts, volume.Name); mounted {
						t.Errorf("init container %q must not mount shared volume %q", initC.Name, volume.Name)
					}
				}
			}
			if !reflect.DeepEqual(gotShared, c.wantShared) {
				t.Errorf("shared volumes = %v, want %v (in configuration order)", gotShared, c.wantShared)
			}
			if !reflect.DeepEqual(pod.Spec.SecurityContext.SupplementalGroups, c.wantGroups) {
				t.Errorf("SupplementalGroups = %v, want %v", pod.Spec.SecurityContext.SupplementalGroups, c.wantGroups)
			}
		})
	}
}

// TestBuildPodWithoutSharedVolumesIsUnchanged pins that configuring none
// leaves the Pod exactly as before shared volumes existed.
func TestBuildPodWithoutSharedVolumesIsUnchanged(t *testing.T) {
	for _, mode := range []v1alpha1.RuntimeMode{v1alpha1.RuntimeModeEdit, v1alpha1.RuntimeModeRun, v1alpha1.RuntimeModeDeploy} {
		withNil := BuildPod(testSession(mode, ubiImage), testOptions())
		opts := testOptions()
		opts.SharedVolumes = []SharedVolume{}
		withEmpty := BuildPod(testSession(mode, ubiImage), opts)
		if !reflect.DeepEqual(withNil, withEmpty) {
			t.Errorf("%s: an empty SharedVolumes list changed the Pod", mode)
		}
	}
}
