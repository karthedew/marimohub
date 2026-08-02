COMPOSE_FILE := podman-compose.yml
COMPOSE_PROJECT := marimohub
BACKEND_VENV_VOLUME := $(COMPOSE_PROJECT)_backend-venv
DOCKER_COMPOSE_CMD := docker compose
PODMAN_COMPOSE_CMD := $(shell if command -v podman >/dev/null 2>&1 && podman compose version >/dev/null 2>&1; then printf 'podman compose'; elif command -v podman-compose >/dev/null 2>&1; then printf 'podman-compose'; fi)
ENGINE ?= docker

ifeq ($(ENGINE),docker)
COMPOSE_CMD := $(DOCKER_COMPOSE_CMD)
else ifeq ($(ENGINE),podman)
COMPOSE_CMD := $(if $(PODMAN_COMPOSE_CMD),$(PODMAN_COMPOSE_CMD),podman compose)
else
$(error Unsupported ENGINE '$(ENGINE)'; use ENGINE=docker or ENGINE=podman)
endif

COMPOSE := $(COMPOSE_CMD) -f $(COMPOSE_FILE) -p $(COMPOSE_PROJECT)

.PHONY: help build up up-d down down-v logs ps restart health \
	backend-build backend-up backend-restart backend-logs refresh-deps \
	docker-build docker-up docker-up-d docker-down docker-down-v docker-logs docker-ps docker-restart docker-health \
	docker-backend-build docker-backend-up docker-backend-restart docker-backend-logs docker-refresh-deps \
	podman-build podman-up podman-up-d podman-down podman-down-v podman-logs podman-ps podman-restart podman-health \
	podman-backend-build podman-backend-up podman-backend-restart podman-backend-logs podman-refresh-deps

help:
	@printf '%s\n' \
	  'Usage:' \
	  '  make up ENGINE=docker' \
	  '  make up ENGINE=podman' \
	  '  make docker-up' \
	  '  make podman-up' \
	  '' \
	  'Stack targets:' \
	  '  build           Build all images' \
	  '  up              Build and start the stack in the foreground' \
	  '  up-d            Build and start the stack in the background' \
	  '  down            Stop the stack (keeps volumes/database)' \
	  '  down-v          Stop the stack and remove volumes (destroys the database)' \
	  '  logs            Follow all service logs' \
	  '  ps              Show service status' \
	  '  restart         Rebuild and restart the whole stack in the background' \
	  '  health          Check the backend health endpoint' \
	  '' \
	  'Backend / dev targets:' \
	  '  backend-build   Build the backend image only' \
	  '  backend-up      Build and start the backend only, in the background' \
	  '  backend-restart Restart the backend without rebuilding (for code-only changes)' \
	  '  backend-logs    Follow backend logs only' \
	  '  refresh-deps    Drop the backend venv volume and rebuild (after dependency changes)' \
	  '' \
	  'Every target also has docker-<name> and podman-<name> variants, e.g.' \
	  '  make docker-refresh-deps   make podman-backend-restart'

build:
	@$(MAKE) _require-compose ENGINE=$(ENGINE)
	@$(COMPOSE) build

up:
	@$(MAKE) _require-compose ENGINE=$(ENGINE)
	@$(COMPOSE) up --build

up-d:
	@$(MAKE) _require-compose ENGINE=$(ENGINE)
	@$(COMPOSE) up --build -d

down:
	@$(MAKE) _require-compose ENGINE=$(ENGINE)
	@$(COMPOSE) down

down-v:
	@$(MAKE) _require-compose ENGINE=$(ENGINE)
	@$(COMPOSE) down -v

logs:
	@$(MAKE) _require-compose ENGINE=$(ENGINE)
	@$(COMPOSE) logs -f

ps:
	@$(MAKE) _require-compose ENGINE=$(ENGINE)
	@$(COMPOSE) ps

restart:
	@$(MAKE) _require-compose ENGINE=$(ENGINE)
	@$(COMPOSE) down
	@$(COMPOSE) up --build -d

health:
	@curl --fail --silent --show-error http://localhost:8000/api/health

