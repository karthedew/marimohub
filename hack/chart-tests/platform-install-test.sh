#!/usr/bin/env bash
# make openshift-install-test: installs charts/marimohub-platform and
# charts/marimohub on a throwaway kind cluster and checks what kind can show
# about an OpenShift install:
#   - Pod Security "restricted" admits every Pod, including a Runtime-shaped
#     Pod with an arbitrary UID, its Workspace directory and a read-only share;
#   - the evaluation PostgreSQL takes verified TLS from app clients only;
#   - generated Secrets survive upgrade, uninstall and reinstall, and an
#     upgrade does not restart PostgreSQL;
#   - helm upgrade applies CRD changes, and the admission policy works;
#   - the app chart's Routes and ServiceMonitor are accepted by the real
#     OpenShift Route and Prometheus Operator CRDs;
#   - the openshift platform profile fails clearly without its live inputs;
#   - the preflight scripts read dotted keys from a real API server.
#
# The cluster is named ocp-test and has its own kubeconfig in a temporary
# directory: this script never reads or changes ~/.kube/config or any other
# cluster, and it deletes the cluster when it exits (KEEP_CLUSTER=1 keeps
# it). Docker, openssl and network access (images, CRDs) are required.
#
# Images: the Runtime-shaped Pod needs marimohub-runtime-ubi:dev and
# marimohub-source-fetcher:dev from `make images`. Without IMAGES_ENV the app
# chart is installed with --no-hooks to check admission only, and its Pods may
# stay in ImagePullBackOff. With IMAGES_ENV the app really runs:
#   IMAGES_ENV       an images.env written by make kind-up (.kind/images.env)
#   IMAGE_REGISTRY   the registry in those references (default localhost:5001)
#   REGISTRY_MIRROR  where kind nodes reach it (default marimohub-registry:5000,
#                    the registry make kind-up runs on the kind network)
#   SESSION_PYTHON   Python with httpx and websockets for load_sessions.py
#                    (default: uv run --project backend python)
#   HTTPS_PORT       host port of the Ingress (default 18443). The app is
#                    https://marimohub:HTTPS_PORT, so marimohub must resolve
#                    to 127.0.0.1 (/etc/hosts).
set -Eeuo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$REPO_ROOT"
# shellcheck source=../tools/versions.env
source hack/tools/versions.env
# shellcheck source=versions.env
source hack/chart-tests/versions.env

CLUSTER=ocp-test
KIND="$REPO_ROOT/.bin/kind"
KUBECTL="$REPO_ROOT/.bin/kubectl"
HELM="$REPO_ROOT/.bin/helm"
APP=charts/marimohub
PLATFORM=charts/marimohub-platform
IMAGES_ENV="${IMAGES_ENV:-}"
IMAGE_REGISTRY="${IMAGE_REGISTRY:-localhost:5001}"
REGISTRY_MIRROR="${REGISTRY_MIRROR:-marimohub-registry:5000}"
HTTPS_PORT="${HTTPS_PORT:-18443}"
KEEP_CLUSTER="${KEEP_CLUSTER:-0}"
PUBLIC_HOST=marimohub
NODE="${CLUSTER}-control-plane"
POSTGRES_IMAGE="$(awk '/^  image: pgvector/ { print $2 }' "$PLATFORM/values.yaml")"

WORK="$(mktemp -d "${TMPDIR:-/tmp}/ocp-test.XXXXXX")"
KCFG="$WORK/kubeconfig"
mkdir -p "$WORK/data/workspaces" "$WORK/nfs"
# The groups that own the Workspace directory and the share on the node.
# With the app running, load_sessions.py reads Workspace files on this host,
# so the Workspace group is then the current user's.
WORKSPACE_GID=5000
SHARE_GID=5001
[ -z "$IMAGES_ENV" ] || WORKSPACE_GID="$(id -g)"

log() { printf '\n==> %s\n' "$*"; }
ok() { printf 'ok   %s\n' "$*"; }
fail() {
	printf 'FAIL %s\n' "$*" >&2
	exit 1
}
k() { "$KUBECTL" --kubeconfig "$KCFG" "$@"; }
h() { "$HELM" --kubeconfig "$KCFG" "$@"; }

