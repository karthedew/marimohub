#!/usr/bin/env bash
#
# Tears down what hack/kind/up.sh created: the kind cluster, the local
# registry, and the PostgreSQL fixture container. It never touches
# MARIMOHUB_NFS_DIR (the shared NFS stand-in) or the Workspace directories
# under MARIMOHUB_DATA_DIR; those are the durable data they exist to keep.
#
# Environment:
#   CLUSTER_NAME  kind cluster name (default: marimohub)
#   PURGE=1       also delete the PostgreSQL data volume and .kind/ (generated
#                 CA, certificates, database password, SECRET_KEY, and the
#                 saved Google OAuth client, which the next kind-up then
#                 needs passed again)
set -Eeuo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
CLUSTER_NAME="${CLUSTER_NAME:-marimohub}"
KIND="$REPO_ROOT/.bin/kind"

if "$KIND" get clusters 2>/dev/null | grep -qx "$CLUSTER_NAME"; then
	"$KIND" delete cluster --name "$CLUSTER_NAME"
fi
for container in marimohub-kind-postgres marimohub-registry; do
	if docker inspect "$container" >/dev/null 2>&1; then
		docker rm -f "$container" >/dev/null
		printf 'removed container %s\n' "$container"
	fi
done
if [ "${PURGE:-0}" = 1 ]; then
	if docker volume inspect marimohub-kind-pgdata >/dev/null 2>&1; then
		docker volume rm marimohub-kind-pgdata >/dev/null
		printf 'removed volume marimohub-kind-pgdata\n'
	fi
	rm -rf "$REPO_ROOT/.kind"
	printf 'removed %s\n' "$REPO_ROOT/.kind"
fi
