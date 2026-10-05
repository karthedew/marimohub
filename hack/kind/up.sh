#!/usr/bin/env bash
#
# Brings up, or converges, a complete local MarimoHub on kind with the real
# Kubernetes Runtime backend: the operator turns every Session and Deployment
# into a Runtime Pod, and edit/run Runtimes mount their Workspace directory
# from a host directory standing in for an NFS export.
#
# Idempotent: every step checks what already exists, so rerunning after a
# code change rebuilds and pushes images, then rolls the Helm release to the
# new digests. Everything it creates outside the cluster (the local registry,
# the PostgreSQL fixture, generated certificates and secrets under .kind/) is
# named so hack/kind/down.sh can remove exactly that and nothing else.
#
# Environment:
#   CLUSTER_NAME        kind cluster name (default: marimohub)
#   MARIMOHUB_NFS_DIR   host directory standing in for a shared NFS export
#                       (default: /data1/nfs). It is mounted whole into every
#                       notebook at this same path, read-only unless
#                       MARIMOHUB_NFS_WRITABLE=1 (which also keeps it out of
#                       Deployments, since those are public).
#   MARIMOHUB_DATA_DIR  host directory for MarimoHub's own Workspace Files
#                       (default: /data1/marimohub). Each Workspace gets
#                       workspaces/<workspace-id>, which its notebooks see as
#                       /work/workspace. Kept off the NFS mock, so no notebook
#                       can see another Workspace's files.
#   MARIMOHUB_HOST      public hostname (default: the one the last run used,
#                       saved in .kind/host, else localhost). A name other than
#                       localhost must resolve to 127.0.0.1 on this machine,
#                       for example through an /etc/hosts line. Google accepts
#                       OAuth redirect URIs only on localhost or on a domain
#                       under a public suffix, such as marimohub.example.com
#   GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET
#                       optional Google OAuth "Web application" client, which
#                       turns on Sign in with Google (see README.md). Saved in
#                       .kind/google-oauth.env; a later run falls back to the
#                       saved value of each variable it is not given. Set both
#                       to empty to turn Google sign-in off again.
#   GOOGLE_HOSTED_DOMAIN
#                       optional Google Workspace domain: only its accounts
#                       can then sign in with Google (saved the same way)
#   SKIP_BUILD=1        push the existing marimohub-*:dev images instead of
#                       rebuilding them
set -Eeuo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
# shellcheck source=../tools/versions.env
source "$REPO_ROOT/hack/tools/versions.env"

CLUSTER_NAME="${CLUSTER_NAME:-marimohub}"
NFS_DIR="${MARIMOHUB_NFS_DIR:-/data1/nfs}"
NFS_DIR="${NFS_DIR%/}"
NFS_WRITABLE="${MARIMOHUB_NFS_WRITABLE:-0}"
DATA_DIR="${MARIMOHUB_DATA_DIR:-/data1/marimohub}"
DATA_DIR="${DATA_DIR%/}"
PUBLIC_HOST="${MARIMOHUB_HOST:-$(cat "$REPO_ROOT/.kind/host" 2>/dev/null || true)}"
PUBLIC_HOST="${PUBLIC_HOST:-localhost}"
SKIP_BUILD="${SKIP_BUILD:-0}"

RELEASE=marimohub
APP_NS=marimohub
SESSIONS_NS=marimohub-sessions
REGISTRY_NAME=marimohub-registry
REGISTRY_PORT=5001
REGISTRY="localhost:${REGISTRY_PORT}"
POSTGRES_NAME=marimohub-kind-postgres
POSTGRES_VOLUME=marimohub-kind-pgdata
# Where the kind node sees NFS_DIR and DATA_DIR. Pods never see these paths:
# they mount the PersistentVolumes below, the same way they would mount real
# NFS ones.
NODE_NFS_PATH=/var/lib/marimohub/nfs
NODE_DATA_PATH=/var/lib/marimohub/data
WORKSPACE_CLAIM=marimohub-workspaces
WORKSPACE_MOUNT_PATH=/work/workspace
# The whole NFS mock, one claim, mounted at NFS_DIR in every notebook.
NFS_CLAIM=marimohub-nfs

STATE_DIR="$REPO_ROOT/.kind"
TLS_DIR="$STATE_DIR/tls"
VALUES_FILE="$STATE_DIR/values-local.yaml"
CONTEXT="kind-${CLUSTER_NAME}"
# Sign in with Google: the saved client, the Secret only backend-public loads,
# and the hash of that Secret's content when backend-public last started.
GOOGLE_ENV_FILE="$STATE_DIR/google-oauth.env"
OIDC_SECRET="${RELEASE}-backend-oidc"
OIDC_HASH_FILE="$STATE_DIR/backend-oidc.sha256"
GOOGLE_REDIRECT_URI="https://${PUBLIC_HOST}/api/auth/oidc/google/callback"

