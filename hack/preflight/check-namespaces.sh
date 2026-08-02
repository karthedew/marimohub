#!/usr/bin/env bash
# check-namespaces.sh: verifies the three namespace trust boundaries exist
# and, when configured, carry required labels, before a MarimoHub Helm
# release is installed or upgraded. This is a real, versioned command, not a
# Helm `lookup` call or a hook that renders nothing: `lookup` silently
# returns an empty result against a missing namespace instead of failing the
# release, and a namespace is exactly the one object this chart is
# forbidden from creating on the installer's behalf (see the Namespace
# Boundaries table), so a missing one has to fail loudly here instead.
#
# Also validates the production (OpenShift-shaped) policy CIDR inputs the
# chart's NetworkPolicy templates render as conditional egress rules
# (network.kubernetesApi.cidrs / network.database.cidrs): those values
# render fine when left empty -- `helm template` stays deterministic either
# way -- but an install that actually leaves them empty gives the operator
# and both backend Deployments no egress path to the Kubernetes API or
# PostgreSQL at all, which is exactly the silent gap this command exists to
# catch before Helm creates a single object.
#
# Secret existence/key checks (the application Secret, TLS Secrets, the
# database CA, image-pull Secrets) are a separate, larger surface this
# script does not attempt -- see the sibling check-dependencies.sh, which
# needs real cluster access the same way this script does.
set -Eeuo pipefail

PREFLIGHT_VERSION="0.2.0"
KUBECTL="${KUBECTL:-kubectl}"

usage() {
	cat <<'EOF'
Usage: check-namespaces.sh [--kubeconfig FILE] [--platform openshift|portable]
                            [--kubernetes-api-cidrs CSV] [--database-cidrs CSV]
                            [--require-nonempty NAME=VALUE ...]
                            APP_NAMESPACE CONTROLLER_NAMESPACE SESSIONS_NAMESPACE [LABEL=VALUE ...]

Verifies that each namespace exists and, if LABEL=VALUE pairs are given,
that every one of them carries every listed label.

When --platform openshift is given, also requires --kubernetes-api-cidrs and
--database-cidrs to each name at least one CIDR: the chart's NetworkPolicy
templates render an empty egress rule set for either one left blank, which
is a silent no-egress-path install on the profile that carries a STIG
claim. --platform portable (or omitting --platform) skips this check.

--require-nonempty NAME=VALUE may be repeated for any other installer-
supplied value that must not be empty before a production install (e.g.
routes.host, an existing Secret name) -- NAME is only ever used in the
failure message, not looked up anywhere.

Exits non-zero and prints every failure found (not just the first).
EOF
}

if [[ "${1:-}" == "--version" ]]; then
	printf 'check-namespaces.sh %s\n' "${PREFLIGHT_VERSION}"
	exit 0
fi

if [[ "${1:-}" == "-h" || "${1:-}" == "--help" ]]; then
	usage
	exit 0
fi

kubeconfig_args=()
platform=""
kubernetes_api_cidrs=""
database_cidrs=""
require_nonempty=()

while [[ "${1:-}" == --* ]]; do
	case "$1" in
	--kubeconfig)
		kubeconfig_args=(--kubeconfig "$2")
		shift 2
		;;
	--platform)
		platform="$2"
		shift 2
		;;
	--kubernetes-api-cidrs)
		kubernetes_api_cidrs="$2"
		shift 2
		;;
	--database-cidrs)
		database_cidrs="$2"
		shift 2
		;;
	--require-nonempty)
		require_nonempty+=("$2")
		shift 2
		;;
	*)
		echo "preflight: unknown option '$1'" >&2
		usage >&2
		exit 2
		;;
	esac
done

if [[ $# -lt 3 ]]; then
	usage >&2
	exit 2
fi

app_ns="$1"
controller_ns="$2"
sessions_ns="$3"
shift 3
required_labels=("$@")

failures=0

check_namespace() {
	local ns="$1"
	local phase

	if ! phase=$("${KUBECTL}" "${kubeconfig_args[@]}" get namespace "${ns}" -o jsonpath='{.status.phase}' 2>/dev/null); then
		echo "preflight: namespace '${ns}' does not exist" >&2
		failures=$((failures + 1))
		return
	fi

	if [[ "${phase}" != "Active" ]]; then
		echo "preflight: namespace '${ns}' is not Active (phase=${phase:-unknown})" >&2
		failures=$((failures + 1))
	fi

	for pair in "${required_labels[@]}"; do
		local key="${pair%%=*}"
		local want="${pair#*=}"
		local got
		got=$("${KUBECTL}" "${kubeconfig_args[@]}" get namespace "${ns}" -o jsonpath="{.metadata.labels.${key}}" 2>/dev/null || true)
		if [[ "${got}" != "${want}" ]]; then
			echo "preflight: namespace '${ns}' label '${key}' = '${got:-<absent>}', want '${want}'" >&2
			failures=$((failures + 1))
		fi
	done
}

check_namespace "${app_ns}"
check_namespace "${controller_ns}"
check_namespace "${sessions_ns}"

if [[ "${platform}" == "openshift" ]]; then
	if [[ -z "${kubernetes_api_cidrs}" ]]; then
		echo "preflight: --platform openshift requires --kubernetes-api-cidrs (network.kubernetesApi.cidrs is empty -- the operator and backend Deployments would have no egress path to the Kubernetes API)" >&2
		failures=$((failures + 1))
	fi
	if [[ -z "${database_cidrs}" ]]; then
		echo "preflight: --platform openshift requires --database-cidrs (network.database.cidrs is empty -- backendPublic/backendInternal/the migration Job would have no egress path to PostgreSQL)" >&2
		failures=$((failures + 1))
	fi
fi

for pair in "${require_nonempty[@]}"; do
	name="${pair%%=*}"
	value="${pair#*=}"
	if [[ -z "${value}" ]]; then
		echo "preflight: '${name}' must not be empty" >&2
		failures=$((failures + 1))
	fi
done

if [[ "${failures}" -gt 0 ]]; then
	echo "preflight: ${failures} check(s) failed" >&2
	exit 1
fi

echo "preflight: namespaces ${app_ns}, ${controller_ns}, ${sessions_ns} are present and Active"
