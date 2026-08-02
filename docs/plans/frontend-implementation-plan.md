# MarimoHub Frontend Implementation Plan

> Completed historical plan. The active plan is `/IMPLEMENTATION_PLAN.md`.

## Status

Complete. All six milestones landed and pass the gates in each milestone section.

## Goal

Replace the frontend's obsolete user-owned notebook contract with the backend's current workspace,
membership, visibility, session, and deployment contract. Deliver complete local registration,
login, logout, expiry recovery, workspace collaboration, notebook management, and automated frontend
verification.

This is a clean frontend alignment. Remove obsolete behavior instead of preserving it.

## Scope

- Update `frontend/` to the current backend API.
- Add active workspace management and member administration UI.
- Add workspace targeting to notebook create, import, and fork flows.
- Replace notebook ownership checks with workspace role capabilities.
- Add Notebook Visibility and permanent notebook deletion controls.
- Remove the deleted explicit session-save flow.
- Display deployment state to readers and management controls to writers.
- Complete local registration/login/logout and expired-session recovery.
- Disable SSR while bearer authentication remains in browser storage.
- Use `adapter-node` and provide a production frontend container.
- Add Vitest and Playwright coverage.
- Update frontend-facing documentation.

## Non-Goals

- No backend code, schema, endpoint, authorization, or deployment-lifecycle changes.
- No compatibility for `draft`, `notebook.user_id`, old lineage fields, body-less forks, old data
  methods, or the old `molab-auth` localStorage value.
- No OIDC UI. The backend has no provider-discovery endpoint and its callback does not hand a token
  back to the SPA.
- No cookie-backed SSR, refresh tokens, or token refresh protocol.
- No user directory. Members are added by raw User ID because that is the frozen API contract.
- No attempt to resolve backend lifecycle defects from the old frontend plan.

## Current Backend Contract

### Authentication

- `POST /api/auth/register` with `{username, email, password}` returns a User.
- `POST /api/auth/login` with `{username, password}` returns `{access_token, token_type}`.
- `POST /api/auth/logout` validates the bearer token and returns 204; JWT invalidation remains local.
- JWT `sub` is the User ID used by the frontend for identity comparisons and the copyable User ID.

### Workspaces

- `GET /api/workspaces` returns the caller's active workspaces and role.
- `POST /api/workspaces` creates a workspace; the caller becomes Owner.
- `GET /api/workspaces/{id}` and `GET /api/workspaces/{id}/members` require membership.
- Rename, archive, restore, and member mutations use the existing backend role checks unchanged.
- `GET /api/workspaces/archived` returns archived workspaces owned by the caller.
- Member creation requires `{user_id, role}`; there is no username/email lookup endpoint.

### Notebooks

- Create, import, and fork require an explicit target `workspace_id`.
- A Notebook has `workspace_id` and nullable `created_by`; it has no owner `user_id`.
- Visibility is `private | unlisted | public`.
- Write capability is Workspace role `owner | editor`.
- Read capability and discovery filtering are enforced by the backend.
- `PUT`, visibility change, permanent delete, deploy, and stop endpoints already exist.
- `GET /api/notebooks/{id}/deployment` returns the current deployment or 404 when none exists.

### Sessions And Deployments

- Session create accepts `{notebook_id, mode}`.
- Session delete is capability-scoped and requires no bearer token.
- The explicit session-save endpoint no longer exists; persistence follows proxied marimo autosave.
- Deployment serving by slug is public and independent of Notebook Visibility.
- Deployment wake responses use 503 while unavailable; 429 represents capacity exhaustion.

### Errors

- Error bodies use `detail`.
- Domain errors generally use a string detail.
- FastAPI validation errors use an array detail.
- `readError()` must continue normalizing both forms.
- A request that actually sent a bearer token and receives 401 clears the local session and sends the
  user through login with a safe `next` path.

## Product Decisions

- The app runs as a client-rendered SPA with root `ssr = false`.
- `adapter-node` is the production adapter.
- Local bearer auth uses a new `marimohub-auth` storage value with no migration from old state.
- Registration without a `next` path lands on `/workspaces`.
- Protected routes redirect to login and preserve `next`.
- The active workspace list is owned by one client store, not duplicated in route data.
- Capability UI waits for workspace-store hydration instead of briefly treating the user as a
  non-member.
