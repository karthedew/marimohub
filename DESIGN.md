# MarimoHub — Design Decisions & MVP Roadmap

Captured from initial planning session (2026-06-12).

---

## What Is MarimoHub?

MarimoHub is a Marimo notebook hub for scientists and engineers. The core loop:

1. **Create** — author a Marimo notebook (blank, upload `.py`, or import from GitLab)
2. **Publish** — share it with the world (or just collaborators) via a discovery hub
3. **Deploy** — give it a stable URL and a live Python kernel; notebooks can receive Grafana Data Action payloads
4. **Fork** — clone any deployed notebook into your own draft and iterate

---

## Tech Stack

| Layer | Choice | Notes |
|---|---|---|
| Backend | Python FastAPI | UV-managed, Python 3.12+ |
| Frontend | SvelteKit + Tailwind CSS | Dark/light mode via Tailwind `class` strategy |
| Database | PostgreSQL 16 | pgvector extension for semantic search |
| Notebook runtime | Marimo subprocesses | edit / run / deploy modes |
| Embeddings | `sentence-transformers` `all-MiniLM-L6-v2` | Local, no external API, air-gap safe |
| Local dev | Podman Compose | `podman-compose.yml` |
| Production target | OpenShift on RHEL | OpenShift resource quotas replace MVP global cap |

---

## Architecture Decisions

### Authentication

- **MVP:** Basic username/password stored in Postgres (bcrypt hashed), JWT session tokens
- **Future:** SAML / OAuth 2.0 (e.g., ORCID, GitHub, institutional SSO)
- Auth layer is abstracted behind an `AuthService` interface to make the swap clean
- **Access model:** anonymous users can browse and run notebooks; auth required for create, publish, deploy, fork

### Notebook Execution

WASM was considered and rejected — PyTorch and other C-extension packages are required and are not Pyodide-compatible. All execution is server-side via Marimo subprocesses.

Three process modes:

- **Edit mode** (`marimo edit`) — creating/editing a draft notebook
- **Run mode** (`marimo run`) — ephemeral session for viewing/running a published notebook
- **Deploy mode** (`marimo run`) — persistent named process with a stable URL

### Runtime Package Environments

Marimo subprocesses inherit the backend's interpreter, so packages installed from an edit or run session (via marimo's in-notebook package manager) land in the **shared backend `.venv`**, global to the backend process and visible to every notebook.

The backend itself runs under `uv run`, which exports `UV` and `UV_PROJECT_ENVIRONMENT`. Those markers make marimo treat the notebook as part of the backend's uv *project* and install with `uv add` against the backend's own `pyproject.toml`, which fails. Spawned marimo processes therefore run with those two variables stripped from the environment (`_marimo_env` in `process_manager.py`), so marimo falls back to `uv pip install` into the active `.venv`.

This is intentional for the MVP — there is no per-notebook environment isolation yet. The seam for that follow-up is marimo's `--sandbox` flag, which runs a notebook in an isolated `uv` environment derived from its PEP 723 inline script metadata. Isolation is a one-line change at the command-construction point (`_marimo_command` in `process_manager.py`): add `--sandbox` and persist a per-notebook dependency manifest. No new subsystem is required, which is why the global-`.venv` choice is safe to ship now.

Edit sessions autosave to a working-copy file; on session end the backend reads that file back and persists it to `notebooks.source` (`_persist_edit_session`), so edits survive across sessions and are what deployments serve.

### Deploy Lifecycle

