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
# The Kubernetes API and database egress values are not checked here any
# more: the app chart's openshift profile refuses to render without them,
# which checks the values the release really uses rather than copies typed on
# this command line. --platform, --kubernetes-api-cidrs and --database-cidrs
# are still accepted, and ignored with a notice.
#
# Secret existence/key checks (the application Secret, TLS Secrets, the
# database CA, image-pull Secrets) are a separate, larger surface this
# script does not attempt -- see the sibling check-dependencies.sh, which
# needs real cluster access the same way this script does.
set -Eeuo pipefail

PREFLIGHT_VERSION="0.3.0"
KUBECTL="${KUBECTL:-kubectl}"

usage() {
	cat <<'EOF'
Usage: check-namespaces.sh [--kubeconfig FILE] [--require-nonempty NAME=VALUE ...]
                            APP_NAMESPACE CONTROLLER_NAMESPACE SESSIONS_NAMESPACE [LABEL=VALUE ...]

Verifies that each namespace exists and is Active and, if LABEL=VALUE pairs
are given, that every one of them carries every listed label, for example
pod-security.kubernetes.io/enforce=restricted.

--platform, --kubernetes-api-cidrs and --database-cidrs are ignored since
0.3.0: the app chart checks those values when it renders.

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
require_nonempty=()

while [[ "${1:-}" == --* ]]; do
	case "$1" in
	--kubeconfig)
		kubeconfig_args=(--kubeconfig "$2")
		shift 2
		;;
	--platform | --kubernetes-api-cidrs | --database-cidrs)
		echo "preflight: $1 is ignored since 0.3.0; the app chart checks network values when it renders" >&2
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
		# kubectl's jsonpath reads every unescaped dot as a field separator,
		# and label keys such as pod-security.kubernetes.io/enforce contain
		# dots, so each one is escaped.
		got=$("${KUBECTL}" "${kubeconfig_args[@]}" get namespace "${ns}" -o jsonpath="{.metadata.labels.${key//./\\.}}" 2>/dev/null || true)
		if [[ "${got}" != "${want}" ]]; then
			echo "preflight: namespace '${ns}' label '${key}' = '${got:-<absent>}', want '${want}'" >&2
			failures=$((failures + 1))
		fi
	done
}

check_namespace "${app_ns}"
check_namespace "${controller_ns}"
check_namespace "${sessions_ns}"

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
