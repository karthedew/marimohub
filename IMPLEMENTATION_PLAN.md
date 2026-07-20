# MarimoHub Backend — Implementation Plan

Source design: `DESIGN.md` (all sections DT-1…DT-13 complete). Every task references design
sections by heading; implementers read the design there — this plan never restates it.

## Goal

Redesign the backend around clear domain boundaries: workspace/collaborator model, notebook
visibility and forking, and Kubernetes-native session management via the MarimoSession CRD,
with a clean seam for swapping local JWT auth for OIDC/SAML.

## Non-goals

- **Frontend**: no changes under `frontend/` are planned or verified here.
- **The Go operator implementation**: per *DT-8 Design → Controller component layout (outside
  `backend/app`)*, the reconciler is a separate component (`marimohub-operator/`, kubebuilder).
  This plan delivers the CRD/manifest YAMLs, the backend's internal endpoints, and the
  `KubeSessionManager` client half; writing the Go controller is out of scope.
- **Object-store (MinIO) storage backend**: the seam stays swap-ready per *DT-11 Design →
  Target design — storage seam (unchanged surface, the swap point)*; no MinIO implementation.
- **OIDC live integration tests against a real IdP**: provisioning and resolve/JIT logic is
  unit-tested against the DB; token verification is isolated per *DT-4 Design → OIDC HTTP flow
  + verification seam (`api/auth.py`)* and mocked in tests.

## Conventions

- All commands run from `backend/` unless stated. Test DB per existing `tests/conftest.py`.
- `LINT` = `uv run ruff check .` — must exit 0.
- `TYPES` = `uv run ty check` — must exit 0 (or introduce no new diagnostics vs. the previous
  milestone if the type-check baseline is nonzero; record the count in the task completion note).
- `TEST` = `uv run pytest` — full suite green.
- Milestones are the green checkpoints: **every milestone ends compiling, LINT/TYPES clean,
  TEST green.** Tasks are the per-subagent units (30k–50k tokens each). All milestones are
  single-task except M5, which is physically indivisible below five tasks (the squashed schema
  baseline and ORM/API ownership flip must land together); within M5, only the
  milestone boundary guarantees a green suite, and per-task verification is stated per task.

---

## Milestone sequence

1. **M1 — Error taxonomy & slug foundations** (DT-13 core, DT-5 workspace slugs)
2. **M2 — Session-manager seam & subprocess decomposition** (DT-7, subprocess half)
3. **M3 — Gateway consolidation & single edit-save path** (DT-9, DT-11)
4. **M4 — Lifecycle source-of-truth & legacy lifecycle deletions** (DT-10, non-schema half)
5. **M5 — Domain schema flip: workspaces, identities, visibility** (DT-1, DT-2, DT-3, DT-4, DT-6 core)
6. **M6 — Workspace & membership management API** (DT-5)
7. **M7 — Notebook API completion: forking, discovery, attribution** (DT-6 remainder)
8. **M8 — Internal API & service-token auth** (DT-8 backend half, DT-6 data-write decision)
9. **M9 — Kube session backend & configuration surface** (DT-7 kube half, DT-12)
10. **M10 — CRD & namespace manifests, final sweep** (DT-8 manifests, global cleanup)

Dependency chain: M1 → M2 → M3 → M4 → M5 → M6/M7 (either order, M6 before M7 preferred) →
M8 → M9 → M10. Deletions land at the earliest point their callers are gone: `process_manager.py`
in M2, duplicated proxy bodies in M3, reaper/boot-reset in M4, per-router authz copies and
`users.password_hash` in M5, public data POST in M8, obsolete settings keys in M2/M4/M9.

---

## M1 — Error taxonomy & slug foundations

**Intent**: land the cross-cutting contracts every later milestone raises errors through, plus
the shared DNS-label slug module, before anything depends on them.

- **Design**: *DT-13 Design → Target design — new module `backend/app/core/errors.py`*,
  *→ Domain error hierarchies (signatures unchanged)*,
  *→ `main.py` wiring — the whole seam*,
   *→ Explicit contracts*, *→ Deletions (by file / symbol)*; *DT-5 Design → Pydantic schemas*
   (shared `app/services/slug.py`); *DT-5 Design → Policy reuse…* names `to_dns_label`/
  `unique_slug`/`DNS_LABEL_RE` as the slug surface.
- **Create**: `backend/app/core/errors.py`; `backend/app/services/slug.py`;
  `backend/tests/test_errors.py` (handler renders status/detail per contract; WS close-code
  mapping unit-tested at the mapping function); slug unit tests (DNS-label sanitisation,
  truncation, collision suffixing) in `backend/tests/test_slug.py`.
- **Modify**: `backend/app/main.py` (register exception handlers); existing service exception
  classes re-rooted onto the new base (`services/process_manager.py`, `services/gitlab_import.py`,
  `services/auth_service.py`); routers listed in *DT-13 Design → Mapping — every current inline
  `HTTPException` raise → new model* lose the raises that the mapping migrates now (those tied
  to not-yet-built modules migrate in their own milestones).
