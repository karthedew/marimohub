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
`e2e/mobile.spec.ts`, at a phone viewport. It starts its own backend and frontend on fixed ports
(`8100` and `5173`) bound to `localhost`, matching the backend's single allowed CORS origin, so it
cannot run at the same time as `npm run dev` on the same machine.

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

## Known Auth Limitations

Local authentication stores a JWT access token in `localStorage`, with no refresh token and no
token-refresh protocol: an expired token requires signing in again. There is no OIDC UI; the
backend has no provider-discovery endpoint and does not hand a token back to this SPA.