cleanup() {
	local status=$?
	if [ "$KEEP_CLUSTER" = 1 ]; then
		printf '\nKEEP_CLUSTER=1: cluster %s kept; kubeconfig %s\n' "$CLUSTER" "$KCFG" >&2
		return
	fi
	# Runtime Pods wrote files under the node mounts with their own UIDs.
	docker exec "$NODE" find /var/lib/marimohub/data /var/lib/marimohub/nfs -mindepth 1 -delete 2>/dev/null || true
	"$KIND" delete cluster --name "$CLUSTER" --kubeconfig "$KCFG" >/dev/null 2>&1 || true
	rm -rf "$WORK" 2>/dev/null || true
	exit "$status"
}

# wait_pod NAMESPACE NAME: waits until the Pod has finished and prints its
# phase (Succeeded or Failed).
wait_pod() {
	local phase="" _
	for _ in $(seq 180); do
		phase="$(k -n "$1" get pod "$2" -o jsonpath='{.status.phase}' 2>/dev/null || true)"
		case "$phase" in Succeeded | Failed) break ;; esac
		sleep 1
	done
	printf '%s' "$phase"
}

# secret_hashes: one sha256 per generated Secret's data, never the data.
secret_hashes() {
	local item
	for item in marimohub-platform/marimohub-platform-ca marimohub/marimohub-backend-env marimohub/marimohub-database-ca \
		marimohub/marimohub-frontend-tls marimohub/marimohub-backend-public-tls marimohub/marimohub-backend-internal-tls \
		marimohub-sessions/marimohub-internal-api-ca marimohub-database/marimohub-postgresql \
		marimohub-database/marimohub-postgresql-tls; do
		printf '%s %s\n' "$item" "$(k -n "${item%%/*}" get secret "${item#*/}" -o jsonpath='{.data}' | sha256sum | cut -d' ' -f1)"
	done
}

# db_client NAME COMPONENT SQL: runs SQL as the app's database role from a
# Pod in the marimohub namespace labelled as COMPONENT of the marimohub
# release, with verified TLS. Prints psql's output and returns its status.
db_client() {
	local name="$1" component="$2" sql="$3" phase
	k -n marimohub delete pod "$name" --ignore-not-found --wait >/dev/null
	k apply -f - >/dev/null <<EOF
apiVersion: v1
kind: Pod
metadata:
  name: $name
  namespace: marimohub
  labels:
    app.kubernetes.io/name: marimohub
    app.kubernetes.io/instance: marimohub
    app.kubernetes.io/component: $component
spec:
  restartPolicy: Never
  automountServiceAccountToken: false
  enableServiceLinks: false
  securityContext:
    runAsNonRoot: true
    runAsUser: 999
    seccompProfile: { type: RuntimeDefault }
  containers:
    - name: psql
      image: $POSTGRES_IMAGE
      command: ["bash", "-c"]
      args:
        - exec psql "\${DATABASE_URL/postgresql+asyncpg:/postgresql:}?sslmode=verify-full&sslrootcert=/ca/ca.crt&connect_timeout=10" -v ON_ERROR_STOP=1 -tAc "\$SQL"
      env:
        - name: DATABASE_URL
          valueFrom: { secretKeyRef: { name: marimohub-backend-env, key: DATABASE_URL } }
        - name: SQL
          value: "$sql"
        - name: HOME
          value: /tmp
      securityContext:
        allowPrivilegeEscalation: false
        readOnlyRootFilesystem: true
        capabilities: { drop: ["ALL"] }
      resources:
        requests: { cpu: 10m, memory: 32Mi, ephemeral-storage: 16Mi }
        limits: { cpu: 200m, memory: 128Mi, ephemeral-storage: 32Mi }
      volumeMounts:
        - { name: ca, mountPath: /ca, readOnly: true }
        - { name: tmp, mountPath: /tmp }
  volumes:
    - name: ca
      secret: { secretName: marimohub-database-ca }
    - name: tmp
      emptyDir: { sizeLimit: 16Mi }
EOF
	phase="$(wait_pod marimohub "$name")"
	k -n marimohub logs "$name" 2>/dev/null || true
	[ "$phase" = Succeeded ]
}