backend-build:
	@$(MAKE) _require-compose ENGINE=$(ENGINE)
	@$(COMPOSE) build backend

backend-up:
	@$(MAKE) _require-compose ENGINE=$(ENGINE)
	@$(COMPOSE) up --build -d backend

backend-restart:
	@$(MAKE) _require-compose ENGINE=$(ENGINE)
	@$(COMPOSE) restart backend

backend-logs:
	@$(MAKE) _require-compose ENGINE=$(ENGINE)
	@$(COMPOSE) logs -f backend

# Dependency changes are not picked up by a plain rebuild because the named
# backend venv volume shadows the image's baked virtualenv. Drop it so uv resyncs.
refresh-deps:
	@$(MAKE) _require-compose ENGINE=$(ENGINE)
	@$(COMPOSE) down
	-@$(ENGINE) volume rm $(BACKEND_VENV_VOLUME)
	@$(COMPOSE) up --build -d

docker-build:
	@$(MAKE) build ENGINE=docker

docker-up:
	@$(MAKE) up ENGINE=docker

docker-up-d:
	@$(MAKE) up-d ENGINE=docker

docker-down:
	@$(MAKE) down ENGINE=docker

docker-down-v:
	@$(MAKE) down-v ENGINE=docker

docker-logs:
	@$(MAKE) logs ENGINE=docker

docker-ps:
	@$(MAKE) ps ENGINE=docker

docker-restart:
	@$(MAKE) restart ENGINE=docker

docker-health:
	@$(MAKE) health ENGINE=docker

docker-backend-build:
	@$(MAKE) backend-build ENGINE=docker

docker-backend-up:
	@$(MAKE) backend-up ENGINE=docker

docker-backend-restart:
	@$(MAKE) backend-restart ENGINE=docker

docker-backend-logs:
	@$(MAKE) backend-logs ENGINE=docker

docker-refresh-deps:
	@$(MAKE) refresh-deps ENGINE=docker

podman-build:
	@$(MAKE) build ENGINE=podman

podman-up:
	@$(MAKE) up ENGINE=podman

podman-up-d:
	@$(MAKE) up-d ENGINE=podman

podman-down:
	@$(MAKE) down ENGINE=podman

podman-down-v:
	@$(MAKE) down-v ENGINE=podman

podman-logs:
	@$(MAKE) logs ENGINE=podman

podman-ps:
	@$(MAKE) ps ENGINE=podman

podman-restart:
	@$(MAKE) restart ENGINE=podman

podman-health:
	@$(MAKE) health ENGINE=podman

podman-backend-build:
	@$(MAKE) backend-build ENGINE=podman

podman-backend-up:
	@$(MAKE) backend-up ENGINE=podman

podman-backend-restart:
	@$(MAKE) backend-restart ENGINE=podman

podman-backend-logs:
	@$(MAKE) backend-logs ENGINE=podman

podman-refresh-deps:
	@$(MAKE) refresh-deps ENGINE=podman

# ── Coordinating targets ────────────────────────────────────────────────────
# The compose targets above remain the direct local dev loop. These targets
# are the shared surface CI and every contributor call instead of reaching
# into each component's own tooling by hand. Component tooling (backend's uv,
# frontend's npm, the operator's own generated Makefile) stays directly
# runnable from its own directory; nothing here replaces that, it just
# coordinates it.

OPERATOR_DIR := marimohub-operator
CHARTS_DIR := charts/marimohub
GENERATED_CRD := $(OPERATOR_DIR)/config/crd/bases/marimohub.io_marimosessions.yaml
KIND_SMOKE := hack/smoke/kind/run.sh
OPENSHIFT_SMOKE := hack/smoke/openshift/run.sh

.PHONY: bootstrap-tools verify-tools generate verify-generated \
	operator-check operator-test backend-check frontend-check \
	images helm-check kind-smoke openshift-smoke verify

bootstrap-tools:
	@hack/tools/bootstrap.sh

verify-tools:
	@hack/tools/verify.sh

