# MoLab

MoLab is a FastAPI, SvelteKit, and PostgreSQL/pgvector application for publishing and running Marimo notebooks.

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
