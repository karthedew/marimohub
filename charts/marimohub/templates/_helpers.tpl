{{/*
Base name for every object this chart renders. Overridable because a
platform installer may run more than one MarimoHub release name across
environments sharing a cluster (e.g. "marimohub" and "marimohub-staging"),
even though the three namespaces themselves are never shared between them.
*/}}
{{- define "marimohub.fullname" -}}
{{- .Values.nameOverride | default .Release.Name -}}
{{- end -}}

{{/*
"<chart-name>-<chart-version>", sanitized the way Helm's own chart label
convention requires (no "+", DNS-label safe, max 63 chars).
*/}}
{{- define "marimohub.chartLabel" -}}
{{- printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{/*
Labels every object in the release carries, regardless of component.
*/}}
{{- define "marimohub.labels" -}}
app.kubernetes.io/name: {{ .Chart.Name }}
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
helm.sh/chart: {{ include "marimohub.chartLabel" . }}
{{- end -}}

{{/*
"<release>-<component>", the Deployment/Service/ServiceAccount/Role name for
one component. Call as `include "marimohub.componentName" (dict "ctx" $ "component" "frontend")`.
*/}}
{{- define "marimohub.componentName" -}}
{{- printf "%s-%s" (include "marimohub.fullname" .ctx) .component -}}
{{- end -}}

{{/*
The label set that must match between a component's Deployment's pod
template and its own Service/NetworkPolicy selector -- never shared across
components, so two components' Pods are never accidentally selected by each
other's rules. Call as `include "marimohub.componentSelectorLabels" (dict "ctx" $ "component" "frontend")`.
*/}}
{{- define "marimohub.componentSelectorLabels" -}}
app.kubernetes.io/name: {{ .ctx.Chart.Name }}
app.kubernetes.io/instance: {{ .ctx.Release.Name }}
app.kubernetes.io/component: {{ .component }}
{{- end -}}

{{/*
Full labels for one component's objects: the release-wide labels plus its
own selector labels.
*/}}
{{- define "marimohub.componentLabels" -}}
{{ include "marimohub.labels" .ctx }}
app.kubernetes.io/component: {{ .component }}
{{- end -}}

{{/*
The resolved, digest-qualified Runtime image for .Values.runtime.defaultImageFlavor.
Fails the render rather than silently falling back if the configured flavor
has no matching entry in .Values.runtime.images -- an admission-rejected
MarimoSession at the first Session request is a much worse place to discover
a values.yaml typo than here.
*/}}
{{- define "marimohub.runtimeImage" -}}
{{- $flavor := .Values.runtime.defaultImageFlavor -}}
{{- $image := index .Values.runtime.images $flavor -}}
{{- if not $image -}}
{{- fail (printf "runtime.defaultImageFlavor %q has no matching entry in runtime.images" $flavor) -}}
{{- end -}}
{{- $image -}}
{{- end -}}

{{/*
The Runtime Pod/Service's fixed port. Not a value: the Runtime contract
(marimohub-operator/internal/runtimecontract and backend/app/services/
runtime_contract.py) fixes this at 8080 for every Runtime regardless of
mode or image flavor, so making it configurable here would let the chart
and the operator/backend disagree about a number neither side ever varies.
*/}}
{{- define "marimohub.runtimePort" -}}
8080
{{- end -}}

{{- define "marimohub.sharedVolumesJson" -}}
{{- /*
The operator's --shared-volumes value: runtime.sharedVolumes with the chart's
existingClaim key renamed to the operator's claimName, and nothing else
changed. Defaults (read-only, edit and run, /mnt/<name>) are applied and
validated in one place, the operator, so a values file and a hand-written
flag can never disagree about them.
*/ -}}
{{- $volumes := list -}}
{{- range .Values.runtime.sharedVolumes -}}
{{- $volume := omit . "existingClaim" -}}
{{- $_ := set $volume "claimName" .existingClaim -}}
{{- $volumes = append $volumes $volume -}}
{{- end -}}
{{- toJson $volumes -}}
{{- end -}}

{{/*
Router timeout, in seconds, for the public API: the backend's own Runtime
start timeout plus headroom, so the router never cuts off a start or wake
request the backend is still legitimately waiting on.
*/}}
{{- define "marimohub.routerTimeoutSeconds" -}}
{{- add .Values.runtime.startTimeoutSeconds 30 -}}
{{- end -}}

{{/*
"<repository>@<digest>" for a chart-managed image, failing fast if either
half is missing rather than rendering a bare repository that would resolve
to a mutable "latest" tag.
*/}}
{{- define "marimohub.image" -}}
{{- if not .repository -}}
{{- fail "image.repository is required" -}}
{{- end -}}
{{- if not .digest -}}
{{- fail (printf "%s must be digest-pinned (image.digest is empty)" .repository) -}}
{{- end -}}
{{- printf "%s@%s" .repository .digest -}}
{{- end -}}

{{/*
Fails the render if verified PostgreSQL TLS is required but unconfigured:
the openshift platform profile always requires database.tls.enabled (see
the Security Baseline), and enabling it anywhere requires naming the
existing CA Secret to actually mount. Called with no output from every
Deployment/Job that connects to the database (backend-public,
backend-internal, migration) so the same two checks never drift between
them.
*/}}
{{- define "marimohub.databaseTLSGuard" -}}
{{- if and (eq .Values.platform "openshift") (not .Values.database.tls.enabled) -}}
{{- fail "database.tls.enabled must be true on the openshift platform profile (verified PostgreSQL TLS is required)" -}}
{{- end -}}
{{- if and .Values.database.tls.enabled (not .Values.database.tls.caSecretName) -}}
{{- fail "database.tls.caSecretName is required when database.tls.enabled" -}}
{{- end -}}
{{- end -}}

{{/*
Fails the render when an openshift install would come up broken without any
error: Routes with no host each get a different generated host, so / and
/api no longer share one origin; with no Kubernetes API or database egress,
the operator and both backends cannot reach either one under default-deny.
Called with no output next to databaseTLSGuard in every workload template.
*/}}
{{- define "marimohub.openshiftGuard" -}}
{{- if eq .Values.platform "openshift" -}}
{{- if not .Values.routes.host -}}
{{- fail "routes.host is required on the openshift profile: both Routes must share one host" -}}
{{- end -}}
{{- if not .Values.network.kubernetesApi.cidrs -}}
{{- fail "network.kubernetesApi.cidrs is required on the openshift profile (the API server endpoints; platform-values.yaml from the marimohub-platform chart sets them)" -}}
{{- end -}}
{{- if not (or .Values.network.database.cidrs .Values.network.database.peers) -}}
{{- fail "network.database.cidrs or network.database.peers is required on the openshift profile" -}}
{{- end -}}
{{- end -}}
{{- end -}}

{{/*
The public https:// URL of this install, which every sign-in redirect URI is
built from: the Route host on openshift, the Ingress host on portable, or
empty when neither is rendered (backendPublic.existingSecret must then carry
PUBLIC_API_URL itself).
*/}}
{{- define "marimohub.publicUrl" -}}
{{- if eq .Values.platform "openshift" -}}
{{- with .Values.routes.host }}https://{{ . }}{{ end -}}
{{- else if .Values.ingress.enabled -}}
{{- with .Values.ingress.host }}https://{{ . }}{{ end -}}
{{- end -}}
{{- end -}}

{{/*
One NetworkPolicy egress rule for DNS: network.dns.to on network.dns.ports.
Every component's policy renders DNS through this, so the profiles change it
in one place. OpenShift's DNS Pods listen on 5353 behind the dns-default
Service's port 53, and OVN-Kubernetes matches egress after the Service DNAT,
so values-openshift.yaml names those Pods and both ports.
*/}}
{{- define "marimohub.dnsEgressRule" -}}
- to:
    {{- toYaml .Values.network.dns.to | nindent 4 }}
  ports:
    {{- toYaml .Values.network.dns.ports | nindent 4 }}
{{- end -}}

{{/*
The egress rule to PostgreSQL: one ipBlock per network.database.cidrs entry,
then each network.database.peers entry (NetworkPolicyPeer objects, for a
database running in the cluster), on network.database.port/TCP. Renders
nothing when both lists are empty.
*/}}
{{- define "marimohub.databaseEgressRule" -}}
{{- if or .Values.network.database.cidrs .Values.network.database.peers -}}
- to:
    {{- range .Values.network.database.cidrs }}
    - ipBlock:
        cidr: {{ . }}
    {{- end }}
    {{- with .Values.network.database.peers }}
    {{- toYaml . | nindent 4 }}
    {{- end }}
  ports:
    - port: {{ .Values.network.database.port }}
      protocol: TCP
{{- end -}}
{{- end -}}

{{/*
The default host+zone topologySpreadConstraints for one component's own
Pods, used whenever that component's own `topologySpreadConstraints` value
is left empty. `ScheduleAnyway` (not `DoNotSchedule`) so a single-zone or
single-node development cluster still schedules every replica instead of
leaving Pods Pending -- production HA comes from the constraint steering
the scheduler's preference, not from hard-blocking placement. Call as
`include "marimohub.defaultTopologySpreadConstraints" (dict "ctx" $ "component" "frontend")`.
*/}}
{{- define "marimohub.defaultTopologySpreadConstraints" -}}
{{- $labels := include "marimohub.componentSelectorLabels" (dict "ctx" .ctx "component" .component) -}}
- maxSkew: 1
  topologyKey: kubernetes.io/hostname
  whenUnsatisfiable: ScheduleAnyway
  labelSelector:
    matchLabels:
      {{- $labels | nindent 6 }}
- maxSkew: 1
  topologyKey: topology.kubernetes.io/zone
  whenUnsatisfiable: ScheduleAnyway
  labelSelector:
    matchLabels:
      {{- $labels | nindent 6 }}
{{- end -}}

{{/*
A soft (preferred, not required) same-host anti-affinity rule for one
component's own Pods, complementing (not duplicating) the topology-spread
constraints above: `ScheduleAnyway` topology spread steers the scheduler but
carries no explicit affinity term of its own, and a single-node development
cluster must still be able to schedule every replica, so this stays
`preferredDuringSchedulingIgnoredDuringExecution` rather than `required`.
Call as `include "marimohub.defaultPodAntiAffinity" (dict "ctx" $ "component" "frontend")`.
*/}}
{{- define "marimohub.defaultPodAntiAffinity" -}}
podAntiAffinity:
  preferredDuringSchedulingIgnoredDuringExecution:
    - weight: 100
      podAffinityTerm:
        topologyKey: kubernetes.io/hostname
        labelSelector:
          matchLabels:
            {{- include "marimohub.componentSelectorLabels" (dict "ctx" .ctx "component" .component) | nindent 12 }}
{{- end -}}