create_cluster() {
	log "kind cluster $CLUSTER (kubeconfig $KCFG)"
	if "$KIND" get clusters 2>/dev/null | grep -qx "$CLUSTER"; then
		fail "a kind cluster named $CLUSTER already exists; delete it first: .bin/kind delete cluster --name $CLUSTER"
	fi
	trap cleanup EXIT
	"$KIND" create cluster --name "$CLUSTER" --kubeconfig "$KCFG" --image "${KIND_NODE_IMAGE}@${KIND_NODE_DIGEST}" \
		--wait 180s --config - <<EOF
kind: Cluster
apiVersion: kind.x-k8s.io/v1alpha4
nodes:
  - role: control-plane
    kubeadmConfigPatches:
      - |
        kind: InitConfiguration
        nodeRegistration:
          kubeletExtraArgs:
            node-labels: "ingress-ready=true"
    extraPortMappings:
      - { containerPort: 443, hostPort: $HTTPS_PORT, listenAddress: "127.0.0.1", protocol: TCP }
    extraMounts:
      - hostPath: "$WORK/data"
        containerPath: /var/lib/marimohub/data
      - hostPath: "$WORK/nfs"
        containerPath: /var/lib/marimohub/nfs
EOF
	# The node directories the kind profile's hostPath volumes name: a
	# setgid workspaces/ owned by the Workspace group, and a share readable
	# only through its group.
	docker exec "$NODE" sh -c "
		chgrp $WORKSPACE_GID /var/lib/marimohub/data/workspaces && chmod 2775 /var/lib/marimohub/data/workspaces &&
		echo region,sales > /var/lib/marimohub/nfs/sales.csv &&
		chgrp -R $SHARE_GID /var/lib/marimohub/nfs && chmod 2750 /var/lib/marimohub/nfs && chmod 0640 /var/lib/marimohub/nfs/sales.csv"
	if [ -n "$IMAGES_ENV" ]; then
		# The node pulls IMAGE_REGISTRY references from the registry
		# container on the kind network, the way make kind-up's nodes do.
		docker exec "$NODE" mkdir -p "/etc/containerd/certs.d/$IMAGE_REGISTRY"
		printf '[host."http://%s"]\n' "$REGISTRY_MIRROR" | docker exec -i "$NODE" cp /dev/stdin "/etc/containerd/certs.d/$IMAGE_REGISTRY/hosts.toml"
	fi
	ok "cluster created"
}

load_images() {
	if [ -n "$IMAGES_ENV" ]; then
		# shellcheck source=/dev/null
		source "$IMAGES_ENV"
		RUNTIME_IMAGE="$IMAGE_REGISTRY/marimohub-runtime-ubi@$RUNTIME_UBI_DIGEST"
		FETCHER_IMAGE="$IMAGE_REGISTRY/marimohub-source-fetcher@$SOURCE_FETCHER_DIGEST"
		PULL_POLICY=IfNotPresent
		return
	fi
	log "Loading the Runtime and fetcher images (make images)"
	local image
	for image in marimohub-runtime-ubi:dev marimohub-source-fetcher:dev; do
		docker image inspect "$image" >/dev/null 2>&1 || fail "$image is missing; run make images"
	done
	"$KIND" load docker-image marimohub-runtime-ubi:dev marimohub-source-fetcher:dev --name "$CLUSTER" >/dev/null
	RUNTIME_IMAGE=marimohub-runtime-ubi:dev
	FETCHER_IMAGE=marimohub-source-fetcher:dev
	PULL_POLICY=Never
}

install_platform() {
	log "helm install marimohub-platform (kind profile)"
	h install marimohub-platform "$PLATFORM" -n marimohub-platform --create-namespace \
		-f "$PLATFORM/values-kind.yaml" \
		--set-json "storage.workspaces.supplementalGroups=[$WORKSPACE_GID]" \
		--set-json "storage.shares[0].supplementalGroups=[$SHARE_GID]" \
		--wait --timeout 8m >/dev/null
	ok "installed"
	platform_tests
}

platform_tests() {
	if ! h test marimohub-platform -n marimohub-platform >"$WORK/helm-test.log" 2>&1; then
		cat "$WORK/helm-test.log" >&2
		k -n marimohub-sessions logs marimohub-platform-storage-test >&2 || true
		k -n marimohub-database logs marimohub-platform-database-test >&2 || true
		fail "helm test marimohub-platform"
	fi
	ok "helm test: storage claims and the evaluation database"
}