# Regenerates deepcopy/CRD/RBAC from the operator's Go source, then copies
# the one generated CRD byte-for-byte to every place it must also live.
# deploy/crd/marimosession.yaml exists today; charts/marimohub/crds is copied
# to only once the chart directory exists, so this stays a plain conditional
# rather than a stub that manufactures an empty chart layout ahead of time.
generate:
	$(MAKE) -C $(OPERATOR_DIR) generate manifests
	cp "$(GENERATED_CRD)" deploy/crd/marimosession.yaml
	@if [ -d "$(CHARTS_DIR)/crds" ]; then \
		cp "$(GENERATED_CRD)" "$(CHARTS_DIR)/crds/marimohub.io_marimosessions.yaml"; \
	fi

verify-generated:
	$(MAKE) generate
	@git diff --quiet -- $(OPERATOR_DIR) deploy/crd $(CHARTS_DIR)/crds 2>/dev/null || { \
		echo "verify-generated: generated output changed the worktree"; \
		git status --short -- $(OPERATOR_DIR) deploy/crd $(CHARTS_DIR)/crds; \
		exit 1; \
	}

operator-check:
	$(MAKE) -C $(OPERATOR_DIR) check

operator-test:
	$(MAKE) -C $(OPERATOR_DIR) test

backend-check:
	cd backend && uv run ruff format --check . && uv run ruff check . && uv run ty check && uv run pytest -q

frontend-check:
	@hack/tools/check-node-version.sh
	cd frontend && npm run check && npm run test:unit && npm run build

# Builds every production image. Each Runtime flavor and the operator/fetcher
# build from their own directory so their Containerfile's COPY paths stay
# relative to the image they actually produce, rather than every image
# reaching across the repo root's build context for its own sources.
images:
	$(ENGINE) build -f Containerfile.backend -t marimohub-backend:dev .
	$(ENGINE) build -f Containerfile.frontend -t marimohub-frontend:dev .
	$(ENGINE) build -f $(OPERATOR_DIR)/Containerfile -t marimohub-operator:dev $(OPERATOR_DIR)
	$(ENGINE) build -f images/source-fetcher/Containerfile -t marimohub-source-fetcher:dev images/source-fetcher
	$(ENGINE) build -f images/marimo-runtime/Containerfile.ubuntu -t marimohub-runtime-ubuntu:dev images/marimo-runtime
	$(ENGINE) build -f images/marimo-runtime/Containerfile.ubi -t marimohub-runtime-ubi:dev images/marimo-runtime

helm-check:
	@if [ -d "$(CHARTS_DIR)" ]; then \
		.bin/helm lint $(CHARTS_DIR); \
	else \
		echo "helm-check: $(CHARTS_DIR) does not exist yet (Phase 6 creates it)"; \
		exit 1; \
	fi

kind-smoke:
	@if [ -x "$(KIND_SMOKE)" ]; then \
		"$(KIND_SMOKE)"; \
	else \
		echo "kind-smoke: $(KIND_SMOKE) does not exist yet (Phase 8 adds it)"; \
		exit 1; \
	fi

openshift-smoke:
	@if [ -x "$(OPENSHIFT_SMOKE)" ]; then \
		"$(OPENSHIFT_SMOKE)"; \
	else \
		echo "openshift-smoke: $(OPENSHIFT_SMOKE) does not exist yet (Phase 9 adds it)"; \
		exit 1; \
	fi

# All checks that do not require a live cluster. Working checks run first so
# a failure further down never hides their result. operator-check and
# helm-check currently fail because their components do not exist yet
# (Phases 1 and 6); that is the correct, honest state of this target until
# those phases land.
verify: verify-tools verify-generated backend-check frontend-check operator-check helm-check

_require-compose:
	@if [ "$(ENGINE)" = 'docker' ]; then \
		command -v docker >/dev/null 2>&1 || { printf '%s\n' 'docker is not installed or not on PATH'; exit 1; }; \
	elif [ "$(ENGINE)" = 'podman' ]; then \
		if [ -z "$(PODMAN_COMPOSE_CMD)" ]; then \
			printf '%s\n' 'podman compose (or podman-compose) is not available'; \
			exit 1; \
		fi; \
	else \
		printf '%s\n' "Unsupported ENGINE '$(ENGINE)'; use docker or podman"; \
		exit 1; \
	fi
