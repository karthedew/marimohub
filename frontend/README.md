# MarimoHub Frontend

SvelteKit SPA for MarimoHub. It renders entirely in the browser (`ssr = false`) because the bearer
token that authenticates every API request lives in `localStorage`, which a server render cannot
see.

## Install

```bash
npm ci
```

## Configure

Copy `.env.example` to `.env` and point it at a running backend:

```bash
cp .env.example .env
```

`PUBLIC_API_URL` is the only environment variable this app reads. It must be an absolute
`http(s)` URL with no trailing slash.

## Develop

```bash
npm run dev
```

Runs the Vite dev server (default `http://localhost:5173`) against whatever `PUBLIC_API_URL`
points at — typically a backend started from `../backend` or via `podman-compose up` from the
repo root.

## Type-check

```bash
npm run check
```

## Test

Unit tests (Vitest, jsdom environment):

```bash
npm run test:unit
npm run test:unit:watch
```

Browser tests (Playwright) require a dedicated backend, database, and built frontend — see
`e2e/global-setup.ts`. They are never pointed at a shared or development database:
`E2E_DATABASE_URL` must name the `molab_e2e` database, and setup refuses to run otherwise, since
it downgrades and re-upgrades that database's schema on every run.

```bash
E2E_DATABASE_URL=postgresql+asyncpg://molab:molab@localhost:5432/molab_e2e npm run test:e2e
npm run test:e2e:ui
```

The suite runs a `chromium` project at desktop viewport plus a `mobile` project, scoped to
`e2e/mobile.spec.ts`, at a phone viewport. Global setup starts, in order, a fake OpenID provider,
its own backend, and a built frontend, all reached through `localhost`:

| Process | Default port | Override |
|---|---|---|
| Fake OpenID provider (`e2e/fakeIdp.ts`) | `8111` | `E2E_IDP_PORT` |
| Backend | `8100` | `E2E_BACKEND_PORT` |
| Built frontend | `5173` | `E2E_FRONTEND_PORT` |

The backend is started with `PUBLIC_APP_URL` set to the e2e frontend's URL, which is both where it
sends the browser after a provider sign-in and the extra origin it allows for CORS. With the
defaults the suite collides with `npm run dev` on `5173`; set `E2E_FRONTEND_PORT` (for example
`5174`) to run both at once.

Once the backend is up, setup registers a throwaway user and publishes one notebook through the
API. The first publish out of Private loads the backend's embedding model (about 5 s, longer if it
must first be downloaded), and doing it during setup keeps that delay out of the specs. GitLab
import stays disabled in this backend, so `e2e/notebooks.spec.ts` answers the browser's import
request itself with `page.route`.

The backend is also given exactly one identity provider, `Test IdP` (slug `e2e`, issuer
`http://localhost:8111`, client `e2e-client` / `e2e-secret`), and blank `GOOGLE_*` and
`OIDC_HTTP_PROXY_URL` values so nothing from a developer's `backend/.env` leaks in. The fake
provider checks what a real one would — the registered redirect URI, client credentials,
single-use codes, PKCE S256 — and signs RS256 ID tokens with a key generated for the run. It never
shows a page: `/authorize` approves at once as the identity a test queued with
`scriptFakeIdpLogin(idpControlUrl, identity)` (or a default user), and denies with
`error=access_denied` when a test queued `{ deny: true }` or the request carries `login_hint=deny`.
`e2e/oidc.spec.ts` covers signing in with a `next` path, cancelling at the provider, and
replaying a handoff URL.

## Build And Run

Production uses `@sveltejs/adapter-node`, so the build output is a standalone Node server rather
than static files:

```bash
npm run build
PUBLIC_API_URL=http://localhost:8000 node build
```

`HOST`, `PORT`, and `ORIGIN` configure the Node server the same way for any adapter-node app.
`PUBLIC_API_URL` is read at request time via `$env/dynamic/public`, so the same build can be
retargeted at a different backend without rebuilding — see `../Containerfile.frontend`, which
builds once and lets the container runtime supply `PUBLIC_API_URL`.

## Sign In With An Identity Provider

The sign-in and registration pages show one button per provider that `GET /api/auth/providers`
lists, and nothing extra when that list is empty or the request fails. Providers are configured
on the backend (`OIDC_PROVIDERS`, or the `GOOGLE_CLIENT_ID` / `GOOGLE_CLIENT_SECRET` shortcut); this
app needs no settings of its own for them.

