#!/usr/bin/env bash
# Installs every repository-owned build and security tool into .bin/, pinned
# to the exact versions recorded in versions.env. This never touches the
# system Go toolchain, PATH, or a package manager: everything lands under the
# repo so a laptop and a CI runner end up with byte-identical tool versions.
#
# Go-ecosystem CLIs (kubebuilder, controller-gen, kustomize, setup-envtest,
# kind, helm, syft, grype, cosign, kube-linter, kubeconform, trivy) are
# installed with `go install module@version`. That pin is exact and
# checksum-verified by the Go module system itself (GOSUMDB), and it builds
# for whatever OS/arch is running this script without per-platform download
# logic. kubectl and oc have no Go module story for their released binaries,
# so they are fetched directly and verified against the checksums their
# projects publish alongside each release.
set -Eeuo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
root="$(cd "$here/../.." && pwd)"
bin="$root/.bin"
mkdir -p "$bin"

# shellcheck source=versions.env
source "$here/versions.env"

log() { printf '==> %s\n' "$*"; }
die() { printf 'bootstrap-tools: %s\n' "$*" >&2; exit 1; }

command -v go >/dev/null 2>&1 || die "go is not on PATH; install Go >= ${GO_MIN_HOST_VERSION} first (GOTOOLCHAIN=auto fetches ${GO_VERSION} automatically from there)"
command -v curl >/dev/null 2>&1 || die "curl is not on PATH"
command -v sha256sum >/dev/null 2>&1 || die "sha256sum is not on PATH"

os="$(uname -s | tr '[:upper:]' '[:lower:]')"
arch="$(uname -m)"
case "$arch" in
  x86_64) arch=amd64 ;;
  aarch64|arm64) arch=arm64 ;;
  *) die "unsupported architecture '$arch'" ;;
esac

export GOTOOLCHAIN=auto
export GOBIN="$bin"

go_install() {
  local module="$1" version="$2" name="$3" extra_env="${4-}"
  log "installing $name $version"
  if [ -n "$extra_env" ]; then
    env "$extra_env" go install "${module}@${version}"
  else
    go install "${module}@${version}"
  fi
}

go_install sigs.k8s.io/kubebuilder/v4 "$KUBEBUILDER_VERSION" kubebuilder
go_install sigs.k8s.io/controller-tools/cmd/controller-gen "$CONTROLLER_GEN_VERSION" controller-gen
go_install sigs.k8s.io/kustomize/kustomize/v5 "$KUSTOMIZE_VERSION" kustomize
go_install sigs.k8s.io/controller-runtime/tools/setup-envtest "$SETUP_ENVTEST_VERSION" setup-envtest
go_install sigs.k8s.io/kind "$KIND_VERSION" kind
go_install helm.sh/helm/v3/cmd/helm "$HELM_VERSION" helm
go_install github.com/anchore/syft/cmd/syft "$SYFT_VERSION" syft
go_install github.com/anchore/grype/cmd/grype "$GRYPE_VERSION" grype
go_install github.com/sigstore/cosign/v2/cmd/cosign "$COSIGN_VERSION" cosign
go_install golang.stackrox.io/kube-linter/cmd/kube-linter "$KUBE_LINTER_VERSION" kube-linter
go_install github.com/yannh/kubeconform/cmd/kubeconform "$KUBECONFORM_VERSION" kubeconform
# Trivy's stdlib JSON handling needs the experimental encoding/json/v2
# package; without GOEXPERIMENT=jsonv2 the build fails on excluded files.
go_install github.com/aquasecurity/trivy/cmd/trivy "$TRIVY_VERSION" trivy "GOEXPERIMENT=jsonv2"

fetch_checked() {
  local url="$1" sha_url="$2" dest="$3"
  local tmp
  tmp="$(mktemp)"
  curl -sSL --fail -o "$tmp" "$url" || die "download failed: $url"
  local expected
  expected="$(curl -sSL --fail "$sha_url")" || die "checksum fetch failed: $sha_url"
  echo "${expected}  ${tmp}" | sha256sum -c - >/dev/null || die "checksum mismatch for $url"
  install -m 0755 "$tmp" "$dest"
  rm -f "$tmp"
}

log "installing kubectl $KUBECTL_VERSION"
fetch_checked \
  "https://dl.k8s.io/release/${KUBECTL_VERSION}/bin/${os}/${arch}/kubectl" \
  "https://dl.k8s.io/release/${KUBECTL_VERSION}/bin/${os}/${arch}/kubectl.sha256" \
  "$bin/kubectl"

log "installing oc $OC_VERSION"
case "${os}-${arch}" in
  linux-amd64)  oc_tar="openshift-client-linux-${OC_VERSION}.tar.gz" ;;
  linux-arm64)  oc_tar="openshift-client-linux-arm64-${OC_VERSION}.tar.gz" ;;
  darwin-amd64) oc_tar="openshift-client-mac-${OC_VERSION}.tar.gz" ;;
  darwin-arm64) oc_tar="openshift-client-mac-arm64-${OC_VERSION}.tar.gz" ;;
  *) die "no oc archive known for ${os}/${arch}" ;;
esac
oc_base="https://mirror.openshift.com/pub/openshift-v4/clients/ocp/${OC_VERSION}"
oc_tmp="$(mktemp -d)"
trap 'rm -rf "$oc_tmp"' EXIT
curl -sSL --fail -o "$oc_tmp/$oc_tar" "$oc_base/$oc_tar" || die "download failed: $oc_base/$oc_tar"
oc_expected="$(curl -sSL --fail "$oc_base/sha256sum.txt" | awk -v f="$oc_tar" '$2 == f {print $1}')"
[ -n "$oc_expected" ] || die "no checksum entry for $oc_tar"
echo "${oc_expected}  ${oc_tmp}/${oc_tar}" | sha256sum -c - >/dev/null || die "checksum mismatch for $oc_tar"
tar xzf "$oc_tmp/$oc_tar" -C "$oc_tmp" oc
install -m 0755 "$oc_tmp/oc" "$bin/oc"

log "installing envtest Kubernetes ${ENVTEST_K8S_VERSION} assets (kube-apiserver, etcd, kubectl)"
envtest_path="$("$bin/setup-envtest" use "$ENVTEST_K8S_VERSION" --bin-dir "$root/.bin/envtest" -p path)"
log "envtest assets at $envtest_path"

log "all tools installed into $bin"
log "Podman is a host prerequisite (system container engine, not a relocatable binary) — install it separately; make verify-tools checks for it"
