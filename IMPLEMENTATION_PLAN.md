# MoLab — Implementation Plan & Agent Orchestration Guide

Companion to `DESIGN.md`. That document says **what** MoLab is; this one says **how to build it**, broken into tasks sized for an orchestrated agent team. All schema, endpoints, and stack decisions come from `DESIGN.md` — if this document and `DESIGN.md` ever disagree, `DESIGN.md` wins and this file should be corrected.

Target dev machine: **macOS + Podman**. PostgreSQL always runs in a podman container (`pgvector/pgvector:pg16`) — never installed on the host.

---

## 1. Agent Team

| Role | Model | Responsibility |
|---|---|---|
| **Orchestrator** | Fable 5 (`claude-fable-5`) | Owns this plan. Assigns task cards, reviews/merges work, triages bug reports, writes detailed fix plans, re-assigns fixes. Writes no feature code itself. |
| **Opus-1 “Backend Core”** | Opus | Backend scaffold, config, DB models, migrations, auth, notebook CRUD, storage service, search, pytest harness. |
| **Opus-2 “Runtime”** | Opus | Marimo process manager, session/proxy layer, deploy lifecycle + idle reaper, Grafana data endpoints, GitLab import, embedding service. |
| **Opus-3 “Frontend”** | Opus | SvelteKit app: scaffold, theme, API client, auth pages, discover, notebook detail, create flow, edit/run/deploy views, fork UI. |
| **QA** | Sonnet 4.6 (`claude-sonnet-4-6`) | After each phase: full regression (everything implemented so far, not just the new phase). Files bug reports. Verifies fixes. Writes no feature code. |

### Orchestration loop

```mermaid
graph LR
    PLAN[This plan] --> ORCH[Fable 5 Orchestrator]
    ORCH -->|task cards| O1[Opus-1 Backend Core]
    ORCH -->|task cards| O2[Opus-2 Runtime]
    ORCH -->|task cards| O3[Opus-3 Frontend]
    O1 & O2 & O3 -->|branches + DoD evidence| ORCH
    ORCH -->|merge to main| MAIN[(main)]
    MAIN -->|phase complete| QA[Sonnet 4.6 QA]
    QA -->|bug reports docs/bugs/| ORCH
    ORCH -->|fix plan + assignment| O1 & O2 & O3
    QA -->|re-test, close| MAIN
```

### Working protocol

- **Task cards** (§4) are the unit of assignment. The orchestrator hands an agent the card ID, the card text, and any bug-fix plans touching the same files. Agents work **only** their card — anything else they notice goes back to the orchestrator as a note, not a code change.
- **Isolation:** each implementer works in its own git worktree/branch named `agent/<lane>/<card-id>` (e.g. `agent/backend/B3`). Only the orchestrator merges to `main`, after reviewing the diff and checking DoD evidence.
- **Definition of Done** for every card: code complete per card spec; `uv run pytest` green (backend cards include new tests; frontend cards: `npm run check` + `npm run build` green); no TODOs left for in-scope behavior; card's "Verify" line demonstrated.
- **Contract freeze:** the API contract (§5) is frozen so Opus-3 can build against it before the backend lands. Any contract change must go through the orchestrator, which updates §5 *first*, then notifies affected lanes.
- **Phase gates:** a phase ends when all its cards are merged and QA's regression pass reports no open `critical`/`major` bugs. Only then does the orchestrator deal the next phase's cards.

### Bug workflow

1. QA files `docs/bugs/BUG-NNN-short-slug.md` using the template in §6 (status: `open`) and appends a line to `docs/bugs/INDEX.md`.
2. Orchestrator triages: confirms severity, reproduces, root-causes, writes a **Fix Plan** section *into the bug file* (files to change, approach, regression test to add), sets status `triaged`, assigns a lane.
3. Assigned Opus agent implements exactly the fix plan (including the regression test), sets status `fixed`, hands back to orchestrator for merge.
4. QA re-runs the repro and the surrounding regression area; sets status `verified` (or back to `open` with new evidence).
5. Severity rules: `critical` (data loss, auth bypass, server crash) and `major` (feature broken) block the phase gate; `minor`/`cosmetic` are scheduled into Phase 5.

---

## 2. Dev Environment (macOS + Podman)

