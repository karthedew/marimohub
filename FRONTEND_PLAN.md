# MarimoHub Frontend — Workspace Rework Plan

## Context

The backend was redesigned (IMPLEMENTATION_PLAN.md M1–M10, complete at `8f80f38`): notebook
ownership moved from `user_id` to workspaces with member roles, visibility `draft` became
`private`, the explicit session-save endpoint was deleted (persistence now rides the proxied
autosave), fork/create/import require an explicit `workspace_id`, and a workspace/membership
management API exists. The SvelteKit frontend still speaks the old API: creation/import/fork
422 (missing `workspace_id`), ownership gating reads the removed `notebook.user_id`, the
`draft` value and old lineage fields are typed and displayed, and `SessionFrame` calls the
deleted save endpoint. Separately, the UI never exposed publish/delete even under the old API.

Scope decided: **full collaboration UI** (workspace CRUD, members, archive/restore — the whole
DT-5 surface), **publish + delete controls**, and **live in-browser verification** against the
real backend. Zero backend changes — the client adapts to the frozen API. Frontend has no test
infra; verification is `svelte-check` + production build + a scripted browser walkthrough
(no Vitest/Playwright added in this pass — recorded as a possible follow-up, not scope).

## New API surface (contract the client must match)

Error bodies carry `detail` — a string for domain/access errors, but FastAPI's **array** shape
on 422 validation failures; the client's `readError()` already normalizes both and must keep
doing so. Access denials follow hide-vs-forbid: private/non-member and missing resources are
indistinguishable **404**; anonymous write on a readable notebook → **401**; identified viewer
write → **403**. Domain conflicts → **409**.

