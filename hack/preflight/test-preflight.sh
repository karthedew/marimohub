#!/usr/bin/env bash
# Offline tests for check-dependencies.sh and check-namespaces.sh, run by
# make helm-check. A fake kubectl serves fixture objects and evaluates each
# jsonpath with the pinned kubectl (`kubectl patch --local`), so dotted keys
# such as ca.crt and pod-security.kubernetes.io/enforce are read exactly the
# way a real cluster lookup reads them.
set -Eeuo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
REAL_KUBECTL="${REAL_KUBECTL:-$REPO_ROOT/.bin/kubectl}"
[ -x "$REAL_KUBECTL" ] || { echo "test-preflight: $REAL_KUBECTL is missing; run 'make bootstrap-tools'" >&2; exit 1; }

work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT
fixtures="$work/fixtures"
mkdir -p "$fixtures"

cat >"$work/kubectl" <<'EOF'
#!/usr/bin/env bash
# Fake kubectl for `get KIND NAME [-n NS] -o json|jsonpath=...`.
set -euo pipefail
args=()
while [ $# -gt 0 ]; do
	case "$1" in
	--kubeconfig | -n) shift 2 ;;
	*) args+=("$1"); shift ;;
	esac
done
[ "${args[0]}" = get ] && [ "${args[3]}" = -o ] || { echo "fake kubectl: unexpected ${args[*]}" >&2; exit 2; }
file="$FIXTURES/${args[1]}-${args[2]}.json"
[ -f "$file" ] || { echo "Error from server (NotFound): ${args[1]} \"${args[2]}\" not found" >&2; exit 1; }
case "${args[4]}" in
json) cat "$file" ;;
jsonpath=*) KUBECONFIG=/dev/null exec "$REAL_KUBECTL" patch --local -f "$file" --type merge -p '{}' -o "${args[4]}" ;;
*) echo "fake kubectl: unexpected output ${args[4]}" >&2; exit 2 ;;
esac
EOF
chmod +x "$work/kubectl"
export KUBECTL="$work/kubectl" FIXTURES="$fixtures" REAL_KUBECTL

# Base64 of placeholder strings, not credentials.
cat >"$fixtures/secret-marimohub-database-ca.json" <<'EOF'
{"apiVersion": "v1", "kind": "Secret", "metadata": {"name": "marimohub-database-ca", "namespace": "marimohub"},
 "data": {"ca.crt": "cGxhY2Vob2xkZXI=", "DATABASE_URL": "cGxhY2Vob2xkZXI=", "empty.key": ""}}
EOF
namespace_fixture() {
	local labels="$2"
	cat >"$fixtures/namespace-$1.json" <<EOF
{"apiVersion": "v1", "kind": "Namespace", "metadata": {"name": "$1", "labels": {$labels}}, "status": {"phase": "Active"}}
EOF
}
restricted='"pod-security.kubernetes.io/enforce": "restricted", "kubernetes.io/metadata.name": "x"'
namespace_fixture marimohub "$restricted"
namespace_fixture marimohub-controller "$restricted"
namespace_fixture marimohub-sessions '"kubernetes.io/metadata.name": "marimohub-sessions"'

failures=0
# expect STATUS DESCRIPTION COMMAND...: run COMMAND and compare its exit status.
expect() {
	local want="$1" description="$2" got=0
	shift 2
	"$@" >"$work/out" 2>&1 || got=$?
	if { [ "$want" = pass ] && [ "$got" -eq 0 ]; } || { [ "$want" = fail ] && [ "$got" -ne 0 ]; }; then
		echo "ok   $description"
	else
		echo "FAIL $description (exit $got):" >&2
		sed 's/^/     /' "$work/out" >&2
		failures=$((failures + 1))
	fi
}

deps="$REPO_ROOT/hack/preflight/check-dependencies.sh"
namespaces="$REPO_ROOT/hack/preflight/check-namespaces.sh"

expect pass "dotted Secret key ca.crt is found" "$deps" marimohub marimohub-database-ca ca.crt
expect pass "plain and dotted keys together" "$deps" --kubeconfig /dev/null marimohub marimohub-database-ca ca.crt DATABASE_URL
expect fail "a missing dotted key fails" "$deps" marimohub marimohub-database-ca tls.crt
expect fail "an empty value fails" "$deps" marimohub marimohub-database-ca empty.key
expect fail "a missing Secret fails" "$deps" marimohub marimohub-backend-env SECRET_KEY
expect pass "namespaces exist without labels to check" "$namespaces" marimohub marimohub-controller marimohub-sessions
expect fail "a namespace without the dotted label fails" "$namespaces" marimohub marimohub-controller marimohub-sessions pod-security.kubernetes.io/enforce=restricted
namespace_fixture marimohub-sessions "$restricted"
expect pass "every namespace carries the dotted label" "$namespaces" marimohub marimohub-controller marimohub-sessions pod-security.kubernetes.io/enforce=restricted
expect fail "a wrong label value fails" "$namespaces" marimohub marimohub-controller marimohub-sessions pod-security.kubernetes.io/enforce=baseline
expect pass "the retired CIDR flags are ignored" "$namespaces" --platform openshift --kubernetes-api-cidrs "" --database-cidrs "" marimohub marimohub-controller marimohub-sessions
expect fail "a missing namespace fails" "$namespaces" marimohub marimohub-controller marimohub-missing

if [ "$failures" -gt 0 ]; then
	echo "test-preflight: $failures test(s) failed" >&2
	exit 1
fi
echo "test-preflight: all tests passed"