- Workspace roles refresh after local mutations and on window focus.
- The Workspaces page shows the current User ID with a copy action so it can be shared out of band.
- Notebook create uses one workspace selector shared across Blank, Upload, and GitLab tabs.
- Fork always confirms its target workspace, even when only one writable workspace exists.
- Notebook actions use the term Visibility, not Publish, and never use Draft.
- Notebook deletion is permanent and requires explicit confirmation.
- A Deployment is clearly labeled public even when its Notebook is Private or Unlisted.
- Every reader can see an active Deployment link/status; only Editors and Owners can manage it.
- Deployment 429 stops automatic retries and offers manual Retry.
- Normal in-app navigation waits for session deletion; browser teardown remains best-effort.
- Unit and browser tests are part of this implementation, not a follow-up.

## Conventions And Gates

Run frontend commands from `frontend/`.

- `CHECK`: `npm run check`
- `UNIT`: `npm run test:unit`
- `BUILD`: `npm run build`
- `E2E`: `npm run test:e2e`
- `ALL`: `npm run check && npm run test:unit && npm run build`

Every milestone must leave `CHECK`, `UNIT`, and `BUILD` green. Milestones that introduce or change a
browser flow also run the relevant Playwright spec. The final milestone runs the full suite.

Use existing frontend conventions unless a milestone explicitly changes them:

- Svelte 5 runes.
- Tailwind v4 and the existing `hub-*` palette.
- `Button.svelte` for actions.
- Inline `role="alert"` errors.
- `ApiError` for request failures.
- No new runtime component library.

## Milestone 1: Test, Rendering, And Packaging Foundation

**Intent:** establish the execution and test environment before behavior changes.

### 1.1 Add Dependencies And Scripts

Modify `frontend/package.json` and lockfile:

- Replace `@sveltejs/adapter-auto` with `@sveltejs/adapter-node`.
- Add `vitest`, `jsdom`, and `@playwright/test` as development dependencies; reuse the existing
  Svelte Vite plugin.
- Add scripts:

```json
{
  "test:unit": "vitest run",
  "test:unit:watch": "vitest",
  "test:e2e": "playwright test",
  "test:e2e:ui": "playwright test --ui"
}
```

### 1.2 Configure The Node Adapter

Modify `frontend/svelte.config.js`:

```js
import adapter from '@sveltejs/adapter-node';

const config = {
  preprocess: vitePreprocess(),
  kit: { adapter: adapter() }
};
```

Create `frontend/src/routes/+layout.ts`:

```ts
export const ssr = false;
```

### 1.3 Configure Vitest

Extend `frontend/vite.config.ts` with a jsdom test environment and a setup file. Create
`frontend/src/test/setup.ts`. Do not add a component-testing library until a test needs DOM-level
component rendering; pure API/store tests are sufficient initially.

### 1.4 Configure Playwright

Create `frontend/playwright.config.ts` and `frontend/e2e/`.

- Require `E2E_DATABASE_URL` to name the dedicated `molab_e2e` database.
- Refuse to run destructive setup for any other database name.
- Run Alembic downgrade/upgrade against only that database.
- Start the backend with `SESSION_BACKEND=subprocess` on a dedicated port.
- Build and start the adapter-node frontend on a dedicated port.
- Run browser tests serially until fixtures prove safe for parallel execution.

Example guard in Playwright global setup:

```ts
const databaseUrl = process.env.E2E_DATABASE_URL ?? '';
if (!databaseUrl.includes('/molab_e2e')) {
  throw new Error('E2E_DATABASE_URL must target molab_e2e');
}
```

### 1.5 Add The Production Frontend Container

Create `Containerfile.frontend`:

```dockerfile
FROM node:22-alpine AS build
WORKDIR /app
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci
COPY frontend/ ./
RUN npm run build

FROM node:22-alpine
WORKDIR /app
ENV NODE_ENV=production HOST=0.0.0.0 PORT=3000
COPY --from=build /app/build ./build
COPY --from=build /app/node_modules ./node_modules
COPY --from=build /app/package.json ./package.json
EXPOSE 3000
CMD ["node", "build"]
```

Use `$env/dynamic/public` so `PUBLIC_API_URL` remains configurable at container runtime.

### 1.6 Document Frontend Environment

Create `frontend/.env.example` with:

```dotenv
PUBLIC_API_URL=http://localhost:8000
```

Replace the generated `frontend/README.md` with project-specific install, dev, test, build, and
adapter-node run commands.

### Verification