```bash
# One-time
podman machine init && podman machine start   # if not already running

# Full stack
podman-compose up                              # postgres + backend + frontend (B1 creates this)

# Postgres only (for running backend/tests on the host)
podman run -d --name molab-pg \
  -e POSTGRES_USER=molab -e POSTGRES_PASSWORD=molab -e POSTGRES_DB=molab \
  -p 5432:5432 -v molab-pgdata:/var/lib/postgresql/data \
  pgvector/pgvector:pg16

# Backend on host (hot dev loop)
cd backend && uv sync && uv run alembic upgrade head \
  && uv run uvicorn app.main:app --reload --port 8000

# Tests (suite creates/migrates molab_test itself, see B7)
cd backend && uv run pytest

# Frontend on host
cd frontend && npm install && npm run dev
```

`DATABASE_URL=postgresql+asyncpg://molab:molab@localhost:5432/molab` for host dev; compose overrides host to `postgres`.

---

## 3. Phases at a Glance

Maps `DESIGN.md`'s 11-step MVP order onto the three lanes. Cards within a phase are parallel unless a dependency is noted.

| Phase | Opus-1 Backend Core | Opus-2 Runtime | Opus-3 Frontend | QA regression scope |
|---|---|---|---|---|
| **0 Foundations** | B1 scaffold+compose, B2 models+migration | R1 marimo process spike | F1 scaffold+theme, F2 API client+mocks | smoke: stack boots |
| **1 Auth + CRUD** | B3 auth, B4 notebook CRUD, B7 test harness | R2 process manager service, R3 sessions API+proxy | F3 auth pages, F4 discover+detail | auth, CRUD, visibility |
| **2 Create + Search** | B5 publish+FTS, B6 semantic search wiring | R4 GitLab import, R5 embedding service | F5 create flow, F6 edit/run views | + create, import, search |
| **3 Deploy + Grafana + Fork** | B8 fork endpoint | R6 deploy lifecycle+reaper, R7 Grafana data API | F7 deploy page, F8 fork UI | + deploy wake/sleep, data buffer, fork |
| **4 Hardening** | — | — | F9 polish (dark mode QA, skeletons, responsive) | full MVP regression |

Cross-lane dependencies: **R2→B2** (models), **R3→B3** (auth dep), **B6→R5** (embedding fn), **F3+→F2** (client), **F6→R3** (proxy), **F7→R6** (deploy API). Opus-3 builds Phase 1–2 UI against §5 with mocks (F2), swapping to the live API as backend cards merge.

---

## 4. Task Cards

### Lane: Opus-1 — Backend Core

**B1 — Backend scaffold + compose** *(Phase 0)*

- `backend/` via `uv init`; `pyproject.toml` requires Python `>=3.12`. Deps: `fastapi`, `uvicorn[standard]`, `sqlalchemy[asyncio]`, `asyncpg`, `alembic`, `pydantic-settings`, `pgvector`, `bcrypt`, `pyjwt`. Dev group: `pytest`, `pytest-asyncio`, `httpx`, `psycopg[binary]`. Do **not** add `sentence-transformers` or `marimo` (R-lane owns those).
  - `bcrypt` directly — **not passlib** (unmaintained, breaks with bcrypt ≥4).
- Layout per `DESIGN.md` §Directory Structure: `app/main.py`, `app/core/{config,security}.py`, `app/api/`, `app/services/`, `app/models/`, `app/schemas/`, `app/db/`.
- `core/config.py`: pydantic-settings `Settings` — `DATABASE_URL`, `SECRET_KEY`, `ACCESS_TOKEN_EXPIRE_MINUTES`, `IDLE_TIMEOUT_MINUTES=10`, `MAX_CONCURRENT_SESSIONS`, `MARIMO_PORT_RANGE=9000-9099` (last three read-but-unused until R-lane consumes them).
- `db/database.py`: async engine, `async_sessionmaker`, `get_db` dependency.
- `app/main.py`: FastAPI app, CORS for `http://localhost:5173`, `/api/health`.
- Repo root: `podman-compose.yml` (postgres `pgvector/pgvector:pg16` with healthcheck + named volume; backend built from `Containerfile.backend`, `--reload`, port 8000, depends_on postgres healthy; frontend node:22 `npm run dev -- --host`, port 5173), `Containerfile.backend` (`python:3.12-slim`, install uv, `uv sync`, entrypoint runs `alembic upgrade head` then uvicorn), `.env.example` (every Settings field), `.gitignore`, brief `README.md`.
- *Verify:* `podman-compose up` boots all three services; `curl localhost:8000/api/health` → 200.