The backend runs the whole OpenID Connect authorization-code flow, so this app never sees a
provider's tokens. It only proves that the tab finishing sign-in is the tab that started it:

1. **Start** (`ProviderSignIn.svelte`, `$lib/oidc`). A click creates a random verifier (32 bytes,
   base64url, 43 characters) and its S256 challenge with WebCrypto, stores
   `{ provider, verifier, next, created_at }` in `sessionStorage` under `marimohub-oidc`, and does a
   full page load (`window.location.assign`, never `goto` or `fetch`) of
   `/api/auth/oidc/{slug}/login?challenge=…`. That URL is absolute when `PUBLIC_API_URL` names a
   separate backend (compose) and a same-origin `/api` path otherwise (kind).
2. **Provider.** The backend redirects to the provider and back to its own callback, then sends
   the browser to `/auth/callback#handoff=…`, a 60-second token bound to the challenge.
3. **Finish** (`routes/auth/callback`, `$lib/oidcCallback`). The page reads the handoff from the
   fragment and immediately removes it from the address bar and history entry, then reads and
   deletes the stored attempt (rejecting one older than 10 minutes), posts
   `{ handoff, verifier }` to `POST /api/auth/oidc/exchange`, stores the returned token and user,
   and replaces the history entry with `next` (re-checked by `safeNextPath`).

A handoff URL is therefore useless on its own: replayed from history it finds no verifier, and in
another browser the backend rejects any other verifier. On any failure the callback page explains
what happened and links back to sign-in.

When sign-in fails before the handoff, the backend sends the browser to `/auth/login?error=CODE`,
and the page shows a message for each code: `oidc_denied` (cancelled at the provider),
`oidc_expired`, `oidc_failed`, `oidc_account_exists`, `oidc_domain_not_allowed`, and
`oidc_invalid_request`. That redirect cannot carry `next`, so the page recovers it from the
abandoned attempt.

Provider sign-in needs a secure context for WebCrypto — `https`, or `http://localhost` — and
`sessionStorage`. Without either, the button explains the problem instead of navigating.

Google's button follows the Sign in with Google branding guidelines: the standard-color "G" on
Google's light (`#FFFFFF` / `#747775` / `#1F1F1F`) or dark (`#131314` / `#8E918F` / `#E3E3E3`)
theme, following this app's theme, "Sign in with Google" on sign-in and "Continue with Google" on
registration, and listed before any other provider. No web font is loaded: the label uses Google
Sans or Roboto when installed and falls back to Arial. Every other provider gets a neutral
"Continue with {display name}" button.

The redirect URIs to register with Google are `http://localhost:8000/api/auth/oidc/google/callback`
(compose) and `https://localhost/api/auth/oidc/google/callback` (kind). Google rejects
`*.localhost` hosts, which is why kind serves on `https://localhost`.

## Known Auth Limitations

Every sign-in, password or provider, ends in the same JWT access token in `localStorage`, with no
refresh token and no token-refresh protocol: an expired token requires signing in again. Signing
out of MarimoHub does not sign you out of your identity provider; Google asks which account to use
on every sign-in instead.

## Adding Workspace Members

On a Workspace's page, an Owner finds the person to add with the **Person** field, a combobox
backed by `GET /api/workspaces/{id}/member-candidates?q=…`. Adding still posts only the chosen
person's `user_id` to `POST /api/workspaces/{id}/members`. The search matches:

- part of a username or display name (the optional "Full name" given at registration, or the
  `name` claim of a provider sign-in);
- an email address, but only the complete address, so nobody can list everyone at a domain;
- a pasted User ID.

Current members are never offered. Each result shows the person's email masked
(`k•••@example.com`) unless the Owner typed that exact address. The field searches from 2
characters after a 250 ms pause and ignores answers to a query the Owner has since changed; while
the next answer is on its way, the people already listed stay listed. A search returns at most 10
people, so a full list says it may have been cut short and to keep typing. Its state, debouncing,
and keyboard handling (WAI-ARIA combobox: arrow keys, Enter, Escape) live in
`src/lib/components/personSearch.ts`, which is unit tested; `PersonSearch.svelte` only renders it.