```bash
npm ci
npm run check
npm run test:unit
npm run build
PUBLIC_API_URL=http://localhost:8000 node build
```

**Status:** complete

## Milestone 2: API Contract And Authentication

**Intent:** make the client accurately represent the backend and provide one complete local-auth flow.

### 2.1 Replace Obsolete API Types

Modify `frontend/src/lib/api.ts`.

- Replace `draft` with `private`.
- Replace Notebook `user_id` with `workspace_id` and nullable `created_by`.
- Replace parent owner fields with `parent_workspace_id` and `parent_workspace_slug`.
- Type `source` as optional and nullable because list rows omit it while detail rows may return null.
- Add Workspace, WorkspaceArchive, WorkspaceMember, and WorkspaceRole types.
- Give update its own type; never advertise workspace reassignment.

Example:

```ts
export type WorkspaceRole = 'owner' | 'editor' | 'viewer';
export type NotebookVisibility = 'private' | 'unlisted' | 'public';

export type Notebook = {
  id: string;
  workspace_id: string;
  created_by: string | null;
  parent_id: string | null;
  parent_title?: string | null;
  parent_workspace_id?: string | null;
  parent_workspace_slug?: string | null;
  title: string;
  description: string | null;
  tags: string[];
  visibility: NotebookVisibility;
  fork_count: number;
  source?: string | null;
  created_at: string;
  updated_at: string;
};

export type NotebookUpdateRequest = {
  title?: string;
  description?: string | null;
  tags?: string[];
  source?: string | null;
};
```

### 2.2 Replace Obsolete API Methods

- Require `workspace_id` in create/import bodies.
- Change fork to `fork(id, {workspace_id})`.
- Add all existing Workspace endpoints.
- Add `notebooks.getDeployment(id)`.
- Delete `sessions.save`, `notebooks.postData`, `notebooks.getData`, and their dead types.
- Let session delete accept request options needed by teardown and call it with `auth: false`.

### 2.3 Fix Query Serialization

Serialize arrays as repeated parameters:

```ts
if (Array.isArray(value)) {
  for (const item of value) url.searchParams.append(key, item);
} else {
  url.searchParams.set(key, String(value));
}
```

Keep `normalizeTagInput()` responsible only for splitting and trimming form input.

### 2.4 Replace The Auth Store Cleanly

Modify `frontend/src/lib/stores/auth.ts`.

- Use storage key `marimohub-auth`.
- Do not read or migrate `molab-auth`.
- Store `{token, currentUser}` in one versionless current shape.
- Require `currentUser.id` whenever authenticated.
- Derive the ID from JWT `sub`; this is display/client-state data, not authorization.
- Expose synchronous token/state snapshots for API calls and client route guards.
- Make logout clear local state even if backend logout fails.

### 2.5 Complete Register, Login, Logout, And Expiry

Modify both auth pages and root layout.

- Share one `safeNextPath()` helper.
- Preserve `next` between Login and Register links.
- Registration logs in and uses `next ?? '/workspaces'`.
- Login uses `next ?? '/workspaces'`.
- Logout clears auth and workspace state before navigating home.
- The logout request bypasses centralized 401 redirect handling; its `finally` path clears local
  state, so an expired token cannot race a redirect back to the page being logged out from.
- Only a 401 from a request that sent a token expires the local session.
- Expiry redirects to `/auth/login?next=<current path and query>`.
- Prevent redirect loops on auth routes.

Example safe return-path helper:

```ts
export function safeNextPath(value: string | null, fallback = '/workspaces') {
  if (!value || !value.startsWith('/') || value.startsWith('//')) return fallback;
  return value;
}
```

### 2.6 Add Protected Route Guards

Create a small client-only guard helper and `+page.ts` guards for:

- `/workspaces`
- `/workspaces/archived`
- `/workspaces/[id]`
- `/notebooks/new`
- `/notebooks/[id]/edit`

Do not guard notebook detail or run routes globally; backend visibility controls them.

### 2.7 Pin API And Auth Behavior With Unit Tests

Add tests for:

- string and array error detail normalization;
- repeated tag parameters;
- exact current request bodies;
- no obsolete API methods;
- JWT User ID extraction;
- invalid stored auth removal;
- safe `next` handling;
- token-bearing versus anonymous 401 behavior.

### Verification

Run `ALL`. Grep `frontend/src` for `draft`, notebook `user_id`, `parent_owner`, `sessions.save`,
`postData`, and `getData`; only WorkspaceMember `user_id` may remain.