check_platform_objects() {
	log "Namespaces, claims and Secrets"
	KUBECTL="$KUBECTL" hack/preflight/check-namespaces.sh --kubeconfig "$KCFG" \
		marimohub marimohub-controller marimohub-sessions \
		pod-security.kubernetes.io/enforce=restricted pod-security.kubernetes.io/audit=restricted pod-security.kubernetes.io/warn=restricted
	local claim
	for claim in marimohub-workspaces marimohub-nfs; do
		[ "$(k -n marimohub-sessions get pvc "$claim" -o jsonpath='{.status.phase}')" = Bound ] || fail "claim $claim is not Bound"
	done
	ok "claims Bound"
	local args
	while read -r args; do
		# shellcheck disable=SC2086
		KUBECTL="$KUBECTL" hack/preflight/check-dependencies.sh --kubeconfig "$KCFG" $args
	done <<'EOF'
marimohub marimohub-backend-env DATABASE_URL SECRET_KEY
marimohub marimohub-database-ca ca.crt
marimohub marimohub-frontend-tls tls.crt tls.key ca.crt
marimohub marimohub-backend-public-tls tls.crt tls.key ca.crt
marimohub marimohub-backend-internal-tls tls.crt tls.key ca.crt
marimohub-sessions marimohub-internal-api-ca ca.crt
EOF
	ok "preflight scripts read dotted keys and labels"
}

check_upgrade_keeps_secrets() {
	log "helm upgrade keeps generated Secrets and PostgreSQL"
	secret_hashes >"$WORK/hashes-install"
	local pod_uid
	pod_uid="$(k -n marimohub-database get pod marimohub-postgresql-0 -o jsonpath='{.metadata.uid}')"
	h upgrade marimohub-platform "$PLATFORM" -n marimohub-platform --reuse-values --wait --timeout 8m >/dev/null
	secret_hashes >"$WORK/hashes-upgrade"
	diff "$WORK/hashes-install" "$WORK/hashes-upgrade" || fail "a generated Secret changed on upgrade"
	ok "Secret data unchanged across upgrade"
	[ "$(k -n marimohub-database get pod marimohub-postgresql-0 -o jsonpath='{.metadata.uid}')" = "$pod_uid" ] ||
		fail "the upgrade restarted PostgreSQL"
	ok "PostgreSQL not restarted"
}

check_database_policy() {
	log "Evaluation database: verified TLS, app role, NetworkPolicy"
	local out
	out="$(db_client db-allowed backend-public "select current_user || ',' || usesuper || ',' || ssl from pg_user join pg_stat_ssl on pid = pg_backend_pid() where usename = current_user")" ||
		fail "an app client could not reach the database: $out"
	[ "$out" = "marimohub,false,true" ] || fail "want marimohub,false,true (non-superuser over TLS), got: $out"
	ok "backend-public label: connected as marimohub, not a superuser, over verified TLS"
	if out="$(db_client db-denied frontend "select 1")"; then
		fail "a frontend-labelled Pod reached the database"
	fi
	ok "frontend label: refused by NetworkPolicy"
	k -n marimohub delete pod db-allowed db-denied --wait=false >/dev/null
}

app_image_values() {
	# shellcheck source=/dev/null
	source "$IMAGES_ENV"
	cat >"$WORK/images.yaml" <<EOF
frontend:
  image: { repository: $IMAGE_REGISTRY/marimohub-frontend, digest: $FRONTEND_DIGEST }
backendPublic:
  image: { repository: $IMAGE_REGISTRY/marimohub-backend, digest: $BACKEND_DIGEST }
backendInternal:
  image: { repository: $IMAGE_REGISTRY/marimohub-backend, digest: $BACKEND_DIGEST }
operator:
  image: { repository: $IMAGE_REGISTRY/marimohub-operator, digest: $OPERATOR_DIGEST }
runtime:
  images:
    ubi: $IMAGE_REGISTRY/marimohub-runtime-ubi@$RUNTIME_UBI_DIGEST
    ubuntu: $IMAGE_REGISTRY/marimohub-runtime-ubuntu@$RUNTIME_UBUNTU_DIGEST
  fetcherImage: { repository: $IMAGE_REGISTRY/marimohub-source-fetcher, digest: $SOURCE_FETCHER_DIGEST }
EOF
}

