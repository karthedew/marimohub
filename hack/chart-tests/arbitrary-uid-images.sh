#!/usr/bin/env bash
# make openshift-image-smoke: runs every image from `make images`, and the
# evaluation PostgreSQL image the marimohub-platform chart uses, the way
# OpenShift's restricted-v2 SCC runs a container: an arbitrary UID with group
# 0 and no passwd entry, a read-only root filesystem, every capability
# dropped and no privilege escalation. Each image must start and answer the
# check its chart probe or its first use needs.
#
# Environment: ENGINE (docker or podman, default docker), IMAGE_TAG (default
# dev). Containers are named marimohub-uid-smoke-* and always removed.
set -Eeuo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$REPO_ROOT"
# shellcheck source=../tools/versions.env
source hack/tools/versions.env

ENGINE="${ENGINE:-docker}"
TAG="${IMAGE_TAG:-dev}"
UID_ARBITRARY=1000770000
COMMON=(--rm --user "${UID_ARBITRARY}:0" --read-only --cap-drop ALL --security-opt no-new-privileges)
WORK="$(mktemp -d)"
PREFIX="marimohub-uid-smoke-$$"
trap '"$ENGINE" rm -f "$PREFIX-postgresql" >/dev/null 2>&1 || true; rm -rf "$WORK"' EXIT

failures=0
ok() { printf 'ok   %s\n' "$*"; }
bad() {
	printf 'FAIL %s\n' "$*" >&2
	failures=$((failures + 1))
}
# run NAME IMAGE ARGS...: runs one container and records the result.
run() {
	local name="$1" image="$2"
	shift 2
	if "$ENGINE" run "${COMMON[@]}" "$@" >"$WORK/$name.log" 2>&1; then
		ok "$name ($image)"
	else
		bad "$name ($image):"
		sed 's/^/     /' "$WORK/$name.log" >&2
	fi
}

for image in backend frontend operator source-fetcher runtime-ubi runtime-ubuntu; do
	"$ENGINE" image inspect "marimohub-$image:$TAG" >/dev/null 2>&1 || {
		echo "arbitrary-uid-images: marimohub-$image:$TAG is missing; run make images" >&2
		exit 1
	}
done

# Placeholder settings, not credentials: the backend needs them to start.
cat >"$WORK/backend.env" <<'EOF'
DATABASE_URL=postgresql+asyncpg://placeholder:placeholder@127.0.0.1:1/placeholder
SECRET_KEY=placeholder-not-a-secret
SESSION_BACKEND=kube
SESSION_RUNTIME_IMAGE=example.invalid/runtime@sha256:0000000000000000000000000000000000000000000000000000000000000000
EOF
run backend "marimohub-backend:$TAG" --tmpfs /tmp --env-file "$WORK/backend.env" --entrypoint sh "marimohub-backend:$TAG" -c '
	set -e
	python -c "import app.main, app.internal_main"
	python -c "import os; from sentence_transformers import SentenceTransformer; SentenceTransformer(os.environ[\"EMBEDDING_MODEL_PATH\"])"
	alembic --help >/dev/null
	python -m app.serve & server=$!
	for _ in $(seq 60); do
		if python -c "import urllib.request; urllib.request.urlopen(\"http://127.0.0.1:8000/api/health\", timeout=2)" 2>/dev/null; then
			kill "$server"; echo "/api/health: 200"; exit 0
		fi
		sleep 1
	done
	exit 1'

run frontend "marimohub-frontend:$TAG" --tmpfs /tmp --entrypoint sh "marimohub-frontend:$TAG" -c '
	node server.js & server=$!
	for _ in $(seq 30); do
		if node -e "fetch(\"http://127.0.0.1:3000/healthz\").then(r => process.exit(r.ok ? 0 : 1), () => process.exit(1))"; then
			kill "$server"; echo "/healthz: 200"; exit 0
		fi
		sleep 1
	done
	exit 1'

run operator "marimohub-operator:$TAG" "marimohub-operator:$TAG" --help

run source-fetcher "marimohub-source-fetcher:$TAG" --tmpfs /work --group-add 5000 --entrypoint sh "marimohub-source-fetcher:$TAG" -c '
	set -e
	mkdir -p -m 2770 /work/workspaces/3f1c6a8e-1b2c-4d5e-8f90-a1b2c3d4e5f6
	stat -c "%a" /work/workspaces/3f1c6a8e-1b2c-4d5e-8f90-a1b2c3d4e5f6
	curl --version | head -1'

# The marimo token is a placeholder, passed in a file like a Secret would be.
printf 'MARIMO_TOKEN=placeholder-token\n' >"$WORK/runtime.env"
run runtime-ubi "marimohub-runtime-ubi:$TAG" --group-add "$UID_ARBITRARY" --group-add 5000 \
	--tmpfs /tmp --tmpfs /work --tmpfs /home/marimo --tmpfs /cache \
	-e HOME=/home/marimo -e XDG_CACHE_HOME=/cache --env-file "$WORK/runtime.env" \
	--entrypoint sh "marimohub-runtime-ubi:$TAG" -c '
	set -e
	printf "import marimo\napp = marimo.App()\n" >/work/notebook.py
	marimo edit /work/notebook.py --host=0.0.0.0 --port=8080 --token-password="$MARIMO_TOKEN" \
		--base-url=/api/proxy/smoke --headless --skip-update-check >/tmp/marimo.log 2>&1 & server=$!
	for _ in $(seq 90); do
		if python3 -c "