**Status:** complete

## Milestone 3: Workspace State And Management UI

**Intent:** add the active ownership/collaboration surface that all notebook writes depend on.

### 3.1 Add The Active Workspace Store

Create `frontend/src/lib/stores/workspaces.ts` as the sole owner of `GET /api/workspaces` state.

```ts
type WorkspaceState =
  | { status: 'anonymous'; items: [] }
  | { status: 'loading'; items: Workspace[] }
  | { status: 'ready'; items: Workspace[] }
  | { status: 'error'; items: Workspace[]; error: string };
```

Expose:

- `refresh()`;
- `clear()`;
- `roleFor(workspaceId)`;
- `canWrite(workspaceId)`;
- writable workspaces (`owner | editor`).

Guard against stale responses after logout or a newer refresh:

```ts
const request = ++generation;
const token = getAuthToken();
const items = await api.workspaces.list();
if (request !== generation || token !== getAuthToken()) return;
state.set({ status: 'ready', items });
```

### 3.2 Synchronize Workspace State

In the root layout:

- refresh after stored auth hydration;
- refresh after login/register;
- clear on logout/expiry;
- refresh on window focus while authenticated;
- never issue a protected workspace request while anonymous.

### 3.3 Add Workspace Navigation And User ID

- Add Workspaces navigation for authenticated users.
- Show the authenticated User ID with a copy button on the Workspaces page.
- Explain that a user shares this ID with a Workspace Owner to be added.

### 3.4 Build The Workspace List/Create Route

Create `frontend/src/routes/workspaces/+page.svelte`.

- Consume the store rather than loading a duplicate list.
- Render loading, retryable error, empty, and ready states.
- Show name, immutable slug, caller role, and creation date.
- Add name and optional slug creation form.
- Validate slug format client-side and show backend 409 inline.
- Refresh the store after successful creation.

### 3.5 Build The Workspace Detail Route

Create `frontend/src/routes/workspaces/[id]/+page.ts` and `+page.svelte`.

- Load workspace detail and member list once.
- Show rename controls only for Owner.
- Show member User ID, username, email, role, and join date.
- Add members by raw User ID and role.
- Show role-change and remove controls only where the current backend permits them.
- Handle duplicate member, unknown user, and last-owner errors inline.
- Refresh the active store after every mutation.
- If focus refresh shows the current workspace disappeared, redirect to `/workspaces` with a notice.

Do not add viewer/editor self-leave UI; the frozen backend's delete-member route is Owner-only.

### 3.6 Build The Archive Route

Create `frontend/src/routes/workspaces/archived/+page.ts` and `+page.svelte`.

- Show archive and purge timestamps.
- Archive uses explicit confirmation and then returns to `/workspaces`.
- Restore refreshes the archived list and active workspace store.
- Present archive as reversible and purge as permanent.
- Do not claim stronger expiration or runtime guarantees than the backend currently provides.

### 3.7 Test Workspace State And UI

Vitest:

- auth hydration and clear;
- stale refresh suppression;
- role and writable-workspace derivation;
- retry behavior.

Playwright:

- register lands on Workspaces;
- create success and slug conflict;
- User ID copy;
- two-user add/promote/demote/remove flow;
- last-owner conflict;
- archive and restore;
- hard refresh of every workspace route.

### Verification

Run `ALL` and the workspace Playwright spec.

**Status:** complete

## Milestone 4: Notebook Workspace Alignment

**Intent:** repair create/import/fork/detail flows and expose current notebook management features.

### 4.1 Add A Shared Workspace Target Picker

Create `frontend/src/lib/components/WorkspaceTargetPicker.svelte`.

- Consume writable workspaces from the store.
- Show loading and retry states.
- Preselect the sole writable workspace.
- Require a deliberate selection when multiple exist.
- Label duplicate names with immutable slugs.
- With zero writable workspaces, link to `/workspaces`.

### 4.2 Rework Notebook Creation

Modify `frontend/src/routes/notebooks/new/+page.svelte`.

- Put one target picker above the Blank/Upload/GitLab tabs.
- Preserve the selected target while switching tabs.
- Include `workspace_id` in every request.
- Replace all Draft copy with Private Notebook.
- Keep GitLab PAT ephemeral and clear it after submission.

### 4.3 Replace Ownership With Capability

Modify notebook detail.

