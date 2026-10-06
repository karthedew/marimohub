#!/usr/bin/env bash
# check-dependencies.sh: verifies that a named Secret this chart references
# by name only (never by value -- see values.yaml's existingSecret/
# tls.secretName/internalApiCA/database.tls fields) actually exists in the
# target namespace and carries every key the chart's templates read out of
# it. This is real cluster access, unlike `helm template`/`helm lint`, which
# stay offline-deterministic by design: Helm has no supported way to inspect
# an external Secret's actual keys without `lookup`, and the app chart
# (charts/marimohub) never uses `lookup`. Only the admin-installed
# charts/marimohub-platform chart does, to generate the Secrets this script
# checks and keep them across upgrades.
#
# Run once per required Secret, before `helm install`/`helm upgrade`, e.g.:
#
#   check-dependencies.sh marimohub marimohub-backend-env DATABASE_URL SECRET_KEY
#   check-dependencies.sh marimohub marimohub-database-ca ca.crt
#   check-dependencies.sh marimohub-sessions marimohub-internal-api-ca ca.crt
#
# and on the portable profile, where you supply the Service certificates:
#
#   check-dependencies.sh marimohub marimohub-frontend-tls tls.crt tls.key
#   check-dependencies.sh marimohub marimohub-backend-public-tls tls.crt tls.key
#   check-dependencies.sh marimohub marimohub-backend-internal-tls tls.crt tls.key
#
# On OpenShift the service CA creates those three only after the app chart's
# Services exist, so do not check them before installing there.
#
# A Secret name left empty in values (e.g. an unset runtime.imagePullSecrets
# entry, or database.tls disabled) simply has nothing to check here -- only
# invoke this for a Secret name the chosen values file actually configures.
set -Eeuo pipefail

PREFLIGHT_VERSION="0.2.0"
KUBECTL="${KUBECTL:-kubectl}"

usage() {
	cat <<'EOF'
Usage: check-dependencies.sh [--kubeconfig FILE] NAMESPACE SECRET_NAME KEY [KEY ...]

Verifies that SECRET_NAME exists in NAMESPACE and carries a non-empty value
for every listed KEY. Exits non-zero and prints every failure found (not
just the first) otherwise.
EOF
}

if [[ "${1:-}" == "--version" ]]; then
	printf 'check-dependencies.sh %s\n' "${PREFLIGHT_VERSION}"
	exit 0
fi

if [[ "${1:-}" == "-h" || "${1:-}" == "--help" ]]; then
	usage
	exit 0
fi

kubeconfig_args=()
if [[ "${1:-}" == "--kubeconfig" ]]; then
	kubeconfig_args=(--kubeconfig "$2")
	shift 2
fi

if [[ $# -lt 3 ]]; then
	usage >&2
	exit 2
fi

namespace="$1"
secret_name="$2"
shift 2
required_keys=("$@")

failures=0

if ! secret_json=$("${KUBECTL}" "${kubeconfig_args[@]}" get secret "${secret_name}" -n "${namespace}" -o json 2>/dev/null); then
	echo "preflight: Secret '${secret_name}' does not exist in namespace '${namespace}'" >&2
	exit 1
fi

for key in "${required_keys[@]}"; do
	# A Secret key may contain dots (e.g. `ca.crt`). kubectl's jsonpath reads
	# an unescaped dot as a field separator, even inside `['...']`, and then
	# prints nothing, so every dot is escaped: `{.data.ca\.crt}`.
	value=$("${KUBECTL}" "${kubeconfig_args[@]}" get secret "${secret_name}" -n "${namespace}" -o jsonpath="{.data.${key//./\\.}}" 2>/dev/null || true)
	if [[ -z "${value}" ]]; then
		echo "preflight: Secret '${secret_name}' in namespace '${namespace}' is missing key '${key}' (or the key's value is empty)" >&2
		failures=$((failures + 1))
	fi
done

if [[ "${failures}" -gt 0 ]]; then
	echo "preflight: ${failures} check(s) failed for Secret '${secret_name}' in namespace '${namespace}'" >&2
	exit 1
fi

echo "preflight: Secret '${secret_name}' in namespace '${namespace}' has every required key"