- Deployed notebooks **spin up on the first request** and **sleep after idle timeout** (default `IDLE_TIMEOUT_MINUTES=10`, configurable via env var)
- A background asyncio task (idle reaper) checks every 60 seconds and kills idle processes
- Deployments are **still reachable when sleeping** — FastAPI buffers the incoming request, wakes the process, waits up to 10 seconds for it to be ready, then forwards
- On server restart, all deployments are marked `sleeping` in Postgres (processes don't survive restarts)
- **MVP resource cap:** `MAX_CONCURRENT_SESSIONS` env var; returns 503 when exceeded
- **Production:** OpenShift resource quotas handle per-notebook limits

### Grafana Data Actions Integration

Two patterns, both supported:

1. **Query params** — Grafana hits `GET /deploy/{slug}?metric=cpu&value=94.2`; notebook reads `mo.query_params` reactively
2. **Data buffer** — Grafana POSTs JSON to `POST /api/notebooks/{id}/data`; FastAPI stores payload in Postgres; notebook fetches `GET /api/notebooks/{id}/data` on load; works correctly even if notebook was sleeping when Grafana fired

### Notebook Storage

- **MVP:** Notebook `.py` source stored as text in the `notebooks` table in Postgres
- **Near-future:** Migrate to MinIO (S3-compatible object storage) — works locally and on OpenShift
- A `NotebookStorageService` abstraction (`get` / `put` / `delete`) is used from day one so the swap is a drop-in replacement

### Discovery & Search

Two search mechanisms run in parallel:

- **Full-text search:** Postgres `tsvector` generated column on title + description + tags, with a GIN index
- **Semantic search:** `pgvector` column (`vector(384)`) populated at publish time using `all-MiniLM-L6-v2`; cosine similarity queries for "find notebooks like this one"

### Notebook Visibility

Three-state enum on every notebook:

- `draft` — only the owner can see it
- `unlisted` — accessible via direct link, not in the discovery feed
- `public` — appears in discovery/search

### Forking

- Fork creates a **new `draft` notebook** in the forker's account with a `parent_id` pointing to the original
- Fork count is displayed on the original notebook's detail page
- The lineage ("forked from…") is surfaced in the UI
- Forks follow the normal publish flow: draft → unlisted → public

### Notebook Ingestion (How Users Add Notebooks)

Three paths:

1. **Create blank** — main CTA on the landing page ("Create new Notebook"); spins up a `marimo edit` session immediately
2. **Upload `.py` file** — for scientists with existing notebooks
3. **GitLab raw file URL** — paste a raw file URL; backend fetches with `httpx`; supports private repos via Personal Access Token (PAT); GitHub support to follow GitLab

---

## Database Schema (Key Tables)

### `users`

```
id, username, email, password_hash, created_at
```

### `notebooks`

```
id, user_id (FK), parent_id (FK self, nullable),
title, description, tags text[],
source text,
visibility enum('draft','unlisted','public'),
fork_count int default 0,
search_vector tsvector GENERATED (GIN index),
embedding vector(384),
created_at, updated_at
```

### `deployments`

```
id, notebook_id (FK), slug (unique),
status enum('running','sleeping','stopped'),
port int,
last_active timestamptz,
created_at
```

### `notebook_data`

```
id, notebook_id (FK),
payload jsonb,
source varchar,
created_at
```

---

## API Surface (Key Endpoints)

```
POST   /api/auth/register
POST   /api/auth/login
POST   /api/auth/logout

GET    /api/notebooks              # discover (q=, tags=, semantic=, page=)
POST   /api/notebooks              # create
GET    /api/notebooks/{id}
PUT    /api/notebooks/{id}
DELETE /api/notebooks/{id}
POST   /api/notebooks/{id}/fork
POST   /api/notebooks/{id}/publish
POST   /api/notebooks/import       # {url, pat?}

POST   /api/sessions               # create run/edit session
DELETE /api/sessions/{id}

POST   /api/notebooks/{id}/deploy
GET    /api/deployments/{slug}     # wakes if sleeping
DELETE /api/deployments/{slug}

POST   /api/notebooks/{id}/data    # Grafana POST
GET    /api/notebooks/{id}/data    # notebook fetches on load

GET    /api/proxy/{session_id}/{path:path}   # HTTP proxy → marimo
WS     /api/proxy/{session_id}/ws            # WebSocket proxy → marimo
```

---

## Frontend Routes (SvelteKit)

```
/                          Landing page — "Create new Notebook" CTA + "Discover"
/discover                  Search + browse (full-text + semantic toggle, tag filters)
/notebooks/new             Create flow: Blank | Upload | GitLab URL tabs
/notebooks/[id]            Notebook detail: metadata, Run, Fork, Deploy
/notebooks/[id]/edit       Edit session (owner only)
/deploy/[slug]             Full-screen deployed notebook (marimo iframe proxy)
/auth/login
/auth/register
```

---

## Podman Compose Services

```
postgres    postgres:16 + pgvector
backend     FastAPI via uvicorn --reload, port 8000
frontend    SvelteKit vite dev, port 5173
```

Marimo subprocesses are spawned by the backend container on ports allocated from `MARIMO_PORT_RANGE` (e.g., `9000-9099`). No separate marimo container for MVP.

---

## Project Directory Structure

```
marimohub/
├── backend/
│   ├── pyproject.toml
│   ├── app/
│   │   ├── main.py
│   │   ├── core/
│   │   │   ├── config.py
│   │   │   └── security.py
│   │   ├── api/
│   │   │   ├── auth.py
│   │   │   ├── notebooks.py
│   │   │   ├── sessions.py
│   │   │   ├── deployments.py
│   │   │   └── data.py
│   │   ├── services/
│   │   │   ├── auth_service.py
│   │   │   ├── notebook_storage.py
│   │   │   ├── process_manager.py
│   │   │   ├── embedding_service.py
│   │   │   └── gitlab_import.py
│   │   ├── models/
│   │   │   ├── user.py
│   │   │   ├── notebook.py
│   │   │   ├── deployment.py
│   │   │   └── notebook_data.py
│   │   └── db/
│   │       ├── database.py
│   │       └── migrations/
├── frontend/
│   ├── package.json
│   ├── tailwind.config.js
│   ├── src/
│   │   ├── routes/
│   │   └── lib/
│   │       ├── api.ts
│   │       └── stores/
├── podman-compose.yml
├── Containerfile.backend
├── .env.example
└── DESIGN.md
```

---

## MVP Implementation Order

1. **Scaffolding** — repo structure, `podman-compose.yml`, Postgres + pgvector, UV backend, SvelteKit frontend
2. **Database** — Alembic migrations for all tables
3. **Auth** — register/login/logout, JWT middleware, auth-gating
4. **Notebook CRUD** — create, read, update, delete, visibility transitions
5. **Process manager** — spawn/proxy/reap marimo subprocesses; edit + run modes
6. **Create flow** — blank notebook → edit session; file upload; GitLab URL import
7. **Publish + discovery** — tsvector search, pgvector semantic search, discover page
8. **Deploy** — stable URLs, cold-start wake, idle reaper
9. **Grafana integration** — query param passthrough + data buffer endpoint
10. **Fork** — copy-on-fork, parent attribution, fork count
11. **UI polish** — dark/light mode, responsive layout, "waking up…" skeleton

---

## Near-Future (Post-MVP)

- MinIO object storage (swap `NotebookStorageService` implementation)
- GitLab repo browser (list `.py` files from a repo URL, not just a raw file)
- GitHub import support
- OAuth 2.0 / SAML auth
- Per-notebook resource limits (CPU/memory via Podman flags; OpenShift quotas in production)
- Notebook dependency manifest (`requirements.txt` or `pyproject.toml` per notebook)
- Grafana data history / audit log
- Notebook versioning / changelog