- Subscribe to workspace state so role changes are reactive.
- Treat `loading` separately from no role.
- `owner | editor`: Edit, Visibility, Delete, Deploy, Stop.
- `viewer`: read/run/fork only.
- non-member reader: read/run/fork only when backend allows read.
- Show Workspace name and slug only when it is available from the caller's active workspace list.
- Label this field Workspace, never Owner.

Avoid a helper/name collision such as `const canWrite = $derived(canWrite(...))`; name the derived
value `mayWriteNotebook` or similar.

### 4.4 Add Visibility Controls

- Use a select containing Private, Unlisted, and Public.
- Label the operation Visibility.
- Explain discovery/direct-link semantics.
- Apply via the existing visibility endpoint and update page state from its response.

### 4.5 Add Permanent Notebook Delete

- Show only to Editors and Owners.
- Require confirmation naming the Notebook and state that deletion cannot be undone.
- On success navigate to `/discover`.
- On 403/404 refresh workspace/auth state before presenting the backend error.

### 4.6 Rework Fork

- Anonymous users go to login with `next`.
- Authenticated users always see target confirmation.
- Preselect the sole writable target but still require confirmation.
- Zero writable targets links to Workspace creation.
- Submit `{workspace_id}` and navigate to the new fork's edit route.
- Show backend source/target access errors inline.

### 4.7 Correct Attribution And Discovery Copy

- Render parent title and readable parent Workspace slug.
- If parent attribution is hidden, say it is unavailable rather than exposing IDs.
- Replace `draft` badge styling with `private`.
- State that Discover shows Public Notebooks plus all Notebooks in the signed-in user's Workspaces;
  Unlisted Notebooks remain direct-link-only for non-members.

### 4.8 Test Notebook Flows

Vitest:

- target picker state rules;
- role capability derivation;
- visibility labels and request body.

Playwright:

- all three create methods send the selected workspace;
- zero/one/multiple target behavior;
- fork always confirms target;
- viewer has no write controls, Editor does;
- visibility transitions Private -> Unlisted -> Public -> Private;
- permanent delete;
- authenticated hard refresh of a Private Notebook;
- multi-tag filtering sends repeated parameters and returns intersection results.

### Verification

Run `ALL`, notebook Playwright specs, and the obsolete-symbol grep from Milestone 2.

**Status:** complete

## Milestone 5: Deployment And Session Lifecycle UI

**Intent:** align status, public-app, and editor teardown behavior with existing backend endpoints.

### 5.1 Load Deployment Read State

On Notebook detail:

- fetch `GET /api/notebooks/{id}/deployment` after the Notebook loads;
- treat deployment 404 as no deployment without turning it into a page error;
- show an active public app link/status to every Notebook reader;
- show stopped state and management actions only to Editors/Owners;
- state that the app URL is public regardless of Notebook Visibility.

### 5.2 Keep Deployment State Fresh

- Re-fetch after deploy and stop.
- Re-fetch on window focus.
- Poll about every 15 seconds only while the document is visible and an active deployment exists.
- Use one abortable/revisioned request path so older responses cannot overwrite newer state.
- Preserve last-known state on transient failures; clear only on confirmed no-deployment 404.

### 5.3 Correct Public Deployment Wake UX

Modify `frontend/src/routes/deploy/[slug]/+page.svelte`.

- Keep existing immediate failure for 404 and other terminal responses.
- Retry 503/504 with a bounded timeout.
- Stop on 429, explain capacity exhaustion, and show manual Retry.
- Prevent overlapping attempts and cancel timers when the component is destroyed.

### 5.4 Remove Explicit Session Save

Modify `frontend/src/lib/components/SessionFrame.svelte`.

- Delete the timer, delay, `saving` state, and `sessions.save` call.
- Retain the user-facing statement that marimo autosaves automatically.
- Treat session DELETE 404 as already ended.

### 5.5 Make Normal Navigation Cleanup Deterministic

- Intercept in-app navigation with SvelteKit `beforeNavigate`.
- Cancel once, await session DELETE, then continue navigation.
- Guard against navigation recursion.
- The End Session button uses the same cleanup function.
- On `pagehide`, send one unauthenticated best-effort DELETE with `keepalive: true`.
- Do not await or promise unload cleanup.

Example teardown request:

```ts
void api.sessions.delete(sessionId, {
  auth: false,
  keepalive: true
});
```

### 5.6 Surface Session Authorization Failures

