# GO_VERSION is read from this module's own go.mod rather than duplicated
# here, so a version bump never has two places to edit. It forces the exact
# toolchain (not GOTOOLCHAIN=auto's own resolution) when `go install`-ing a
# tool such as golangci-lint whose own go.mod requests an older Go: without
# forcing it, that tool gets built with ITS preferred (older) toolchain and
# then refuses to lint a module whose go directive is newer than the
# compiler that built it.
GO_VERSION := $(shell awk '/^go [0-9]/ { print $$2; exit }' go.mod)

# IMG is the manager image reference used by build-installer/deploy. The
# Containerfile that produces it lives at the module root once the operator
# image is built; there is no docker-build target here since image builds
# are coordinated by the root Makefile's `images` target instead.
IMG ?= controller:latest

# YEAR defines the year value used for substituting the YEAR placeholder in the boilerplate header.
YEAR ?= $(shell date +%Y)

# Get the currently used golang install path (in GOPATH/bin, unless GOBIN is set)
ifeq (,$(shell go env GOBIN))
GOBIN=$(shell go env GOPATH)/bin
else
GOBIN=$(shell go env GOBIN)
endif

# Setting SHELL to bash allows bash commands to be executed by recipes.
# Options are set to exit when a recipe line exits non-zero or a piped command fails.
SHELL = /usr/bin/env bash -o pipefail
.SHELLFLAGS = -ec

.PHONY: all
all: build

##@ General

# The help target prints out all targets with their descriptions organized
# beneath their categories. The categories are represented by '##@' and the
# target descriptions by '##'. The awk command is responsible for reading the
# entire set of makefiles included in this invocation, looking for lines of the
# file as xyz: ## something, and then pretty-format the target and help. Then,
# if there's a line with ##@ something, that gets pretty-printed as a category.
# More info on the usage of ANSI control characters for terminal formatting:
# https://en.wikipedia.org/wiki/ANSI_escape_code#SGR_parameters
# More info on the awk command:
# http://linuxcommand.org/lc3_adv_awk.php

.PHONY: help
help: ## Display this help.
	@awk 'BEGIN {FS = ":.*##"; printf "\nUsage:\n  make \033[36m<target>\033[0m\n"} /^[a-zA-Z_0-9-]+:.*?##/ { printf "  \033[36m%-15s\033[0m %s\n", $$1, $$2 } /^##@/ { printf "\n\033[1m%s\033[0m\n", substr($$0, 5) } ' $(MAKEFILE_LIST)

##@ Development

.PHONY: manifests
manifests: controller-gen ## Generate CRD and RBAC manifests.
	"$(CONTROLLER_GEN)" rbac:roleName=manager-role crd paths="./..." output:crd:artifacts:config=config/crd/bases

.PHONY: generate
generate: controller-gen ## Generate code containing DeepCopy, DeepCopyInto, and DeepCopyObject method implementations.
	"$(CONTROLLER_GEN)" object:headerFile="hack/boilerplate.go.txt",year=$(YEAR) paths="./..."

.PHONY: fmt
fmt: ## Reformat code with go fmt.
	go fmt ./...

.PHONY: fmt-check
fmt-check: ## Fail if any file is not gofmt-formatted, without modifying anything.
	@out="$$(gofmt -l .)"; \
	if [ -n "$$out" ]; then \
		echo "gofmt needs to be run on:"; \
		echo "$$out"; \
		exit 1; \
	fi

.PHONY: vet
vet: ## Run go vet against code.
	go vet ./...

.PHONY: lint
lint: golangci-lint ## Run golangci-lint linter.
	"$(GOLANGCI_LINT)" run

.PHONY: lint-fix
lint-fix: golangci-lint ## Run golangci-lint linter and perform fixes.
	"$(GOLANGCI_LINT)" run --fix

.PHONY: lint-config
lint-config: golangci-lint ## Verify golangci-lint linter configuration.
	"$(GOLANGCI_LINT)" config verify