KIND="$REPO_ROOT/.bin/kind"
KUBECTL="$REPO_ROOT/.bin/kubectl"
HELM="$REPO_ROOT/.bin/helm"

IMAGES=(backend frontend operator source-fetcher runtime-ubi runtime-ubuntu)

log() { printf '\n==> %s\n' "$*" >&2; }
warn() { printf 'kind-up: warning: %s\n' "$*" >&2; }
die() {
	printf 'kind-up: %s\n' "$*" >&2
	exit 1
}
kubectl() { "$KUBECTL" --context "$CONTEXT" "$@"; }
apply_stdin() { kubectl apply -f - >/dev/null; }

trap 'die "failed at line $LINENO (exit $?): $BASH_COMMAND"' ERR

preflight() {
	log "Checking prerequisites"
	command -v docker >/dev/null || die "docker is required"
	docker info >/dev/null 2>&1 || die "docker daemon is not reachable"
	command -v openssl >/dev/null || die "openssl is required"
	for tool in "$KIND" "$KUBECTL" "$HELM"; do
		[ -x "$tool" ] || die "$tool is missing; run 'make bootstrap-tools'"
	done
	case "$NFS_DIR" in
	/*) ;;
	*) die "MARIMOHUB_NFS_DIR must be an absolute path, got '$NFS_DIR'" ;;
	esac
	case "$NFS_DIR/" in
	/work/* | /tmp/* | /home/marimo/* | /cache/* | /var/run/secrets/*)
		die "MARIMOHUB_NFS_DIR '$NFS_DIR' would hide a directory every notebook needs; choose another path" ;;
	esac
	[ -d "$NFS_DIR" ] && [ -r "$NFS_DIR" ] || die "$NFS_DIR must exist and be readable by $(id -un)"
	# Runtime Pods join the export's group, so group-readable (and, when
	# writable, group-writable) files work without matching any UID.
	NFS_GID="$(stat -c %g "$NFS_DIR")"
	if [ "$NFS_WRITABLE" = 1 ] && [ "$NFS_GID" = 0 ]; then
		die "MARIMOHUB_NFS_WRITABLE=1 needs $NFS_DIR to have a non-root group the notebooks can join"
	fi

	# Workspace Files live under DATA_DIR, never on the NFS mock. Its
	# workspaces/ directory is the one storage prerequisite the chart
	# documents: group-writable, setgid so every Workspace directory and file
	# keeps the directory's group, which Runtime Pods join through
	# runtime.workspaceStorage.supplementalGroups.
	install -d -m 2775 "$DATA_DIR/workspaces"
	if [ -O "$DATA_DIR/workspaces" ]; then chmod 2775 "$DATA_DIR/workspaces"; fi
	WORKSPACE_GID="$(stat -c %g "$DATA_DIR/workspaces")"
	[ "$WORKSPACE_GID" != 0 ] || die "$DATA_DIR/workspaces is owned by group 0; give it a non-root group"

	local instances
	instances="$(sysctl -n fs.inotify.max_user_instances 2>/dev/null || echo 0)"
	if [ "$instances" -lt 512 ]; then
		printf 'kind-up: warning: fs.inotify.max_user_instances=%s; kind recommends 512 for many Pods:\n' "$instances" >&2
		printf '  sudo sysctl fs.inotify.max_user_instances=512 fs.inotify.max_user_watches=524288\n' >&2
	fi
	mkdir -p "$STATE_DIR" "$TLS_DIR"
	chmod 700 "$STATE_DIR"
	# The host goes into certificates, the Ingress and every public URL.
	[[ "$PUBLIC_HOST" =~ ^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?(\.[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?)*$ ]] ||
		die "MARIMOHUB_HOST must be a lowercase DNS name, got '$PUBLIC_HOST'"
	printf '%s\n' "$PUBLIC_HOST" >"$STATE_DIR/host"
}

# saved_google_value KEY prints KEY's value from .kind/google-oauth.env, or
# nothing. The file is parsed, never sourced.
saved_google_value() {
	local line value=""
	if [ -s "$GOOGLE_ENV_FILE" ]; then
		while IFS= read -r line || [ -n "$line" ]; do
			case "$line" in "$1="*) value="${line#*=}" ;; esac
		done <"$GOOGLE_ENV_FILE"
	fi
	printf '%s' "$value"
}

# google_accepts_host HOST succeeds when Google would accept an OAuth
# redirect URI on HOST here: localhost, or a domain under a public suffix. It
# fails for reserved and private-use names (*.localhost, *.local, *.test, ...),
# single labels, and IP addresses, which an Ingress host cannot be. See
# https://developers.google.com/identity/protocols/oauth2/web-server#uri-validation
google_accepts_host() {
	case "$1" in
	localhost) return 0 ;;
	*.localhost | *.local | *.test | *.example | *.invalid | *.internal | *.lan | *.home | *.corp | *.home.arpa) return 1 ;;
	*[!0-9.]*.*) return 0 ;;
	*) return 1 ;;
	esac
}

warn_google_host() {
	warn "Google will refuse the redirect URI ${GOOGLE_REDIRECT_URI}: it accepts only localhost or a domain under a public suffix, added under Branding > Authorized domains. Use MARIMOHUB_HOST=localhost, or a domain you own that resolves to 127.0.0.1."
}

# resolve_google_oauth settles the optional Google OAuth client. Each variable
# left unset falls back to the value an earlier run saved, so credentials are
# passed once; GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET both empty turn
# Google sign-in off and forget the saved client. Sets GOOGLE_ENABLED,
# OIDC_ENV (the KEY=VALUE lines of the backend's OIDC Secret, empty while
# Google is off), and OIDC_SECRET_HASH.
resolve_google_oauth() {
	local domain_given="${GOOGLE_HOSTED_DOMAIN:+1}" name
	GOOGLE_CLIENT_ID="${GOOGLE_CLIENT_ID-$(saved_google_value GOOGLE_CLIENT_ID)}"
	GOOGLE_CLIENT_SECRET="${GOOGLE_CLIENT_SECRET-$(saved_google_value GOOGLE_CLIENT_SECRET)}"
	GOOGLE_HOSTED_DOMAIN="${GOOGLE_HOSTED_DOMAIN-$(saved_google_value GOOGLE_HOSTED_DOMAIN)}"
	for name in GOOGLE_CLIENT_ID GOOGLE_CLIENT_SECRET GOOGLE_HOSTED_DOMAIN; do
		case "${!name}" in *[[:space:]]*) die "$name contains whitespace; copy the value exactly as Google shows it" ;; esac
	done

	GOOGLE_ENABLED=0
	OIDC_ENV=""
	if [ -z "${GOOGLE_CLIENT_ID}${GOOGLE_CLIENT_SECRET}" ]; then
		if [ -n "$domain_given" ]; then
			warn "GOOGLE_HOSTED_DOMAIN is ignored without GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET"
		fi
		GOOGLE_HOSTED_DOMAIN=""
		rm -f "$GOOGLE_ENV_FILE"
	elif [ -z "$GOOGLE_CLIENT_ID" ] || [ -z "$GOOGLE_CLIENT_SECRET" ]; then
		die "set both GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET, or neither"
	else
		GOOGLE_ENABLED=1
		case "$GOOGLE_CLIENT_ID" in
		*.apps.googleusercontent.com) ;;
		*) warn "GOOGLE_CLIENT_ID does not end in .apps.googleusercontent.com; check it is the client ID" ;;
		esac
		OIDC_ENV="GOOGLE_CLIENT_ID=${GOOGLE_CLIENT_ID}"$'\n'"GOOGLE_CLIENT_SECRET=${GOOGLE_CLIENT_SECRET}"
		if [ -n "$GOOGLE_HOSTED_DOMAIN" ]; then
			OIDC_ENV+=$'\n'"GOOGLE_HOSTED_DOMAIN=${GOOGLE_HOSTED_DOMAIN}"
		fi
		(
			umask 077
			printf '# Google OAuth client saved by hack/kind/up.sh for later runs.\n# Delete this file to turn Google sign-in off.\n%s\n' \
				"$OIDC_ENV" >"$GOOGLE_ENV_FILE.tmp"
			mv -f "$GOOGLE_ENV_FILE.tmp" "$GOOGLE_ENV_FILE"
		)
		chmod 600 "$GOOGLE_ENV_FILE"
		google_accepts_host "$PUBLIC_HOST" || warn_google_host
	fi
	OIDC_SECRET_HASH="$(printf '%s' "$OIDC_ENV" | sha256sum | cut -d' ' -f1)"
}

ensure_registry() {
	log "Local image registry ($REGISTRY)"
	if [ "$(docker inspect -f '{{.State.Running}}' "$REGISTRY_NAME" 2>/dev/null || true)" != true ]; then
		if docker inspect "$REGISTRY_NAME" >/dev/null 2>&1; then
			docker start "$REGISTRY_NAME" >/dev/null
		else
			docker run -d --restart=always -p "127.0.0.1:${REGISTRY_PORT}:5000" --network bridge \
				--name "$REGISTRY_NAME" "$LOCAL_REGISTRY_IMAGE" >/dev/null
		fi
	fi
}

ensure_cluster() {
	log "kind cluster '$CLUSTER_NAME'"
	if ! "$KIND" get clusters 2>/dev/null | grep -qx "$CLUSTER_NAME"; then
		"$KIND" create cluster --name "$CLUSTER_NAME" --image "${KIND_NODE_IMAGE}@${KIND_NODE_DIGEST}" \
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
      - { containerPort: 80, hostPort: 80, listenAddress: "127.0.0.1", protocol: TCP }
      - { containerPort: 443, hostPort: 443, listenAddress: "127.0.0.1", protocol: TCP }
    extraMounts:
      - hostPath: "$NFS_DIR"
        containerPath: "$NODE_NFS_PATH"
      - hostPath: "$DATA_DIR"
        containerPath: "$NODE_DATA_PATH"
EOF
	fi
	local mounted
	# kind fixes a node's host mounts when it creates the cluster, so a
	# cluster made with other directories must be recreated. Users and
	# notebooks live in the PostgreSQL fixture outside the cluster and survive
	# that.
	local mounted want node_path
	for want in "$NFS_DIR:$NODE_NFS_PATH" "$DATA_DIR:$NODE_DATA_PATH"; do
		node_path="${want#*:}"
		mounted="$(docker inspect -f '{{range .Mounts}}{{if eq .Destination "'"$node_path"'"}}{{.Source}}{{end}}{{end}}' "${CLUSTER_NAME}-control-plane")"
		[ "$mounted" = "${want%%:*}" ] ||
			die "cluster '$CLUSTER_NAME' mounts '${mounted:-nothing}' at $node_path, not ${want%%:*}; recreate it with 'make kind-down && make kind-up' (users and notebooks are kept)"
	done

	# Containerd on the node resolves localhost:5001 to the registry
	# container, since localhost inside the node is the node itself.
	if [ "$(docker inspect -f '{{json .NetworkSettings.Networks.kind}}' "$REGISTRY_NAME")" = null ]; then
		docker network connect kind "$REGISTRY_NAME"
	fi
	local node
	for node in $("$KIND" get nodes --name "$CLUSTER_NAME"); do
		docker exec "$node" mkdir -p "/etc/containerd/certs.d/${REGISTRY}"
		printf '[host."http://%s:5000"]\n' "$REGISTRY_NAME" |
			docker exec -i "$node" cp /dev/stdin "/etc/containerd/certs.d/${REGISTRY}/hosts.toml"
	done
	apply_stdin <<EOF
apiVersion: v1
kind: ConfigMap
metadata:
  name: local-registry-hosting
  namespace: kube-public
data:
  localRegistryHosting.v1: |
    host: "${REGISTRY}"
    help: "https://kind.sigs.k8s.io/docs/user/local-registry/"
EOF
}

ensure_postgres() {
	log "PostgreSQL fixture ($POSTGRES_NAME, outside the chart)"
	local password_file="$STATE_DIR/postgres-password"
	[ -s "$password_file" ] || (umask 077 && openssl rand -hex 24 >"$password_file")
	POSTGRES_PASSWORD="$(cat "$password_file")"
	if [ "$(docker inspect -f '{{.State.Running}}' "$POSTGRES_NAME" 2>/dev/null || true)" != true ]; then
		if docker inspect "$POSTGRES_NAME" >/dev/null 2>&1; then
			docker start "$POSTGRES_NAME" >/dev/null
		else
			# The password is read through a pipe, never from a command line.
			docker run -d --restart=unless-stopped --network kind --name "$POSTGRES_NAME" \
				-e POSTGRES_USER=marimohub -e POSTGRES_DB=marimohub \
				--env-file <(printf 'POSTGRES_PASSWORD=%s\n' "$POSTGRES_PASSWORD") \
				-v "${POSTGRES_VOLUME}:/var/lib/postgresql/data" "$LOCAL_POSTGRES_IMAGE" >/dev/null
		fi
	fi
	local _
	for _ in $(seq 60); do
		docker exec "$POSTGRES_NAME" pg_isready -q -U marimohub -d marimohub && break
		sleep 1
	done
	docker exec "$POSTGRES_NAME" pg_isready -q -U marimohub -d marimohub || die "PostgreSQL did not become ready"
	POSTGRES_IP="$(docker inspect -f '{{(index .NetworkSettings.Networks "kind").IPAddress}}' "$POSTGRES_NAME")"
}

push_images() {
	if [ "$SKIP_BUILD" != 1 ]; then
		log "Building images"
		make -C "$REPO_ROOT" images ENGINE=docker
	fi
	log "Pushing images to $REGISTRY"
	local name ref digest
	: >"$STATE_DIR/images.env"
	for name in "${IMAGES[@]}"; do
		docker image inspect "marimohub-${name}:dev" >/dev/null 2>&1 ||
			die "marimohub-${name}:dev does not exist; rerun without SKIP_BUILD=1"
		ref="${REGISTRY}/marimohub-${name}:dev"
		docker tag "marimohub-${name}:dev" "$ref"
		digest="$(docker push "$ref" | awk '/digest: sha256:/ { print $3 }' | tail -1)"
		[[ "$digest" =~ ^sha256:[0-9a-f]{64}$ ]] || die "could not read the pushed digest for $ref"
		printf '%s=%s\n' "$(printf '%s' "$name" | tr 'a-z-' 'A-Z_')_DIGEST" "$digest" >>"$STATE_DIR/images.env"
	done
	# shellcheck source=/dev/null
	source "$STATE_DIR/images.env"
}

install_ingress() {
	log "ingress-nginx $INGRESS_NGINX_VERSION"
	kubectl apply -f "https://raw.githubusercontent.com/kubernetes/ingress-nginx/${INGRESS_NGINX_COMMIT}/deploy/static/provider/kind/deploy.yaml" >/dev/null
	kubectl -n ingress-nginx rollout status deployment/ingress-nginx-controller --timeout=300s >/dev/null
}

apply_prerequisites() {
	log "CRD, admission policy, and namespaces"
	kubectl apply --server-side --force-conflicts -f "$REPO_ROOT/deploy/crd/marimosession.yaml" >/dev/null
	kubectl apply -f "$REPO_ROOT/marimohub-operator/config/policy/marimosession_label_identity.yaml" >/dev/null
	kubectl apply -f "$REPO_ROOT/deploy/namespace/namespaces.yaml" >/dev/null
}

# sorted_sans SAN-LIST prints a comma-separated subjectAltName list sorted,
# the form cert_sans prints a certificate's own list in.
sorted_sans() { printf '%s\n' "$1" | tr ',' '\n' | sort | paste -sd, -; }

# cert_sans FILE prints FILE's subjectAltName entries, sorted and
# comma-separated, or nothing for a missing or unreadable certificate.
cert_sans() {
	{ openssl x509 -in "$1" -noout -ext subjectAltName 2>/dev/null || true; } |
		sed 1d | tr ',' '\n' | tr -d ' ' | sed '/^$/d' | sort | paste -sd, -
}

# leaf_cert NAME SAN-LIST issues one server certificate from the local CA,
# with the key identifiers Python's strict X.509 verification requires. An
# existing certificate is kept only while it names exactly SAN-LIST and still
# verifies against the CA, so a changed MARIMOHUB_HOST, an expired
# certificate, or a regenerated CA reissues it.
leaf_cert() {
	local name="$1" sans="$2"
	if [ -s "$TLS_DIR/$name.crt" ] && [ "$(cert_sans "$TLS_DIR/$name.crt")" = "$(sorted_sans "$sans")" ] &&
		openssl verify -CAfile "$TLS_DIR/ca.crt" "$TLS_DIR/$name.crt" >/dev/null 2>&1; then
		return 0
	fi
	openssl req -new -newkey ec -pkeyopt ec_paramgen_curve:P-256 -nodes \
		-keyout "$TLS_DIR/$name.key" -out "$TLS_DIR/$name.csr" -subj "/CN=$name" 2>/dev/null
	openssl x509 -req -in "$TLS_DIR/$name.csr" -CA "$TLS_DIR/ca.crt" -CAkey "$TLS_DIR/ca.key" \
		-CAcreateserial -days 397 -out "$TLS_DIR/$name.crt" -extfile <(printf '%s\n' \
			"subjectAltName=$sans" \
			"basicConstraints=critical,CA:FALSE" \
			"keyUsage=critical,digitalSignature" \
			"extendedKeyUsage=serverAuth" \
			"subjectKeyIdentifier=hash" \
			"authorityKeyIdentifier=keyid") 2>/dev/null
	rm -f "$TLS_DIR/$name.csr"
}

service_sans() {
	local svc="${RELEASE}-$1"
	printf 'DNS:%s,DNS:%s.%s,DNS:%s.%s.svc,DNS:%s.%s.svc.cluster.local' \
		"$svc" "$svc" "$APP_NS" "$svc" "$APP_NS" "$svc" "$APP_NS"
}

ensure_secrets() {
	log "TLS certificates and application Secrets"
	(
		umask 077
		if [ ! -s "$TLS_DIR/ca.crt" ]; then
			openssl req -x509 -new -newkey ec -pkeyopt ec_paramgen_curve:P-256 -nodes -days 825 \
				-keyout "$TLS_DIR/ca.key" -out "$TLS_DIR/ca.crt" -subj "/CN=MarimoHub local kind CA" \
				-addext "basicConstraints=critical,CA:TRUE" -addext "keyUsage=critical,keyCertSign,cRLSign" 2>/dev/null
		fi
		leaf_cert frontend "$(service_sans frontend),DNS:${PUBLIC_HOST}"
		leaf_cert backend-public "$(service_sans backend-public),DNS:${PUBLIC_HOST}"
		leaf_cert backend-internal "$(service_sans backend-internal)"
		[ -s "$STATE_DIR/secret-key" ] || openssl rand -hex 32 >"$STATE_DIR/secret-key"
	)

	local name
	for name in frontend backend-public backend-internal; do
		kubectl -n "$APP_NS" create secret tls "${RELEASE}-${name}-tls" \
			--cert "$TLS_DIR/$name.crt" --key "$TLS_DIR/$name.key" --dry-run=client -o yaml | apply_stdin
	done
	kubectl -n "$SESSIONS_NS" create secret generic "${RELEASE}-internal-api-ca" \
		--from-file=ca.crt="$TLS_DIR/ca.crt" --dry-run=client -o yaml | apply_stdin
	# The fixture is reached by container name: kind forwards Pod DNS
	# lookups to Docker's embedded resolver for the kind network. Read
	# through a pipe, so neither the database password nor SECRET_KEY
	# appears on a command line.
	local secret_key
	secret_key="$(cat "$STATE_DIR/secret-key")"
	kubectl -n "$APP_NS" create secret generic "${RELEASE}-backend-env" --from-env-file=<(printf '%s\n' \
		"DATABASE_URL=postgresql+asyncpg://marimohub:${POSTGRES_PASSWORD}@${POSTGRES_NAME}:5432/marimohub" \
		"SECRET_KEY=${secret_key}" \
		"PUBLIC_API_URL=https://${PUBLIC_HOST}") \
		--dry-run=client -o yaml | apply_stdin
	# Identity-provider credentials get a Secret of their own that only
	# backend-public loads (backendPublic.oidc.existingSecret); it is empty
	# while Google sign-in is off. Read through a pipe, so the client secret
	# never appears on a command line.
	kubectl -n "$APP_NS" create secret generic "$OIDC_SECRET" --from-env-file=<(printf '%s\n' "$OIDC_ENV") \
		--dry-run=client -o yaml | apply_stdin
}

ensure_workspace_storage() {
	log "Workspace storage ($DATA_DIR -> PersistentVolume ${WORKSPACE_CLAIM})"
	# A statically bound ReadWriteMany volume, exactly the shape a real NFS
	# export takes; only the volume source differs (hostPath here, `nfs:`
	# with a server and export path in production).
	apply_stdin <<EOF
apiVersion: v1
kind: PersistentVolume
metadata:
  name: ${WORKSPACE_CLAIM}
  labels:
    app.kubernetes.io/part-of: marimohub
spec:
  capacity:
    storage: 100Gi
  accessModes: ["ReadWriteMany"]
  persistentVolumeReclaimPolicy: Retain
  storageClassName: ""
  claimRef:
    namespace: ${SESSIONS_NS}
    name: ${WORKSPACE_CLAIM}
  hostPath:
    path: ${NODE_DATA_PATH}
    type: Directory
---
apiVersion: v1
kind: PersistentVolumeClaim
metadata:
  name: ${WORKSPACE_CLAIM}
  namespace: ${SESSIONS_NS}
spec:
  accessModes: ["ReadWriteMany"]
  storageClassName: ""
  volumeName: ${WORKSPACE_CLAIM}
  resources:
    requests:
      storage: 100Gi
EOF
}

ensure_nfs_volume() {
	log "Shared NFS mock ($NFS_DIR -> PersistentVolume ${NFS_CLAIM}, at $NFS_DIR in notebooks)"
	# Earlier versions of this script exposed only NFS_DIR/shares/<name>,
	# through a claim nothing references any more.
	kubectl -n "$SESSIONS_NS" delete pvc marimohub-shares --ignore-not-found >/dev/null
	kubectl delete pv marimohub-shares --ignore-not-found >/dev/null
	apply_stdin <<EOF
apiVersion: v1
kind: PersistentVolume
metadata:
  name: ${NFS_CLAIM}
  labels:
    app.kubernetes.io/part-of: marimohub
spec:
  capacity:
    storage: 100Gi
  accessModes: ["ReadWriteMany"]
  persistentVolumeReclaimPolicy: Retain
  storageClassName: ""
  claimRef:
    namespace: ${SESSIONS_NS}
    name: ${NFS_CLAIM}
  hostPath:
    path: ${NODE_NFS_PATH}
    type: Directory
---
apiVersion: v1
kind: PersistentVolumeClaim
metadata:
  name: ${NFS_CLAIM}
  namespace: ${SESSIONS_NS}
spec:
  accessModes: ["ReadWriteMany"]
  storageClassName: ""
  volumeName: ${NFS_CLAIM}
  resources:
    requests:
      storage: 100Gi
EOF
}

# nfs_volume_values prints the runtime.sharedVolumes entry that mounts the
# whole NFS mock at its own host path, so notebook code uses the same paths
# as the machine. Read-only shares may reach Deployments, which are public;
# a writable one never does.
nfs_volume_values() {
	printf '  sharedVolumes:\n'
	printf '    - name: nfs\n      existingClaim: %s\n      mountPath: "%s"\n' "$NFS_CLAIM" "$NFS_DIR"
	if [ "$NFS_WRITABLE" = 1 ]; then
		printf '      readOnly: false\n      modes: [edit, run]\n'
	else
		printf '      readOnly: true\n      modes: [edit, run, deploy]\n'
	fi
	if [ "$NFS_GID" != 0 ]; then
		printf '      supplementalGroups: [%s]\n' "$NFS_GID"
	fi
}

write_values() {
	log "Generating $VALUES_FILE"
	local kind_subnet external_proxy=""
	kind_subnet="$(docker network inspect kind -f '{{range .IPAM.Config}}{{println .Subnet}}{{end}}' | grep -v ':' | head -1)"
	[ -n "$kind_subnet" ] || die "could not read the kind network's IPv4 subnet"
	if [ "$GOOGLE_ENABLED" = 1 ]; then
		external_proxy='  # Development only: HTTPS to any address, so backend-public can reach
  # Google (accounts.google.com, oauth2.googleapis.com, www.googleapis.com),
  # whose addresses change too often for CIDRs. Production sends that traffic
  # through an egress proxy (backendPublic.oidc.proxyUrl) and allows only the
  # proxy here.
  externalProxy:
    cidrs: ["0.0.0.0/0"]
    ports: [{ port: 443, protocol: TCP }]'
	fi
	cat >"$VALUES_FILE" <<EOF
# Generated by hack/kind/up.sh; do not edit. Layered over values-kind.yaml.
frontend:
  image: { repository: ${REGISTRY}/marimohub-frontend, digest: ${FRONTEND_DIGEST} }
backendPublic:
  image: { repository: ${REGISTRY}/marimohub-backend, digest: ${BACKEND_DIGEST} }
  publicAppUrl: https://${PUBLIC_HOST}
  oidc:
    existingSecret: ${OIDC_SECRET}
backendInternal:
  image: { repository: ${REGISTRY}/marimohub-backend, digest: ${BACKEND_DIGEST} }
operator:
  image: { repository: ${REGISTRY}/marimohub-operator, digest: ${OPERATOR_DIGEST} }
runtime:
  images:
    ubi: ${REGISTRY}/marimohub-runtime-ubi@${RUNTIME_UBI_DIGEST}
    ubuntu: ${REGISTRY}/marimohub-runtime-ubuntu@${RUNTIME_UBUNTU_DIGEST}
  fetcherImage: { repository: ${REGISTRY}/marimohub-source-fetcher, digest: ${SOURCE_FETCHER_DIGEST} }
  workspaceStorage:
    existingClaim: ${WORKSPACE_CLAIM}
    mountPath: ${WORKSPACE_MOUNT_PATH}
    supplementalGroups: [${WORKSPACE_GID}]
$(nfs_volume_values)
network:
  kubernetesApi:
    cidrs: ["${kind_subnet}"]
  database:
    cidrs: ["${POSTGRES_IP}/32"]
${external_proxy}
ingress:
  host: ${PUBLIC_HOST}
EOF
}

# deployment_revision DEPLOYMENT prints the Deployment's rollout revision,
# which changes only when its Pod template does, or nothing.
deployment_revision() {
	kubectl -n "$APP_NS" get "$1" -o jsonpath='{.metadata.annotations.deployment\.kubernetes\.io/revision}' 2>/dev/null || true
}

install_release() {
	log "Helm release '$RELEASE'"
	# A kubeconfig pinned to this cluster, so the preflight script can never
	# check whatever cluster the user's current context happens to name.
	(umask 077 && "$KIND" get kubeconfig --name "$CLUSTER_NAME" >"$STATE_DIR/kubeconfig")
	KUBECTL="$KUBECTL" "$REPO_ROOT/hack/preflight/check-dependencies.sh" --kubeconfig "$STATE_DIR/kubeconfig" \
		"$APP_NS" "${RELEASE}-backend-env" DATABASE_URL SECRET_KEY >/dev/null
	if [ "$GOOGLE_ENABLED" = 1 ]; then
		KUBECTL="$KUBECTL" "$REPO_ROOT/hack/preflight/check-dependencies.sh" --kubeconfig "$STATE_DIR/kubeconfig" \
			"$APP_NS" "$OIDC_SECRET" GOOGLE_CLIENT_ID GOOGLE_CLIENT_SECRET >/dev/null
	fi

	local deployment="deployment/${RELEASE}-backend-public" existed=0 revision="" recorded_hash
	if kubectl -n "$APP_NS" get "$deployment" >/dev/null 2>&1; then
		existed=1
		revision="$(deployment_revision "$deployment")"
	fi
	"$HELM" upgrade --install "$RELEASE" "$REPO_ROOT/charts/marimohub" --kube-context "$CONTEXT" \
		--namespace "$APP_NS" -f "$REPO_ROOT/charts/marimohub/values-kind.yaml" -f "$VALUES_FILE" \
		--wait --timeout 10m >/dev/null

	# A container reads its Secret environment only when it starts. When the
	# OIDC Secret changed and the upgrade did not already roll backend-public,
	# restart it. No recorded hash means a release from before this Secret
	# existed, so nothing was loaded from it yet. The hash is recorded only
	# once backend-public runs with the current content, so an interrupted
	# run restarts it next time.
	recorded_hash="$(cat "$OIDC_HASH_FILE" 2>/dev/null || printf '' | sha256sum | cut -d' ' -f1)"
	if [ "$existed" = 1 ] && [ "$recorded_hash" != "$OIDC_SECRET_HASH" ] &&
		[ "$(deployment_revision "$deployment")" = "$revision" ]; then
		log "Restarting backend-public to load the changed OIDC Secret"
		kubectl -n "$APP_NS" rollout restart "$deployment" >/dev/null
		kubectl -n "$APP_NS" rollout status "$deployment" --timeout=300s >/dev/null
	fi
	printf '%s\n' "$OIDC_SECRET_HASH" >"$OIDC_HASH_FILE"
}

smoke() {
	log "Checking https://${PUBLIC_HOST}/api/health"
	local _ google="off (see README.md, \"Sign in with Google\")"
	# --resolve reaches the Ingress's loopback port mapping whether or not this
	# machine can resolve PUBLIC_HOST yet.
	local resolve="${PUBLIC_HOST}:443:127.0.0.1"
	for _ in $(seq 30); do
		curl -fsS --resolve "$resolve" --cacert "$TLS_DIR/ca.crt" "https://${PUBLIC_HOST}/api/health" >/dev/null 2>&1 && break
		sleep 2
	done
	curl -fsS --resolve "$resolve" --cacert "$TLS_DIR/ca.crt" "https://${PUBLIC_HOST}/api/health" >/dev/null ||
		die "https://${PUBLIC_HOST}/api/health is not answering through the Ingress"
	[ "$GOOGLE_ENABLED" != 1 ] || google="on (client saved in $GOOGLE_ENV_FILE)"
	cat >&2 <<EOF

MarimoHub is up on kind cluster '$CLUSTER_NAME' (kubectl context $CONTEXT).
  App:         https://${PUBLIC_HOST}  (CA: $TLS_DIR/ca.crt)
  NFS:         $NFS_DIR  -> $NFS_DIR in every notebook ($(nfs_access))
  Workspaces:  $DATA_DIR/workspaces/<workspace-id>  -> ${WORKSPACE_MOUNT_PATH} in that Workspace's notebooks
  Runtimes:    kubectl --context $CONTEXT -n $SESSIONS_NS get marimosessions,pods
  Google:      sign-in ${google}
  Redirect:    ${GOOGLE_REDIRECT_URI}  (authorized redirect URI to register on the Google OAuth client)
EOF
	if [ "$GOOGLE_ENABLED" = 1 ] && ! google_accepts_host "$PUBLIC_HOST"; then
		warn_google_host
	fi
	if ! getent ahostsv4 "$PUBLIC_HOST" 2>/dev/null | awk '{print $1}' | grep -qx 127.0.0.1; then
		warn "$PUBLIC_HOST does not resolve to 127.0.0.1 on this machine, so browsers cannot reach it yet. Add it once with: echo '127.0.0.1 $PUBLIC_HOST' | sudo tee -a /etc/hosts"
	fi
}

nfs_access() {
	if [ "$NFS_WRITABLE" = 1 ]; then
		printf 'read-write, edit and run notebooks'
	else
		printf 'read-only, also in Deployments'
	fi
}

main() {
	preflight
	resolve_google_oauth
	ensure_registry
	ensure_cluster
	ensure_postgres
	push_images
	install_ingress
	apply_prerequisites
	ensure_secrets
	ensure_workspace_storage
	ensure_nfs_volume
	write_values
	install_release
	smoke
}

main "$@"