install_app() {
	k -n marimohub get configmap marimohub-platform-values -o jsonpath='{.data.values\.yaml}' >"$WORK/platform-values.yaml"
	if [ -z "$IMAGES_ENV" ]; then
		log "helm install marimohub --no-hooks (admission only; its images may not pull)"
		h install marimohub "$APP" -n marimohub --no-hooks -f "$APP/values-kind.yaml" -f "$WORK/platform-values.yaml" \
			--set ingress.host="$PUBLIC_HOST" >/dev/null
		local _
		for _ in $(seq 60); do
			[ "$(k -n marimohub get pods -l app.kubernetes.io/instance=marimohub --no-headers 2>/dev/null | wc -l)" -ge 3 ] &&
				[ "$(k -n marimohub-controller get pods --no-headers 2>/dev/null | wc -l)" -ge 1 ] && break
			sleep 2
		done
	else
		log "ingress-nginx $INGRESS_NGINX_VERSION"
		k apply -f "https://raw.githubusercontent.com/kubernetes/ingress-nginx/${INGRESS_NGINX_COMMIT}/deploy/static/provider/kind/deploy.yaml" >/dev/null
		k -n ingress-nginx rollout status deployment/ingress-nginx-controller --timeout=300s >/dev/null
		log "helm install marimohub (local images, migration hook against the evaluation database)"
		app_image_values
		h install marimohub "$APP" -n marimohub -f "$APP/values-kind.yaml" -f "$WORK/platform-values.yaml" \
			-f "$WORK/images.yaml" --set ingress.host="$PUBLIC_HOST" --wait --timeout 15m >/dev/null
		ok "installed; migration hook succeeded and every Deployment is Ready"
	fi
	local events
	events="$(k get events -A --field-selector reason=FailedCreate -o jsonpath='{range .items[*]}{.involvedObject.namespace}/{.involvedObject.name}: {.message}{"\n"}{end}' |
		grep -E '^marimohub' || true)"
	[ -z "$events" ] || fail "Pod creation was refused:
$events"
	[ "$(k -n marimohub get pods -l app.kubernetes.io/instance=marimohub --no-headers | wc -l)" -ge 3 ] || fail "app Pods were not created"
	ok "Pod Security restricted admitted every app Pod (no FailedCreate events)"
}

run_app_smoke() {
	[ -n "$IMAGES_ENV" ] || return 0
	log "App smoke through the Ingress at https://$PUBLIC_HOST:$HTTPS_PORT"
	k -n marimohub-platform get secret marimohub-platform-ca -o jsonpath='{.data.tls\.crt}' | base64 -d >"$WORK/ca.crt"
	local _
	for _ in $(seq 30); do
		curl -fsS --cacert "$WORK/ca.crt" "https://$PUBLIC_HOST:$HTTPS_PORT/api/health" >/dev/null 2>&1 && break
		sleep 2
	done
	curl -fsS --cacert "$WORK/ca.crt" "https://$PUBLIC_HOST:$HTTPS_PORT/api/health" >/dev/null || fail "/api/health is not answering"
	ok "/api/health answers over TLS with the platform CA"
	local env_url
	env_url="$(k -n marimohub get deploy marimohub-backend-public -o jsonpath='{.spec.template.spec.containers[0].env[?(@.name=="PUBLIC_API_URL")].value}')"
	[ "$env_url" = "https://$PUBLIC_HOST" ] || fail "backend-public PUBLIC_API_URL is '$env_url'"
	ok "backend-public PUBLIC_API_URL is https://$PUBLIC_HOST without a PUBLIC_API_URL in the Secret"
	# shellcheck disable=SC2086
	${SESSION_PYTHON:-uv run --project backend python} hack/kind/load_sessions.py --users 2 --hold-seconds 5 \
		--base-url "https://$PUBLIC_HOST:$HTTPS_PORT" --ca "$WORK/ca.crt" --data-dir "$WORK/data" ||
		fail "notebook Sessions did not complete"
	ok "two users ran edit Sessions that wrote into their Workspace directories"
	k -n marimohub create job smoke-reconcile --from=cronjob/marimohub-reconcile-runtimes >/dev/null
	k -n marimohub wait --for=condition=Complete job/smoke-reconcile --timeout=300s >/dev/null ||
		fail "the reconcile-runtimes CronJob's Pod did not complete"
	ok "reconcile-runtimes CronJob reaches the database and the API server"
}