.PHONY: test-unit
test-unit: manifests generate ## Run pure unit tests (no envtest API server).
	go test $$(go list ./... | grep -v /internal/controller) -count=1

.PHONY: test
test: manifests generate setup-envtest ## Run the envtest suite (real API server, no kubelet/quota/GC).
	KUBEBUILDER_ASSETS="$$($(ENVTEST) use $(ENVTEST_K8S_VERSION) --bin-dir "$(ENVTEST_BIN_DIR)" -p path)" \
		go test ./internal/controller/... -count=1 -coverprofile cover.out

.PHONY: check
check: fmt-check vet lint test-unit ## gofmt, go vet, static analysis, and unit tests (no cluster).

##@ Build

.PHONY: build
build: manifests generate fmt vet ## Build manager binary.
	go build -o bin/manager cmd/main.go

.PHONY: run
run: manifests generate fmt vet ## Run a controller from your host.
	go run ./cmd/main.go

.PHONY: build-installer
build-installer: manifests generate kustomize ## Generate a consolidated YAML with CRDs and the Deployment.
	mkdir -p dist
	cd config/manager && "$(KUSTOMIZE)" edit set image controller=${IMG}
	"$(KUSTOMIZE)" build config/default > dist/install.yaml

##@ Deployment

ifndef ignore-not-found
  ignore-not-found = false
endif

.PHONY: install
install: manifests kustomize ## Install CRDs and the label-identity policy into the K8s cluster specified in ~/.kube/config.
	@out="$$( "$(KUSTOMIZE)" build config/crd 2>/dev/null || true )"; \
	if [ -n "$$out" ]; then echo "$$out" | "$(KUBECTL)" apply -f -; else echo "No CRDs to install; skipping."; fi
	"$(KUBECTL)" apply -f config/policy/marimosession_label_identity.yaml

.PHONY: uninstall
uninstall: manifests kustomize ## Uninstall CRDs and the label-identity policy from the K8s cluster specified in ~/.kube/config. Call with ignore-not-found=true to ignore resource not found errors during deletion.
	"$(KUBECTL)" delete --ignore-not-found=$(ignore-not-found) -f config/policy/marimosession_label_identity.yaml
	@out="$$( "$(KUSTOMIZE)" build config/crd 2>/dev/null || true )"; \
	if [ -n "$$out" ]; then echo "$$out" | "$(KUBECTL)" delete --ignore-not-found=$(ignore-not-found) -f -; else echo "No CRDs to delete; skipping."; fi

.PHONY: deploy
deploy: manifests kustomize ## Deploy controller to the K8s cluster specified in ~/.kube/config. Requires IMG to point at a built manager image.
	cd config/manager && "$(KUSTOMIZE)" edit set image controller=${IMG}
	"$(KUSTOMIZE)" build config/default | "$(KUBECTL)" apply -f -
	"$(KUBECTL)" apply -f config/policy/marimosession_label_identity.yaml

.PHONY: undeploy
undeploy: kustomize ## Undeploy controller from the K8s cluster specified in ~/.kube/config. Call with ignore-not-found=true to ignore resource not found errors during deletion.
	"$(KUBECTL)" delete --ignore-not-found=$(ignore-not-found) -f config/policy/marimosession_label_identity.yaml
	"$(KUSTOMIZE)" build config/default | "$(KUBECTL)" delete --ignore-not-found=$(ignore-not-found) -f -

##@ Dependencies
#
# controller-gen, kustomize, setup-envtest, and kubectl are the repository's
# pinned toolchain from hack/tools/versions.env, installed once into the
# repo-root .bin/ by `make bootstrap-tools` (see root Makefile). Reaching
# into that directory here, rather than letting this Makefile `go install`
# its own copies at whatever version happens to be current, is what keeps
# every part of the repo -- not just this module -- on the exact same
# controller-gen/kustomize/envtest version set. golangci-lint is this Go
# module's own concern (no other component needs it), so it is fetched
# locally at the pinned version below, matching kubebuilder's own convention.
ROOT_DIR := $(abspath ..)
ROOT_BIN := $(ROOT_DIR)/.bin
ENVTEST_BIN_DIR := $(ROOT_BIN)/envtest

