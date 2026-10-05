# MarimoHub

MarimoHub is a FastAPI, SvelteKit, and PostgreSQL/pgvector application for publishing and running
marimo notebooks, backed by a Go operator that reconciles `MarimoSession` Kubernetes resources into
hardened notebook Runtime Pods and Services.

## Local Development

Compose runs with its built-in defaults:

```bash
podman-compose up
```

Backend host loop. The backend reads `.env` from its working directory, so copy `.env.example` to
`backend/.env`, not to the repository root. Make sure that file sets
`PUBLIC_APP_URL=http://localhost:5173`: provider sign-in (Google, OIDC) returns to that URL, and
without it the sign-in ends on the backend at `http://localhost:8000` instead of the frontend.

```bash
cp .env.example backend/.env
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

## Local Kubernetes

To run Sessions as real Runtime Pods instead of local subprocesses, bring up the full stack on
kind. This needs `make bootstrap-tools` first and Docker. It mounts two host directories, which
you can override with `MARIMOHUB_NFS_DIR` and `MARIMOHUB_DATA_DIR`:

```bash
make kind-up      # then open https://localhost
make kind-down    # keeps users, notebooks, and Workspace files
```

Your browser has to trust the generated CA, `.kind/tls/ca.crt`. See
[deploy/README.md](deploy/README.md#local-kind-environment) for what it installs.

Two kinds of storage reach notebooks:
- **Shared NFS:** `/data1/nfs` (standing in for an NFS export) is mounted whole into every notebook
  and Deployment, read-only, at the same path, so notebook code reads
  `/data1/nfs/sales.csv` exactly as it would on the host. New files appear immediately.
  `MARIMOHUB_NFS_WRITABLE=1 make kind-up` makes it writable in edit and run notebooks, and then
  keeps it out of public Deployments. For real NFS exports, see
  [deploy/README.md](deploy/README.md#shared-volumes-for-notebooks-optional).
- **Workspace files:** MarimoHub keeps these off the NFS share, in
  `/data1/marimohub/workspaces/<workspace-id>`. A notebook sees only its own Workspace's directory, at
  `/work/workspace` (`MARIMOHUB_WORKSPACE_DIR`). Edit sessions can write it; run sessions can only read
  it.

kind fixes host mounts when it creates the cluster, so changing either directory needs
`make kind-down && make kind-up`. Users and notebooks are kept, because PostgreSQL runs outside the
cluster.

## Sign in with Google

People can sign in with a Google account as well as with a username and password. This needs a
Google OAuth client, and one client can serve both local environments:

1. In the [Google Cloud console](https://console.cloud.google.com/), select or create a project.
   Google asks for a separate project per environment, so keep production out of this one.
2. Open [Google Auth Platform > Branding](https://console.cloud.google.com/auth/branding). If it
   says the platform is not configured yet, click **Get started**, enter an app name and support
   email, choose the audience, add a contact email, and accept the user data policy.
3. Under [Audience](https://console.cloud.google.com/auth/audience), choose **External** to accept
   any Google account, or **Internal** to accept only your Google Workspace organization (offered
   only for projects that belong to one).
4. Under [Data access](https://console.cloud.google.com/auth/scopes), click **Add or remove
   scopes**, add `openid`, `.../auth/userinfo.email`, and `.../auth/userinfo.profile`, and save.
   These scopes need no Google verification, and an app that asks only for them works for any
   Google account while it is still in testing, without a test-user list.
5. Under [Clients](https://console.cloud.google.com/auth/clients), click **Create client**, choose
   **Web application**, leave **Authorized JavaScript origins** empty, and add both
   **Authorized redirect URIs** exactly as written:
   - `http://localhost:8000/api/auth/oidc/google/callback` (compose)
   - `https://localhost/api/auth/oidc/google/callback` (kind)
6. Copy the client ID and client secret when the client is created; Google does not show the
   secret again. New settings can take from five minutes to a few hours to apply.

**Compose** reads the credentials from a `.env` file in the repository root, which git ignores:

```dotenv
GOOGLE_CLIENT_ID=1234567890-abc123.apps.googleusercontent.com
GOOGLE_CLIENT_SECRET=GOCSPX-...
# Optional: accept only accounts from this Google Workspace domain.
# GOOGLE_HOSTED_DOMAIN=example.com
```

Start the stack with `make up`, or run `make backend-up` if it is already running, so compose
recreates the backend with the new environment (`make backend-restart` keeps the old one). Open
the app at `http://localhost:5173`, not `127.0.0.1`. The backend host loop does not read the
repository-root `.env`: put the same variables in `backend/.env`, which also needs
`PUBLIC_APP_URL=http://localhost:5173` (see [Local Development](#local-development)).

**kind** takes them as environment variables for `make kind-up`, which saves them in
`.kind/google-oauth.env` (mode 0600), so later runs need nothing extra:

```bash
read -rsp 'Google client secret: ' GOOGLE_CLIENT_SECRET && echo
GOOGLE_CLIENT_ID=1234567890-abc123.apps.googleusercontent.com \
  GOOGLE_CLIENT_SECRET="$GOOGLE_CLIENT_SECRET" make kind-up
```

`GOOGLE_HOSTED_DOMAIN` is passed the same way. Pass a new value to replace a saved one, or run
`GOOGLE_CLIENT_ID= GOOGLE_CLIENT_SECRET= make kind-up` to turn Google sign-in off. kind serves the
app on `https://localhost` because Google refuses redirect URIs on `*.localhost` names. While
Google sign-in is on, kind also lets the public backend open HTTPS connections to any address,
which is acceptable only for development; see
[deploy/README.md](deploy/README.md#google-and-openid-connect-sign-in) for production.

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