check_runtime_shaped_pod() {
	log "Runtime-shaped Pod with an arbitrary UID in marimohub-sessions"
	local workspace_id=3f1c6a8e-1b2c-4d5e-8f90-a1b2c3d4e5f6 phase
	sed -e "s|@RUNTIME_IMAGE@|$RUNTIME_IMAGE|; s|@FETCHER_IMAGE@|$FETCHER_IMAGE|; s|@PULL_POLICY@|$PULL_POLICY|g" \
		-e "s|@WORKSPACE_ID@|$workspace_id|g; s|@WORKSPACE_GID@|$WORKSPACE_GID|; s|@SHARE_GID@|$SHARE_GID|" \
		hack/chart-tests/fixtures/runtime-shaped-pod.yaml | k apply -f - >/dev/null
	phase="$(wait_pod marimohub-sessions runtime-shaped)"
	k -n marimohub-sessions logs runtime-shaped -c marimo >"$WORK/runtime-shaped.log" 2>&1 || true
	sed 's/^/     /' "$WORK/runtime-shaped.log"
	[ "$phase" = Succeeded ] || fail "the Runtime-shaped Pod ended $phase"
	grep -q 'share write: .*Read-only file system' "$WORK/runtime-shaped.log" || fail "the share was not read-only"
	grep -q 'share read: region,sales' "$WORK/runtime-shaped.log" || fail "the share was not readable through its group"
	local stat
	stat="$(docker exec "$NODE" stat -c '%a %g %u' "/var/lib/marimohub/data/workspaces/$workspace_id")"
	[ "$stat" = "2770 $WORKSPACE_GID 1000770000" ] || fail "workspace directory is '$stat', want '2770 $WORKSPACE_GID 1000770000'"
	ok "workspace-init created workspaces/<id> as 2770, group $WORKSPACE_GID; marimo wrote it and read the share at /data1/nfs"
	k -n marimohub-sessions delete pod runtime-shaped --wait=false >/dev/null
}

check_admission_policy() {
	log "Admission policy: identity labels must match the spec"
	local id=3f1c6a8e-1b2c-4d5e-8f90-a1b2c3d4e5f6 notebook=11111111-1111-4111-8111-111111111111 workspace=22222222-2222-4222-8222-222222222222
	session() {
		cat <<EOF
apiVersion: marimohub.io/v1alpha1
kind: MarimoSession
metadata:
  name: $id
  namespace: marimohub-sessions
  labels: { marimohub.io/notebook: "$notebook", marimohub.io/workspace: "$workspace", marimohub.io/mode: "$1" }
spec:
  notebookId: "$notebook"
  workspaceId: "$workspace"
  mode: edit
  image: example.invalid/runtime@sha256:$(printf '0%.0s' $(seq 64))
  baseUrl: /api/proxy/$id
EOF
	}
	session edit | k apply --dry-run=server -f - >/dev/null || fail "a MarimoSession with matching labels was denied"
	ok "matching labels admitted"
	if session run | k apply --dry-run=server -f - >"$WORK/denied.log" 2>&1; then
		fail "a MarimoSession whose mode label disagrees with spec.mode was admitted"
	fi
	grep -q 'marimohub.io/mode label must equal spec.mode' "$WORK/denied.log" || fail "unexpected denial: $(cat "$WORK/denied.log")"
	ok "mismatched mode label denied by the ValidatingAdmissionPolicy"
}

check_crd_upgrade() {
	log "helm upgrade applies CRD changes"
	cp -r "$PLATFORM" "$WORK/platform-next"
	python3 - "$WORK/platform-next/files/marimohub.io_marimosessions.yaml" <<'EOF'
import sys
import yaml
path = sys.argv[1]
crd = yaml.safe_load(open(path))
crd["spec"]["versions"][0]["additionalPrinterColumns"].append({"name": "Workspace", "type": "string", "jsonPath": ".spec.workspaceId"})
yaml.safe_dump(crd, open(path, "w"))
EOF
	h upgrade marimohub-platform "$WORK/platform-next" -n marimohub-platform --reuse-values --wait --timeout 8m >/dev/null
	k get crd marimosessions.marimohub.io -o jsonpath='{.spec.versions[0].additionalPrinterColumns[*].name}' | grep -qw Workspace ||
		fail "the upgraded CRD has no Workspace column"
	ok "a new printer column reached the live CRD"
	h upgrade marimohub-platform "$PLATFORM" -n marimohub-platform --reuse-values --wait --timeout 8m >/dev/null
	if k get crd marimosessions.marimohub.io -o jsonpath='{.spec.versions[0].additionalPrinterColumns[*].name}' | grep -qw Workspace; then
		fail "upgrading back did not remove the column"
	fi
	ok "upgrading back removed it"
}