**B2 — Models + initial migration** *(Phase 0, after B1)*

- SQLAlchemy 2.0 typed declarative models for **all four tables** exactly per `DESIGN.md` §Database Schema: `users`, `notebooks`, `deployments`, `notebook_data`.
- `alembic init -t async`; env.py reads `DATABASE_URL` from Settings. **One** initial migration, hand-written (don't trust autogenerate for enums/vector/generated columns):
  1. `CREATE EXTENSION IF NOT EXISTS vector`
  2. enums `notebook_visibility('draft','unlisted','public')`, `deployment_status('running','sleeping','stopped')`; the four tables; `embedding vector(384)` nullable.
  3. **Gotcha:** `array_to_string` is STABLE, so it can't appear in a `GENERATED` column. Create an IMMUTABLE wrapper first:

     ```sql
     CREATE FUNCTION f_textarr2text(text[]) RETURNS text
       LANGUAGE sql IMMUTABLE AS $$ SELECT array_to_string($1, ' ') $$;
     ```

     then `search_vector tsvector GENERATED ALWAYS AS (to_tsvector('english', coalesce(title,'') || ' ' || coalesce(description,'') || ' ' || coalesce(f_textarr2text(tags),''))) STORED` + GIN index.
- In the model, map `search_vector` with `FetchedValue()` / deferred so the ORM never writes it.
- *Verify:* `uv run alembic upgrade head` against the podman postgres; `psql ... -c '\d notebooks'` shows generated tsvector + vector(384); `downgrade base` also works.

**B3 — Auth** *(Phase 1)*

- `core/security.py`: `hash_password`/`verify_password` (bcrypt), `create_access_token`/`decode_token` (pyjwt HS256, `sub`=user id, `exp`).
- `services/auth_service.py`: `AuthService` ABC (`register`, `authenticate`) + `BasicAuthService` Postgres impl — keep the seam for future SAML/OAuth per `DESIGN.md`.
- `api/auth.py`: register (201; 409 on duplicate username **or** email), login (token; 401 bad creds), logout (204 stateless no-op, documented).
- `api/deps.py`: `get_current_user` (401) and `get_current_user_optional` (None for anonymous — this powers the "anonymous can browse" model).
- *Verify:* register → login → call an authed endpoint with the token; wrong password → 401.

**B4 — Notebook CRUD + visibility** *(Phase 1, after B3)*

- `services/notebook_storage.py`: `NotebookStorageService` ABC (`get`/`put`/`delete`) + `PostgresNotebookStorage` writing `notebooks.source` — the MinIO swap seam.
- `api/notebooks.py` per §5: list (paginated; `public` + caller's own), create (auth; draft), get (draft → owner only, **404 not 403** to avoid leaking existence; unlisted/public → anyone), update (owner; bumps `updated_at`), delete (owner, 204), publish (owner; visibility transition incl. back to draft).
- Schemas: `UserCreate/UserOut`, `Token`, `NotebookCreate/NotebookUpdate/NotebookOut` (out excludes `embedding`/`search_vector`).
- *Verify:* the §5 visibility matrix manually via curl; covered properly by B7 tests.

**B5 — Publish + full-text search** *(Phase 2)*

- Extend `GET /api/notebooks` with `q=` (tsquery via `websearch_to_tsquery` against `search_vector`, ranked by `ts_rank`) and `tags=` filter. Anonymous results stay public-only.
- *Verify:* seed notebooks, confirm title/description/tag hits and ranking order.

**B6 — Semantic search wiring** *(Phase 2, needs R5)*

- On publish (visibility leaves `draft`), call R5's `EmbeddingService.embed(title + description + tags)` and store in `embedding`.
- `GET /api/notebooks?semantic=<text>`: embed query, order by cosine distance (`embedding <=> :q`), public-only, exclude NULL embeddings.
- *Verify:* publish 3+ thematically distinct notebooks; semantic query ranks the on-topic one first.

**B7 — Test harness + API test suite** *(Phase 1, alongside B3/B4)*

- Real Postgres only (tsvector/pgvector rule out SQLite). `tests/conftest.py`: create `molab_test` DB if absent (sync psycopg against the podman postgres), run `alembic upgrade head` on it (migration is thereby tested), per-test async session + `httpx.AsyncClient(transport=ASGITransport(...))` with `get_db` overridden; truncate tables between tests.
- Suites: auth (register/login/duplicate/bad creds), CRUD happy path, the full visibility matrix (anonymous/owner/other × draft/unlisted/public), publish transitions, ownership on PUT/DELETE. Extend in later phases as endpoints land.
- *Verify:* `uv run pytest` green against a fresh `molab-pg` container.

**B8 — Fork** *(Phase 3)*

- `POST /api/notebooks/{id}/fork` (auth): source must be visible to caller; creates a `draft` copy owned by caller with `parent_id` set; increments parent `fork_count` atomically. `NotebookOut` gains `parent_id`, `fork_count`, and parent title/owner for the "forked from…" UI.
- *Verify:* tests — fork public/unlisted ok, fork someone's draft → 404, fork_count increments, forked draft is private to forker.

### Lane: Opus-2 — Runtime

**R1 — Marimo process spike** *(Phase 0, independent)*

- Add `marimo` dep (R-lane owns it from here). Throwaway-quality but committed spike under `backend/spikes/`: spawn `marimo edit` and `marimo run` headless subprocesses on a chosen port, confirm readiness probing (poll HTTP until up), confirm auth/token flags, document graceful-shutdown behavior. **Verify exact CLI flags against the installed marimo version** (`marimo edit --help`) and record findings in `spikes/NOTES.md` — R2's design consumes them.
- *Verify:* spike script starts/stops both modes cleanly; NOTES.md answers: flags, readiness signal, shutdown.

**R2 — Process manager service** *(Phase 1, after B2 + R1)*

- `services/process_manager.py`: async singleton owning subprocesses. Port allocator from `MARIMO_PORT_RANGE`; `spawn(notebook, mode)` → writes source to a tmp workdir, launches per R1 NOTES, readiness-probes (10s cap); `stop(session_id)`; `MAX_CONCURRENT_SESSIONS` cap → raise → API maps to **503** per `DESIGN.md`; on app shutdown, terminate all children.
- Track sessions in memory (id, notebook_id, mode, port, pid, last_active); processes don't survive restarts by design.
- *Verify:* unit tests with a stub command; one integration test spawning real marimo (skippable via marker for CI-less envs).

**R3 — Sessions API + HTTP/WS proxy** *(Phase 1, after R2 + B3)*

- `api/sessions.py`: `POST /api/sessions` (`{notebook_id, mode}`; `edit` → owner only; `run` → anyone who can view the notebook, anonymous included), `DELETE /api/sessions/{id}`.
- `api/proxy.py`: `GET|POST /api/proxy/{session_id}/{path:path}` streaming reverse proxy via `httpx.AsyncClient` (preserve headers, stream bodies) + `WS /api/proxy/{session_id}/ws` bidirectional relay (`websockets` client ↔ FastAPI WebSocket). Touch `last_active` on every proxied request/frame — the idle reaper (R6) depends on it.
- *Verify:* create a run session via API, open the proxied URL in a browser, notebook is interactive end-to-end.

**R4 — GitLab import** *(Phase 2)*

- `services/gitlab_import.py`: fetch raw-file URL with `httpx` (`PRIVATE-TOKEN` header when PAT given); validate it parses as Python (`ast.parse`) and looks like a marimo notebook (imports `marimo`); never store or log the PAT; size cap (e.g. 1 MB); map upstream 401/403/404 to clear client errors.
- `api/notebooks.py`: `POST /api/notebooks/import` `{url, pat?}` → creates a draft (title from filename).
- *Verify:* tests with mocked httpx (public ok, PAT header sent, non-Python rejected, upstream 404 surfaced).

**R5 — Embedding service** *(Phase 2)*

- Add `sentence-transformers`. `services/embedding_service.py`: lazy-load `all-MiniLM-L6-v2` once (startup or first use — it's ~90 MB; never per-request), `embed(text) -> list[float]` (384-dim) run via `asyncio.to_thread` to keep the event loop free.
- *Verify:* unit test asserts 384 dims and that similar texts beat dissimilar ones on cosine similarity.

**R6 — Deploy lifecycle + idle reaper** *(Phase 3, after R2/R3)*

- `api/deployments.py` + `services/process_manager.py` extensions per `DESIGN.md` §Deploy Lifecycle: `POST /api/notebooks/{id}/deploy` (owner; unique slug; status `sleeping` until first hit), `GET /api/deployments/{slug}` (if sleeping: buffer the request, wake, wait ≤10 s for readiness, forward; if waking fails → 503), `DELETE /api/deployments/{slug}` (owner; status `stopped`).
- Idle reaper: asyncio background task, 60 s interval, kills processes idle > `IDLE_TIMEOUT_MINUTES`, sets status `sleeping`. On startup, mark all `running` rows `sleeping` (processes died with the server).
- Frontend route `/deploy/[slug]` proxies through this (F7).
- *Verify:* deploy → first request wakes it (observe cold start) → wait past a short test timeout → reaper sleeps it → next request wakes it again; restart server → row is `sleeping`.

**R7 — Grafana data endpoints** *(Phase 3)*

- `api/data.py`: `POST /api/notebooks/{id}/data` (store JSON payload + `source` in `notebook_data`; works while the notebook sleeps — that's the point of the buffer), `GET /api/notebooks/{id}/data` (latest payload; the notebook fetches this on load). Query-param passthrough (`?metric=…`) needs no backend work beyond R6's proxy preserving query strings — add a regression test for that.
- *Verify:* POST while deployment is sleeping → wake it → notebook GET returns the payload.

### Lane: Opus-3 — Frontend

**F1 — Scaffold + theme** *(Phase 0)*

- `npx sv create` (SvelteKit + TypeScript) in `frontend/`; Tailwind v4 via `@tailwindcss/vite`; dark/light via class strategy (`@custom-variant dark` on `.dark`), toggle persisted to localStorage, respects `prefers-color-scheme` default. Layout shell: nav (logo, Discover, login/user), footer.
- *Verify:* `npm run dev` renders shell; toggle persists across reload.

**F2 — API client + auth store + mocks** *(Phase 0)*

- `src/lib/api.ts`: typed fetch wrapper, base URL from `PUBLIC_API_URL`, attaches Bearer token, normalizes errors. Types mirror §5 exactly.
- `src/lib/stores/auth.ts`: token + current user, localStorage-backed.
- Dev mock mode (flag-switched fixture data) so F3–F5 proceed before backend cards merge; remove or disable by Phase 3.
- *Verify:* `npm run check` clean; mock mode renders fixture notebooks.

**F3 — Auth pages** *(Phase 1)* — `/auth/login`, `/auth/register` working forms with field/server error display; on success store token, redirect home; nav reflects auth state; logout clears store.
**F4 — Discover + detail** *(Phase 1)* — `/discover` (paginated list, q + tags inputs ready for B5, semantic toggle ready for B6), `/notebooks/[id]` (metadata, owner, tags, fork lineage placeholder; Run/Fork/Deploy buttons rendered but disabled until their APIs land).
**F5 — Create flow** *(Phase 2)* — `/notebooks/new` tabs per `DESIGN.md`: Blank (create → redirect to edit), Upload `.py` (read file client-side, send as `source`), GitLab URL (+ optional PAT field, clearly marked never-stored). Landing page CTA wires here.
**F6 — Edit + run session views** *(Phase 2, needs R3)* — `/notebooks/[id]/edit` (owner-only: create edit session, embed proxied marimo in full-height iframe, end session on leave), Run button on detail page → run session in iframe. Loading state while session spins up.
**F7 — Deploy page** *(Phase 3, needs R6)* — deploy controls on detail page (deploy/slug/delete); `/deploy/[slug]` full-screen iframe with a **“waking up…” skeleton** while the cold start completes (poll or retry until 200).
**F8 — Fork UI** *(Phase 3, needs B8)* — Fork button (auth-gated) → fork → redirect to the new draft; fork count + “forked from …” lineage link on detail pages.
**F9 — Polish** *(Phase 4)* — dark-mode audit of every page, responsive pass, empty/loading/error states everywhere, favicon/title.

---

## 5. API Contract (frozen)

Changes only via the orchestrator (update here first). Auth: `Authorization: Bearer <jwt>`. Errors: `{"detail": "..."}` with conventional status codes. Draft notebooks return **404** (not 403) to non-owners.

| Endpoint | Auth | Request | Response |
|---|---|---|---|
| `POST /api/auth/register` | — | `{username, email, password}` | 201 `User`; 409 duplicate |
| `POST /api/auth/login` | — | `{username, password}` | `{access_token, token_type:"bearer"}`; 401 |
| `POST /api/auth/logout` | bearer | — | 204 (stateless no-op) |
| `GET /api/notebooks` | optional | `q=, tags=, semantic=, page=1, page_size=20` | `{items: Notebook[], total, page, page_size}` |
| `POST /api/notebooks` | required | `{title, description?, tags?, source?}` | 201 `Notebook` (draft) |
| `GET /api/notebooks/{id}` | optional | — | `Notebook`; 404 per visibility |
| `PUT /api/notebooks/{id}` | owner | partial `{title?, description?, tags?, source?}` | `Notebook` |
| `DELETE /api/notebooks/{id}` | owner | — | 204 |
| `POST /api/notebooks/{id}/publish` | owner | `{visibility: "draft"\|"unlisted"\|"public"}` | `Notebook` |
| `POST /api/notebooks/{id}/fork` | required | — | 201 `Notebook` (new draft) |
| `POST /api/notebooks/import` | required | `{url, pat?}` | 201 `Notebook` (draft) |
| `POST /api/sessions` | edit: owner / run: viewer | `{notebook_id, mode:"edit"\|"run"}` | `{id, notebook_id, mode, proxy_url}`; 503 at cap |
| `DELETE /api/sessions/{id}` | creator | — | 204 |
| `POST /api/notebooks/{id}/deploy` | owner | `{slug?}` | `{slug, status, url}` |
| `GET /api/deployments/{slug}` | optional | — | proxied notebook (wakes if sleeping); 503 wake-fail |
| `DELETE /api/deployments/{slug}` | owner | — | 204 |
| `POST /api/notebooks/{id}/data` | — (Grafana) | arbitrary JSON | 201 `{id}` |
| `GET /api/notebooks/{id}/data` | — | — | latest `{payload, source, created_at}`; 404 if none |
| `GET\|POST /api/proxy/{session_id}/{path}` | session-scoped | — | proxied |
| `WS /api/proxy/{session_id}/ws` | session-scoped | — | relayed |

`Notebook` = `{id, user_id, parent_id, title, description, tags, visibility, fork_count, source?, created_at, updated_at}` (`source` only for owner / fork / run contexts as needed; never `embedding`/`search_vector`). `User` = `{id, username, email, created_at}`.

---

## 6. QA Protocol (Sonnet 4.6)

**When:** after every phase gate, and after every batch of bug fixes. **Scope:** full regression of everything merged so far — earlier phases are re-tested every time, not just the new work.

**How:** bring the stack up via `podman-compose up` (plus host-mode where iframe/browser checks are needed). Three layers:

1. `cd backend && uv run pytest` — must be green before anything else.
2. API-level end-to-end via curl/httpx scripts against the running stack: full user journeys (register → create → edit → publish → discover → run → deploy → Grafana POST → fork), plus adversarial cases — other-user access to drafts, expired/garbage tokens, visibility downgrades, session cap (503), wake-timeout behavior, concurrent forks of one notebook, oversized/malformed import payloads.
3. UI smoke per phase checklist (§3 rightmost column): every route renders in dark and light, forms validate, error states show.

**Bug report template** (`docs/bugs/BUG-NNN-slug.md`):

```markdown
# BUG-NNN: <one-line summary>
- Status: open | triaged | fixed | verified | closed
- Severity: critical | major | minor | cosmetic
- Phase found / Area: <phase> / <lane or endpoint>

## Steps to reproduce   (exact commands/requests, smallest repro)
## Expected vs actual   (include response bodies / logs)
## Suspected cause      (optional, with file:line if known)

## Fix Plan             (orchestrator fills in at triage)
## Verification         (QA fills in after re-test)
```

QA never commits fixes — only bug reports, INDEX.md updates, and verification notes.

---

## 7. Running the Team

Orchestrator session (Fable 5) in Claude Code at the repo root, with this file as its brief. Implementers and QA are spawned as subagents with model overrides — Opus for the three lanes, Sonnet 4.6 for QA — each given: their card ID(s) + card text, §1's protocol, §5's contract, and worktree isolation. Suggested cadence per phase: deal all parallel cards at once; merge as DoD evidence comes back; when the phase's cards are merged, spawn QA; triage whatever QA files; loop until the gate is clean; deal the next phase.
