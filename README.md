# MarimoHub

MarimoHub is a FastAPI, SvelteKit, and PostgreSQL/pgvector application for publishing and running
marimo notebooks, backed by a Go operator that reconciles `MarimoSession` Kubernetes resources into
hardened notebook Runtime Pods and Services.

## Local Development

Copy `.env.example` to `.env` for host-based backend runs, or use compose defaults:

```bash
podman-compose up
```

Backend host loop:

```bash
cd backend
uv sync
uv run alembic upgrade head
uv run uvicorn app.main:app --reload --port 8000
```

Health check:

```bash
curl localhost:8000/api/health
```

Frontend host loop (see `frontend/README.md` for the full command set, including build and browser
test instructions):

```bash
cd frontend
npm ci
cp .env.example .env
npm run dev
```

## Documentation

- Domain vocabulary, roles, and product relationships: [CONTEXT.md](CONTEXT.md)
- The active implementation plan for the operator, backend Runtime lifecycle, images, Helm chart,
  and release gates: [IMPLEMENTATION_PLAN.md](IMPLEMENTATION_PLAN.md)
- Backend design history and gap analysis: [DESIGN.md](DESIGN.md)
- The accepted Runtime credential and internal API decision:
  [docs/adr/0001-runtime-credentials-and-internal-api.md](docs/adr/0001-runtime-credentials-and-internal-api.md)
- Completed and superseded design drafts, retained as history: [docs/plans/](docs/plans/)
- Cluster-side Runtime manifests: [deploy/README.md](deploy/README.md)
- Frontend install, test, build, and container instructions: [frontend/README.md](frontend/README.md)

`hack/tools/versions.env` pins the exact toolchain versions `make bootstrap-tools` and
`make verify-tools` install and check.