check_openshift_shapes() {
	log "OpenShift Route and ServiceMonitor objects against the real CRDs"
	k apply --server-side -f "https://raw.githubusercontent.com/openshift/api/${OPENSHIFT_API_COMMIT}/route/v1/zz_generated.crd-manifests/routes.crd.yaml" >/dev/null
	k apply --server-side -f "https://raw.githubusercontent.com/prometheus-operator/prometheus-operator/${PROMETHEUS_OPERATOR_VERSION}/example/prometheus-operator-crd/monitoring.coreos.com_servicemonitors.yaml" >/dev/null
	k wait --for=condition=Established crd/routes.route.openshift.io crd/servicemonitors.monitoring.coreos.com --timeout=60s >/dev/null
	"$HELM" template ocp-shape "$APP" -n marimohub -f "$APP/values-openshift.yaml" -f hack/chart-tests/values-openshift-ci.yaml \
		--set routes.tls.externalCertificateSecretName=marimohub-public-tls --kube-version 1.35.0 >"$WORK/openshift-render.yaml"
	k apply --dry-run=server -f "$WORK/openshift-render.yaml" >"$WORK/openshift-apply.log" 2>&1 ||
		fail "the API server refused the openshift render: $(cat "$WORK/openshift-apply.log")"
	! grep -qi 'PodSecurity' "$WORK/openshift-apply.log" || fail "PodSecurity warnings: $(grep -i PodSecurity "$WORK/openshift-apply.log")"
	ok "all $(grep -c '(server dry run)' "$WORK/openshift-apply.log") objects of the openshift render accepted, with no PodSecurity warning"
	python3 - "$WORK/openshift-render.yaml" >"$WORK/route-both.yaml" <<'EOF'
import sys
import yaml
for doc in yaml.safe_load_all(open(sys.argv[1])):
    if doc and doc["kind"] == "Route" and doc["spec"]["path"] == "/api":
        doc["spec"]["tls"]["certificate"] = "-----BEGIN CERTIFICATE-----\nplaceholder\n-----END CERTIFICATE-----\n"
        print(yaml.safe_dump(doc))
EOF
	if k apply --dry-run=server -f "$WORK/route-both.yaml" >/dev/null 2>&1; then
		fail "the Route CRD accepted certificate together with externalCertificate"
	fi
	ok "the Route CRD's own rules run (certificate with externalCertificate refused)"
}

check_openshift_platform_profile() {
	log "openshift platform profile against the cluster (helm --dry-run=server)"
	local args=(upgrade marimohub-platform "$PLATFORM" -n marimohub-platform --dry-run=server
		-f "$PLATFORM/values-openshift.yaml" -f hack/chart-tests/platform-values-openshift-ci.yaml)
	if h "${args[@]}" >"$WORK/dry.log" 2>&1; then fail "rendered without the database password Secret"; fi
	grep -q 'database.passwordSecret: Secret marimohub-platform/marimohub-db-password with key password was not found' "$WORK/dry.log" ||
		fail "unexpected error: $(cat "$WORK/dry.log")"
	ok "a missing database password Secret fails with a clear message"
	(umask 077 && openssl rand -hex 24 >"$WORK/db-password")
	k -n marimohub-platform create secret generic marimohub-db-password --from-file=password="$WORK/db-password" >/dev/null
	if h "${args[@]}" >"$WORK/dry.log" 2>&1; then fail "rendered without the OpenShift service CA"; fi
	grep -q 'could not read ConfigMap openshift-config-managed/service-ca' "$WORK/dry.log" || fail "unexpected error: $(cat "$WORK/dry.log")"
	ok "a missing service CA fails with a clear message"
	openssl req -x509 -newkey ec -pkeyopt ec_paramgen_curve:P-256 -nodes -days 1 -subj /CN=test-service-ca \
		-keyout "$WORK/service-ca.key" -out "$WORK/service-ca.crt" 2>/dev/null
	h "${args[@]}" --set-file internalApiCA.serviceCABundle="$WORK/service-ca.crt" >"$WORK/dry.log" 2>&1 ||
		fail "the openshift profile did not render: $(cat "$WORK/dry.log")"
	local url
	url="$(python3 - "$WORK/dry.log" <<'EOF'
import base64
import sys
import yaml
text = open(sys.argv[1]).read().split("MANIFEST:", 1)[1].split("NOTES:", 1)[0]
for doc in yaml.safe_load_all(text):
    if doc and doc["kind"] == "Secret" and doc["metadata"]["name"] == "marimohub-backend-env":
        url = base64.b64decode(doc["data"]["DATABASE_URL"]).decode()
        scheme, rest = url.split("://", 1)
        user, host = rest.split("@", 1)
        print(f"{scheme}://{user.split(':')[0]}:<redacted>@{host}")
EOF
)"
	[ "$url" = "postgresql+asyncpg://marimohub:<redacted>@db.example.internal:5432/marimohub" ] || fail "DATABASE_URL renders as $url"
	ok "DATABASE_URL built from the password Secret: $url"
	k -n marimohub-platform delete secret marimohub-db-password >/dev/null
}