- Anonymous edit 401 redirects to login with `next`.
- Viewer edit 403 explains that Editor role is required and links back to Notebook detail.
- Hidden/missing Notebook 404 uses the existing not-found presentation.
- Run remains available anonymously when backend visibility permits it.

### 5.7 Test Lifecycle UI

Vitest:

- deployment status request revision handling;
- 404/no-deployment versus transient error;
- 429 manual retry and bounded 503 retry;
- session DELETE option construction.

Playwright:

- readers see active app, writers see controls;
- Private Notebook deployment displays the public-access warning;
- focus/poll observes running/sleeping changes;
- 429 requires user Retry;
- edit autosave persists after navigation without a save endpoint call;
- in-app navigation waits for DELETE;
- hard refresh and teardown do not produce duplicate session deletes.

### Verification

Run `ALL`, lifecycle Playwright specs, and:

```bash
grep -R "sessions.save\|postData\|getData\|/save" src
```

The grep returns no matches.

**Status:** complete

## Milestone 6: Full Browser Verification And Documentation

**Intent:** prove the integrated frontend against the real frozen backend and close all stale docs/copy.

### 6.1 Run The Dedicated E2E Stack

- Start PostgreSQL with a separate `molab_e2e` database.
- Apply the current backend Alembic migrations to it.
- Start backend with subprocess sessions and E2E-only configuration.
- Start the built adapter-node frontend, not Vite dev mode.
- Verify CORS and runtime `PUBLIC_API_URL` before running tests.

### 6.2 Run The Complete Two-User Journey

Automate and manually smoke:

1. Register User A; verify default Workspaces landing.
2. Create Workspace Alpha and a Private Notebook in it.
3. Open Edit, change source, wait for marimo autosave, navigate away, and reopen.
4. Change Visibility to Public and verify Discover.
5. Register User B, create Workspace Beta, and fork A's Notebook into Beta through confirmation.
6. Add B to Alpha by copied User ID; verify Viewer then Editor capability changes.
7. Exercise last-owner conflict and owner-only controls.
8. Deploy A's Notebook and verify every reader sees the public app while only writers manage it.
9. Stop and redeploy; verify status refresh on focus/poll.
10. Archive and restore a scratch Workspace through the current backend behavior.
11. Verify anonymous public read/run, login return path, and no write controls.
12. Verify hard refreshes on every new route and both desktop/mobile viewports.

### 6.3 Run Production Artifact Checks

```bash
npm run build
PUBLIC_API_URL=http://localhost:8000 ORIGIN=http://localhost:3000 node build
```

- Request `/`, `/discover`, `/workspaces`, and a nested Notebook route from the Node server.
- Confirm SPA deep links return the app shell.
- Build `Containerfile.frontend` and pass the same smoke checks against the container.

### 6.4 Finish Documentation And Copy Sweep

- Replace the generated frontend README.
- Link root `README.md` to `CONTEXT.md` and this plan.
- Ensure all frontend copy uses Workspace ownership, Private visibility, and Archive terminology.
- Document local auth limitations: localStorage JWT, no refresh token, no OIDC UI.
- Document the dedicated E2E database safety rule.
- Do not add ADRs.

### 6.5 Final Gates

```bash
cd frontend
npm ci
npm run check
npm run test:unit
npm run build
npm run test:e2e
```

Final greps:

```bash
grep -R "'draft'\|\"draft\"\|parent_owner\|sessions.save\|postData\|getData" src
grep -R "notebook.user_id" src
```

Both return no matches. `user_id` remains only where it denotes a Workspace Member or copied User ID.

**Status:** complete

## Definition Of Done

- All six milestones are complete and independently green.
- Frontend types and request bodies match the frozen backend contract.
- Local registration, login, logout, safe return paths, and expired-token recovery work.
- Workspace create/list/detail/member/archive/restore UI works within current backend authorization.
- Every notebook create/import/fork names a target Workspace.
- No obsolete user-owned Notebook, Draft, old lineage, explicit save, or public data-client code remains.
- Notebook Visibility and permanent delete controls are capability-gated.
- Deployment read state is visible to readers and management is limited to Editors/Owners.
- Session navigation cleanup and marimo autosave behavior are verified.
- Vitest, Playwright, Svelte check, adapter-node build, and frontend container smoke all pass.
- Desktop and mobile browser flows pass against the dedicated real-backend E2E stack.
- `CONTEXT.md`, root README, frontend README, and environment examples agree with the shipped UI.
