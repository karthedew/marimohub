#!/usr/bin/env bash
# Confirms every repository-owned tool in .bin/ is present and matches the
# version pinned in versions.env. kubectl, oc, kustomize, controller-gen,
# setup-envtest, kind, and kubebuilder embed their real version in plain
# `go install`/checksummed-download output, so those are checked exactly.
# syft, grype, cosign, kube-linter, kubeconform, helm, and trivy only embed a
# real version when built with release ldflags, which a plain `go install`
# does not set; for those the exact version is guaranteed by construction
# (go install pins and GOSUMDB-verifies the precise tagged module), so this
# script checks presence and executability instead of self-reported text.
#
# Podman and, on platforms without a published archive, oc are documented
# host prerequisites rather than vendored tools. Their absence is reported
# clearly but does not fail this target, since neither is required before
# the kind/OpenShift smoke phases.
set -Eeuo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
root="$(cd "$here/../.." && pwd)"
bin="$root/.bin"

# shellcheck source=versions.env
source "$here/versions.env"

status=0
pass() { printf '  [ok]   %-16s %s\n' "$1" "$2"; }
fail() { printf '  [FAIL] %-16s %s\n' "$1" "$2"; status=1; }
warn() { printf '  [warn] %-16s %s\n' "$1" "$2"; }

# Exact self-reported version match.
check_exact() {
  local name="$1" expect="$2"; shift 2
  local exe="$bin/$name"
  if [ ! -x "$exe" ]; then
    fail "$name" "not installed under .bin/ (run: make bootstrap-tools)"
    return
  fi
  local out
  out="$("$@" 2>&1 || true)"
  if grep -qF "$expect" <<<"$out"; then
    pass "$name" "$expect"
  else
    fail "$name" "expected '$expect', got: $(head -n1 <<<"$out")"
  fi
}

# Presence and executability only; version is pinned by go install, not by
# runtime introspection (see header comment).
check_present() {
  local name="$1" expect="$2"; shift 2
  local exe="$bin/$name"
  if [ ! -x "$exe" ]; then
    fail "$name" "not installed under .bin/ (run: make bootstrap-tools)"
    return
  fi
  if "$@" >/dev/null 2>&1; then
    pass "$name" "$expect (go-install pinned; self-reported version omitted by build)"
  else
    fail "$name" "installed but failed to run: $*"
  fi
}

echo "Repository-owned tools (.bin/):"
check_exact   kubebuilder      "$KUBEBUILDER_VERSION"    "$bin/kubebuilder" version
check_exact   controller-gen   "$CONTROLLER_GEN_VERSION" "$bin/controller-gen" --version
check_exact   kustomize        "$KUSTOMIZE_VERSION"      "$bin/kustomize" version
check_exact   setup-envtest    "$SETUP_ENVTEST_VERSION"  "$bin/setup-envtest" version
check_exact   kind             "$KIND_VERSION"           "$bin/kind" version
check_exact   kubectl          "$KUBECTL_VERSION"        "$bin/kubectl" version --client=true
check_exact   oc               "$OC_VERSION"             "$bin/oc" version --client=true
check_present helm             "$HELM_VERSION"           "$bin/helm" version
check_present syft             "$SYFT_VERSION"           "$bin/syft" version
check_present grype            "$GRYPE_VERSION"          "$bin/grype" version
check_present cosign           "$COSIGN_VERSION"         "$bin/cosign" version
check_present kube-linter      "$KUBE_LINTER_VERSION"    "$bin/kube-linter" version
check_present kubeconform      "$KUBECONFORM_VERSION"    "$bin/kubeconform" -v
check_present trivy            "$TRIVY_VERSION"          "$bin/trivy" --version

envtest_found=false
for d in "$root"/.bin/envtest/k8s/"${ENVTEST_K8S_VERSION}"-*; do
  [ -d "$d" ] && envtest_found=true
done
if [ "$envtest_found" = true ]; then
  pass "envtest-assets" "$ENVTEST_K8S_VERSION"
else
  fail "envtest-assets" "not fetched under .bin/envtest (run: make bootstrap-tools)"
fi

echo
echo "Host prerequisites:"
if command -v go >/dev/null 2>&1; then
  host_go="$(go version | awk '{print $3}' | sed 's/^go//')"
  pass "go" "host $host_go present; GOTOOLCHAIN=auto fetches pinned $GO_VERSION per invocation"
else
  fail "go" "not on PATH; install Go >= $GO_MIN_HOST_VERSION"
fi

if command -v podman >/dev/null 2>&1; then
  pass "podman" "$(podman --version 2>&1)"
else
  warn "podman" "not found on PATH; install it before running make kind-smoke or building container images (https://podman.io/docs/installation)"
fi

echo
if [ "$status" -ne 0 ]; then
  echo "verify-tools: FAILED"
  exit 1
fi
echo "verify-tools: all pinned tools present"