- **Delete**: per *DT-13 Design → Deletions (by file / symbol)* — the per-router try/except
  translation blocks whose exceptions are now handler-rendered.
- **Decision (closes DT-13's blanket-handler open question)**: `register_error_handlers`
  installs the catch-all `Exception` handler unconditionally — no test-off settings flag. Any
  existing test that relied on `raise_server_exceptions=True` propagation is rewritten here to
  assert the rendered `500 {"detail": "Internal server error"}` response instead.
- **Verification**: `LINT`; `TYPES`; `TEST`. New tests pin: each taxonomy class → expected
  HTTP status and client-safe detail; unknown `MarimoHubError` subclass → 500 without detail
  leak; slug properties above.
- **Dependencies**: none.
- **Status**: complete (impl ×1, verify ×1 PASS — ruff 0, ty 0, pytest 94 passed)

## M2 — Session-manager seam & subprocess decomposition

**Intent**: put the `SessionManager` protocol in front of all callers and decompose the
609-line `process_manager.py` into the subprocess backend package, deleting the port-range
and capacity machinery.

- **Design**: *DT-7 Design → Target design — the seam (`backend/app/services/session_manager.py`)*,
  *→ Value-object construction rule (both backends)*, *→ Module decomposition
  (`process_manager.py` → cohesive concerns)*, *→ How callers change*, *→ Explicit contracts*,
  *→ Deletions (by symbol / file)*. Error base definitions per *DT-13 Design → Domain error
  hierarchies (signatures unchanged)*. Include `read_source` in the protocol for now (mapping
  `current_source → await read_source` per *DT-7 Design → Deletions*): its sole caller
  `_persist_edit_session` survives until M4, which deletes both together per *DT-11 Design →
  Deletions (by symbol / file)*.
- **Create**: `backend/app/services/session_manager.py` (protocol + value objects +
  `get_session_manager()` selector, subprocess-only for now);
  `backend/app/services/subprocess_backend/{__init__,manager,registry,runtime,workdir,readiness}.py`;
  `backend/tests/test_session_manager.py` (rewritten from `test_process_manager.py`).
- **Modify**: `backend/app/api/sessions.py`, `backend/app/api/deployments.py`,
  `backend/app/api/proxy.py`, `backend/app/services/marimo_proxy.py` — depend on the protocol,
  async accessors, `touch`→`mark_active`, `current_source`→`read_source`; `backend/app/schemas/session.py`
  per *DT-7 Design → How callers change*; `backend/app/core/config.py` — remove `MARIMO_PORT_RANGE`
  per *DT-12 Design → Deletions (settings keys + related code)* (only this key; the rest wait
  for their milestones — in particular the readiness code keeps reading the still-present
  `MARIMO_READY_TIMEOUT_SECONDS` until M9 renames it to `SESSION_READY_TIMEOUT_SECONDS`);
  `backend/app/main.py` lifespan shutdown call rename;
  `backend/app/services/deployment_lifecycle.py` — survives until M4 but imports
  `ProcessManager`/`SessionNotFoundError`/`get_process_manager` and calls sync `manager.get()`:
  rewire onto the seam (`session_manager` imports, `SessionManager` type,
  `await manager.get(...)` in `reap_once`) so M2's grep gate can pass.
- **Delete**: `backend/app/services/process_manager.py`; `_reserved_ports`, port allocator,
  `PortAllocationError`; `backend/tests/test_process_manager.py`.
- **Verification**: `LINT`; `TYPES`; `TEST` (including rewritten session-manager tests and
  `-m integration` subprocess spawn test). `grep -rn "process_manager\|MARIMO_PORT_RANGE" app tests`
  returns nothing.
- **Dependencies**: M1.
- **Status**: complete (impl ×1, verify ×1 PASS — ruff 0, ty 0, pytest 94 passed incl. integration;
  carry-forwards: drop caller-less `registry.snapshot()` in M4; sessions.py capacity 503 catch dies
  in M4 per DT-10)

## M3 — Gateway consolidation & single edit-save path

**Intent**: collapse the duplicated HTTP/WS proxy paths into one gateway with resolver seams,
and make the proxied `api/kernel/save` interception the only edit-persistence path.

- **Design**: *DT-9 Design → Target design — the gateway (`backend/app/services/marimo_proxy.py`)*,
  *→ Resolvers — the isolated entry-point difference*, *→ The routers after consolidation*,
  *→ Serving-access decision (DT-3 handoff, recorded)*, *→ Explicit contracts*, *→ Deletions
  (by file / symbol)*; *DT-11 Design → Target design — the one edit-save persistence path*,
  *→ Explicit contracts*, *→ Deletions (by symbol / file)*.
- **Create**: `GatewayError` hierarchy in the gateway module (rooted per DT-13);
  gateway tests in `backend/tests/test_gateway.py` pinning: resolve-or-wake ordering,
  `mark_active` call points (per HTTP request; WS connect + frame), error→status and →WS-close
  mapping, save-callback guard conditions (2xx/3xx + edit mode) and swallow-on-failure.
- **Modify**: `backend/app/services/marimo_proxy.py` (gateway + private transport helpers);
  `backend/app/api/proxy.py` (SessionResolver + `edit_save_callback` beside its injection
  site); `backend/app/api/deployments.py` (DeploymentResolver; two-line proxy bodies);
  `backend/app/main.py` if handler registration shifts per *DT-13 Design → `main.py` wiring*;
  `backend/tests/test_deployments.py` — rewrite/remove the serving-path and wake assertions
  that pin the deleted DB writes (`status==RUNNING` after wake, serving-path `last_active`
  mirror); the reaper-unit tests that drive `reap_once` directly with hand-built rows stay
  until M4 deletes the reaper.
- **Delete**: duplicated `deployment_ws`/`deployment_http` and `proxy_ws`/`proxy_http` bodies,
  `_wake_deployment`, `persist_marimo_save`'s old hardcoded-storage form, per the two Deletions
  headings above.
- **Accepted transient (M3→M4)**: with `_wake_deployment`'s DB writes gone, nothing writes
  `deployments.status=RUNNING` or serving-path `last_active` anymore, so the reaper (deleted in
  M4) no longer matches gateway-woken deployments and the row may read `sleeping` while a
  session serves. No API surface reads that status between M3 and M4 (the read-model endpoint
  arrives in M4); recorded as accepted, not worked around.
- **Verification**: `LINT`; `TYPES`; `TEST`. `grep -n "relay_websocket\|forward_http" app/api`
  shows no direct router calls (gateway-only).
- **Dependencies**: M2.
- **Status**: complete (impl ×1, verify ×1 PASS — ruff 0, ty 0, pytest 112 passed incl. 2 integration;
  carry-forward: relocate `get_notebook_storage` out of `api/notebooks.py` when M9 adds the
  `NOTEBOOK_STORAGE_BACKEND` selector)

## M4 — Lifecycle source-of-truth & legacy lifecycle deletions

**Intent**: make runtime phase live only behind the seam (read-through projection), and delete
the reaper, boot reset, explicit save endpoint, and persist-on-delete.

- **Design**: *DT-10 Design → Source-of-truth statement*, *→ Read-model projection rule
  (resolves DT-9 handoff #1)*, *→ Per-endpoint changes*, *→ Wake / idle-sleep / stop flows
  (against the DT-7 seam)*, *→ Explicit contracts*, *→ Handoff resolutions*, *→ Deletions
  (files / symbols)*. Note the `deployments.port` column is absent from M5's squashed baseline;
  `DeploymentOut` already has no port field. Everything else lands here.
- **Create**: `resolved_status(...)` projection helper and its unit tests; test for the new
  `GET /api/notebooks/{id}/deployment` read-model endpoint per *DT-10 Design → Per-endpoint
  changes*; tests pinning: failed wake never persists `running`; `stopped` short-circuits.
- **Authz carve-out (ordering)**: DT-10's per-endpoint table names DT-3 deps
  (`NotebookRead`/`NotebookWrite`, `load_notebook_for`) that do not exist until M5·T3. M4
  implements the lifecycle/flow changes against the *existing* user_id authz helpers
  (`_load_owned_notebook`, `_authorize_create`, `_can_view`); the swap onto DT-3 deps is
  M5·T5's re-keying. The capability-scoped session delete (drop `_authorize_session`) has no
  DT-3 dependency and lands here.
- **Modify**: `backend/app/api/deployments.py`, `backend/app/api/sessions.py` (endpoint set and
  flows per the per-endpoint table; capability-scoped session delete per *DT-10 Design →
  Handoff resolutions*); `backend/app/main.py` lifespan (remove reaper start/stop);
  `backend/app/core/config.py` — remove `IDLE_TIMEOUT_MINUTES` per *DT-12 Design → Deletions*;
  `backend/tests/test_sessions.py`, `backend/tests/test_deployments.py` fallout.
- **Delete**: `backend/app/services/deployment_lifecycle.py` (`IdleDeploymentReaper`,
  `mark_running_deployments_sleeping`); `POST /api/sessions/{id}/save`; `_persist_edit_session`;
  persist-on-delete in `delete_session`; `read_source` from the `SessionManager` protocol and
  the subprocess backend (now caller-less) per *DT-11 Design → Deletions (by symbol / file)*,
  including `workdir.read_current_source`.
- **Verification**: `LINT`; `TYPES`; `TEST`.
  `grep -rn "deployment_lifecycle\|IdleDeploymentReaper\|IDLE_TIMEOUT" app tests` returns nothing.
- **Dependencies**: M3.
- **Status**: complete (impl ×2, verify ×2 — attempt 1 FAIL on a comment-only DT reference, fixed;
  attempt 2 PASS — ruff 0, ty 0, pytest 118 passed incl. 2 integration)

## M5 — Domain schema flip: workspaces, identities, visibility

**Intent**: land the workspace/identity data model, the squashed schema baseline, the shared
access-policy module, the split-credential auth services, and the mechanical re-keying of every
router and test from `user_id` ownership to workspace ownership — one green checkpoint.

This milestone is the plan's one multi-task exception (see Conventions). Tasks run in order;
the suite is only green after T5. Each task still has a mechanical gate.

### M5·T1 — Models package (DT-1)
- **Design**: *DT-1 Design → Target ERD*, *→ Model-file layout — decision*, *→ Enums*,
  *→ Provider value scheme (`identities.provider`)*, *→ Workspace lifecycle and user deletion*,
  *→ ORM `Mapped` definitions (new / changed)*, *→ Explicit contracts*, *→ Deletions (by
  symbol / file)*.
- **Create/modify**: split `backend/app/models/__init__.py` into the per-aggregate package with
  re-export facade; new `Workspace`, `WorkspaceMember`, `Identity`, `LocalCredential`,
  `WorkspaceRole`; slim `User`; workspace `archived_at`/`purge_after` (no personal subtype);
  restrictive membership→user FK; one-local-identity partial unique index;
  `Notebook.workspace_id`/`created_by`; scalar `Notebook.deployment`; `NotebookVisibility`
  created as `PRIVATE/UNLISTED/PUBLIC`; no `Deployment.port`; `enum_values` public in `models/base.py`.
- **Verification**: `uv run python -c "import app.models"`; `LINT` scoped to `app/models`.
  (Suite red is expected until T5.)
- **Status**: complete (impl ×1, verify ×1 PASS — configure_mappers 0, ruff/ty scoped 0; suite red as designed)

### M5·T2 — Squashed baseline + conftest (DT-2)
- **Design**: *DT-2 Design → Baseline decision*, *→ Target `upgrade()` contract*,
  *→ Explicit contracts*, *→ Deletions*, *→ Downgrade strategy*.
- **Delete**: the two existing revision files `20260615_0001_initial_schema.py` and
  `20260625_0002_unique_deployment_notebook.py`.
- **Create**: `backend/app/db/migrations/versions/20260713_0001_initial_schema.py`
  (`down_revision = None`) containing the final schema directly, with no data backfill, personal
  workspace columns, `notebooks.user_id`, `users.password_hash`, `draft`, or deployment port.
  **Modify**: `backend/tests/conftest.py` TRUNCATE lists gain the four new tables.
- **Verification**: recreate the test database; `uv run alembic upgrade head` exits 0;
  `uv run alembic downgrade base && uv run alembic upgrade head` exits 0; schema inspection tests
  pin archive columns, restrictive membership FK, local-identity partial unique index, required
  notebook workspace FK, and unique deployment notebook FK.
- **Dependencies**: M5·T1.
- **Status**: complete (impl ×1, verify ×1 PASS — alembic round-trip 0, 8 inspection tests green,
  ruff/ty scoped 0; note: hand-named unique constraints will show phantom drift under any future
  `alembic revision --autogenerate`)

### M5·T3 — Access-policy module + deps (DT-3)
- **Design**: *DT-3 Design → Target design — module `backend/app/services/access.py`*,
  *→ Access matrix → read/write mapping (encoded by `can_access`)*, *→ Centralised
  hide-vs-forbid (the one 401/403/404 rule)*, *→ Explicit contracts*; *DT-5 Design → Policy
  reuse — `services/access.py`*.
- **Create**: `backend/app/services/access.py`; `require_notebook` dep factory and annotated
  deps in `backend/app/api/deps.py`; `backend/tests/test_access.py` pinning the full
  visibility×role×action matrix and denial selection as pure-function tests, plus DB-backed tests
  for membership lookup, active-workspace filtering, missing/hidden notebook equivalence, and final
  workspace denial behavior (missing/archived/non-member → 404; under-role member → 403).
- **Verification**: `uv run pytest tests/test_access.py` green; `LINT`.
- **Dependencies**: M5·T2.
- **Status**: complete (impl ×1, verify ×1 PASS — 41 access tests + 8 migration tests green,
  ruff clean, ty scoped 0; full 24-cell matrix pinned)

### M5·T4 — Auth services on split credentials (DT-4)
- **Design**: *DT-4 Design → Target design — module `backend/app/services/auth_service.py`*,
  *→ OIDC HTTP flow + verification seam (`api/auth.py`)*, *→ SAML decision*, *→ Explicit
  contracts*, *→ Impact on adjacent modules*, *→ Deletions (by symbol)*. Registration creates no
  workspace; `local` identity subject = `str(user.id)` exactly. Identity misses use trusted,
  verified, normalized-email linking only when the provider explicitly enables it, else JIT.
- **Modify**: `backend/app/services/auth_service.py` (`_provision`, `BasicAuthService`,
  `OIDCAuthService`); `backend/app/api/auth.py` (OIDC routes); `backend/pyproject.toml` +
  `uv lock` — add `authlib>=1.3.0` per *DT-12 Design → Dependency additions —
  `backend/pyproject.toml`* (the OIDC verification adapter builds on it); `core/security.py`
  and the JWT seam untouched — assert no diff.
- **Verification**: `uv run pytest tests/test_auth.py` green after its rewrite here (register
  → user + local identity + credentials, no workspace, one transaction; one-local-identity DB guard;
  authenticate against `local_credentials`; OIDC resolve/link/JIT with trusted verified and
  untrusted/unverified email cases mocked);
  `git diff --stat backend/app/core/security.py` is empty.
- **Dependencies**: M5·T2, M5·T3.
- **Status**: complete (impl ×1, verify ×1 PASS — 64 tests green across auth/access/migrations,
  security.py byte-identical; M9 carry-forwards: declare `joserfc>=1.6.0` in pyproject, pin an
  algorithms allow-list in `oidc_verifier.verify_callback`, replace `_lookup_oidc_provider`'s
  always-404 body with the `OIDC_PROVIDERS` settings lookup + wire `trusted_email_linking`)

### M5·T5 — Router re-keying + schema surface + test-suite green (DT-6 core)
- **Design**: *DT-6 Design → Per-endpoint target design*, *→ Shared target-workspace resolver —
  `notebooks.py`*, *→ Response shaping — `_notebook_out`* (scoped to the `user_id` removal:
  rewrite the owner join onto `workspace_id`/`created_by` and stub parent attribution; the
  readability-gated `parent_workspace_id`/`parent_workspace_slug` fields finish in M7),
  *→ Revised schemas — `backend/app/schemas/notebook.py`* (ownership fields
  only; attribution/fork fields finish in M7), *→ Deletions (by file / symbol)*; *DT-1 Design →
  Affected pydantic schemas*; *DT-10 Design → Schema changes* (`DeploymentOut` is already port-free;
  only the ORM column/writers disappear).
- **Modify**: `backend/app/api/{notebooks,sessions,deployments,data}.py` onto DT-3 deps
  (delete every `_is_owner`/`_can_view`/`_get_owned_notebook`/`_load_owned_notebook`/
  `_authorize_*` copy); `backend/app/schemas/{notebook,deployment}.py`; rewrite affected
  fixtures/assertions in `backend/tests/{test_notebooks,test_data,test_sessions,test_deployments}.py`.
  Notebook create/import/fork schemas require `workspace_id`; no route infers a default workspace.
  If the test rewrite exceeds this task's budget, split at the file boundary (notebooks+data /
  sessions+deployments) into T5a/T5b — record the split in this file.
- **Verification**: `LINT`; `TYPES`; `TEST` — **full suite green closes M5**.
  `grep -rn "user_id" backend/app/api backend/app/schemas` shows no notebook-ownership use;
  `grep -rn "draft" backend/app backend/tests` returns nothing.
- **Dependencies**: M5·T4.
- **Status**: complete (impl ×1 — thrice infra-interrupted, no fix passes needed; verify ×1 PASS —
  ruff 0, ty 0, pytest 179 passed + 2 integration, all greps empty; no T5a/T5b split needed.
  M5 milestone closed. M7 note: `_notebook_out` currently does NO parent lookup — the attribution
  join + readability gating is net-new work there, not a tweak)

## M6 — Workspace & membership management API

**Intent**: expose workspace lifecycle and collaboration endpoints, including hidden/restorable
archives and safe user/workspace deletion behavior.

- **Design**: *DT-5 Design → Endpoint surface*, *→ Pydantic schemas —
  `backend/app/schemas/workspace.py`*, *→ FastAPI dependency pair — `backend/app/api/deps.py`
  (additions, mirrors DT-3)*, *→ Router module — `backend/app/api/workspaces.py` (concrete
  signatures)*, *→ Wiring — `backend/app/main.py`*, *→ Explicit contracts*.
- **Create**: `backend/app/api/workspaces.py`; `backend/app/schemas/workspace.py`;
  `backend/app/services/workspace_service.py`; a scheduler-agnostic
  `backend/app/commands/purge_archived_workspaces.py` CLI entrypoint;
  `backend/tests/test_workspaces.py` pinning: explicit create + owner membership, last-owner
  protection, non-member 404 hide, owner-only member management, active "my workspaces" listing,
  non-empty archive, hidden archived resources, owner archive listing, restore, fixed purge deadline,
  due-only idempotent purge, sole-member hard-delete on user deletion, and shared last-owner block.
- **Modify**: `backend/app/api/deps.py` (`require_workspace` pair using DT-3's active filtering);
  `backend/app/services/access.py` only for the archived-workspace owner loader used by list/restore;
  `backend/app/core/config.py` + `.env.example` (add positive
  `WORKSPACE_ARCHIVE_RETENTION_DAYS`, default 30); `backend/app/main.py`.
- **Verification**: `LINT`; `TYPES`; `TEST`.
- **Dependencies**: M5.
- **Status**: complete (impl ×1, verify ×1 PASS — ruff 0, ty 0, pytest 197 passed + 2 integration,
  purge CLI executed clean; carry-forward to M7: add the delete_user combined-ordering test —
  sole-member workspace A survives when sole-owner-of-shared workspace B triggers the 409)

## M7 — Notebook API completion: forking, discovery, attribution

**Intent**: finish DT-6 — fork semantics, discovery predicate, parent attribution, publish
transitions, and the `data.py` read rule.

- **Design**: *DT-6 Design → Per-endpoint target design*, *→ Discovery query (replaces the
  inline `visibility_filter`)*, *→ Response shaping — `_notebook_out`*, *→ Revised schemas —
  `backend/app/schemas/notebook.py`* (remainder), *→ `data.py` access rules & the runtime-write
  decision (resolves DT-3 open question)* (GET rule only; POST moves in M8), *→ Explicit
  contracts*, *→ Data flow — fork*.
- **Modify**: `backend/app/api/notebooks.py` (fork endpoint semantics: private, `parent_id`,
  `fork_count` increment in one transaction; target-workspace resolver; discovery/search onto
  `visible_notebooks`); `backend/app/api/data.py` (GET = notebook READ); schemas
  (`NotebookFork`, attribution fields `parent_title`/`parent_workspace_id`/`parent_workspace_slug`
  gated on parent readability).
- **Create/extend tests**: fork lands private in chosen workspace and bumps `fork_count`;
  create/import/fork reject missing `workspace_id`; discovery excludes every archived-workspace
  notebook plus private non-member notebooks and includes active membership ones; attribution
  hidden when parent unreadable; publish/unpublish embedding behavior per *DT-6 Design →
  Explicit contracts*.
- **Verification**: `LINT`; `TYPES`; `TEST`.
- **Dependencies**: M5 (M6 for fork-into-shared-workspace test fixtures).
- **Status**: complete (impl ×1, verify ×1 PASS — ruff 0, ty 0, pytest 210 passed + 2 integration;
  M10 sweep note: replace deprecated `row.tuple()` with `._tuple()` repo-wide — notebooks.py
  `_parent_attribution` + deployments.py:82,111)

## M8 — Internal API & service-token auth

**Intent**: add the NetworkPolicy-scoped internal endpoints (source fetch, data write/read-back)
with per-session token binding, and remove the public data POST.

- **Design**: *DT-8 Design → Internal endpoint surface — `backend/app/api/internal.py`*,
  *→ Service-token auth contract (resolves the DT-3/DT-6 open item)*, *→ Explicit contracts*;
  *DT-6 Design → `data.py` access rules & the runtime-write decision* (public POST removal);
  *DT-11 Design → Session-start source flow (per backend)* (the source endpoint reads via the
  storage seam).
- **Create**: `backend/app/api/internal.py` (`GET /api/internal/notebooks/{id}/source`,
  `POST /api/internal/notebooks/{id}/data`, internal GET read-back); token issuance/binding per
  the auth contract; `backend/tests/test_internal.py` pinning: valid token for notebook A cannot
  read/write notebook B; expired/absent token → contract's error; source body matches storage.
- **Modify**: `backend/app/main.py` (router registration); `backend/app/api/data.py` (delete
  public POST); `backend/tests/test_data.py`; `backend/app/core/security.py` — **additive
  only**: `create_session_token`/`decode_session_token` beside the untouched user-token helpers
  per *DT-8 Design → Service-token auth contract* (M5·T4's no-diff assertion was task-scoped
  and is not violated by this later addition). `SESSION_TOKEN_TTL_SECONDS` does not exist until
  M9's DT-12 layout — use a module-level default constant here; M9 replaces it with the setting.
- **Verification**: `LINT`; `TYPES`; `TEST`. `grep -n "post" backend/app/api/data.py` shows no
  public write route.
- **Dependencies**: M5, M3 (gateway), M7 (data GET rule in place).
- **Status**: complete (impl ×1, verify ×1 PASS — ruff 0, ty 0, pytest 221 passed + 2 integration;
  token-confusion pinned in all four directions. Deliberately NOT done: an explicit typ-absence
  check in the user `decode_token` — the wall is already airtight since session tokens carry no
  `sub`, and DT-4's user-JWT seam guarantee stays intact)

## M9 — Kube session backend & configuration surface

**Intent**: implement `KubeSessionManager` against the CRD contract and rationalise settings
with the backend selector.

- **Design**: *DT-7 Design → KubeSessionManager — CR/Secret/Service mapping*, *→ Value-object
  construction rule (both backends)*, *→ Explicit contracts*, *→ Dependency to add*;
  *DT-10 Design → Handoff resolutions* (QuotaExceeded sentinel → `SessionCapacityError`);
  *DT-12 Design → Target design — `backend/app/core/config.py`*, *→ Target design —
  `.env.example` (rewritten)*, *→ `mark_active` coalescing window — handoff decision (DT-7/DT-8)*,
  *→ Explicit contracts*, *→ Deletions (settings keys + related code)*, *→ Dependency additions —
  `backend/pyproject.toml`*.
- **Create**: `backend/app/services/kube_session_manager.py`;
  `backend/tests/test_kube_session_manager.py` with a faked `kubernetes_asyncio` client pinning:
  CR name rule (deployment id vs uuid4), label list/get, wake = token refresh + annotation +
  poll, `SessionTarget` from `status.serviceName` + Secret, QuotaExceeded → 429-class error,
  ready-timeout → 503-class error.
- **Modify**: `backend/app/core/config.py` (full DT-12 layout: `SESSION_BACKEND`, kube-only
  settings with conditional validation, `NOTEBOOK_STORAGE_BACKEND`, OIDC settings including
  per-provider `trusted_email_linking`, retain `WORKSPACE_ARCHIVE_RETENTION_DAYS`, new
  `PUBLIC_API_URL`, the `MARIMO_READY_TIMEOUT_SECONDS`→`SESSION_READY_TIMEOUT_SECONDS` rename,
  `MAX_CONCURRENT_SESSIONS` disposition per DT-12); `get_session_manager()` selector;
  `backend/pyproject.toml` + `uv lock` (`kubernetes-asyncio>=32.0.0`); `.env.example` rewritten.
- **Verification**: `LINT`; `TYPES`; `TEST` (kube tests run against fakes; no cluster needed).
  `SESSION_BACKEND=kube` without `SESSION_NAMESPACE` fails settings validation (pinned test);
  `grep -rn "MAX_CONCURRENT_SESSIONS\|MARIMO_PORT_RANGE\|IDLE_TIMEOUT" backend/app` matches only
  what DT-12 keeps.
- **Dependencies**: M2, M4, M8.
- **Status**: pending

## M10 — CRD & namespace manifests, final sweep

**Intent**: check in the cluster-side contracts the operator and platform team consume, and
close out global acceptance.

- **Design**: *DT-8 Design → Finalised CRD schema*, *→ Per-session manifests (restricted-v2 SCC
  compliant)*, *→ Namespace guardrails (`marimohub-sessions`)*, *→ Reconcile loop (create /
  ready / fail / idle / wake / delete)* (checked in as the operator's spec, not implemented).
- **Create**: `deploy/crd/marimosession.yaml`; `deploy/namespace/{namespace,networkpolicy,
  resourcequota,limitrange}.yaml`; `deploy/README.md` pointing the operator implementer at the
  DT-8 reconcile pseudocode section.
- **Modify**: none beyond stragglers found by the sweep below.
- **Verification**: from `backend/`: `uv run --with pyyaml python -c "import pathlib,yaml;
  [list(yaml.safe_load_all(p.read_text())) for p in pathlib.Path('../deploy').rglob('*.yaml')]"`
  exits 0; global sweep greps return nothing:
  `grep -rn "TODO\|XXX\|FIXME" backend/app`, `grep -rniE "\bDT-[0-9]" backend/app backend/tests`
  (no design-task references in code), plus every per-milestone deletion grep re-run;
  `LINT`; `TYPES`; `TEST`; `uv run alembic upgrade head` from scratch DB.
- **Dependencies**: M9.
- **Status**: pending

---

## Risk register

1. **Squashed baseline coordination** (M5·T2). Symptom: a developer or shared environment is still
   stamped with one of the deleted revision ids and cannot upgrade. Watch: announce the reset and
   recreate every development/CI database at the M5 boundary. Fallback: restore the old chain and use
   a destructive `0003` only if an environment unexpectedly requires revision continuity.
2. **Test-rewrite volume underestimate** (M5·T5). Symptom: the task blows its token budget
   before the suite is green. Fallback: split at the documented T5a/T5b file boundary; do not
   merge M5 partially.
3. **Gateway consolidation regressions in WS relay** (M3). Symptom: edit sessions disconnect,
   saves stop persisting, or deployment apps hang after idle. Watch: the M3 gateway tests plus
   a manual smoke (edit → autosave → reload; deploy → idle → wake). Fallback: keep the milestone
   unmerged; do not reintroduce the duplicated bodies.
4. **Kube backend correctness unprovable without a cluster** (M9). Symptom: fakes pass but CR
   shapes drift from the checked-in CRD. Watch: validate test fixtures against
   `deploy/crd/marimosession.yaml` required fields in the test itself. Fallback decision point:
   if drift is found after M10, the CRD YAML is the contract — fix the manager, never the CRD,
   unless the operator team agrees.
5. **`ty` type-checker instability** (all milestones). Symptom: `uv run ty check` fails on
   pre-existing or upstream issues unrelated to the change. Fallback: record the baseline count
   at M1 and hold milestones to "no new diagnostics" instead of zero.
6. **Archive filtering or purge omissions** (M6+). Symptom: archived notebooks remain discoverable,
   runtimes stay live, or due workspaces never purge. Watch: policy/query tests cover every read path;
   archive tests assert runtime stop and due-only idempotent purge; deployment runs the purge CLI on a
   schedule. Fallback: disable archive deletion until filtering and scheduling are corrected.

## Definition of done

- All milestones `complete`; every task's Status updated in this file.
- From `backend/`: `uv run ruff check .`, `uv run ty check` (per the M1 baseline rule),
  `uv run pytest` (full suite incl. `-m integration`), and `uv run alembic upgrade head` on a
  fresh database all pass.
- Every deletion listed in DESIGN.md's per-task "Deletions" headings is gone: the per-milestone
  greps in this plan all return empty.
- New contracts are pinned by named tests: access matrix (test_access), provisioning/linking atomicity
  and subject scheme (test_auth), workspace archive/restore/purge and deletion guards (test_workspaces), fork/discovery/attribution
  (test_notebooks), internal token binding (test_internal), gateway resolve-or-wake and error
  mapping (test_gateway), kube CR mapping (test_kube_session_manager), error taxonomy
  (test_errors), squashed-baseline up/down smoke and schema constraints.
- `deploy/` manifests parse and match the DT-8 schema; `.env.example` matches DT-12's layout.
- No TODOs, commented-out code, compatibility shims, or references to this plan in the code.

---

## Verification

**COMPLETE (2026-07-15, cycle 3).** All-DT conflict assessment and milestone-order verification
performed against the post-revision DESIGN.md (DT-1…DT-13 all `complete`, including the DT-1/DT-2
revisions: no personal-workspace subtype, explicit workspace targets, archive/restore/purge, safe
user deletion, trusted verified-email linking, scalar deployments, squashed baseline).

Checks performed and passed:

- **Coverage** — every DT maps to exactly one owning milestone (or a stated split: DT-6 core in
  M5·T5 / remainder in M7; DT-7 subprocess half in M2 / kube half in M9; DT-8 backend half in M8 /
  manifests in M10); no DT is orphaned and no milestone cites a pending DT.
- **Milestone ordering vs. design dependencies** — the M1→M10 chain respects the DESIGN.md
  dependency sketch. The one forward reference found (M4 consuming DT-10's table, which names
  DT-3 deps that arrive in M5·T3) is resolved by M4's recorded authz carve-out.
- **Deletion timing** — every deletion lands at or after the point its last caller is removed.
  Fixed in this cycle: `deployment_lifecycle.py` survives M2's `process_manager.py` deletion but
  imported it — M2 now rewires it onto the seam. `MAX_CONCURRENT_SESSIONS` staying unused
  M2→M9 is a deliberate staging choice, gated by M9's grep.
- **Current-state accuracy** — DESIGN.md's file map, importer list, and reaper behavior verified
  against the working tree (importers of `process_manager`: `main`, `schemas/session`,
  `api/{sessions,deployments,proxy}`, `services/{deployment_lifecycle,marimo_proxy}` — all now in
  M2's Modify set; `reap_once` matches on DB `status==RUNNING`, confirming the recorded M3→M4
  transient).
- **Spec-doc consistency** — `marimohub-schema-redesign.md` and `marimosession-crd-spec.md` agree
  with DT-1/DT-2 and DT-7/DT-8 respectively; DESIGN.md's deliberate extensions (finalizer, wake
  annotation, TCP readiness probe, backend-authored Secret) are recorded as decisions, not drift.
- **Test-gate integrity** — each milestone's green requirement is achievable: M3 now carries the
  `test_deployments.py` serving-path fallout; M1 pins the blanket-`Exception`-handler decision the
  DT-13 open question left to the test rewrite.

Resolutions recorded in this cycle: M1 blanket-handler decision; M2 `deployment_lifecycle.py`
rewiring; M3 test fallout + accepted M3→M4 status transient; M4 authz carve-out; M8 additive
`core/security.py` change + TTL-constant-until-M9 note; DT-6 fork flowchart's stale
optional/personal workspace-target references corrected in DESIGN.md.

Known accepted gaps (not blockers): 429 capacity fidelity depends on the out-of-repo operator
emitting the `QuotaExceeded:` sentinel (503 fallback documented in DT-10/M9); kube correctness is
proven against fakes only until a cluster is available (risk register #4).

The plan is approved for implementation starting at M1.