import os, sys, urllib.request
req = urllib.request.Request(\"http://127.0.0.1:8080/api/proxy/smoke/api/version\", headers={\"Authorization\": \"Bearer \" + os.environ[\"MARIMO_TOKEN\"]})
sys.exit(0 if urllib.request.urlopen(req, timeout=3).status == 200 else 1)" 2>/dev/null; then
			kill "$server"
			echo "readiness: 200"
			if grep -q "access_token=" /tmp/marimo.log; then echo "startup banner logs the access token"; fi
			exit 0
		fi
		sleep 1
	done
	cat /tmp/marimo.log
	exit 1'
if grep -q 'startup banner logs the access token' "$WORK/runtime-ubi.log" 2>/dev/null; then
	printf 'note runtime-ubi: marimo prints its URL with ?access_token=<token> at startup, so Runtime Pod logs carry the token (an operator or image fix, outside the charts)\n'
fi

run runtime-ubuntu "marimohub-runtime-ubuntu:$TAG" --tmpfs /tmp --tmpfs /home/marimo --tmpfs /cache -e HOME=/home/marimo \
	"marimohub-runtime-ubuntu:$TAG" --version

# The evaluation PostgreSQL, with the platform chart's own first-start script:
# initdb, the non-superuser app role and pgvector under an arbitrary UID.
mkdir -m 0755 "$WORK/initdb"
.bin/helm template marimohub-platform charts/marimohub-platform -n marimohub-platform \
	-f charts/marimohub-platform/values-kind.yaml --kube-version 1.35.0 |
	python3 -c '
import sys, yaml
for doc in yaml.safe_load_all(sys.stdin):
    if doc and doc["kind"] == "ConfigMap" and doc["metadata"]["name"].endswith("-config"):
        sys.stdout.write(doc["data"]["10-marimohub.sh"])
' >"$WORK/initdb/10-marimohub.sh"
chmod 0555 "$WORK/initdb/10-marimohub.sh"
(
	umask 077
	printf 'POSTGRES_PASSWORD=%s\nMARIMOHUB_DB_PASSWORD=%s\n' "$(openssl rand -hex 16)" "$(openssl rand -hex 16)" >"$WORK/postgresql.env"
)
chmod 0644 "$WORK/postgresql.env"
"$ENGINE" run -d --name "$PREFIX-postgresql" --user "${UID_ARBITRARY}:0" --group-add "$UID_ARBITRARY" --read-only \
	--cap-drop ALL --security-opt no-new-privileges \
	--tmpfs /var/run/postgresql --tmpfs /tmp --tmpfs /var/lib/postgresql/data \
	-e PGDATA=/var/lib/postgresql/data/pgdata -e POSTGRES_USER=postgres -e POSTGRES_DB=marimohub \
	--env-file "$WORK/postgresql.env" -v "$WORK/initdb:/docker-entrypoint-initdb.d:ro" \
	"$LOCAL_POSTGRES_IMAGE" >/dev/null
ready=0
for _ in $(seq 90); do
	# The entrypoint's first server listens only while the init scripts run;
	# wait for the final one, which writes its PID file under PGDATA last.
	if "$ENGINE" exec "$PREFIX-postgresql" sh -c 'pg_isready -q -h /var/run/postgresql && ! pgrep -f "listen_addresses=" >/dev/null' 2>/dev/null &&
		"$ENGINE" logs "$PREFIX-postgresql" 2>&1 | grep -q 'PostgreSQL init process complete'; then
		ready=1
		break
	fi
	sleep 1
done
query() { "$ENGINE" exec "$PREFIX-postgresql" psql -h /var/run/postgresql -U postgres -d marimohub -tAc "$1"; }
if [ "$ready" = 1 ] &&
	[ "$(query "select rolsuper from pg_roles where rolname = 'marimohub'")" = f ] &&
	[ "$(query "select pg_get_userbyid(datdba) from pg_database where datname = 'marimohub'")" = marimohub ] &&
	[ "$(query "select pg_get_userbyid(nspowner) from pg_namespace where nspname = 'public'")" = marimohub ] &&
	vector="$(query "select extversion from pg_extension where extname = 'vector'")" && [ -n "$vector" ]; then
	ok "postgresql ($LOCAL_POSTGRES_IMAGE): initdb, non-superuser app role owning marimohub and public, pgvector $vector"
else
	bad "postgresql ($LOCAL_POSTGRES_IMAGE):"
	"$ENGINE" logs "$PREFIX-postgresql" 2>&1 | tail -30 | sed 's/^/     /' >&2
fi

if [ "$failures" -gt 0 ]; then
	echo "arbitrary-uid-images: $failures image(s) failed" >&2
	exit 1
fi
echo "arbitrary-uid-images: every image runs with UID $UID_ARBITRARY, group 0, a read-only root and no capabilities"