check_uninstall_and_reinstall() {
	log "Uninstall keeps data; reinstall adopts it"
	db_client db-marker backend-public "create table if not exists install_test_marker (note text); insert into install_test_marker values ('kept'); select count(*) from install_test_marker" >/dev/null ||
		fail "could not write the marker row"
	k -n marimohub delete pod db-marker --wait=false >/dev/null
	if [ -n "$IMAGES_ENV" ]; then
		h uninstall marimohub -n marimohub --wait --timeout 5m >/dev/null # runs the pre-delete drain hook
	else
		h uninstall marimohub -n marimohub --no-hooks --wait --timeout 5m >/dev/null
	fi
	h uninstall marimohub-platform -n marimohub-platform --wait --timeout 5m >/dev/null
	local ns
	for ns in marimohub marimohub-controller marimohub-sessions marimohub-database; do
		k get namespace "$ns" >/dev/null || fail "namespace $ns was deleted"
	done
	k get crd marimosessions.marimohub.io >/dev/null || fail "the CRD was deleted"
	k get pv marimohub-sessions-marimohub-workspaces marimohub-sessions-marimohub-nfs >/dev/null || fail "a PersistentVolume was deleted"
	k -n marimohub-sessions get pvc marimohub-workspaces marimohub-nfs >/dev/null || fail "a claim was deleted"
	k -n marimohub-database get pvc data-marimohub-postgresql-0 >/dev/null || fail "the database volume was deleted"
	if k get validatingadmissionpolicy marimosession-label-identity.marimohub.io >/dev/null 2>&1; then
		fail "the admission policy survived uninstall"
	fi
	ok "namespaces, CRD, Secrets, volumes and claims kept; admission policy removed"
	secret_hashes >"$WORK/hashes-uninstalled"
	diff "$WORK/hashes-install" "$WORK/hashes-uninstalled" || fail "a generated Secret changed on uninstall"
	h install marimohub-platform "$PLATFORM" -n marimohub-platform \
		-f "$PLATFORM/values-kind.yaml" \
		--set-json "storage.workspaces.supplementalGroups=[$WORKSPACE_GID]" \
		--set-json "storage.shares[0].supplementalGroups=[$SHARE_GID]" \
		--wait --timeout 8m >/dev/null
	secret_hashes >"$WORK/hashes-reinstall"
	diff "$WORK/hashes-install" "$WORK/hashes-reinstall" || fail "a generated Secret changed on reinstall"
	ok "reinstall adopted every kept object; Secret data unchanged"
	platform_tests
	local out
	out="$(db_client db-marker backend-public "select count(*) from install_test_marker")" || fail "the marker table is gone: $out"
	[ "$out" = 1 ] || fail "want one marker row, got: $out"
	ok "database data kept across uninstall and reinstall"
}

main() {
	for tool in "$KIND" "$KUBECTL" "$HELM"; do
		[ -x "$tool" ] || fail "$tool is missing; run make bootstrap-tools"
	done
	command -v docker >/dev/null || fail "docker is required"
	command -v openssl >/dev/null || fail "openssl is required"
	if [ -n "$IMAGES_ENV" ]; then
		[ -f "$IMAGES_ENV" ] || fail "IMAGES_ENV=$IMAGES_ENV does not exist"
		getent hosts "$PUBLIC_HOST" | grep -q '^127\.0\.0\.1' || fail "$PUBLIC_HOST must resolve to 127.0.0.1 (/etc/hosts)"
	fi
	create_cluster
	load_images
	install_platform
	check_platform_objects
	check_upgrade_keeps_secrets
	check_database_policy
	install_app
	run_app_smoke
	check_runtime_shaped_pod
	check_admission_policy
	check_crd_upgrade
	check_openshift_shapes
	check_openshift_platform_profile
	check_uninstall_and_reinstall
	log "platform install test passed"
}

main "$@"