| Area | Endpoint | Notes |
|---|---|---|
| Auth | POST `/api/auth/register` `/login` `/logout` | unchanged shapes; login returns `{access_token}`; JWT `sub` is still the user id (the store's decode keeps working) |
| Workspaces | GET `/api/workspaces` | `[{id, slug, name, role, created_at}]` — caller's active workspaces with own role (`owner\|editor\|viewer`) |
| | POST `/api/workspaces` `{name, slug?}` | 201; caller becomes owner; explicit slug conflict → 409 |
| | GET `/api/workspaces/{id}` · PATCH `{name}` · DELETE (archive, 204) · POST `/{id}/restore` | PATCH/DELETE/restore owner-only; non-member/archived → 404 via normal reads |
| | GET `/api/workspaces/archived` | caller's owned archives, `{...,archived_at, purge_after}` |
| | GET/POST `/api/workspaces/{id}/members`, PATCH/DELETE `/members/{user_id}` | member list `{workspace_id,user_id,username,email,role,created_at}`; add takes `{user_id, role}` (**raw UUID — no user-directory endpoint exists**; accepted UX wart, backend follow-up candidate); last-owner demote/remove → 409 |
| Notebooks | GET `/api/notebooks` (q/tags/semantic/page) | unchanged params; results now workspace-visibility filtered server-side |
| | POST `/api/notebooks`, POST `/import` | **require `workspace_id`** in body |
| | POST `/api/notebooks/{id}/fork` | **requires body `{workspace_id}`** (target), was body-less |
| | `NotebookOut` | `{id, workspace_id, created_by (nullable), parent_id, parent_title?, parent_workspace_id?, parent_workspace_slug?, title, description, tags, visibility, fork_count, source?, created_at, updated_at}` — no `user_id`, no `parent_owner_*`; `source` present for ANY reader on single-notebook responses; visibility ∈ `private\|unlisted\|public` |
| | POST `/{id}/publish` `{visibility}` · PUT `/{id}` · DELETE `/{id}` | exist, previously unreachable from UI |
| | GET `/api/notebooks/{id}/deployment` | NEW read-model: live `{slug, status, url}`, 404 when none |
| Sessions | POST `/api/sessions` `{notebook_id, mode}` | edit needs editor role (else 403/404), run needs read |
| | DELETE `/api/sessions/{id}` | capability-scoped; **POST `/{id}/save` is GONE** |
| Deployments | POST `/api/notebooks/{id}/deploy` `{slug?}` · DELETE `/api/deployments/{slug}` | write-gated by role |
| | GET `/api/deployments/{slug}/` | public serving proxy; hitting it wakes a sleeping deployment (the `/deploy/[slug]` polling pattern remains valid) |
| Data | `/api/notebooks/{id}/data` | GET is READ-gated now; client's unused `postData`/`getData` (`auth:false`) are deleted — runtime writes moved to an internal pod-only API |

## Rendering-mode decision (resolves the SSR/auth conflict)

**SSR is disabled globally this pass** (`export const ssr = false` in a new root
`src/routes/+layout.ts`). Universal loads currently run server-side with no access to the
localStorage token, so `/discover` SSRs anonymous results that are never refetched after
hydration, and a hard refresh on any private-but-readable notebook 404s at SSR time even for a
logged-in member — a defect that exists **today** (own draft + refresh → 404) and that the
workspace model widens. SPA mode kills the whole class with one line, keeps every `+page.ts`
load working unchanged (they simply run in the browser, where the token lives), matches the
frozen-backend constraint, and fits an app-shaped product. The better long-term answer for SEO
on public pages — HttpOnly cookie set by the SvelteKit server and translated to a Bearer header
in `handleFetch` (backend stays frozen; commits to `adapter-node` + CSRF care) — is recorded as
a designed follow-up, deliberately not coupled to this migration.

**Capability model (replaces `isOwner`)**: the caller's role in a notebook's workspace comes
from a client-side join — `myWorkspaces.find(w => w.id === notebook.workspace_id)?.role`.
Write capability = role `owner`/`editor`. Workspace admin = `owner`. A non-member viewing a
public notebook gets no role and no workspace-name resolution (workspace reads 404 for
non-members by design) — the detail page omits the owner/workspace line in that case and shows
`parent_workspace_slug` lineage only when the backend populated it (readability-gated
server-side).

## Conventions

- All commands from `frontend/` unless stated. `CHECK` = `npm run check` (svelte-check) — no
  errors (record the pre-existing warning/error baseline in F1 and hold later milestones to it).
  `BUILD` = `npm run build` — must succeed.
- Existing idioms to match: Svelte 5 runes (`$props`/`$state`/`$derived`/`$effect`), the single
  `writable` auth store pattern, Tailwind v4 utility classes with the `hub-*` palette,
  `Button.svelte` for actions, inline `<p role="alert">` error boxes, `ApiError` catch-per-call.
  No component library; no new runtime dependencies.
- Backend for live checks: podman postgres (`marimohub_postgres_1`) + `cd backend && uv run
  uvicorn app.main:app --reload --port 8000`; frontend `PUBLIC_API_URL=http://localhost:8000
  npm run dev -- --port 5173 --strictPort`. Backend CORS already allows
  `http://localhost:5173` (verified in `backend/app/main.py`); `--strictPort` prevents Vite
  silently drifting to 5174. No CORS edit is needed or sanctioned.
- The **one sanctioned backend change** is milestone B1 below (an archived-workspace serving
  fix — a verified defect against the design contract, not frontend accommodation).

---

## F1 — API client, types, and workspace state

**Intent**: make `src/lib/api.ts` speak the new contract and give the app a workspace store, so
every later milestone builds on correct types.

- **Modify `src/lib/api.ts`**: `NotebookVisibility = 'private'|'unlisted'|'public'`; rewrite
  `Notebook` per the table (drop `user_id`/`parent_owner_*`, add `workspace_id`, `created_by`,
  `parent_workspace_id/slug`); add `WorkspaceRole`, `Workspace`, `WorkspaceArchived`,
  `WorkspaceMember`, and request types; `NotebookCreateRequest`/`NotebookImportRequest` gain
  required `workspace_id`; `NotebookUpdateRequest` becomes its own type
  (`Partial<Omit<NotebookCreateRequest, 'workspace_id'>>` or explicit fields) — the backend's
  update schema has no `workspace_id` and ignores extras, so the old `Partial<Create>` alias
  would advertise a workspace reassignment that silently no-ops; `fork(id, {workspace_id})`;
  add `workspaces.*` methods (list, create, get, rename, archive, restore, listArchived,
  members list/add/changeRole/remove); add `notebooks.getDeployment(id)`; **delete**
  `sessions.save`, `notebooks.postData`, `notebooks.getData`,
  `NotebookData`/`NotebookDataCreated` types.
- **Fix `buildUrl` array serialization** (pre-existing live bug): arrays are currently
  `join(',')`-ed into one param, but the backend declares `tags: list[str]` (repeated-param
  semantics), so `?tags=a,b` parses as the single tag `"a,b"` and multi-tag filtering has never
  worked. Serialize arrays as repeated `searchParams.append(key, v)` entries and drop
  `normalizeTagInput`'s csv join accordingly (keep its input-splitting for the discover form).
- **Create `src/routes/+layout.ts`** with `export const ssr = false` per the rendering-mode
  decision above.
- **Type `Notebook.source` as `source?: string | null`** — the backend declares
  `source: str | None` and list rows serialize `"source": null` (pydantic doesn't omit None),
  so optional-only typing is a lie at runtime. Preserve `readError()`'s dual string/array
  detail handling untouched.
- **Create `src/lib/stores/workspaces.ts`**: loads `GET /api/workspaces` when a token exists
  (hydrate + after login/register), exposes `myWorkspaces`, `roleFor(workspaceId)`,
  `canWrite(workspaceId)` (editor+), `writableWorkspaces` (for pickers), and a `refresh()`
  invalidated after any workspace mutation. Clear on logout. Follow the existing
  `stores/auth.ts` `writable` pattern.
- **Create `frontend/.env.example`** documenting `PUBLIC_API_URL=http://localhost:8000` (the
  explorer flagged the var as undocumented).
- **Verification**: `CHECK` (record baseline); `BUILD`;
  `grep -rn "user_id\|'draft'\|parent_owner\|sessions/save\|/data" src` → only intentional hits
  (member `user_id` fields are legitimate; notebook-ownership `user_id` is not).
- **Status**: pending

## F2 — Workspace management UI (full DT-5 surface)

**Intent**: the collaboration surface — list/create, detail with members, archive/restore.

- **Create `src/routes/workspaces/+page.svelte`** (+`+page.ts` load): my active workspaces
  (name, slug, my role, created) + create form (name, optional slug; 409 slug conflict shown
  inline); link into detail. Auth-gated like `notebooks/new` (sign-in prompt when anonymous).
- **Create `src/routes/workspaces/[id]/+page.svelte`** (+load): workspace header (rename inline,
  owner-only), members table (username/email/role) with owner-only add (user-id input + role
  select — label the input honestly, e.g. "User ID"), role change, remove; last-owner 409s and
  duplicate-member 409s rendered inline; archive button (owner-only, confirm step) → archives
  and returns to the list.
- **Create `src/routes/workspaces/archived/+page.svelte`**: owned archives with `archived_at`/
  `purge_after` and a Restore button.
- **Modify `src/routes/+layout.svelte`**: "Workspaces" nav link when authenticated.
- All mutations call `workspaces.refresh()` so pickers/capability stay correct.
- **Self-mutation handling**: the backend permits an owner to demote or remove *themself* when
  another owner remains (only the last owner is guarded). After self-removal → refresh the
  store and redirect to `/workspaces` (the detail page 404s for the ex-member); after
  self-demotion → refresh so owner-only controls disappear immediately.
- **Verification**: `CHECK` at baseline; `BUILD`.
- **Status**: pending

## F3 — Notebook flows re-keyed + publish/delete

**Intent**: repair creation/fork/detail against the new contract and add the missing
management controls.

- **Modify `src/routes/notebooks/new/+page.svelte`**: workspace picker (from
  `writableWorkspaces`) required on all three tabs; empty state links to `/workspaces` ("create
  a workspace first"); create/import send `workspace_id`.
- **Modify `src/routes/notebooks/[id]/+page.svelte`**:
  - Capability: `canWrite = $derived(canWrite(notebook.workspace_id))` replaces the
    `user_id === currentUser.id` checks; Edit button and Deploy panel gate on it.
  - Owner line: show workspace name/slug when the viewer is a member (join against
    `myWorkspaces`); omit the line for non-members. Lineage renders `parent_title` +
    `parent_workspace_slug` when present.
  - **Publish control** (write-gated): visibility select `private/unlisted/public` + apply →
    `notebooks.publish`; badge updates from the response.
  - **Delete button** (write-gated, confirm step) → `notebooks.delete` → `goto('/discover')`.
  - **Live deployment status**: on load (and after deploy/stop), fetch
    `notebooks.getDeployment(id)`; render status from it instead of only the deploy response;
    404 = no deployment. Stop keeps the optimistic flip but reconciles with a re-fetch. Because
    wakes happen out-of-band (someone opens the public URL), the status is NOT self-updating:
    add refetch-on-window-focus plus a light poll (~15s) while the deployment panel is visible,
    both stopping when the tab is hidden.
  - Fork: prompt for a target workspace when the user has more than one writable workspace
    (single writable workspace → use it directly); send `{workspace_id}`.
- **Modify `src/routes/discover/+page.svelte`**: badge styling map keys `draft`→`private`;
  fix the page copy — it currently promises "public and unlisted", but the real semantics are
  public notebooks plus (when signed in) everything in your workspaces; unlisted stays
  direct-link-only. No query-logic change (the server filters).
- **Modify `src/routes/deploy/[slug]/+page.svelte`**: fail fast on 404 (deployment
  stopped/missing — show the error immediately instead of burning all 12 poll attempts);
  keep retrying on 503 (waking) and 429 (capacity, once the operator lands).
- **Verification**: `CHECK`; `BUILD`; grep: no `user_id` ownership comparison, no `'draft'`
  literal outside git history.
- **Status**: pending

## F4 — SessionFrame + client sweep

**Intent**: align the editor lifecycle with server-side persistence and delete dead client
surface.

- **Modify `src/lib/components/SessionFrame.svelte`**: remove the `sessions.save` best-effort
  call and its trigger (persistence is continuous server-side via the proxied autosave — a
  comment may note this lifecycle fact); keep the `sessions.create`/`sessions.delete` teardown
  flow, but pass `keepalive: true` on the unload-path DELETE — session deletion is
  capability-scoped (no Authorization header required), so a keepalive fetch survives page
  teardown where a normal async fetch is cancelled. This improves but does not guarantee
  cleanup: record the residual (browser may still drop it; the kube controller idle-reaps
  orphans in prod, while dev-subprocess sessions linger until stopped or API restart). Surface
  session-create failures per hide-vs-forbid (404 on a notebook you can't see, 403 as viewer,
  401 anonymous → route to login with `next`).
- Sweep: confirm no `postData`/`getData`/`save` references remain anywhere
  (`grep -rn "postData\|getData\|sessions.save\|/save" src` → nothing).
- **Verification**: `CHECK`; `BUILD`.
- **Status**: pending

## B1 — Backend fix: archived workspaces must not serve or wake deployments

**Intent**: close a verified backend defect the frontend cannot enforce. `_load_active_deployment`
(`backend/app/api/deployments.py:101-114`) joins only `Notebook` and filters only `STOPPED`, so
the public `/api/deployments/{slug}` URL on an archived workspace's deployment still resolves
and `spawn_deployment` **wakes a runtime the archive stopped** — violating DT-1/DT-5's contract
("archived workspaces… cannot start sessions/deployments"). Session-create is already safe
(policy-gated via `load_notebook_for`); the public serving path is the one hole.

- **Modify `backend/app/api/deployments.py`**: `_load_active_deployment` joins `Workspace` and
  requires `Workspace.archived_at.is_(None)`; archived → the same `UpstreamNotFound` as
  missing/stopped (existence-hiding preserved). Check the read-model path too:
  `GET /api/notebooks/{id}/deployment` already 404s via `NotebookRead` (archived filter in the
  policy) — confirm, don't duplicate.
- **Test** (`backend/tests/test_deployments.py`): archive a workspace with a sleeping
  deployment → public slug GET/WS → 404 and the manager records NO wake; restore → serving
  works again.
- **Verification**: `cd backend && uv run ruff check . && uv run ty check && uv run pytest -q`
  all green.
- **Status**: pending

## F5 — Live end-to-end verification

**Intent**: drive the real stack in a browser and fix fallout in place (small fixes land in
this milestone; anything structural reopens the owning milestone).

- Bring up: postgres (podman), backend (uvicorn :8000, `SESSION_BACKEND=subprocess`), frontend
  (`PUBLIC_API_URL=http://localhost:8000 npm run dev`). Confirm CORS from the Vite origin first.
- Walkthrough (two users, A and B):
  1. A registers → lands home → creates workspace "alpha" → creates a blank notebook in it.
  2. Edit session opens marimo in the iframe; make an edit; wait ≥2s (autosave); leave; reopen
     → edit persisted (proves the server-side save path end-to-end, no client save call).
  3. A publishes → `public`; discover shows it with the `public` badge.
  4. B registers → creates workspace "beta" → forks A's notebook into beta (picker) → fork is
     `private` in beta with lineage "forked from … (alpha)".
  5. A opens workspace alpha → adds B by user id as `viewer` (B's id retrieved from the DB or
     B's session for the walkthrough) → B sees alpha's private notebooks; B gets no Edit button
     (viewer); A promotes B to `editor` → B can edit.
  6. Last-owner guard: A attempts to demote themself with no co-owner → inline 409 message.
  7. A deploys the public notebook → detail shows live status; `/deploy/{slug}` page wakes and
     serves the app; stop → status `stopped`.
  8. A archives a scratch workspace containing a deployed notebook → it disappears from lists,
     its notebooks vanish from discover, AND its public deployment URL 404s without waking
     (proves B1 end-to-end) → restore from `/workspaces/archived` → serving works again.
  8b. Deployment status lifecycle on the detail page: deploy → `sleeping`; open the public URL
     in another tab (wakes it) → detail page catches `running` via focus-refetch/poll without
     a manual reload; stop → `stopped`.
  8c. Discovery semantics: anonymous sees public only; signed-in A additionally sees alpha's
     private/unlisted notebooks; B's unlisted notebooks appear for B but never for A.
  9. Anonymous window: public notebook readable with source; no Edit/Deploy/Publish controls;
     fork prompts login via `?next=`.
  10. Hard-refresh checks (the SSR-era defects): refresh while logged in on a private notebook
     you're a member of → page renders (no 404); refresh `/discover` while logged in → own
     private notebooks still listed. Multi-tag filter (`tags=a,b` in the form) returns only
     notebooks carrying BOTH tags — proves the repeated-param serialization fix.
- Gates: full walkthrough passes; `CHECK` and `BUILD` still clean; `cd backend && uv run
  pytest -q` green with the backend diff confined to B1's fix + test.
- **Status**: pending

---

## Risks / accepted gaps

1. **Member-add by raw UUID** — no user-directory/search endpoint exists (DT-5 open question).
   Accepted for this pass; a `GET /api/users?email=` lookup is the natural backend follow-up.
2. **Non-member workspace names unresolvable** — by design (existence-hiding); the detail page
   omits the owner line rather than showing a UUID.
3. **No automated frontend tests** — unchanged from today; verification is check/build + the
   live walkthrough. Adding Vitest/Playwright is a separate decision.
4. **SPA trade-off** — with SSR off, first paint waits on client fetches and public pages lose
   server rendering (SEO). Accepted for this pass; the HttpOnly-cookie + `handleFetch` follow-up
   restores SSR without unfreezing the backend. Write controls may still appear a beat late
   while the workspaces store hydrates; backend enforces the real authz either way.
5. **Token expiry** — localStorage JWTs expire server-side with no client refresh flow;
   expired tokens surface as 401 errors mid-session. Unchanged from today; a re-auth prompt /
   expiry-aware store is a recorded follow-up alongside the cookie+SSR work.
6. **Unload-time session cleanup is best-effort** even with `keepalive` — the kube controller
   idle-reaps orphans in prod; dev-subprocess sessions can linger until stopped or API restart.

## Definition of done

- All six milestones (F1–F5 + B1) `complete` with their gates.
- `npm run check` at (or better than) the F1-recorded baseline; `npm run build` clean.
- Greps: no notebook-ownership `user_id`, no `'draft'`, no `sessions.save`/`postData`/`getData`
  in `frontend/src`.
- The F5 walkthrough passes end-to-end on the real stack, including the B1 archived-serving
  proof and the deployment status lifecycle.
- Backend test suite green; the backend diff is exactly B1's `_load_active_deployment` fix +
  its test — nothing else.