## Location to install this module's own tool dependencies to.
LOCALBIN ?= $(shell pwd)/bin
$(LOCALBIN):
	mkdir -p "$(LOCALBIN)"

## Tool binaries
KUBECTL ?= $(ROOT_BIN)/kubectl
KUSTOMIZE ?= $(ROOT_BIN)/kustomize
CONTROLLER_GEN ?= $(ROOT_BIN)/controller-gen
ENVTEST ?= $(ROOT_BIN)/setup-envtest
GOLANGCI_LINT = $(LOCALBIN)/golangci-lint

## Kubernetes minor envtest targets; must match hack/tools/versions.env's
## ENVTEST_K8S_VERSION so this module's tests run against the same assets
## `make bootstrap-tools` already downloaded.
ENVTEST_K8S_VERSION ?= 1.35.0

GOLANGCI_LINT_VERSION ?= v2.12.2

.PHONY: kustomize
kustomize: ## Verify the repo-root pinned kustomize binary is present.
	@test -x "$(KUSTOMIZE)" || { echo "missing $(KUSTOMIZE); run 'make bootstrap-tools' from the repo root"; exit 1; }

.PHONY: controller-gen
controller-gen: ## Verify the repo-root pinned controller-gen binary is present.
	@test -x "$(CONTROLLER_GEN)" || { echo "missing $(CONTROLLER_GEN); run 'make bootstrap-tools' from the repo root"; exit 1; }

.PHONY: setup-envtest
setup-envtest: ## Verify the repo-root pinned setup-envtest binary and its Kubernetes assets are present.
	@test -x "$(ENVTEST)" || { echo "missing $(ENVTEST); run 'make bootstrap-tools' from the repo root"; exit 1; }
	@"$(ENVTEST)" use $(ENVTEST_K8S_VERSION) --bin-dir "$(ENVTEST_BIN_DIR)" -p path >/dev/null || { \
		echo "envtest assets for Kubernetes $(ENVTEST_K8S_VERSION) are missing; run 'make bootstrap-tools' from the repo root"; \
		exit 1; \
	}

.PHONY: golangci-lint
golangci-lint: $(GOLANGCI_LINT) ## Download golangci-lint locally if necessary.
$(GOLANGCI_LINT): $(LOCALBIN)
	$(call go-install-tool,$(GOLANGCI_LINT),github.com/golangci/golangci-lint/v2/cmd/golangci-lint,$(GOLANGCI_LINT_VERSION))
	@test -f .custom-gcl.yml && { \
		echo "Building custom golangci-lint with plugins..." && \
		GOTOOLCHAIN=go$(GO_VERSION) $(GOLANGCI_LINT) custom --destination $(LOCALBIN) --name golangci-lint-custom && \
		mv -f $(LOCALBIN)/golangci-lint-custom $(GOLANGCI_LINT); \
	} || true

# go-install-tool will 'go install' any package with custom target and name of binary, if it doesn't exist
# $1 - target path with name of binary
# $2 - package url which can be installed
# $3 - specific version of package
define go-install-tool
@[ -f "$(1)-$(3)" ] && [ "$$(readlink -- "$(1)" 2>/dev/null)" = "$(1)-$(3)" ] || { \
set -e; \
package=$(2)@$(3) ;\
echo "Downloading $${package}" ;\
rm -f "$(1)" ;\
GOBIN="$(LOCALBIN)" GOTOOLCHAIN=go$(GO_VERSION) go install $${package} ;\
mv "$(LOCALBIN)/$$(basename "$(1)")" "$(1)-$(3)" ;\
} ;\
ln -sf "$$(realpath "$(1)-$(3)")" "$(1)"
endef
