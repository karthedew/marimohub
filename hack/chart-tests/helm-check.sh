#!/usr/bin/env bash
# make helm-check: lints and renders charts/marimohub and
# charts/marimohub-platform on every shipped profile, validates each render
# with strict kubeconform (pinned OpenShift Route and ServiceMonitor schemas
# included) and kube-linter, then runs the chart assertions and the preflight
# script tests. No cluster is needed; kubeconform downloads its schemas.
#
# The openshift profiles need values only a real install has (a Route host,
# the API server and database addresses, an NFS server), so they render with
# the placeholder overlays in this directory. Nothing renders bare
# values.yaml alone.
set -Eeuo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$REPO_ROOT"
# shellcheck source=versions.env
source hack/chart-tests/versions.env

HELM="$REPO_ROOT/.bin/helm"
KUBECONFORM="$REPO_ROOT/.bin/kubeconform"
KUBE_LINTER="$REPO_ROOT/.bin/kube-linter"
# Offline helm falls back to Kubernetes v1.20.0, below both charts'
# kubeVersion floor, so every render pins a version.
KUBE_VERSION="${CHART_KUBE_VERSION:-1.35.0}"
APP=charts/marimohub
PLATFORM=charts/marimohub-platform
TESTS=hack/chart-tests
CATALOG="https://raw.githubusercontent.com/datreeio/CRDs-catalog/${CRDS_CATALOG_COMMIT}"
SCHEMAS=(
	-schema-location default
	-schema-location "${CATALOG}/openshift/v4.15-strict/{{.ResourceKind}}_{{.Group}}_{{.ResourceAPIVersion}}.json"
	-schema-location "${CATALOG}/{{.Group}}/{{.ResourceKind}}_{{.ResourceAPIVersion}}.json"
)

rendered="$(mktemp -d)"
trap 'rm -rf "$rendered"' EXIT

# check NAME RELEASE CHART HELM-ARGS...: lint, render, kubeconform, kube-linter.
check() {
	local name="$1" release="$2" chart="$3"
	shift 3
	echo "== helm lint ($name) =="
	"$HELM" lint "$chart" --kube-version "$KUBE_VERSION" "$@"
	echo "== helm template ($name) =="
	"$HELM" template "$release" "$chart" --kube-version "$KUBE_VERSION" --include-crds "$@" >"$rendered/$name.yaml"
	echo "== kubeconform ($name) =="
	# No CustomResourceDefinition schema is published for this Kubernetes
	# version; the install test applies the CRD to a real API server instead.
	"$KUBECONFORM" -kubernetes-version "$KUBE_VERSION" -strict -summary "${SCHEMAS[@]}" \
		-skip CustomResourceDefinition "$rendered/$name.yaml"
	echo "== kube-linter ($name) =="
	"$KUBE_LINTER" lint "$rendered/$name.yaml"
}

check app-kind marimohub "$APP" -n marimohub -f "$APP/values-kind.yaml"
check app-openshift marimohub "$APP" -n marimohub -f "$APP/values-openshift.yaml" -f "$TESTS/values-openshift-ci.yaml"
check platform-kind marimohub-platform "$PLATFORM" -n marimohub-platform -f "$PLATFORM/values-kind.yaml"
check platform-openshift marimohub-platform "$PLATFORM" -n marimohub-platform \
	-f "$PLATFORM/values-openshift.yaml" -f "$TESTS/platform-values-openshift-ci.yaml"
check platform-openshift-evaldb marimohub-platform "$PLATFORM" -n marimohub-platform \
	-f "$PLATFORM/values-openshift.yaml" -f "$TESTS/platform-values-openshift-ci.yaml" -f "$TESTS/platform-values-evaldb-ci.yaml"

export HELM
echo "== app chart assertions (kind) =="
python3 "$TESTS/verify_chart.py" "$APP" "$APP/values-kind.yaml"
echo "== app chart assertions (openshift) =="
python3 "$TESTS/verify_chart.py" "$APP" "$APP/values-openshift.yaml" "$TESTS/values-openshift-ci.yaml"
echo "== platform chart assertions and app contract (kind) =="
python3 "$TESTS/verify_platform_chart.py" "$PLATFORM" "$PLATFORM/values-kind.yaml"
echo "== platform chart assertions and app contract (openshift) =="
python3 "$TESTS/verify_platform_chart.py" "$PLATFORM" "$PLATFORM/values-openshift.yaml" "$TESTS/platform-values-openshift-ci.yaml"
echo "== platform chart assertions and app contract (openshift, evaluation database) =="
python3 "$TESTS/verify_platform_chart.py" "$PLATFORM" "$PLATFORM/values-openshift.yaml" \
	"$TESTS/platform-values-openshift-ci.yaml" "$TESTS/platform-values-evaldb-ci.yaml"
echo "== preflight script tests =="
hack/preflight/test-preflight.sh
