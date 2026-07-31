# MarimoHub

MarimoHub is a FastAPI, SvelteKit, and PostgreSQL/pgvector application for publishing and running Marimo notebooks.

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

- Domain vocabulary, roles, and product decisions: [CONTEXT.md](CONTEXT.md)
- Frontend rebuild scope and milestones: [IMPLEMENTATION_PLAN.md](IMPLEMENTATION_PLAN.md)
- Frontend install, test, build, and container instructions: [frontend/README.md](frontend/README.md)
