{{/* Labels on every object this chart renders. */}}
{{- define "platform.labels" -}}
app.kubernetes.io/name: {{ .Chart.Name }}
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
app.kubernetes.io/part-of: marimohub
helm.sh/chart: {{ printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" | trunc 63 | trimSuffix "-" }}
{{- end -}}

{{/* Objects that hold data, or that the app release depends on, survive helm uninstall. */}}
{{- define "platform.keep" -}}
helm.sh/resource-policy: keep
{{- end -}}

{{/*
"true" when rendering against a live cluster. helm template and client-side
dry runs cannot look anything up, so a required lookup fails only when
online and renders a throwaway placeholder otherwise.
*/}}
{{- define "platform.online" -}}
{{- if lookup "v1" "Namespace" "" "kube-system" }}true{{ end -}}
{{- end -}}

{{/* A clean absolute path with one trailing slash, for prefix comparisons. */}}
{{- define "platform.dirPrefix" -}}
{{- printf "%s/" (clean .) | replace "//" "/" -}}
{{- end -}}

{{/*
storage.shares with every default applied, as a JSON list. Read it with
`include "platform.shares" . | fromJsonArray`.
*/}}
{{- define "platform.shares" -}}
{{- $out := list -}}
{{- range .Values.storage.shares -}}
{{- $path := "" -}}
{{- with .path }}{{ $path = clean . }}{{ end -}}
{{- $readOnly := true -}}
{{- if hasKey . "readOnly" }}{{ $readOnly = .readOnly }}{{ end -}}
{{- $share := dict
  "name" .name
  "path" $path
  "server" (.server | default $.Values.storage.nfs.server)
  "hostPath" (.hostPath | default $path)
  "claimName" (.claimName | default (printf "marimohub-share-%s" .name))
  "capacity" (.capacity | default "100Gi")
  "mountPath" (.mountPath | default $path)
  "subPath" (.subPath | default "")
  "readOnly" $readOnly
  "modes" (.modes | default list)
  "supplementalGroups" (.supplementalGroups | default list)
-}}
{{- $out = append $out $share -}}
{{- end -}}
{{- toJson $out -}}
{{- end -}}

{{/*
Fails the render for every configuration this chart refuses. Included once,
from namespaces.yaml.
*/}}
{{- define "platform.validate" -}}
{{- $v := .Values -}}
{{- $openshift := eq $v.platform "openshift" -}}
{{- if not (has $v.platform (list "openshift" "portable")) -}}
{{- fail "platform must be openshift or portable" -}}
{{- end -}}
{{- if not (has $v.storage.mode (list "nfs" "hostPath")) -}}
{{- fail "storage.mode must be nfs or hostPath" -}}
{{- end -}}
{{- if and $openshift (eq $v.storage.mode "hostPath") -}}
{{- fail "storage.mode hostPath is for single-node test clusters only; use nfs on openshift" -}}
{{- end -}}
{{- if and $openshift $v.postgresql.podSecurityContext -}}
{{- fail "postgresql.podSecurityContext must stay empty on openshift: restricted-v2 assigns the UID, GID and fsGroup" -}}
{{- end -}}
{{- $hostPathMode := eq $v.storage.mode "hostPath" -}}
{{- $shares := include "platform.shares" . | fromJsonArray -}}
{{- $names := list -}}
{{- $claims := list -}}
{{- if $v.storage.workspaces.enabled }}{{ $claims = append $claims $v.storage.workspaces.claimName }}{{ end -}}
{{- range $shares -}}
{{- if not .path }}{{ fail (printf "storage.shares %q: path is required" .name) }}{{ end -}}
{{- if and (not $hostPathMode) (not .server) }}{{ fail (printf "storage.shares %q: server (or storage.nfs.server) is required" .name) }}{{ end -}}
{{- if has .name $names }}{{ fail (printf "storage.shares: name %q is used twice" .name) }}{{ end -}}
{{- $names = append $names .name -}}
{{- if has .claimName $claims }}{{ fail (printf "storage.shares %q: claim name %q is used twice" .name .claimName) }}{{ end -}}
{{- $claims = append $claims .claimName -}}
{{- if and (not .readOnly) (has "deploy" .modes) }}{{ fail (printf "storage.shares %q: only a read-only share may list mode deploy, because Deployments are public" .name) }}{{ end -}}
{{- end -}}
{{- with $v.storage.workspaces -}}
{{- if .enabled -}}
{{- if not .path }}{{ fail "storage.workspaces.path is required when storage.workspaces.enabled" }}{{ end -}}
{{- $server := .server | default $v.storage.nfs.server -}}
{{- if and (not $hostPathMode) (not $server) }}{{ fail "storage.workspaces.server (or storage.nfs.server) is required" }}{{ end -}}
{{- $ws := include "platform.dirPrefix" .path -}}
{{- $wsNode := include "platform.dirPrefix" (.hostPath | default .path) -}}
{{- range $shares -}}
{{- $share := include "platform.dirPrefix" .path -}}
{{- $shareNode := include "platform.dirPrefix" .hostPath -}}
{{- $overlap := false -}}
{{- if or $hostPathMode (eq $server .server) -}}
{{- $overlap = or (hasPrefix $share $ws) (hasPrefix $ws $share) -}}
{{- end -}}
{{- if $hostPathMode -}}
{{- $overlap = or $overlap (hasPrefix $shareNode $wsNode) (hasPrefix $wsNode $shareNode) -}}
{{- end -}}
{{- if $overlap -}}
{{- fail (printf "storage.workspaces (%s) and share %q (%s) overlap: Workspace Files must never be inside, or contain, a share that notebooks mount" $v.storage.workspaces.path .name .path) -}}
{{- end -}}
{{- end -}}
{{- end -}}
{{- end -}}
{{- if and $v.postgresql.enabled $v.database.host -}}
{{- fail "set postgresql.enabled (evaluation) or database.host (external database), not both" -}}
{{- end -}}
{{- if not $v.postgresql.enabled -}}
{{- if and (not $v.backendEnv.existingSecret) (or (not $v.database.host) (not $v.database.passwordSecret.name)) -}}
{{- fail "set postgresql.enabled (evaluation), backendEnv.existingSecret, or database.host and database.passwordSecret" -}}
{{- end -}}
{{- if not (or $v.database.ca.existingSecret $v.database.ca.bundle $v.database.ca.fromSecret.name) -}}
{{- fail "an external database needs the CA that signed its certificate: set database.ca.bundle, database.ca.fromSecret or database.ca.existingSecret" -}}
{{- end -}}
{{- end -}}
{{- end -}}

{{/* Service DNS names for one app component's certificate, as JSON. */}}
{{- define "platform.serviceSANs" -}}
{{- $svc := printf "%s-%s" .ctx.Values.app.releaseName .component -}}
{{- $ns := .ctx.Values.namespaces.app -}}
{{- toJson (list $svc (printf "%s.%s" $svc $ns) (printf "%s.%s.svc" $svc $ns) (printf "%s.%s.svc.cluster.local" $svc $ns)) -}}
{{- end -}}

{{/* DNS names of the evaluation database's certificate, as JSON. */}}
{{- define "platform.databaseSANs" -}}
{{- $svc := .Values.postgresql.name -}}
{{- $ns := .Values.namespaces.database -}}
{{- toJson (list $svc (printf "%s.%s" $svc $ns) (printf "%s.%s.svc" $svc $ns) (printf "%s.%s.svc.cluster.local" $svc $ns)) -}}
{{- end -}}

{{/*
The data of an existing leaf certificate Secret, as JSON, when it can be
kept: it names the same DNS names and was issued by caCert. Empty otherwise,
and the leaf is issued again in this render. Call with
(dict "namespace" ns "name" name "sans" list "caCert" pem).
*/}}
{{- define "platform.reusableLeaf" -}}
{{- $existing := lookup "v1" "Secret" .namespace .name -}}
{{- if and $existing .caCert -}}
{{- $annotations := $existing.metadata.annotations | default dict -}}
{{- $data := $existing.data | default dict -}}
{{- $sameNames := eq (index $annotations "marimohub.io/dns-names-sha256" | default "") (join "," .sans | sha256sum) -}}
{{- $sameCA := eq (index $annotations "marimohub.io/ca-sha256" | default "") (.caCert | sha256sum) -}}
{{- if and $sameNames $sameCA (index $data "tls.crt") (index $data "tls.key") -}}
{{- toJson $data -}}
{{- end -}}
{{- end -}}
{{- end -}}

{{/*
A value that changes exactly when a leaf certificate is issued again, for a
Pod annotation that restarts the server reading it. A kept leaf keeps the key
stored on its Secret; a leaf issued in this render gets one from its DNS
names and the release revision, which secrets.yaml and the Pod's template
both compute the same way without seeing each other's output. Same arguments
as platform.reusableLeaf, plus "revision".
*/}}
{{- define "platform.leafRestartKey" -}}
{{- if include "platform.reusableLeaf" . -}}
{{- $existing := lookup "v1" "Secret" .namespace .name -}}
{{- index ($existing.metadata.annotations | default dict) "marimohub.io/restart-key" | default (index $existing.data "tls.crt" | sha256sum) -}}
{{- else -}}
{{- printf "%s/%d" (join "," .sans | sha256sum) (int .revision) | sha256sum -}}
{{- end -}}
{{- end -}}

{{/*
The values the app chart needs to use what this chart created, as YAML.
Printed by NOTES.txt and stored in the marimohub-platform-values ConfigMap.
It holds names, paths and addresses, never a secret value.
*/}}
{{- define "platform.appValues" -}}
{{- $v := .Values -}}
{{- $openshift := eq $v.platform "openshift" -}}
{{- $envSecret := $v.backendEnv.existingSecret | default $v.backendEnv.name -}}
{{- $out := dict
  "namespaces" (dict "app" $v.namespaces.app "controller" $v.namespaces.controller "sessions" $v.namespaces.sessions)
  "backendPublic" (dict "existingSecret" $envSecret)
  "backendInternal" (dict "existingSecret" $envSecret)
-}}
{{- $caName := $v.database.ca.existingSecret | default $v.database.ca.name -}}
{{- $_ := set $out "database" (dict "tls" (dict "enabled" true "caSecretName" $caName "caSecretKey" "ca.crt")) -}}
{{- $runtime := dict "internalApiCA" (dict "secretName" $v.internalApiCA.name "secretKey" $v.internalApiCA.key) -}}
{{- if $v.storage.workspaces.enabled -}}
{{- $groups := list -}}
{{- range $v.storage.workspaces.supplementalGroups }}{{ $groups = append $groups (int64 .) }}{{ end -}}
{{- $_ := set $runtime "workspaceStorage" (dict "existingClaim" $v.storage.workspaces.claimName "mountPath" $v.storage.workspaces.mountPath "supplementalGroups" $groups) -}}
{{- end -}}
{{- $shared := list -}}
{{- range include "platform.shares" . | fromJsonArray -}}
{{- $entry := dict "name" .name "existingClaim" .claimName "mountPath" .mountPath "readOnly" .readOnly -}}
{{- if .subPath }}{{ $_ := set $entry "subPath" .subPath }}{{ end -}}
{{- if .modes }}{{ $_ := set $entry "modes" .modes }}{{ end -}}
{{- if .supplementalGroups -}}
{{- $groups := list -}}
{{- range .supplementalGroups }}{{ $groups = append $groups (int64 .) }}{{ end -}}
{{- $_ := set $entry "supplementalGroups" $groups -}}
{{- end -}}
{{- $shared = append $shared $entry -}}
{{- end -}}
{{- $_ := set $runtime "sharedVolumes" $shared -}}
{{- $_ := set $out "runtime" $runtime -}}
{{- $network := dict -}}
{{- $api := lookup "discovery.k8s.io/v1" "EndpointSlice" "default" "kubernetes" -}}
{{- if $api -}}
{{- $cidrs := list -}}
{{- range $api.endpoints }}{{ range .addresses }}{{ $cidrs = append $cidrs (printf "%s/32" .) }}{{ end }}{{ end -}}
{{- $port := 6443 -}}
{{- range $api.ports }}{{ $port = .port }}{{ end -}}
{{- $_ := set $network "kubernetesApi" (dict "cidrs" $cidrs "port" (int $port)) -}}
{{- end -}}
{{- if $v.postgresql.enabled -}}
{{- $peer := dict "namespaceSelector" (dict "matchLabels" (dict "kubernetes.io/metadata.name" $v.namespaces.database)) "podSelector" (dict "matchLabels" (dict "app.kubernetes.io/name" $v.postgresql.name)) -}}
{{- $_ := set $network "database" (dict "cidrs" list "peers" (list $peer) "port" 5432) -}}
{{- else if or $v.database.cidrs $v.database.peers -}}
{{- $_ := set $network "database" (dict "cidrs" ($v.database.cidrs | default list) "peers" ($v.database.peers | default list) "port" (int $v.database.port)) -}}
{{- end -}}
{{- if $network }}{{ $_ := set $out "network" $network }}{{ end -}}
{{- if not $openshift -}}
{{- range $component, $key := dict "frontend" "frontend" "backend-public" "backendPublic" "backend-internal" "backendInternal" -}}
{{- $existing := index $out $key | default dict -}}
{{- $_ := set $existing "tls" (dict "enabled" true "secretName" (printf "%s-%s-tls" $v.app.releaseName $component)) -}}
{{- $_ := set $out $key $existing -}}
{{- end -}}
{{- end -}}
{{- toYaml $out -}}
{{- end -}}
