# MarimoHub Backend — Clean-Sweep Design

**Primary design goal:** redesign the backend around clear domain boundaries — a workspace/collaborator model, notebook visibility and forking, and Kubernetes-native session management via the `MarimoSession` CRD — with a clean seam for swapping local JWT auth for OIDC/SAML.

Scope: `backend/` only. This is a clean-sweep effort; design tasks favour removing obsolete paths, compatibility shims, legacy behaviour, and duplicate abstractions over preserving backwards compatibility.

This document has two parts:
- **Current State** — a factual map of the backend as built today, with diagrams and an explicit gap analysis against the two authoritative spec documents.
- **Design Tasks** — scoped, subagent-sized design areas (DT-*), each with acceptance criteria, likely files, and `Status: pending`.

Authoritative target-state inputs (constrain every design task):
- `marimohub-schema-redesign.md` — ownership moves users → workspaces; `identities` + `local_credentials` split out of `users`; visibility `draft` → `private`; drop `deployments.port`.
- `marimosession-crd-spec.md` — replace the subprocess `ProcessManager` with a pod-per-session `MarimoSession` CRD + controller; drop the port allocator and in-process idle reaper; add an internal source endpoint.

---

## Current State

### Stack

| Layer | Technology | Location |
|---|---|---|
| API | Python 3.12, FastAPI, SQLAlchemy 2 async, Alembic | `backend/app` |
| Runtime | marimo subprocesses (`marimo edit`/`run`, `--sandbox`) spawned in-process by the API | `backend/app/services/process_manager.py` |
| DB | PostgreSQL + pgvector (tsvector FTS + 384-dim embeddings) | `backend/app/db` |
| Embeddings | `sentence-transformers` `all-MiniLM-L6-v2` (local, lazy-loaded) | `backend/app/services/embedding_service.py` |
| Auth | local username/password (bcrypt) + stateless HS256 JWT | `backend/app/core/security.py`, `services/auth_service.py` |

### Backend package map

```
backend/app/
├── main.py                     app assembly, CORS, router registration, lifespan (starts reaper + process manager)
├── core/
│   ├── config.py               pydantic-settings Settings + get_settings() (lru_cache)
│   └── security.py             bcrypt hash/verify, JWT encode/decode
├── db/
│   ├── database.py             Base, get_engine, get_sessionmaker, get_db (request dep)
│   └── migrations/             2 alembic revisions (0001 initial, 0002 unique deployment/notebook)
├── models/
│   └── __init__.py             ALL ORM models + enums in one file (User, Notebook, Deployment, NotebookData)
├── schemas/                    pydantic request/response models, split per domain
│   └── auth.py, user.py, notebook.py, deployment.py, session.py, data.py
├── api/                        6 routers; hold auth + orchestration logic, not just I/O
│   ├── deps.py                 get_current_user / get_current_user_optional
│   └── auth.py, notebooks.py, sessions.py, deployments.py, proxy.py, data.py
└── services/
    ├── auth_service.py         AuthService (ABC) + BasicAuthService (bcrypt, users table)
    ├── notebook_storage.py     NotebookStorageService (ABC) + PostgresNotebookStorage (source on the row)
    ├── process_manager.py      609-line subprocess lifecycle manager + module singleton
    ├── deployment_lifecycle.py IdleDeploymentReaper + mark_running_deployments_sleeping (boot reset)
    ├── marimo_proxy.py         HTTP + WebSocket forwarding helpers (KEPT in target state)
    ├── embedding_service.py    EmbeddingService (class) + module singleton + embed() fn
    └── gitlab_import.py        fetch + AST-validate a marimo notebook from a URL
```

### Domain model (as built today)

```mermaid
erDiagram
    users ||--o{ notebooks : owns
    notebooks ||--o{ notebooks : "parent_id (fork)"
    notebooks ||--o| deployments : "1:0..1 (unique notebook_id)"
    notebooks ||--o{ notebook_data : has

    users {
        uuid id PK
        string username UK
        string email UK
        string password_hash
        timestamptz created_at
    }
    notebooks {
        uuid id PK
        uuid user_id FK
        uuid parent_id FK "nullable, self"
        text title
        text description
        text_array tags
        text source
        enum visibility "draft|unlisted|public"
        int fork_count
        tsvector search_vector "generated, GIN"
        vector embedding "384, pgvector"
    }
    deployments {
        uuid id PK
        uuid notebook_id FK "UNIQUE"
        string slug UK
        enum status "running|sleeping|stopped"
        int port "subprocess port"
        timestamptz last_active
    }
    notebook_data {
        uuid id PK
        uuid notebook_id FK
        jsonb payload
        string source
        timestamptz created_at
    }
```

Facts: ownership is a direct `notebooks.user_id` link (no workspaces/collaborators/roles); credentials live inline on `users.password_hash` (no `identities`, no provider concept); visibility enum is `draft|unlisted|public`; `deployments.port` mirrors the subprocess TCP port; notebook `source` is stored on the row behind the `NotebookStorageService` ABC.

### Auth flow (today)

```mermaid
sequenceDiagram
    participant C as Client
    participant A as api/auth.py
    participant S as BasicAuthService
    participant DB as Postgres
    C->>A: POST /api/auth/login {username,password}
    A->>S: authenticate(username,password)
    S->>DB: SELECT user WHERE username=?
    S->>S: bcrypt.checkpw(password, user.password_hash)
    S-->>A: User | None
    A->>A: create_access_token(user.id)  %% HS256, sub=user_id
    A-->>C: {access_token}
    Note over C,A: later requests carry Bearer JWT → deps.get_current_user
```

The `AuthService` ABC / `BasicAuthService` split and the provider-neutral JWT (`sub` = user id, `create_access_token`/`decode_token`) are already the intended OIDC extension seam; the JWT stays regardless of upstream IdP.

### Notebook lifecycle (today)

```mermaid
flowchart TD
    create[POST /api/notebooks] --> draft[visibility=draft, user_id=me]
    draft --> publish[POST /:id/publish → unlisted/public + embed]
    draft --> edit[POST /api/sessions mode=edit]
    publish --> fork[POST /:id/fork → new draft, parent_id set, fork_count++]
    publish --> deploy[POST /api/notebooks/:id/deploy]
    fork --> draft2[new draft owned by forker]
    import[POST /api/notebooks/import] --> draft
```

Authorization is **duplicated, ownership-by-`user_id`** logic with no shared policy module:
- `notebooks.py`: `_is_owner`, `_can_view`, `_get_visible_notebook`, `_get_owned_notebook`.
- `sessions.py`: its own copies of `_is_owner`, `_can_view`, plus `_authorize_create`/`_authorize_session`.
- `deployments.py`: `_load_owned_notebook` + inline `notebook.user_id != current_user.id` checks.
- `data.py`: no authorization at all — create/get-latest are unauthenticated given a notebook id.
- Discovery filter (`list_notebooks`): `visibility == public OR user_id == me`.

### Session / deployment proxying (today)

```mermaid
flowchart LR
    subgraph api [single API process]
      PM[ProcessManager<br/>_sessions dict + _reserved_ports]
      RE[IdleDeploymentReaper<br/>60s loop]
    end
    subgraph runtimes
      edit[edit/run session<br/>uuid4, ephemeral]
      dep[deploy session<br/>id = deployment.id]
    end
    Client -->|POST /api/sessions| S[api/sessions.py] --> PM
    Client -->|/api/deployments/:slug| D[api/deployments.py] --> PM
    S --> PX[api/proxy.py] --> MP[marimo_proxy]
    D --> MP
    PM -->|spawn subprocess| edit
    PM -->|spawn_deployment| dep
    MP -->|http://127.0.0.1:port+base_url| edit
    RE -.->|idle > timeout → stop + status=sleeping| dep
    PM -. last_active/status/port .-> DBP[(Postgres)]
```

Mechanics the CRD spec removes wholesale: `ProcessManager._sessions` in-memory dict is the sole source of truth for live sessions (not crash-safe, not multi-replica); port allocation (`_parse_port_range`, `_reserved_ports`, `MARIMO_PORT_RANGE`) and `MAX_CONCURRENT_SESSIONS` gating; `spawn_deployment`'s per-deployment lock dance for idempotency; `IdleDeploymentReaper` + `mark_running_deployments_sleeping` boot reset; tempdir source write (`_prepare_workdir`) and `secrets.token_urlsafe`; `SessionTarget` built from `127.0.0.1:port`. `marimo_proxy.py` (HTTP/WS forwarding, `touch`-on-traffic) is explicitly **kept** — only target resolution and the activity edge change.

The proxy path is also duplicated between routers: `proxy.proxy_http`/`proxy_ws` (sessions) and `deployments.deployment_http`/`deployment_ws` share the same resolve → touch → forward/relay shape, and edit-save persistence exists twice (`sessions._persist_edit_session` reads the workdir file; `proxy.persist_marimo_save` intercepts the proxied `api/kernel/save` body).

### Config surface (today)

`core/config.py`: `DATABASE_URL`, `SECRET_KEY`, `ACCESS_TOKEN_EXPIRE_MINUTES`, `IDLE_TIMEOUT_MINUTES`, `MAX_CONCURRENT_SESSIONS`, `MARIMO_PORT_RANGE`, `MARIMO_READY_TIMEOUT_SECONDS`. The last four are subprocess-model settings the CRD spec deletes or relocates to the controller. `pyproject.toml` has **no Kubernetes client** dependency — the target state needs one (e.g. `kubernetes-asyncio`).

### Tests (today)

`tests/` (~3,000 lines) exercises the current model directly: `test_notebooks.py`/`test_sessions.py` assume `user_id` ownership and `draft` visibility; `test_deployments.py`/`test_process_manager.py` assume subprocesses and ports; `conftest.py` truncates `notebook_data, deployments, notebooks, users` and runs Alembic to head. Every schema/session design task carries test-rewrite fallout: new tables to truncate, new fixtures, and removal of subprocess/port tests.

---

## Gap Analysis (current backend ↔ spec docs)

Schema redesign (`marimohub-schema-redesign.md`):

| # | Current | Target | Impact |
|---|---|---|---|
| G1 | `notebooks.user_id` direct ownership | `notebooks.workspace_id` + `created_by` | Ownership, discovery, all authz change |
| G2 | No `workspaces` / `workspace_members` / roles | New tables + `workspace_role` enum (`owner|editor|viewer`) | New domain + management API |
| G3 | `users.password_hash` inline | `local_credentials` (1:0..1) split out | Auth service + migration backfill |
| G4 | No `identities` / provider concept | `identities(provider, subject)` | Enables OIDC/SAML seam |
| G5 | visibility value `draft` | visibility value `private` (enum rename) | Migration + code/schema/test churn |
| G6 | `deployments.port` column | dropped | Migration + deployment code |
| G7 | No workspace lifecycle/archive state | explicit workspace creation; archive/restore, then timed purge | Workspace service + schema |
| G8 | Authz by `user_id`, duplicated in 4 routers | visibility × membership-role matrix | New shared authz module |
| G9 | Discovery filter `user_id == me` | `workspace_id IN (my memberships)` | Notebook list query |
| G10 | Fork → forker's draft (implicit personal) | Fork → chosen workspace, `visibility=private`, `parent_id` set | Notebook fork endpoint |

Session/CRD (`marimosession-crd-spec.md`):

| # | Current | Target | Impact |
|---|---|---|---|
| G11 | `ProcessManager` subprocesses, in-memory dict | `KubeSessionManager` CRUDing `MarimoSession` CRs | Delete `process_manager.py`; new manager |
| G12 | Port allocator + `MAX_CONCURRENT_SESSIONS` | Service-per-session on :8080; ResourceQuota | Delete port paths; config cleanup |
| G13 | `IdleDeploymentReaper` + boot reset | Controller reconcile owns idleness/restart/orphans | Delete `deployment_lifecycle.py` |
| G14 | Tempdir source write + `secrets` token | init-container fetch + per-session Secret | New `GET /api/internal/notebooks/{id}/source` |
| G15 | `SessionTarget` from `127.0.0.1:port` | from `status.serviceName` + token Secret | Manager + proxy target resolution |
| G16 | Activity via in-memory `manager.touch()` | PATCH `status.lastActivity` / annotation | Proxy activity edge |
| G17 | Deployment status is authoritative DB row | read-model cache of controller CR status (read-through first) | Deployment API read path |
| G18 | No CRD / controller | `MarimoSession` CRD + reconcile controller | New component (outside `backend/app`) |
| G19 | No `SESSION_BACKEND` seam / k8s client dep | injectable backend + k8s client | Config + dependency |

---

## Design Tasks

Each task produces a **design** (target abstractions, module boundaries, seams, migration sketch), not an implementation, and is scoped to one subagent within a 30k–50k token budget. Dependencies are noted where a task consumes another's output; tasks may still be designed in parallel against the spec docs.

Suggested order: DT-1/DT-2 (data model + migration) and DT-7 (session seam) are foundational; DT-3, DT-4, DT-5, DT-6 build on the data model; DT-8/DT-9/DT-10 build on the session seam; DT-11/DT-12/DT-13 are cross-cutting.

---

### DT-1 — Domain data-model restructure (workspaces, identities, visibility)
**Status:** complete

Design the target ORM/domain model per `marimohub-schema-redesign.md`: add explicitly-created `workspaces` (including DNS-label `slug` plus archive/purge timestamps), `workspace_members` (PK `(workspace_id, user_id)`, `workspace_role` enum `owner|editor|viewer`, `user_id` index), `identities` (`UNIQUE(provider, subject)`, `user_id` index, one-local-identity guard, `email`/`last_login_at`), and `local_credentials` (1:0..1 with users). Slim `users` to `id/username/email/created_at`. Move notebook ownership from `user_id` to `workspace_id` (+ `created_by`, ON DELETE SET NULL); rename visibility `draft`→`private`; drop `deployments.port`. Keep `tags`, `search_vector`, `embedding`, `fork_count`, `parent_id`. Decide model-file layout, define role and workspace lifecycle semantics, and enumerate the pydantic schema surface changes this implies.

- **Acceptance criteria:** target ERD + ORM `Mapped` definitions and relationships for all new/changed tables; `workspace_role` enum and role semantics stated; `provider` value scheme (`local`, `google`, `oidc:<slug>`, `saml:<slug>`) documented; no formal personal-workspace subtype or implicit default; archive/restore/purge and user-deletion semantics stated; one local identity per user DB-guarded; visibility rename applied to `NotebookVisibility`; confirmation `fork_count`/FTS/embedding are retained; a decision on splitting the model file; list of affected pydantic schemas. No migration code (that is DT-2).
- **Likely files:** `backend/app/models/__init__.py`, `backend/app/schemas/*`, `marimohub-schema-redesign.md`.
- **Relates to:** DT-2, DT-3, DT-4, DT-6.

#### DT-1 Design

##### Target ERD

```mermaid
erDiagram
    users ||--o{ identities : has
    users ||--o| local_credentials : "1:0..1"
    users ||--o{ workspace_members : "is member"
    users ||--o{ notebooks : "created_by (attribution, SET NULL)"
    workspaces ||--o{ workspace_members : "has members"
    workspaces ||--o{ notebooks : owns
    notebooks ||--o{ notebooks : "parent_id (fork)"
    notebooks ||--o| deployments : "1:0..1 (unique notebook_id)"
    notebooks ||--o{ notebook_data : has

    users {
        uuid id PK
        string username UK
        string email UK
        timestamptz created_at
    }
    identities {
        uuid id PK
        uuid user_id FK "index, CASCADE"
        string provider "local|google|oidc:slug|saml:slug"
        string subject
        string email "nullable, last-login"
        timestamptz created_at
        timestamptz last_login_at "nullable"
    }
    local_credentials {
        uuid user_id PK "FK users, CASCADE"
        string password_hash "bcrypt"
        timestamptz updated_at
    }
    workspaces {
        uuid id PK
        string slug UK "DNS-label, varchar(63)"
        text name
        timestamptz archived_at "nullable"
        timestamptz purge_after "nullable"
        timestamptz created_at
    }
    workspace_members {
        uuid workspace_id PK "FK workspaces, CASCADE"
        uuid user_id PK "FK users, RESTRICT, index"
        enum role "owner|editor|viewer"
        timestamptz created_at
    }
    notebooks {
        uuid id PK
        uuid workspace_id FK "index, NOT NULL, CASCADE"
        uuid created_by FK "nullable, SET NULL"
        uuid parent_id FK "nullable, self, SET NULL"
        text title
        text description
        text_array tags
        text source
        enum visibility "private|unlisted|public"
        int fork_count "maintained counter"
        tsvector search_vector "generated, GIN"
        vector embedding "384, pgvector"
        timestamptz created_at
        timestamptz updated_at
    }
    deployments {
        uuid id PK
        uuid notebook_id FK "UNIQUE, CASCADE"
        string slug UK
        enum status "running|sleeping|stopped"
        timestamptz last_active "nullable"
        timestamptz created_at
    }
    notebook_data {
        uuid id PK
        uuid notebook_id FK "CASCADE"
        jsonb payload
        string source "nullable"
        timestamptz created_at
    }
```

##### Model-file layout — decision

**Split `models/__init__.py` into a per-aggregate package** and keep `__init__.py` as a
pure re-export facade so every existing `from app.models import X` site is unchanged. The file
grows from 4 to 9 models spanning three new bounded contexts (identity, workspace, catalog); a
single module stops being cohesive. Cross-module relationships resolve through SQLAlchemy's
string-based lazy refs, and the facade imports every module so the registry is fully populated
before mappers configure.

```
backend/app/models/
├── __init__.py     # re-export facade only; defines __all__ (import surface unchanged)
├── base.py         # enum_values() helper (shared by Enum(values_callable=...))
├── user.py         # User, Identity, LocalCredential
├── workspace.py    # WorkspaceRole, Workspace, WorkspaceMember
├── notebook.py     # NotebookVisibility, Notebook, NotebookData
└── deployment.py   # DeploymentStatus, Deployment
```

Each enum lives with its aggregate. `Base` stays in `app.db.database`; only the `enum_values`
helper (renamed public from `_enum_values`) moves to `models/base.py`.

##### Enums

```python
# models/workspace.py
class WorkspaceRole(enum.StrEnum):
    OWNER = "owner"     # manage members, rename/archive/restore workspace, + editor operations
    EDITOR = "editor"   # create/edit/deploy/fork notebooks in the workspace
    VIEWER = "viewer"   # read private notebooks in the workspace; no writes

# models/notebook.py  — value 'draft' renamed to 'private'
class NotebookVisibility(enum.StrEnum):
    PRIVATE = "private"
    UNLISTED = "unlisted"
    PUBLIC = "public"

# models/deployment.py — unchanged
class DeploymentStatus(enum.StrEnum):
    RUNNING = "running"
    SLEEPING = "sleeping"
    STOPPED = "stopped"
```

**Role → access** is applied by DT-3, not here; the enum only carries authority ordering
`owner > editor > viewer`. Access matrix (visibility × role), from the schema doc, that DT-3
will encode:

| | non-member | viewer | editor / owner |
|---|---|---|---|
| private | — (404) | read | read / write |
| unlisted | read via direct link | read | read / write |
| public | read / search / fork | read | read / write |

##### Provider value scheme (`identities.provider`)

`local` (one per locally registered user; `subject = users.id::text`), `google`, `oidc:<issuer-slug>`,
`saml:<idp-slug>`. `varchar(64)`. Uniqueness is `UNIQUE(provider, subject)` so the same external
subject under two providers is two identities; index on `user_id` serves "identities for user".
DT-4 owns how `OIDCAuthService` mints these; DT-1 only fixes the column shape and scheme.

##### Workspace lifecycle and user deletion

There is **no formal personal-workspace subtype**. Registration creates no workspace. A user creates
a workspace explicitly and becomes its first `owner`; a workspace with only one member may be
described informally as personal, but it remains the same kind of resource and may later gain members.
Notebook create/import/fork always names a target `workspace_id`; there is no implicit default.

Explicit workspace deletion archives rather than hard-deletes: set `archived_at = now()` and persist
`purge_after = now() + WORKSPACE_ARCHIVE_RETENTION_DAYS` (default 30). Archived workspaces are hidden
from normal APIs, cannot start sessions/deployments or accept writes, and are visible only through an
owner archive-management surface. Restore clears both timestamps. The globally unique slug remains
reserved until physical purge, avoiding restore conflicts. A scheduler calls an idempotent purge
service which hard-deletes rows whose `purge_after <= now()`; DB cascades then remove their notebooks,
deployments, data, and memberships.

User deletion is intentionally different: in one locked transaction, hard-delete each workspace where
the user is the sole member, reject deletion if the user is the last owner of any multi-member
workspace, remove their remaining memberships, then delete the user. `workspace_members.user_id` uses
`ON DELETE RESTRICT` so direct user deletion cannot bypass this orchestration.

##### ORM `Mapped` definitions (new / changed)

```python
# models/base.py
def enum_values(enum_class: type[enum.StrEnum]) -> list[str]:
    """Values for SQLAlchemy Enum(values_callable=...)."""
    return [str(member) for member in enum_class]

# models/user.py
class User(Base):
    __tablename__ = "users"
    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid4)
    username: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)
    email: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False)
    # password_hash REMOVED → local_credentials
    identities: Mapped[list["Identity"]] = relationship(
        back_populates="user", cascade="all, delete-orphan")
    local_credential: Mapped["LocalCredential | None"] = relationship(
        back_populates="user", cascade="all, delete-orphan")
    memberships: Mapped[list["WorkspaceMember"]] = relationship(back_populates="user")
    # NOTE: `notebooks` back_populates removed — ownership is via workspace, not user.

class Identity(Base):
    __tablename__ = "identities"
    __table_args__ = (
        UniqueConstraint("provider", "subject", name="uq_identities_provider_subject"),
        Index("uq_identities_one_local_per_user", "user_id", unique=True,
              postgresql_where=text("provider = 'local'")),
    )
    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid4)
    user_id: Mapped[UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    provider: Mapped[str] = mapped_column(String(64), nullable=False)
    subject: Mapped[str] = mapped_column(String(255), nullable=False)
    email: Mapped[str | None] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False)
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    user: Mapped["User"] = relationship(back_populates="identities")

class LocalCredential(Base):
    __tablename__ = "local_credentials"
    user_id: Mapped[UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), primary_key=True)   # PK == FK, 1:0..1
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)
    user: Mapped["User"] = relationship(back_populates="local_credential")

# models/workspace.py
class Workspace(Base):
    __tablename__ = "workspaces"
    __table_args__ = (
        CheckConstraint(
            "(archived_at IS NULL) = (purge_after IS NULL)",
            name="ck_workspaces_archive_pair",
        ),
    )
    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid4)
    slug: Mapped[str] = mapped_column(String(63), unique=True, nullable=False)   # DNS-label
    name: Mapped[str] = mapped_column(Text, nullable=False)
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    purge_after: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False)
    members: Mapped[list["WorkspaceMember"]] = relationship(
        back_populates="workspace", cascade="all, delete-orphan")
    notebooks: Mapped[list["Notebook"]] = relationship(back_populates="workspace")

class WorkspaceMember(Base):
    __tablename__ = "workspace_members"
    workspace_id: Mapped[UUID] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), primary_key=True)
    user_id: Mapped[UUID] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), primary_key=True, index=True)
    role: Mapped[WorkspaceRole] = mapped_column(
        Enum(WorkspaceRole, name="workspace_role", values_callable=enum_values), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False)
    workspace: Mapped["Workspace"] = relationship(back_populates="members")
    user: Mapped["User"] = relationship(back_populates="memberships")

# models/notebook.py — changed columns only (rest unchanged from today)
class Notebook(Base):
    __tablename__ = "notebooks"
    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid4)
    workspace_id: Mapped[UUID] = mapped_column(                       # replaces user_id
        ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False, index=True)
    created_by: Mapped[UUID | None] = mapped_column(                 # attribution, survives deletion
        ForeignKey("users.id", ondelete="SET NULL"))
    parent_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("notebooks.id", ondelete="SET NULL"))
    # title, description, tags, source, fork_count, search_vector, embedding,
    # created_at, updated_at — UNCHANGED (retained: tags, fork_count, FTS, embedding).
    visibility: Mapped[NotebookVisibility] = mapped_column(
        Enum(NotebookVisibility, name="notebook_visibility", values_callable=enum_values),
        nullable=False, default=NotebookVisibility.PRIVATE)          # default draft→private
    workspace: Mapped["Workspace"] = relationship(back_populates="notebooks")
    creator: Mapped["User | None"] = relationship()                  # via created_by, no back_populates
    parent: Mapped["Notebook | None"] = relationship(
        remote_side="Notebook.id", back_populates="forks")
    forks: Mapped[list["Notebook"]] = relationship(back_populates="parent")
    deployment: Mapped["Deployment | None"] = relationship(
        back_populates="notebook", uselist=False)
    data: Mapped[list["NotebookData"]] = relationship(back_populates="notebook")

# models/deployment.py — drop `port`; everything else unchanged.
#   (remove `port: Mapped[int | None]`; Integer import drops if unused)
```

`NotebookData` moves verbatim into `models/notebook.py`. `enum_name` values persist (`notebook_visibility`, `deployment_status`, new `workspace_role`).

##### Explicit contracts

- **User** — an account with no login material. The user-deletion service first applies the locked
  workspace ownership flow above; deleting the user then CASCADEs identities/local credentials and
  SET-NULLs `notebooks.created_by`. Username/email both UNIQUE; email is stored in canonical lowercase
  form for trusted verified-email identity linking. Username is a display handle, not a credential.
- **Identity** — immutable `(provider, subject)`; `email`/`last_login_at` are mutated on each
  login by DT-4. A partial unique index permits at most one `local` identity per user. A new trusted
  provider identity may attach to an existing user only from a normalized, verified email claim.
- **LocalCredential** — present iff the user can log in with a password; absent for pure-SSO users.
  PK is the FK (`user_id`), enforcing 0..1. `password_hash` format unchanged (bcrypt).
- **Workspace** — explicitly-created ownership boundary with no personal subtype or implicit default.
  It always has at least one owner; the last owner may not be removed. `slug` must match
  `^[a-z0-9]([-a-z0-9]*[a-z0-9])?$`
  (RFC-1123 DNS label, ≤63) so it is reusable inside k8s names later (validated in
  schema/service, not the ORM). Archive timestamps implement hidden/restorable retention before purge.
- **WorkspaceMember** — composite PK forbids duplicate membership. `role` orders authority; the
  last `owner` of a shared workspace may not be demoted/removed (app-enforced, DT-5).
- **Notebook** — owned by exactly one workspace (`workspace_id` NOT NULL, CASCADE). `created_by`
  is attribution only and may be NULL after user deletion. `parent_id` SET NULL on parent
  deletion; `fork_count` is a maintained denormalized counter on the parent (accepted). Default
  visibility `private`.
- **Deployment** — unchanged 1:0..1 with a notebook; DB row is the API-facing cache of controller
  status (`status`, `slug`, `last_active`). `port` removed.

##### Deletions (by symbol / file)

- `models/__init__.py`: `User.password_hash` column; `User.notebooks` relationship;
  `Notebook.user_id` column and `Notebook.user` relationship; `Deployment.port` column;
  `NotebookVisibility.DRAFT` member. The monolithic module itself is replaced by the package
  above (its content redistributed, not kept as a shim).
- `_enum_values` renamed to public `enum_values` in `models/base.py` (no private copy retained).

##### Affected pydantic schemas

| schema file | change | owning task |
|---|---|---|
| `schemas/user.py` `UserOut` | **no change** — already `id/username/email/created_at` | — |
| `schemas/notebook.py` `NotebookOut` | drop `user_id`; add `workspace_id: UUID`, `created_by: UUID \| None`; rename parent-attribution `parent_owner_id/parent_owner_username` → `parent_workspace_id/parent_workspace_slug` (final naming DT-6) | DT-6 |
| `schemas/notebook.py` `NotebookCreate` / `NotebookImport` | add required `workspace_id: UUID`; fork target is required too | DT-6 |
| `schemas/notebook.py` `NotebookPublish` | no field change; values now `private/unlisted/public` (enum-driven) | DT-6 |
| `schemas/deployment.py` `DeploymentOut` | **no change** — already `slug/status/url`, never carried `port` | — |
| `schemas/session.py` | no DT-1 change (DT-7/DT-10) | — |
| `schemas/workspace.py` (new) | `WorkspaceCreate`, `WorkspaceOut`, `WorkspaceMemberOut`, archive/restore views, role enum surface | DT-5 |
| `schemas/auth.py` | unchanged shape; register flow rewires under DT-4 | DT-4 |

Only `NotebookOut`'s `user_id→workspace_id/created_by` swap is forced by DT-1's model change; the
rest are enumerated for DT-4/DT-5/DT-6 to execute.

##### Rejected alternatives

- **Keep one `models/__init__.py`** — rejected: 9 models across 3 contexts is no longer cohesive;
  the facade+package keeps the import surface identical while restoring cohesion.
- **Formal personal workspace plus implicit notebook target** — rejected: a one-member workspace is
  not a distinct domain object, and implicit target selection becomes ambiguous once a user joins a
  second workspace. All workspaces use one model and notebook writes require `workspace_id`.
- **Fold `local_credentials` into `identities`** — rejected: password material would ride along on
  every identity resolve query; the split is the point.

##### Open questions

- **Fork attribution fields** — final `NotebookOut` parent fields (`parent_workspace_slug` vs
  `parent_created_by_username`, given `created_by` may be NULL). Deferred to DT-6; flagged so the
  ERD/`created_by` semantics don't get re-litigated.
- **`local` identity `subject`** — resolved: DT-4 mints `subject = str(user.id)`; no username/email
  subject scheme and no migration backfill exist.

---

### DT-2 — Squashed Alembic baseline
**Status:** complete

Replace the two development-era revisions with one baseline that creates DT-1's final schema directly.
No deployed environment or retained data requires an upgrade path, so do not create obsolete
user-owned notebooks, inline credentials, `draft` visibility, or deployment ports merely to migrate
away from them. Existing development databases are recreated.

- **Acceptance criteria:** one `down_revision = None` baseline contains the complete final schema;
  DT-1's workspace/archive, identity, credential, notebook, and deployment constraints are represented
  directly; no personal-workspace columns or backfill SQL exists; `conftest.py` truncates the new tables;
  fresh upgrade and structural downgrade/upgrade pass.
- **Likely files:** `backend/app/db/migrations/versions/`, `backend/tests/conftest.py`,
  `marimohub-schema-redesign.md`.
- **Depends on:** DT-1.

#### DT-2 Design

##### Baseline decision

- Delete `20260615_0001_initial_schema.py` and `20260625_0002_unique_deployment_notebook.py`.
- Create `backend/app/db/migrations/versions/20260713_0001_initial_schema.py`.
- Set `revision = "20260713_0001"` and `down_revision = None`.
- Developers and CI recreate their database before applying the new baseline. Alembic does not attempt
  to upgrade databases stamped with the removed revision identifiers.
- The baseline still runs inside Alembic's transaction and creates extensions, enums, tables,
  constraints, generated/search columns, and indexes in dependency order.

##### Target `upgrade()` contract

The baseline combines the existing extensions and retained schema with these final-state rules:

1. `users` contains `id`, canonical-lowercase unique `email`, unique `username`, and `created_at`; it
   has no password column.
2. `identities` has `UNIQUE(provider, subject)`, an index on `user_id`, and the PostgreSQL partial
   unique index `uq_identities_one_local_per_user ON identities(user_id) WHERE provider = 'local'`.
3. `local_credentials.user_id` is both PK and `ON DELETE CASCADE` FK to users.
4. `workspaces` contains globally unique DNS-label `slug`, `name`, nullable `archived_at`, nullable
   `purge_after`, and `created_at`. A check requires both archive timestamps to be NULL or both set.
   It has no `is_personal` or `personal_owner_id` columns. Index
   `purge_after` for the due-purge query; the global slug constraint reserves archived slugs.
5. `workspace_members` has composite PK `(workspace_id, user_id)`, workspace `ON DELETE CASCADE`, user
   `ON DELETE RESTRICT`, role enum `owner|editor|viewer`, and an index on `user_id`.
6. `notebooks.workspace_id` is NOT NULL and `ON DELETE CASCADE`; `created_by` is nullable and
   `ON DELETE SET NULL`. Visibility is created directly as `private|unlisted|public`; tags,
   `fork_count`, FTS, and embeddings remain.
7. `deployments.notebook_id` remains unique, encoding zero-or-one current deployment per notebook.
   There is no `port` column.
8. `notebook_data` retains its notebook cascade and existing payload/source shape.

There is no backfill, enum rename, slug generation from usernames, or data-preservation SQL. Workspace
slugs are generated only by the explicit workspace-creation service. Registration creates a user,
identity, and optional local credential but no workspace.

##### Explicit contracts

- **Fresh database only.** Applying the baseline to an empty database is the supported path. Existing
  development databases are dropped and recreated; preserving their data is explicitly out of scope.
- **Archive state.** `archived_at IS NULL` and `purge_after IS NULL` means active. Archive sets both;
  restore clears both. The service validates the paired invariant and computes `purge_after` from the
  configured retention at archive time. A due-purge query uses `purge_after <= now()`.
- **User deletion.** The restrictive membership FK prevents deleting a user before the service has
  locked memberships, hard-deleted sole-member workspaces, checked last-owner safety, and removed
  remaining memberships.
- **Identity integrity.** `(provider, subject)` identifies one external identity globally; the partial
  index permits at most one local identity per user. DT-4 mints local subject as `str(user.id)` and may
  link a new trusted provider identity only from a normalized verified-email claim.
- **Deployment cardinality.** Unique `deployments.notebook_id` matches DT-1's scalar ORM relationship.
- **conftest.py.** Both cleanup paths explicitly truncate `notebook_data, deployments, notebooks,
  workspace_members, workspaces, identities, local_credentials, users RESTART IDENTITY CASCADE`.

##### Downgrade strategy

`downgrade()` structurally removes the baseline objects in reverse dependency order, including custom
enums and extensions that the baseline owns. It does not reconstruct either removed historical schema
or discarded development data. Recovery is database recreation followed by `upgrade head`, not a data
rollback.

##### Deletions

- Migration files `20260615_0001_initial_schema.py` and
  `20260625_0002_unique_deployment_notebook.py`.
- Every migration/backfill reference to `is_personal`, `personal_owner_id`, username-derived workspace
  slugs, `notebooks.user_id`, `users.password_hash`, visibility `draft`, and `deployments.port`.
- The prior best-effort downgrade and its nullable reconstruction columns.

##### Rejected alternatives

- **Destructive `0003` on the old chain.** Rejected because fresh installs would create obsolete schema
  solely to delete it, while no environment needs the old revision identifiers.
- **Preserve data in generated ordinary workspaces.** Rejected because development data is disposable
  and generated workspaces would differ from the explicit-creation behavior new users receive.
- **Multiple expand/backfill/contract revisions.** Rejected because there is no live-data or rolling
  deployment compatibility requirement.

---

### DT-3 — Shared authorization & access-control policy module
**Status:** complete

Design a single access-control abstraction that replaces the duplicated `_is_owner`/`_can_view`/`_get_owned_notebook`/`_load_owned_notebook`/`_authorize_*` logic spread across `notebooks.py`, `sessions.py`, `deployments.py`, and the missing checks in `data.py`. It must express the spec's access matrix (private/unlisted/public × non-member/viewer/editor-owner) as read vs. write decisions given a user's role in a notebook's workspace, provide the membership-role lookup (`SELECT role FROM workspace_members WHERE workspace_id=? AND user_id=?`), and centralise the 401-vs-403-vs-404 (hide-existence) decision. Define the module surface (functions/protocol + FastAPI dependencies) the routers call.

- **Acceptance criteria:** a named policy abstraction (inputs: actor, resource, action; outputs: allow / typed denial) with defined signatures; a table mapping each current router check to the new abstraction; full access-matrix → read/write mapping; centralised hide-vs-forbid rules; membership query defined; no per-router copies remain.
- **Likely files:** new authz module under `backend/app/services/` or `backend/app/core/`; `backend/app/api/{notebooks,sessions,deployments,data}.py`, `backend/app/api/deps.py`, `backend/app/models/__init__.py`.
- **Depends on:** DT-1. **Relates to:** DT-13 (denial → HTTP mapping).

#### DT-3 Design

One access policy for the whole backend, factored into three layers: a **pure decision** (the access matrix, no I/O), a **service layer** (membership lookup + resource authorization that raises *typed denials*), and thin **FastAPI dependencies** for the common path-param routes. Routers never build `HTTPException`s for access decisions — the shared DT-13 boundary renders the typed denials to HTTP in one place.

##### Target design — module `backend/app/services/access.py`

Named `access.py` (access control) to sit beside `auth_service.py` (authentication) without overloading "auth". It depends on models, `core.errors`, and the async session only; no FastAPI imports (those live in `deps.py`).

```python
# ── inputs ──────────────────────────────────────────────────────────────────
class Action(enum.StrEnum):
    READ = "read"
    WRITE = "write"

# ── typed denials (outputs) — the shared boundary renders these, routers never do
class AccessError(DomainError):
    """Base denied decision; carries a client-safe detail string."""

class ResourceHidden(AccessError):         # existence must not be revealed
    status = 404
class AuthenticationRequired(AccessError): # anonymous, authenticating may grant access
    status = 401
class PermissionDenied(AccessError):        # identified actor, insufficient role
    status = 403

# ── authority ordering (owner > editor > viewer) ────────────────────────────
def role_at_least(role: WorkspaceRole | None, minimum: WorkspaceRole) -> bool: ...

# ── PURE decision: the entire access matrix, no DB, fully unit-testable ──────
def can_access(
    visibility: NotebookVisibility, role: WorkspaceRole | None, action: Action
) -> bool:
    if action is Action.READ:
        if visibility in (NotebookVisibility.PUBLIC, NotebookVisibility.UNLISTED):
            return True                              # anyone, incl. anonymous
        return role is not None                      # private → any member (viewer+)
    return role_at_least(role, WorkspaceRole.EDITOR)  # write → editor|owner

# ── membership-role lookup (the spec's SELECT role FROM workspace_members …) ─
async def get_role(
    db: AsyncSession, workspace_id: UUID, user_id: UUID | None
) -> WorkspaceRole | None: ...   # None for anonymous or non-member

# ── centralised hide-vs-forbid (the single 401/403/404 decision) ────────────
def _deny(actor: User | None, can_read: bool, resource: str) -> AccessError:
    if not can_read:               return ResourceHidden(f"{resource} not found")
    if actor is None:              return AuthenticationRequired("Authentication required")
    return PermissionDenied(f"{resource} editor role required")

# ── resource authorization (caller already holds the Notebook) ──────────────
async def authorize_notebook(
    db: AsyncSession, notebook: Notebook, actor: User | None, action: Action
) -> WorkspaceRole | None:
    """Return actor's role if `action` is allowed on `notebook`; else raise AccessError."""
    active = await db.scalar(select(Workspace.id).where(
        Workspace.id == notebook.workspace_id, Workspace.archived_at.is_(None)))
    if active is None:
        raise ResourceHidden("Notebook not found")
    role = await get_role(db, notebook.workspace_id, actor.id if actor else None)
    if can_access(notebook.visibility, role, action):
        return role
    raise _deny(actor, can_access(notebook.visibility, role, Action.READ), "Notebook")

# ── load-then-authorize (missing id and hidden id both raise ResourceHidden) ─
async def load_notebook_for(
    db: AsyncSession, notebook_id: UUID, actor: User | None, action: Action
) -> tuple[Notebook, WorkspaceRole | None]:
    notebook = await db.scalar(select(Notebook).join(Workspace).where(
        Notebook.id == notebook_id, Workspace.archived_at.is_(None)))
    if notebook is None:
        raise ResourceHidden("Notebook not found")
    return notebook, await authorize_notebook(db, notebook, actor, action)

# ── workspace-scoped authority (create/import/fork-target; reused by DT-5) ───
async def authorize_workspace(
    db: AsyncSession, workspace_id: UUID, actor: User, minimum: WorkspaceRole
) -> WorkspaceRole:
    active = await db.scalar(select(Workspace.id).where(
        Workspace.id == workspace_id, Workspace.archived_at.is_(None)))
    if active is None:
        raise ResourceHidden("Workspace not found")
    role = await get_role(db, workspace_id, actor.id)
    if role is None:
        raise ResourceHidden("Workspace not found")
    if not role_at_least(role, minimum):
        raise PermissionDenied("Insufficient workspace permissions")
    return role

# ── load-then-authorize for workspace endpoints that need the object ─────────
async def load_workspace_for(
    db: AsyncSession, workspace_id: UUID, actor: User, minimum: WorkspaceRole
) -> tuple[Workspace, WorkspaceRole]:
    workspace = await db.scalar(select(Workspace).where(
        Workspace.id == workspace_id, Workspace.archived_at.is_(None)))
    role = await get_role(db, workspace_id, actor.id) if workspace is not None else None
    if role is None:
        raise ResourceHidden("Workspace not found")
    if not role_at_least(role, minimum):
        raise PermissionDenied("Insufficient workspace permissions")
    return workspace, role

# ── discovery predicate: active workspace AND (public OR one of my memberships) ─
def visible_notebooks(user_id: UUID | None) -> ColumnElement[bool]:
    public = Notebook.visibility == NotebookVisibility.PUBLIC
    active = Notebook.workspace.has(Workspace.archived_at.is_(None))
    if user_id is None:
        return and_(active, public)
    mine = select(WorkspaceMember.workspace_id).where(WorkspaceMember.user_id == user_id)
    return and_(active, or_(public, Notebook.workspace_id.in_(mine)))
```

FastAPI dependencies live in `backend/app/api/deps.py` (beside `get_current_user`), keeping FastAPI plumbing out of the service:

```python
@dataclass(frozen=True, slots=True)
class NotebookContext:
    notebook: Notebook
    actor: User | None
    role: WorkspaceRole | None          # actor's role in the notebook's workspace, or None

def require_notebook(action: Action) -> Callable[..., Awaitable[NotebookContext]]:
    async def dep(
        notebook_id: UUID,
        db: Annotated[AsyncSession, Depends(get_db)],
        actor: Annotated[User | None, Depends(get_current_user_optional)],
    ) -> NotebookContext:
        notebook, role = await load_notebook_for(db, notebook_id, actor, action)
        return NotebookContext(notebook, actor, role)
    return dep

NotebookRead  = Annotated[NotebookContext, Depends(require_notebook(Action.READ))]
NotebookWrite = Annotated[NotebookContext, Depends(require_notebook(Action.WRITE))]
```

`NotebookRead`/`NotebookWrite` cover every `/{notebook_id}`-path route (get, update, delete, publish, deploy, and the fork *source*). Routes whose notebook id arrives in the body/slug (`sessions.create_session`, deployment management) call `load_notebook_for` / `authorize_notebook` directly.

##### Access matrix → read/write mapping (encoded by `can_access`)

| visibility | non-member (role=None) | viewer | editor / owner |
|---|---|---|---|
| private  | read ✗ (→404), write ✗ | read ✓, write ✗ (→403) | read ✓, write ✓ |
| unlisted | read ✓, write ✗          | read ✓, write ✗ (→403) | read ✓, write ✓ |
| public   | read ✓, write ✗          | read ✓, write ✗ (→403) | read ✓, write ✓ |

READ is allowed for public/unlisted to everyone (incl. anonymous) and for private only to members; WRITE is `role ∈ {owner, editor}` irrespective of visibility. Discovery/listing (`visible_notebooks`) surfaces **public only** to others plus **all notebooks in my workspaces** (my private/unlisted included); unlisted stays "direct-link only", excluded from others' listings.

##### Centralised hide-vs-forbid (the one 401/403/404 rule)

`_deny` is the single place the distinction is made, computed **visibility-first** off the READ decision:

1. Actor cannot READ the resource → `ResourceHidden` (**404**) — a missing id and a private-notebook-you're-not-a-member-of are indistinguishable. Applies to anonymous *and* authenticated non-members of a private notebook.
2. Actor can READ but the (write) action is denied and actor is anonymous → `AuthenticationRequired` (**401**) — logging in may grant it.
3. Actor can READ but is an identified viewer/non-member → `PermissionDenied` (**403**).

This replaces the current inconsistent ordering (sessions.py 401-before-visibility vs notebooks.py visibility-before-403) with one rule; a private notebook now uniformly 404s for non-members on every verb.

Workspace authorization follows the same existence-hiding principle without a visibility matrix:
missing, archived, and non-member workspaces all raise `ResourceHidden` (404), while a member below
the required role raises `PermissionDenied` (403). Workspace callers require authentication before
entering the policy, so this path has no anonymous 401 branch.

##### Router-check → abstraction mapping

| File | Current check | Replaced by |
|---|---|---|
| `notebooks.py` | `_is_owner` | `get_role` + `role_at_least` (no standalone owner concept) |
| `notebooks.py` | `_can_view` | `can_access(…, Action.READ)` |
| `notebooks.py` | `_get_visible_notebook` | `NotebookRead` dep / `load_notebook_for(…, READ)` |
| `notebooks.py` | `_get_owned_notebook` | `NotebookWrite` dep / `load_notebook_for(…, WRITE)` |
| `notebooks.py` | `_not_found` | `ResourceHidden` |
| `notebooks.py` | inline `visibility_filter` in `list_notebooks` | `visible_notebooks(user_id)` |
| `notebooks.py` | create/import/fork owner-set (`user_id=me`) | `authorize_workspace(target_ws, EDITOR)` (applied by DT-6) |
| `sessions.py` | `_is_owner`, `_can_view` | folded into policy |
| `sessions.py` | `_authorize_create` | `load_notebook_for(…, READ if run else WRITE)` |
| `sessions.py` | `_not_found`, `_auth_error` | `ResourceHidden`, `AuthenticationRequired` |
| `sessions.py` | `_authorize_session` | **NOT DT-3** — ephemeral-session creator check; stays (see Open questions / DT-10) |
| `deployments.py` | `_load_owned_notebook` | `load_notebook_for(…, WRITE)` |
| `deployments.py` | inline `notebook.user_id != current_user.id` (delete) | `authorize_notebook(…, WRITE)` |
| `deployments.py` | `deployment_http`/`deployment_ws` optional-user | unchanged — public serving is intentionally unauthenticated (DT-9/DT-10) |
| `data.py` | `_ensure_notebook_exists` (no authz) | `load_notebook_for(…, READ)` for GET, `(…, WRITE)` for POST (wired by DT-6) |

##### Explicit contracts

- **`can_access`** — pure, total, side-effect-free; the sole authority on notebook read/write decisions. A new visibility updates this function and discovery; a new role updates `role_at_least` and its matrix tests.
- **`get_role`** — returns `None` for anonymous and for authenticated non-members alike; callers must not distinguish them except through `_deny`. One indexed query (PK-prefix on `workspace_members`).
- **`authorize_notebook` / `load_notebook_for`** — either return (allowed, yielding the actor's role) or raise exactly one `AccessError`; never return a denied state. `load_notebook_for` collapses "absent" and "hidden" to the same `ResourceHidden` so existence never leaks.
- **`authorize_workspace` / `load_workspace_for`** — require an authenticated actor; absent, archived, and non-member workspaces are indistinguishable 404s, while an existing member below `minimum` receives 403. Archive list/restore uses a separate DT-5 owner-only loader that selects archived rows.
- **Typed denials** — the only thing the policy raises. Routers/deps let them propagate; the DT-13 `DomainError` handler renders `ResourceHidden→404`, `AuthenticationRequired→401` (with `WWW-Authenticate: Bearer`), and `PermissionDenied→403`, using `AccessError.detail` as the response detail.
- **`role` return value** — provided so response shaping (e.g. whether `NotebookOut` includes `source` for viewer vs editor) is a DT-6 decision, not re-derived.
- **Lifecycle** — all functions are request-scoped and bound to the caller's `AsyncSession`; they read, never commit.

##### Deletions (by file / symbol)

- `notebooks.py`: `_is_owner`, `_can_view`, `_get_visible_notebook`, `_get_owned_notebook`, `_not_found`, and the inline `visibility_filter` block in `list_notebooks`.
- `sessions.py`: `_is_owner`, `_can_view`, `_authorize_create`, `_not_found`, `_auth_error`. **Kept:** `_authorize_session`, `_persist_edit_session` (not access-matrix; session lifecycle, DT-10).
- `deployments.py`: `_load_owned_notebook`; the inline `notebook.user_id != current_user.id` branch in `delete_deployment`.
- `data.py`: `_ensure_notebook_exists`, `_notebook_not_found`.
- `deps.py`: **additions only** — `NotebookContext`, `require_notebook`, `NotebookRead`, `NotebookWrite`.

No per-router copy of an ownership/visibility check remains; every one routes through `access.py`.

##### Data flow

```mermaid
flowchart TD
    subgraph routers [api routers]
        NB[notebooks.py]
        SE[sessions.py]
        DE[deployments.py]
        DA[data.py]
    end
    DEP[deps.py<br/>require_notebook → NotebookRead/Write]
    subgraph policy [services/access.py]
        LNF[load_notebook_for] --> AN[authorize_notebook]
        AN --> GR[get_role<br/>SELECT role FROM workspace_members]
        AN --> CA[can_access<br/>pure matrix]
        AN --> DENY[_deny<br/>404 / 401 / 403]
        AW[authorize_workspace]
        VN[visible_notebooks<br/>discovery predicate]
    end
    NB -->|path-param routes| DEP
    DE -->|path-param routes| DEP
    DEP --> LNF
    SE -->|payload notebook_id| LNF
    DA -->|GET=READ / POST=WRITE| LNF
    NB -->|create/import/fork target| AW
    NB -->|list_notebooks WHERE| VN
    AW --> GR
    DENY -.raises typed AccessError.-> HANDLER[DT-13 exception handler<br/>→ JSON response]
```

##### Rejected alternatives

- **Return a `Decision` result object everywhere instead of raising typed denials** — rejected: raising composes with DT-13's single handler and keeps router bodies branch-free; a result object pushes the same 401/403/404 fan-out back into every caller.
- **General RBAC/permission table (Casbin-style)** — rejected: the matrix is a closed 2-action × 3-role set; a 6-line pure `can_access` is clearer, faster, and unit-testable without fixtures.
- **Fold the session-creator check (`_authorize_session`) into the notebook policy** — rejected: it authorizes an *ephemeral session* by `creator_id`, not a notebook by workspace role — a different resource that belongs to DT-10.
- **Return 403 for authenticated workspace non-members** — rejected: a workspace has no public visibility axis, so membership is its read gate and 403 would permit workspace-id probing. Missing, archived, and non-member workspaces therefore collapse to 404.

##### Open questions

- **`data.py` POST authorization** — mapped to notebook WRITE here, but if a *public deployment's runtime* must persist data anonymously, WRITE is wrong for POST and it needs an internal/service-token path (cf. DT-8's internal source endpoint). Flagged for **DT-6/DT-8** to confirm who calls `POST /{id}/data` and under what identity.
- **Session save/stop scope** — `_authorize_session` currently restricts to the creator; should a workspace editor manage a co-member's live session on the same notebook? A **DT-10** decision; `get_role` is available if role-based is chosen.
- **Deployment management on a private notebook** — `delete`/`deploy` now 404 for non-members even though the deployment slug is publicly routable. Accepted here (hide the notebook); **DT-9/DT-10** to confirm the serving path stays public while management hides.

### DT-4 — Auth & identity service seam (JWT → OIDC/SAML)
**Status:** complete

Design the `AuthService` evolution for the split-credential/multi-identity model: `BasicAuthService` reads/writes `local_credentials` + `identities` instead of `users.password_hash`, and `register()` atomically creates only the user + `local` identity + `local_credentials`. Add `OIDCAuthService(AuthService)` that resolves `(provider, subject)`, then safely links a new identity by normalized verified email for configured trusted providers, else JIT-provisions a user + identity. Workspace creation is always explicit and outside auth. Keep `create_access_token`/`decode_token` unchanged as the session-token boundary; document the SAML-via-OIDC-broker (Dex/Keycloak) decision and how `get_current_user` maps a token to a user under the new model.

- **Acceptance criteria:** updated `AuthService` ABC surface; `BasicAuthService` register/authenticate against split tables; the provisioning transaction (user+identity+optional credentials, no workspace) specified; `OIDCAuthService` resolve/link/JIT flow and trusted verified-email requirement sketched; explicit statement the JWT seam is untouched; impact on `core/security.py`, `api/auth.py`, `api/deps.py` enumerated.
- **Likely files:** `backend/app/services/auth_service.py`, `backend/app/api/auth.py`, `backend/app/api/deps.py`, `backend/app/core/security.py` (read-only), `marimohub-schema-redesign.md`.
- **Depends on:** DT-1.

#### DT-4 Design

Authentication splits into two axes that the current single-table design conflated: **credential
verification** (provider-specific: password check vs. verified OIDC id-token) and **account
provisioning** (identical across providers: one atomic `user + identity`, plus
`local_credentials` only for local signup). DT-4 factors the second axis
into a shared kernel on `AuthService` and lets each provider own only the first. The JWT session
boundary is untouched: once any provider yields a `User`, `create_access_token(user.id)` mints the
same HS256 token as today, and `get_current_user` maps it back with the same `db.get(User, id)` —
the upstream IdP is invisible past login.

##### Target design — module `backend/app/services/auth_service.py`

```python
# ── normalized, already-verified external assertion ─────────────────────────
@dataclass(frozen=True, slots=True)
class OIDCClaims:
    provider: str               # 'google' | 'oidc:<slug>' | 'saml:<slug>' (broker-fronted)
    subject: str                # verified `sub` / NameID — the identities.subject
    email: str
    email_verified: bool
    preferred_username: str | None = None

class DuplicateUserError(Exception):
    """Raised when a username or email is already registered."""

# ── shared identity kernel ──────────────────────────────────────────────────
class AuthService(ABC):
    """Shared provisioning + resolution kernel every provider builds on.

    Not instantiated directly: each provider subclass adds its own credential
    verification entry point. Those entry points differ too much in shape to
    share one signature (local verifies a password and never auto-provisions;
    OIDC resolves-or-JIT-provisions a verified assertion), so the polymorphism
    lives in the *provisioning* kernel, not in a forced `authenticate` method.
    """

    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    async def _resolve_identity(self, provider: str, subject: str) -> User | None:
        """Return the user owning identity (provider, subject), or None."""
        return await self.db.scalar(
            select(User)
            .join(Identity, Identity.user_id == User.id)
            .where(Identity.provider == provider, Identity.subject == subject)
        )

    async def _touch_identity(self, provider: str, subject: str, email: str | None) -> None:
        """Bump last_login_at (and provider-asserted email) on an existing identity."""
        await self.db.execute(
            update(Identity)
            .where(Identity.provider == provider, Identity.subject == subject)
            .values(last_login_at=func.now(), email=email)
        )
        await self.db.commit()

    async def _provision(
        self, *, username: str, email: str, provider: str, subject: str,
        password_hash: str | None,
    ) -> User:
        """Atomically create user + identity (+ local_credentials), but no workspace.
        Ids are minted up-front so a local
        identity's `subject` equals `str(user.id)`. Raises DuplicateUserError."""
        if await self._name_or_email_taken(username, email):
            raise DuplicateUserError
        user_id = uuid4()
        identity_subject = str(user_id) if provider == "local" else subject
        self.db.add_all([
            User(id=user_id, username=username, email=normalize_email(email)),
            Identity(user_id=user_id, provider=provider, subject=identity_subject,
                     email=email, last_login_at=func.now()),
        ])
        if password_hash is not None:
            self.db.add(LocalCredential(user_id=user_id, password_hash=password_hash))
        try:
            await self.db.commit()
        except IntegrityError as exc:            # race backstop (username/email/subject/local guard)
            await self.db.rollback()
            raise DuplicateUserError from exc
        return await self.db.get(User, user_id)  # never None: just committed

    async def _name_or_email_taken(self, username: str, email: str) -> bool: ...

# ── local username/password ─────────────────────────────────────────────────
class BasicAuthService(AuthService):
    async def register(self, username: str, email: str, password: str) -> User:
        return await self._provision(
            username=username, email=email, provider="local",
            subject="",                       # ignored for local; _provision uses str(user_id)
            password_hash=hash_password(password),
        )

    async def authenticate(self, username: str, password: str) -> User | None:
        row = (await self.db.execute(
            select(User, LocalCredential.password_hash)
            .join(LocalCredential, LocalCredential.user_id == User.id)
            .where(User.username == username)
        )).first()
        if row is None:                        # unknown user OR SSO-only (no local_credentials)
            return None
        user, password_hash = row
        if not verify_password(password, password_hash):
            return None
        await self._touch_identity("local", str(user.id), user.email)
        return user

# ── external OIDC (one instance per configured provider) ────────────────────
class OIDCAuthService(AuthService):
    def __init__(self, db: AsyncSession, provider: str) -> None:
        super().__init__(db)
        self.provider = provider              # 'google' | 'oidc:<slug>' | 'saml:<slug>'

    async def complete_login(self, claims: OIDCClaims) -> User:
        user = await self._resolve_identity(claims.provider, claims.subject)
        if user is not None:
            await self._touch_identity(claims.provider, claims.subject, claims.email)
            return user
        linked = await self._link_by_verified_email(claims)
        if linked is not None:
            return linked
        return await self._provision(          # JIT: first trusted identity and no matching user
            username=await self._available_username(claims),
            email=claims.email, provider=claims.provider,
            subject=claims.subject, password_hash=None,
        )

    async def _available_username(self, claims: OIDCClaims) -> str:
        """Seed from preferred_username / email local-part, suffixed until free."""
```

**Local `subject` = `str(user.id)` (resolves the inherited DT-1 constraint).** The live path mints
this scheme directly; DT-2 has no data backfill. Because `_provision` generates `user_id = uuid4()`
*before* insert, `register`
passes `subject=str(user_id)`; the placeholder `""` in the sketch above is set from the freshly
minted id inside `_provision` (the cleanest form: `_provision` computes `subject` for the `local`
provider itself, so `register` never passes a subject). Either way the live subject is byte-for-byte
`str(user.id)`, so `authenticate`'s `_touch_identity("local", str(user.id))` and any future
re-resolution find the row. No other local subject scheme (username/email) is used.

##### OIDC HTTP flow + verification seam (`api/auth.py`)

`OIDCAuthService` consumes **already-verified** `OIDCClaims`, never a raw token — token verification
(discovery, JWKS, signature, `iss`/`aud`/`exp`) is an isolated adapter (`authlib`) so provisioning
stays unit-testable without a network. Two new endpoints per provider:

- `GET /api/auth/oidc/{provider}/login` → 307 redirect to the IdP authorize URL (state/nonce set).
- `GET /api/auth/oidc/{provider}/callback?code=…&state=…` → adapter exchanges code, verifies the
  id-token → `OIDCClaims` → `OIDCAuthService(db, provider).complete_login(claims)` →
  `create_access_token(user.id)` → return `Token` (same shape as `/login`).

`get_auth_service` (local) is unchanged; add `get_oidc_auth_service(provider)` + a small
`get_oidc_verifier(provider)` DI. OIDC provider config (issuer, client id/secret, redirect URI) is a
DT-12 concern; DT-4 only names it.

##### SAML decision

The backend speaks **OIDC only**. SAML IdPs are fronted by an OIDC broker (Dex or Keycloak) that
performs the SAML dance and re-asserts the user as OIDC to MarimoHub. The broker may still surface
the original IdP so a `saml:<idp-slug>` provider value is recorded in `identities.provider`, but no
SAML/XML code enters `backend/`. This keeps one protocol adapter (`authlib` OIDC) instead of two.

##### Explicit contracts

- **Provisioning atomicity.** `_provision` issues one `commit()`; on any failure it rolls back, so a
  user never exists without its identity (and, for local signup, its `local_credentials`). It never
  creates a workspace; the user must explicitly create one through DT-5.
- **Duplicate handling.** A pre-flight `_name_or_email_taken` gives a clean `DuplicateUserError` for
  the common case; the `IntegrityError` catch is the concurrency backstop (also covers a lost
  slug/subject race). Both surface as `DuplicateUserError` → `api/auth.py`'s existing 409.
- **Local login is verify-only.** `authenticate` never provisions; an unknown username and an
  SSO-only user (no `local_credentials`) are indistinguishable (both → `None` → 401), so identity
  existence never leaks via login.
- **OIDC login is resolve-link-or-JIT.** Existing `(provider, subject)` wins. On a miss, a configured
  trusted provider with `email_verified=true` may attach the identity to the user whose canonical
  email matches. Untrusted/unverified claims never link; absent a safe match, JIT provisions a user.
  The identity insert and any new user commit atomically; uniqueness constraints are race backstops.
- **Identity mutability.** `(provider, subject)` is immutable; `email`/`last_login_at` are updated on
  every successful login (`_touch_identity`), per DT-1.
- **Session boundary unchanged.** The JWT still carries `sub = user.id`; nothing provider-specific
  enters the token. `create_access_token`/`decode_token`/`hash_password`/`verify_password` are byte
  identical to today — `core/security.py` is read-only for this task.

##### Impact on adjacent modules

| File | Change |
|---|---|
| `core/security.py` | **none** — JWT + bcrypt helpers untouched (the explicit seam guarantee). |
| `api/deps.py` | **none** — `get_current_user_optional` still `decode_token → db.get(User, id)`; `User` keeps `id`, so it resolves unchanged. |
| `api/auth.py` | `get_auth_service`/`/register`/`/login`/`/logout` bodies unchanged in shape; **add** `get_oidc_auth_service`, `get_oidc_verifier`, and the two `/oidc/{provider}/…` routes. |
| `schemas/auth.py` | **none** — `UserCreate`/`LoginRequest`/`Token` shapes unchanged. |
| `services/auth_service.py` | rewritten per above (kernel + two providers). |

##### Deletions (by symbol)

- `BasicAuthService.register` old body: the `User(username=…, password_hash=hash_password(...))`
  construction (the `password_hash` column no longer exists) and the single-row `add`/`commit`.
- `BasicAuthService.authenticate` old body: the `select(User).where(username==…)` +
  `verify_password(password, user.password_hash)` — replaced by the `local_credentials` join.
- No symbol removed from `core/security.py`, `api/deps.py`, or `schemas/`.

##### Data flow

```mermaid
flowchart TD
    subgraph local [local]
        R[POST /api/auth/register] --> RS[BasicAuthService.register]
        L[POST /api/auth/login] --> AS[BasicAuthService.authenticate]
        AS -->|join local_credentials + verify_password| AV{ok?}
        AV -->|no| N401[401]
        AV -->|yes| TOK
    end
    subgraph oidc [external OIDC / SAML-via-broker]
        OL[GET /oidc/:provider/login] -->|307| IdP[(IdP / Dex broker)]
        IdP --> CB[GET /oidc/:provider/callback]
        CB --> VER[OIDC verifier adapter<br/>authlib: JWKS, iss/aud/exp]
        VER -->|OIDCClaims| CL[OIDCAuthService.complete_login]
        CL --> RES[_resolve_identity<br/>provider, subject]
        RES -->|hit| TOUCH[_touch_identity] --> TOK
        RES -->|miss + trusted verified email match| LINK[attach identity to existing user] --> TOK
        RES -->|miss + no safe match| PROV
    end
    RS --> PROV[_provision<br/>atomic tx]
    PROV --> DB[(user + identity<br/>+ local_credentials?)]
    PROV --> TOK
    TOUCH --> TOK[create_access_token user.id<br/>HS256 sub=user.id]
    TOK --> CLIENT[Token]
    CLIENT -.later Bearer.-> GCU[deps.get_current_user<br/>decode_token → db.get User<br/>UNCHANGED]
```

##### Rejected alternatives

- **One polymorphic `authenticate(Credential)` on the ABC.** Rejected: local verifies-and-never-JITs
  while OIDC resolves-or-JITs, so the two entry points can't share a signature or return contract
  without violating LSP; the shared axis is provisioning, which is what the ABC captures.
- **Fold `local_credentials` write into the `identities` row.** Rejected: DT-1 deliberately split
  password material off the identity so it never rides identity-resolve queries — `_provision`
  writes it as a separate optional row (present iff `password_hash is not None`).
- **Verify the raw OIDC id-token inside `OIDCAuthService`.** Rejected: couples provisioning to network
  I/O and JWKS state; the verifier adapter yields `OIDCClaims` so the service is pure DB + testable.

##### Open questions

- **Trusted-provider configuration.** DT-12 must identify which configured OIDC providers may link
  by verified email. Email normalization is lowercase + surrounding-whitespace removal; provider
  alias-specific transformations are intentionally not applied.
- **Multiple identities per user (account linking UI).** The model supports N identities per user,
  but DT-4 provides no "link another provider to my account" endpoint (only resolve/JIT). Flagged if
  linking is in scope for the workspace/settings surface (relates to DT-5).

---

### DT-5 — Workspace & membership management API (new surface)
**Status:** complete

Design the new router for workspace lifecycle and collaboration: create workspace, list "my workspaces" (the `workspace_members WHERE user_id=me` query), get workspace, manage members (add/remove/change role, owner-only), and archive/list-archived/restore workspaces. Define the purge service, user-deletion ownership checks, pydantic schemas, and how role checks reuse DT-3.

- **Acceptance criteria:** endpoint list (methods/paths), request/response schemas, and per-endpoint authz (owner vs member); active/archive filtering, hidden/restorable archive behavior, retention deadline and purge entrypoint specified; last-owner and user-deletion guards specified; "my workspaces" query defined; wiring into `main.py`.
- **Likely files:** new `backend/app/api/workspaces.py`, new `backend/app/schemas/workspace.py`, `backend/app/main.py`.
- **Depends on:** DT-1, DT-3.

#### DT-5 Design

A single new router, `backend/app/api/workspaces.py`, owns workspace lifecycle and collaboration.
It is a **new surface** (nothing to migrate off), so the work is: pydantic schemas, a
membership-role dependency pair layered on DT-3's policy, and the workspace-specific business rules
(archive lifecycle, last-owner protection, user deletion, slug uniqueness) that are *not* access
decisions and therefore stay in the router as domain `409`s. Every access decision routes through
`services/access.py`; the router never encodes a role comparison itself.

##### Endpoint surface

All paths are under `prefix="/api/workspaces"`. Every endpoint requires authentication — there is
no anonymous or public read of a workspace object (a workspace's only read gate is membership), so
`get_current_user` (401 on anonymous) fronts them all; the policy layer then decides member-vs-role.

| Method | Path | Purpose | Authz (min role) | Archive rule | Success |
|---|---|---|---|---|---|
| POST | `/api/workspaces` | Create workspace; caller becomes `owner` | authenticated | creates active | 201 `WorkspaceOut` |
| GET | `/api/workspaces` | List active **my workspaces** | authenticated | excludes archived | 200 `list[WorkspaceOut]` |
| GET | `/api/workspaces/archived` | List caller-owned archived workspaces | `owner` membership | archived only | 200 `list[WorkspaceArchiveOut]` |
| GET | `/api/workspaces/{workspace_id}` | Get one workspace | member (`viewer`) | — | 200 `WorkspaceOut` |
| PATCH | `/api/workspaces/{workspace_id}` | Rename (`name` only; `slug` immutable) | `owner` | active only | 200 `WorkspaceOut` |
| DELETE | `/api/workspaces/{workspace_id}` | Archive workspace and contained resources | `owner` | active → archived | 204 |
| POST | `/api/workspaces/{workspace_id}/restore` | Restore before purge deadline | archived `owner` | archived → active | 200 `WorkspaceOut` |
| GET | `/api/workspaces/{workspace_id}/members` | List members | member (`viewer`) | — | 200 `list[WorkspaceMemberOut]` |
| POST | `/api/workspaces/{workspace_id}/members` | Add a member by `user_id` | `owner` | reject (409) | 201 `WorkspaceMemberOut` |
| PATCH | `/api/workspaces/{workspace_id}/members/{user_id}` | Change a member's role | `owner` | reject (409) | 200 `WorkspaceMemberOut` |
| DELETE | `/api/workspaces/{workspace_id}/members/{user_id}` | Remove a member | `owner` | reject (409) | 204 |

##### Pydantic schemas — `backend/app/schemas/workspace.py`

```python
from datetime import datetime
from uuid import UUID
from pydantic import BaseModel, ConfigDict, Field, field_validator
from app.models import WorkspaceRole
from app.services.slug import DNS_LABEL_RE, to_dns_label   # shared helper (see below)

class WorkspaceCreate(BaseModel):
    """Create a workspace. `slug` optional; derived from `name` when omitted."""
    name: str = Field(min_length=1, max_length=255)
    slug: str | None = Field(default=None, max_length=63)

    @field_validator("slug")
    @classmethod
    def validate_slug(cls, value: str | None) -> str | None:
        if value is not None and not DNS_LABEL_RE.match(value):
            raise ValueError("slug must be a lowercase DNS label (RFC-1123, <=63 chars)")
        return value

class WorkspaceUpdate(BaseModel):
    """Rename a workspace. Slug is immutable (it may appear in k8s names)."""
    name: str = Field(min_length=1, max_length=255)

class WorkspaceOut(BaseModel):
    """A workspace as seen by a member, annotated with the caller's own role."""
    id: UUID
    slug: str
    name: str
    role: WorkspaceRole                     # the CALLER's role in this workspace
    created_at: datetime
    model_config = ConfigDict(from_attributes=True)

class WorkspaceArchiveOut(WorkspaceOut):
    archived_at: datetime
    purge_after: datetime

class WorkspaceMemberCreate(BaseModel):
    user_id: UUID
    role: WorkspaceRole = WorkspaceRole.EDITOR

class WorkspaceMemberUpdate(BaseModel):
    role: WorkspaceRole

class WorkspaceMemberOut(BaseModel):
    workspace_id: UUID
    user_id: UUID
    username: str                           # joined from users for a usable member list
    email: str
    role: WorkspaceRole
    created_at: datetime
    model_config = ConfigDict(from_attributes=True)
```

`role` on `WorkspaceOut` is the *caller's* authority, not a property of the workspace, so it is
supplied explicitly (never populated by `from_attributes`) via a `_workspace_out(ws, role)` helper.
These names are added to `schemas/__init__.py`'s re-export facade and `__all__`
(`WorkspaceCreate`, `WorkspaceUpdate`, `WorkspaceOut`, `WorkspaceArchiveOut`, `WorkspaceMemberCreate`,
`WorkspaceMemberUpdate`, `WorkspaceMemberOut`).

##### Policy reuse — `services/access.py`

DT-5 consumes DT-3's final `authorize_workspace` and `load_workspace_for` contracts unchanged.
Normal dependencies hide missing, archived, and non-member workspaces as 404; members below the
required role receive 403. Archive list/restore adds a separate owner-only loader that requires
`archived_at IS NOT NULL`, because normal policy helpers intentionally never return archived rows.

##### FastAPI dependency pair — `backend/app/api/deps.py` (additions, mirrors DT-3)

```python
@dataclass(frozen=True, slots=True)
class WorkspaceContext:
    workspace: Workspace
    actor: User
    role: WorkspaceRole                     # caller's role, >= the required minimum

def require_workspace(minimum: WorkspaceRole) -> Callable[..., Awaitable[WorkspaceContext]]:
    async def dep(
        workspace_id: UUID,
        db: Annotated[AsyncSession, Depends(get_db)],
        actor: Annotated[User, Depends(get_current_user)],      # mandatory auth → 401 on anon
    ) -> WorkspaceContext:
        workspace, role = await load_workspace_for(db, workspace_id, actor, minimum)
        return WorkspaceContext(workspace, actor, role)
    return dep

WorkspaceMemberDep = Annotated[WorkspaceContext, Depends(require_workspace(WorkspaceRole.VIEWER))]
WorkspaceOwnerDep  = Annotated[WorkspaceContext, Depends(require_workspace(WorkspaceRole.OWNER))]
```

`WorkspaceMemberDep` fronts the two read endpoints; `WorkspaceOwnerDep` fronts rename, delete, and
all three member-management endpoints. Create/list take no `{workspace_id}` and depend on
`get_current_user` directly.

##### Router module — `backend/app/api/workspaces.py` (concrete signatures)

```python
router = APIRouter(prefix="/api/workspaces", tags=["workspaces"])

async def _owner_count(db: AsyncSession, workspace_id: UUID) -> int:
    return await db.scalar(
        select(func.count()).select_from(WorkspaceMember).where(
            WorkspaceMember.workspace_id == workspace_id,
            WorkspaceMember.role == WorkspaceRole.OWNER)) or 0

def _workspace_out(ws: Workspace, role: WorkspaceRole) -> WorkspaceOut:
    return WorkspaceOut(id=ws.id, slug=ws.slug, name=ws.name,
                        role=role, created_at=ws.created_at)

@router.post("", response_model=WorkspaceOut, status_code=201)
async def create_workspace(payload: WorkspaceCreate, db, current_user: <get_current_user>) -> WorkspaceOut:
    slug = await unique_slug(db, payload.slug or to_dns_label(payload.name))
    if payload.slug is not None and await _slug_taken(db, slug):     # explicit slug must be free
        raise HTTPException(409, "Workspace slug already in use")
    ws = Workspace(slug=slug, name=payload.name)
    db.add(ws); await db.flush()
    db.add(WorkspaceMember(workspace_id=ws.id, user_id=current_user.id, role=WorkspaceRole.OWNER))
    await db.commit(); await db.refresh(ws)
    return _workspace_out(ws, WorkspaceRole.OWNER)

@router.get("", response_model=list[WorkspaceOut])
async def list_my_workspaces(db, current_user: <get_current_user>) -> list[WorkspaceOut]:
    rows = await db.execute(
        select(Workspace, WorkspaceMember.role)
        .join(WorkspaceMember, WorkspaceMember.workspace_id == Workspace.id)
        .where(WorkspaceMember.user_id == current_user.id,
               Workspace.archived_at.is_(None))
        .order_by(Workspace.created_at.asc()))
    return [_workspace_out(ws, role) for ws, role in rows.all()]

@router.get("/{workspace_id}", response_model=WorkspaceOut)
async def get_workspace(ctx: WorkspaceMemberDep) -> WorkspaceOut:
    return _workspace_out(ctx.workspace, ctx.role)

@router.patch("/{workspace_id}", response_model=WorkspaceOut)
async def rename_workspace(payload: WorkspaceUpdate, ctx: WorkspaceOwnerDep, db) -> WorkspaceOut:
    ctx.workspace.name = payload.name
    await db.commit(); await db.refresh(ctx.workspace)
    return _workspace_out(ctx.workspace, ctx.role)

@router.delete("/{workspace_id}", status_code=204, response_class=Response)
async def delete_workspace(ctx: WorkspaceOwnerDep, db) -> Response:
    now = utcnow()
    ctx.workspace.archived_at = now
    ctx.workspace.purge_after = now + timedelta(days=settings.WORKSPACE_ARCHIVE_RETENTION_DAYS)
    await stop_workspace_runtimes(ctx.workspace.id)
    await db.commit()
    return Response(status_code=204)

@router.get("/{workspace_id}/members", response_model=list[WorkspaceMemberOut])
async def list_members(ctx: WorkspaceMemberDep, db) -> list[WorkspaceMemberOut]:
    rows = await db.execute(
        select(WorkspaceMember, User.username, User.email)
        .join(User, User.id == WorkspaceMember.user_id)
        .where(WorkspaceMember.workspace_id == ctx.workspace.id)
        .order_by(WorkspaceMember.created_at.asc()))
    return [WorkspaceMemberOut(workspace_id=m.workspace_id, user_id=m.user_id,
            username=un, email=em, role=m.role, created_at=m.created_at)
            for m, un, em in rows.all()]

@router.post("/{workspace_id}/members", response_model=WorkspaceMemberOut, status_code=201)
async def add_member(payload: WorkspaceMemberCreate, ctx: WorkspaceOwnerDep, db) -> WorkspaceMemberOut:
    target = await db.get(User, payload.user_id)
    if target is None:
        raise HTTPException(404, "User not found")
    if await get_role(db, ctx.workspace.id, target.id) is not None:
        raise HTTPException(409, "User is already a member")
    member = WorkspaceMember(workspace_id=ctx.workspace.id, user_id=target.id, role=payload.role)
    db.add(member)
    await db.commit(); await db.refresh(member)
    return WorkspaceMemberOut(..., username=target.username, email=target.email, ...)

@router.patch("/{workspace_id}/members/{user_id}", response_model=WorkspaceMemberOut)
async def change_member_role(user_id: UUID, payload: WorkspaceMemberUpdate,
                             ctx: WorkspaceOwnerDep, db) -> WorkspaceMemberOut:
    member = await db.get(WorkspaceMember, (ctx.workspace.id, user_id))
    if member is None:
        raise HTTPException(404, "Member not found")
    if (member.role == WorkspaceRole.OWNER and payload.role != WorkspaceRole.OWNER
            and await _owner_count(db, ctx.workspace.id) == 1):
        raise HTTPException(409, "Workspace must retain at least one owner")
    member.role = payload.role
    await db.commit()
    return WorkspaceMemberOut(...)   # re-joined username/email

@router.delete("/{workspace_id}/members/{user_id}", status_code=204, response_class=Response)
async def remove_member(user_id: UUID, ctx: WorkspaceOwnerDep, db) -> Response:
    member = await db.get(WorkspaceMember, (ctx.workspace.id, user_id))
    if member is None:
        raise HTTPException(404, "Member not found")
    if member.role == WorkspaceRole.OWNER and await _owner_count(db, ctx.workspace.id) == 1:
        raise HTTPException(409, "Workspace must retain at least one owner")
    await db.delete(member); await db.commit()
    return Response(status_code=204)
```

`_slug_taken(db, slug)` and the shared slug helpers `to_dns_label` / `unique_slug` (async
suffix-`-2/-3`-until-free) / `DNS_LABEL_RE` live in a new **`backend/app/services/slug.py`**.
Only explicit workspace creation uses these helpers; auth and the squashed baseline never generate
workspace slugs.

##### Wiring — `backend/app/main.py`

- `from app.api.workspaces import router as workspaces_router` and
  `app.include_router(workspaces_router)`.
- Register the DT-3 typed-denial → HTTP handler (DT-5 is the first full consumer of `AccessError`
  in a router). Per DT-3, until DT-13 lands this is a small `@app.exception_handler(AccessError)`
  mapping `ResourceHidden→404`, `AuthenticationRequired→401` (`WWW-Authenticate: Bearer`),
  `PermissionDenied→403`, using `err.detail`. It is shared with DT-6; whichever lands first adds it,
  and DT-13 later relocates it into the central error seam.

##### Explicit contracts

- **Create.** Caller becomes the sole `owner`. Workspace + owner-membership commit as one transaction;
  a partial workspace with no owner is never observable.
- **My-workspaces query.** `SELECT Workspace, role FROM workspaces JOIN workspace_members ON … WHERE
  workspace_members.user_id = :me AND workspaces.archived_at IS NULL`, oldest-first. Uses the
  `ix_workspace_members_user_id` index (DT-1/DT-2). Returns every active workspace the caller belongs
  to, each carrying the caller's own role.
- **Read (`get`, `list_members`).** `viewer` and up. Non-members and missing ids are indistinguishable
  (`ResourceHidden` → **404**); an authenticated member with any role can read.
- **Owner-only mutations.** Rename, delete, and all member management require `owner`
  (`PermissionDenied` → **403** for viewer/editor members; **404** for non-members). No self-service
  role change or self-leave in this surface (see open questions).
- **Last-owner protection.** A shared workspace must always retain ≥1 `owner`; demoting or removing
  the final owner returns **409**. This is app-enforced (not a DB constraint) via `_owner_count`.
- **Add-member.** By `user_id`; unknown user → **404**, existing member → **409** (pre-checked, with
  the composite-PK `IntegrityError` as the concurrency backstop → 409). Any role including `owner`
  (co-owners) may be granted.
- **Archive lifecycle.** Delete accepts non-empty workspaces, stamps `archived_at` and a fixed
  `purge_after`, and stops their runtimes. Normal workspace/notebook lookups exclude archived rows.
  Owners list/restore archives through the dedicated surface. `purge_due_workspaces(now)` is
  idempotent and physically deletes due rows; FK cascades intentionally remove contained notebooks,
  deployments, and data at that final stage. Slugs remain reserved until purge.
- **User deletion.** Lock all memberships. Hard-delete sole-member workspaces immediately. If the user
  is final owner of any multi-member workspace, return 409 until ownership is transferred. Otherwise
  remove memberships and delete the user. The restrictive membership FK prevents bypass.
- **Transaction/lifecycle.** Every handler is request-scoped on the injected `AsyncSession`; reads
  never commit, mutations issue exactly one `commit()`. All access decisions are raised by
  `access.py` as typed `AccessError`s and rendered by the single handler; the router only raises the
  domain **409/404** conflicts above.

##### Deletions

None — DT-5 is a new surface. The only change to existing code is the DT-3 `authorize_workspace`
**revision** (403→404 for non-members) recorded above, and the DT-4-flagged extraction of slug logic
into `services/slug.py` (a consolidation, not a deletion). No obsolete workspace code exists to remove.

##### Rejected alternatives

- **Members embedded in `WorkspaceOut` instead of a separate `/members` collection** — rejected:
  couples the workspace read to an unbounded member join and blocks pagination; a sub-collection is
  the idiomatic REST shape.
- **Add members by `email`/`username`** — rejected for the canonical contract: `user_id` is the
  stable identifier and avoids email/username-enumeration and rename ambiguity; an email→id
  directory lookup is a separate surface (open question), not baked into membership.
- **Immediate cascade-delete on workspace delete** — rejected: archive + restore provides a 30-day
  safety window; cascade deletion occurs only when the persisted purge deadline passes.

##### Open questions

- **Self-leave / self-service role.** Member management is `owner`-only per the task, so a non-owner
  cannot leave a shared workspace on their own. A `DELETE /members/me` (self-leave, still last-owner
  guarded) is a likely follow-up; flagged as a product decision, intentionally out of this surface.
- **User directory for invites.** Add-member takes `user_id`, but there is no endpoint to resolve a
  person → id. A minimal `GET /api/users?email=` / search surface is needed for a usable invite UX;
  scoped to a future task (relates to DT-4's account-linking note), not DT-5.
- **Purge scheduling.** `purge_due_workspaces` is scheduler-agnostic and safe to retry. Deployment must
  invoke it periodically through the provided CLI entrypoint; the exact platform scheduler manifest
  is deployment-owned.

---

### DT-6 — Notebook & data API redesign onto workspaces (visibility & forking)
**Status:** complete

Design `notebooks.py` and `data.py` reworked for workspace ownership: create/import/fork require an explicit, active, membership-checked target workspace; discovery/search filter to `visibility='public' OR workspace_id IN (my active memberships)`; fork lands in the chosen workspace with `visibility=private` and `parent_id` set (maintaining `fork_count`); `publish` and all visibility handling use `private`; update `NotebookOut`/`NotebookListOut` (drop `user_id`; add `workspace_id`, `created_by`) and parent-attribution fields; move write-authz onto DT-3; define `data.py` authorization (today unauthenticated).

- **Acceptance criteria:** per-endpoint ownership/authz changes; updated discovery query; fork-target semantics; revised notebook schemas and create/update payloads; `data.py` access rules; visibility rename applied throughout.
- **Likely files:** `backend/app/api/notebooks.py`, `backend/app/api/data.py`, `backend/app/schemas/notebook.py`, `backend/app/schemas/data.py`.
- **Depends on:** DT-1, DT-3.

#### DT-6 Design

`notebooks.py` and `data.py` stop being self-authorizing. Every access decision routes through
DT-3's `services/access.py`: path-param routes ride the `NotebookRead`/`NotebookWrite` deps;
workspace-target routes (create/import/fork) call `authorize_workspace(…, EDITOR)`; discovery uses
`visible_notebooks(user_id)`; `_notebook_out` gates parent attribution through the pure
`can_access`. Ownership is a workspace, not a user, throughout; visibility is `private/unlisted/public`.

##### Per-endpoint target design

| Method / path | Authz mechanism (DT-3) | Ownership / target | Notes |
|---|---|---|---|
| `GET /api/notebooks` | `get_current_user_optional` + `visible_notebooks(user.id)` in the `WHERE` | — | one uniform predicate for keyword, tag, and semantic search |
| `POST /api/notebooks` | `get_current_user` → `_resolve_target_workspace(…, EDITOR)` | required `workspace_id` (body) | created `visibility=private`, `created_by=me` |
| `POST /api/notebooks/import` | `get_current_user` → `_resolve_target_workspace(…, EDITOR)` | required `workspace_id` (body) | GitLab fetch unchanged; then same create path |
| `GET /api/notebooks/{id}` | `NotebookRead` dep | notebook's workspace | includes `source` (reader-visible) |
| `POST /api/notebooks/{id}/fork` | `get_current_user` + `load_notebook_for(id, READ)` (source) + `_resolve_target_workspace(…, EDITOR)` (target) | source id (path) → required target `workspace_id` (body) | fork `visibility=private`, `parent_id=source.id`; `source.fork_count++` |
| `PUT /api/notebooks/{id}` | `NotebookWrite` dep | notebook's workspace | partial update; `source` via storage |
| `DELETE /api/notebooks/{id}` | `NotebookWrite` dep | notebook's workspace | 204 |
| `POST /api/notebooks/{id}/publish` | `NotebookWrite` dep | notebook's workspace | sets visibility; embeds on leaving `private` |
| `GET /api/notebooks/{id}/data` | `NotebookRead` dep | notebook's workspace | latest payload; 404 hides missing/private |
| ~~`POST /api/notebooks/{id}/data`~~ | **removed** | — | runtime writes move to DT-8's internal service-token endpoint (below) |

`NotebookRead`/`NotebookWrite` inject `NotebookContext(notebook, actor, role)`; handlers read
`ctx.notebook`/`ctx.actor`/`ctx.role` and never re-check access. Fork is the one path-param route
that does **not** use the dep: it additionally requires an authenticated forker (to own the fork), so
it takes `get_current_user` and calls `load_notebook_for(READ)` on the source directly — the
"id-in-path but needs extra authz" case DT-3 anticipated.

##### Shared target-workspace resolver — `notebooks.py`

```python
async def _resolve_target_workspace(
    db: AsyncSession, actor: User, requested: UUID
) -> UUID:
    """Require EDITOR in the explicitly selected active target workspace."""
    await authorize_workspace(db, requested, actor, WorkspaceRole.EDITOR)  # 404 absent/archived/non-member
    return requested
```

##### Discovery query (replaces the inline `visibility_filter`)

```python
filters = [visible_notebooks(current_user.id if current_user else None)]  # DT-3 predicate
# semantic / tag / FTS clauses appended exactly as today
if semantic_query is not None:
    filters.append(Notebook.embedding.is_not(None))   # private notebooks carry no embedding
```

The current special-case that dropped the ownership half of the filter for semantic search is
**deleted**: `visible_notebooks` applies uniformly, and `embedding.is_not(None)` already excludes
private (un-embedded) notebooks, so my own unlisted/public notebooks now correctly appear in my
semantic results while nobody else's private ones leak.

##### Response shaping — `_notebook_out`

```python
async def _notebook_out(
    db: AsyncSession, notebook: Notebook, actor: User | None,
    storage: NotebookStorageService, *, include_source: bool,
) -> NotebookOut:
    payload = {
        "id": notebook.id, "workspace_id": notebook.workspace_id,
        "created_by": notebook.created_by, "parent_id": notebook.parent_id,
        "title": notebook.title, "description": notebook.description,
        "tags": notebook.tags, "visibility": notebook.visibility,
        "fork_count": notebook.fork_count,
        "created_at": notebook.created_at, "updated_at": notebook.updated_at,
    }
    if notebook.parent_id is not None:
        row = (await db.execute(
            select(Notebook, Workspace.slug)
            .join(Workspace, Notebook.workspace_id == Workspace.id)
            .where(Notebook.id == notebook.parent_id)
        )).one_or_none()
        if row is not None:
            parent, parent_slug = row.tuple()
            parent_role = await get_role(db, parent.workspace_id, actor.id if actor else None)
            if can_access(parent.visibility, parent_role, Action.READ):   # hide unreadable lineage
                payload["parent_title"] = parent.title
                payload["parent_workspace_id"] = parent.workspace_id
                payload["parent_workspace_slug"] = parent_slug
    if include_source:
        payload["source"] = await storage.get(notebook)
    return NotebookOut.model_validate(payload)
```

`include_source=True` on every single-notebook response (`get`, `create`, `import`, `fork`,
`update`, `publish`) and `False` in the list loop (source is heavy and irrelevant to a page). This
replaces the old owner-only rule: because the single-notebook routes are already READ-gated by the
policy, **any reader** (owner, viewer, or an anonymous viewer of a public/unlisted notebook) receives
`source` — reading a notebook means seeing its code. Parent attribution is workspace-based
(non-null) and gated on the actor being able to READ the parent, so a since-privatized parent's
existence never leaks.

##### Revised schemas — `backend/app/schemas/notebook.py`

```python
class NotebookCreate(BaseModel):
    title: str = Field(min_length=1)
    description: str | None = None
    tags: list[str] = Field(default_factory=list)
    source: str | None = None
    workspace_id: UUID

class NotebookImport(BaseModel):
    url: str = Field(min_length=1)
    pat: str | None = None
    workspace_id: UUID

class NotebookFork(BaseModel):
    """Explicit fork target workspace."""
    workspace_id: UUID

class NotebookUpdate(BaseModel):                # UNCHANGED (title/description/tags/source partial)
    ...

class NotebookPublish(BaseModel):               # shape UNCHANGED; values private|unlisted|public
    visibility: NotebookVisibility

class NotebookOut(BaseModel):
    id: UUID
    workspace_id: UUID                          # was user_id
    created_by: UUID | None                     # attribution, NULL after user deletion
    parent_id: UUID | None
    parent_title: str | None = None
    parent_workspace_id: UUID | None = None     # replaces parent_owner_id
    parent_workspace_slug: str | None = None    # replaces parent_owner_username
    title: str
    description: str | None
    tags: list[str]
    visibility: NotebookVisibility
    fork_count: int
    source: str | None = None
    created_at: datetime
    updated_at: datetime
    model_config = ConfigDict(from_attributes=True)

class NotebookListOut(BaseModel):               # UNCHANGED
    items: list[NotebookOut]
    total: int
    page: int
    page_size: int
```

`NotebookFork` is added to `schemas/__init__.py`'s facade + `__all__`.

##### `data.py` access rules & the runtime-write decision (resolves DT-3 open question)

`notebook_data` is **operational state written by the notebook runtime**, not part of the shareable
artifact. DT-3 tentatively mapped `POST /{id}/data` to notebook WRITE; that is wrong for a public
deployment whose pod persists data on behalf of an anonymous end-user (the pod holds no workspace
membership). Resolution — split the two directions:

- **Read (user-facing).** `GET /api/notebooks/{id}/data` → `NotebookRead` dep (notebook READ).
  Public/unlisted notebook data is readable by anyone with the link; private only to members; a
  missing or hidden notebook 404s. This is the collaborator/frontend "show latest payload" path.
- **Write (runtime-facing).** The public `POST /{id}/data` is **removed**. Writes originate from the
  session pod and go to DT-8's internal, service-token-authenticated, NetworkPolicy-restricted
  surface — mirroring `GET /api/internal/notebooks/{id}/source`:

  > **Handoff to DT-8** — `POST /api/internal/notebooks/{id}/data` (body `JsonValue`, optional
  > `?source=`), returning `NotebookDataCreated`. Authenticated by the session's service-account
  > token and scoped to the notebook the calling `MarimoSession` serves (the token→notebook binding
  > is DT-8's to enforce). A matching internal `GET` gives a pod read-back of its own state without
  > the public visibility gate. `NotebookData`/`NotebookDataCreated`/`NotebookDataOut` schemas are
  > retained for this endpoint and the public read.

This keeps one authorization rule per direction (public read = notebook READ; runtime write =
service token), and no anonymous end-user ever writes through the public API.

##### Explicit contracts

- **Fork semantics.** A fork always commits `workspace_id=<resolved target>`, `created_by=<forker>`,
  `parent_id=<source.id>`, `visibility=private`, with `title/description/tags/source` snapshotted
  from the source at fork time (later source edits do not propagate). `embedding` is **not** copied —
  a private fork has none until published. Requires READ on the source (public/unlisted → any
  authenticated user; private → a member) **and** EDITOR on the target workspace.
- **`fork_count` maintenance.** Incremented on the **source** via
  `UPDATE notebooks SET fork_count = fork_count + 1 WHERE id = source.id`, in the same transaction as
  the fork insert (both commit atomically or roll back together). Retained denormalized counter per
  DT-1; **no decrement** on fork deletion (accepted, matches current behaviour).
- **Visibility transitions.** Create/import/fork ⇒ `private`. `publish` sets any of
  `private|unlisted|public`; the embedding is computed **only when leaving `private`**
  (`private → unlisted|public`). Re-privatizing keeps the stored embedding (harmless — it stays out
  of others' results via `visible_notebooks`) and issues no re-embed. No `draft` value exists anywhere.
- **Error behaviour (via DT-3 typed denials).** Missing or read-hidden notebook →
  `ResourceHidden` (404); WRITE by an identified viewer → `PermissionDenied` (403); WRITE by a
  non-member of a *private* notebook → `ResourceHidden` (404, existence hidden); create/import/fork
  into a workspace the caller doesn't belong to → `ResourceHidden` (404), into one where they are only
  `viewer` → `PermissionDenied` (403). All are raised by `access.py` and rendered by the single
  `AccessError` handler (added by DT-5's `main.py` wiring; relocated by DT-13). Non-access domain
  errors keep their local `HTTPException`: `GitLabImportError` (import) and notebook-data-not-found
  (data GET).
- **Lifecycle.** Every handler is request-scoped on the injected `AsyncSession`; reads never commit;
  create/import/fork/update/publish/delete issue exactly one `commit()`. `NotebookStorageService`
  (source get/put/delete) is unchanged — DT-11 owns that seam.

##### Deletions (by file / symbol)

- `notebooks.py`: `_not_found`, `_is_owner`, `_can_view`, `_get_visible_notebook`,
  `_get_owned_notebook`, and the inline `visibility_filter` / semantic special-case block in
  `list_notebooks`. All replaced by DT-3 (`NotebookRead`/`NotebookWrite`, `visible_notebooks`,
  `authorize_workspace`, `can_access`/`get_role`).
- `data.py`: `_notebook_not_found`, `_ensure_notebook_exists`, and the entire `create_notebook_data`
  POST handler (relocated to DT-8 internal). `data.py` retains only the READ-gated GET.
- `schemas/notebook.py`: `NotebookOut.user_id`, `NotebookOut.parent_owner_id`,
  `NotebookOut.parent_owner_username`.
- No `Notebook(user_id=…)` / `NotebookVisibility.DRAFT` construction remains in either router.

##### Data flow — fork

```mermaid
flowchart TD
    C[POST /api/notebooks/:id/fork<br/>body: workspace_id] --> AUTH[get_current_user<br/>401 if anon]
    AUTH --> SRC[load_notebook_for id, actor, READ]
    SRC -->|ResourceHidden| E404[404]
    SRC -->|ok| TGT[_resolve_target_workspace<br/>required workspace_id from body]
    TGT --> AW[authorize_workspace target, EDITOR]
    AW -->|non-member| E404
    AW -->|viewer| E403[403]
    AW -->|editor/owner| CP[copy title/desc/tags/source snapshot]
    CP --> NEW[insert Notebook<br/>workspace_id=target, created_by=me,<br/>parent_id=source, visibility=private]
    NEW --> INC[UPDATE source.fork_count += 1]
    INC --> COMMIT[(single commit)]
    COMMIT --> OUT[NotebookOut incl. source + parent attribution]
```

##### Rejected alternatives

- **Keep `POST /{id}/data` as notebook WRITE.** Rejected: a public deployment's pod persists data for
  anonymous users and holds no workspace membership; WRITE would make legitimate runtime writes
  impossible. The internal service-token path is the correct identity.
- **Attribute fork lineage via `created_by` (`parent_owner_*`).** Rejected: `created_by` is nullable
  (SET NULL on user deletion), so lineage would blank out; the parent's `workspace_id`/`slug` are
  stable and non-null.
- **Include `source` only for editors.** Rejected: read access already implies the code is viewable
  (public notebooks are meant to be read and forked); gating source below READ adds a role branch with
  no security value.

##### Open questions

- **Sensitivity of `GET /{id}/data` on public notebooks.** DT-6 maps it to notebook READ, so a public
  notebook's collected data is world-readable. If `notebook_data` may hold sensitive end-user
  submissions from public deployments, GET should instead require *membership* (`get_role is not None`)
  regardless of visibility. Flagged as a product/security decision; the READ default is the
  consistent choice absent that ruling.
- **Internal data endpoint auth (DT-8).** The exact service-token → notebook binding for
  `POST /api/internal/notebooks/{id}/data` is specified in shape here but finalised by DT-8 alongside
  the source endpoint; DT-6 only fixes the contract (path, body, response, scoping intent).

---

### DT-7 — Session runtime manager seam (subprocess ↔ Kube)
**Status:** complete

Design the `SessionManager` interface that decouples callers from the subprocess implementation and admits the `KubeSessionManager` from `marimosession-crd-spec.md`. Define the seam's methods (create/spawn, get, target, stop, activity, source read-back) and value objects, and what is dropped from them (`port`, `pid`, `_reserved_ports`, `MARIMO_PORT_RANGE`, capacity gating). `KubeSessionManager` CRUDs `MarimoSession` CRs (CR name = deployment id for deploy, uuid4 for edit/run), lists/gets by label instead of an in-memory dict, and builds `SessionTarget` from `status.serviceName` + the token Secret. Decompose the 609-line `process_manager.py` into cohesive concerns (runtime backend, session registry, workdir/source, readiness) behind the seam, and specify a `SESSION_BACKEND=subprocess|kube` selector. State the deletion of `process_manager.py` internals that no longer apply and the k8s-client dependency to add.

- **Acceptance criteria:** a defined `SessionManager` protocol with signatures/return types, plus how `sessions.py`/`deployments.py`/`proxy.py`/`marimo_proxy.py` depend on it instead of the concrete class; target value objects with port/pid removed and a construction rule for both backends; CR create/get/list/delete mapping and wake-on-annotation described; a decomposition of `process_manager.py`; the dependency to add named.
- **Likely files:** `backend/app/services/process_manager.py`, new `backend/app/services/kube_session_manager.py`, `backend/app/services/marimo_proxy.py`, `backend/app/api/{sessions,deployments,proxy}.py`, `backend/app/schemas/session.py`, `backend/app/core/config.py`, `backend/pyproject.toml`, `marimosession-crd-spec.md`.
- **Depends on:** DT-8 (CRD shape). **Relates to:** DT-9, DT-10, DT-12.

#### DT-7 Design

One `SessionManager` **Protocol** is the whole surface `sessions.py`/`deployments.py`/`proxy.py`/
`marimo_proxy.py` see. Two implementations sit behind it: `SubprocessSessionManager` (dev, the
decomposed remains of `process_manager.py`) and `KubeSessionManager` (prod, CRUDs `MarimoSession` CRs
from DT-8). A `SESSION_BACKEND=subprocess|kube` selector picks one at process start. The value objects
lose everything subprocess-specific (`port`, `pid`); `SessionTarget` keeps its three-field shape so
`marimo_proxy` is untouched by the swap — only *how* a target is constructed differs. Four accessors
(`get`, `target`, `read_source`, `mark_active`) become **async**, because the kube backend resolves
state from the cluster instead of an in-memory dict; this is the one ripple every caller absorbs.

##### Target design — the seam (`backend/app/services/session_manager.py`)

```python
from __future__ import annotations
import enum
from dataclasses import dataclass
from datetime import datetime
from typing import Literal, Protocol, runtime_checkable
from uuid import UUID
from app.models import Notebook

SessionMode  = Literal["edit", "run"]           # what a user may request (validated in sessions.py)
RuntimeMode  = Literal["edit", "run", "deploy"] # what actually runs (deploy is internal)

class SessionPhase(enum.StrEnum):
    STARTING = "starting"   # created, not yet serving
    READY    = "ready"      # serving traffic
    SLEEPING = "sleeping"   # deploy only: idled, wakeable on demand
    FAILED   = "failed"     # deterministic startup failure; will not self-heal

# ── value objects (port + pid REMOVED) ──────────────────────────────────────
@dataclass(frozen=True, slots=True)
class SessionInfo:
    """Backend-neutral snapshot of a session's identity and lifecycle state."""
    id: UUID
    notebook_id: UUID
    mode: RuntimeMode
    phase: SessionPhase
    last_active: datetime | None
    creator_id: UUID | None

@dataclass(frozen=True, slots=True)
class SessionTarget:
    """Upstream URLs + access token for proxying. Shape unchanged; construction differs per backend."""
    http_base_url: str
    ws_base_url: str
    access_token: str

@runtime_checkable
class SessionManager(Protocol):
    async def spawn(
        self, notebook: Notebook, mode: SessionMode, creator_id: UUID | None = None
    ) -> SessionInfo: ...
    async def spawn_deployment(
        self, notebook: Notebook, deployment_id: UUID, slug: str
    ) -> SessionInfo: ...
    async def get(self, session_id: UUID) -> SessionInfo | None: ...
    async def target(self, session_id: UUID) -> SessionTarget | None: ...
    async def read_source(self, session_id: UUID) -> str | None: ...
    async def mark_active(self, session_id: UUID) -> None: ...
    async def stop(self, session_id: UUID) -> None: ...
    async def shutdown(self) -> None: ...

# ── error taxonomy (raised by both backends; DT-13 renders to HTTP) ──────────
class SessionManagerError(Exception): ...                 # base (was ProcessManagerError)
class SessionCapacityError(SessionManagerError): ...      # kept → 429 (was 503)
class SessionStartError(SessionManagerError):             # kept; carries client-safe `detail` → 503
    DEFAULT_DETAIL = "Deployment failed to wake"
    def __init__(self, message: str, *, detail: str | None = None) -> None: ...
class NotebookStartupError(SessionStartError): ...        # kept; deterministic → 502
class SessionNotFoundError(SessionManagerError): ...      # kept → 404
# DELETED: PortAllocationError (no ports anywhere)

# ── backend selection (replaces get_process_manager / shutdown_process_manager) ──
def get_session_manager() -> SessionManager: ...          # process-wide singleton per SESSION_BACKEND
async def shutdown_session_manager() -> None: ...
```

`schemas/session.py` imports `SessionMode` from here instead of `process_manager`; `SessionOut` is
unchanged (it never carried `port`). `deps`/routers depend on `SessionManager` (the Protocol) via
`Depends(get_session_manager)`, never on a concrete class.

##### Value-object construction rule (both backends)

`SessionInfo` and `SessionTarget` are identical types regardless of backend; construction is the only
difference. `port`/`pid` are gone from both — in the subprocess backend `port` survives **only** as a
private field of the internal live-session record (needed to form the loopback URL), obtained per spawn
from an **ephemeral OS-assigned port** (`socket.bind(("127.0.0.1", 0))`), which is what lets
`MARIMO_PORT_RANGE` and the reserved-port allocator disappear entirely.

| field | subprocess construction | kube construction |
|---|---|---|
| `SessionTarget.http_base_url` | `http://127.0.0.1:{port}{base_url}` | `http://{status.serviceName}.{SESSION_NAMESPACE}.svc:8080{spec.baseUrl}` |
| `SessionTarget.ws_base_url` | `ws://127.0.0.1:{port}{base_url}` | `ws://{status.serviceName}.{SESSION_NAMESPACE}.svc:8080{spec.baseUrl}` |
| `SessionTarget.access_token` | per-session `secrets.token_urlsafe(24)` | `MARIMO_TOKEN` read from Secret `msess-{id}-env` |
| `SessionInfo.phase` | `READY` once registered (only registered post-readiness) | mapped from `status.phase` (`Pending/Starting`→`STARTING`, `Ready`→`READY`, `Sleeping`→`SLEEPING`, `Failed`→`FAILED`) |
| `SessionInfo.last_active` | in-memory `last_active` | `status.lastActivity` |

`base_url`/`spec.baseUrl` is `/api/proxy/{session_id}` for edit/run and `/api/deployments/{slug}` for
deploy — identical between backends, so the proxy path prefix is a cross-backend invariant.

##### Module decomposition (`process_manager.py` → cohesive concerns)

```
backend/app/services/
├── session_manager.py          # NEW — the seam above: Protocol, value objects, errors,
│                               #   get_session_manager()/shutdown_session_manager() + SESSION_BACKEND select
├── subprocess_backend/         # NEW package — the decomposed dev backend (was process_manager.py)
│   ├── __init__.py             #   exports SubprocessSessionManager
│   ├── manager.py              #   SubprocessSessionManager(SessionManager): orchestrates the pieces below
│   ├── registry.py             #   LiveSessionRegistry: the _sessions dict + asyncio.Lock, register/pop/get
│   ├── runtime.py              #   OS-process backend: build command/env, start/terminate subprocess, ephemeral port
│   ├── workdir.py              #   workdir prep + source read/write (edit-reuse), replaces _prepare_workdir
│   └── readiness.py            #   wait_until_ready + HTTP probe
├── kube_session_manager.py     # NEW — KubeSessionManager(SessionManager): CRs + Secret/Service resolution
└── process_manager.py          # DELETED (content redistributed or removed)
```

The `subprocess_backend` package name avoids shadowing the stdlib `subprocess` module. Four concerns
that were tangled in the 609-line file now separate cleanly:

- **runtime** (`runtime.py`) — everything about *an OS process*: `_marimo_command`, `_marimo_env`,
  `_current_virtualenv`, `_start_marimo_process`, `_terminate_process`, plus a new `reserve_ephemeral_port()`.
- **registry** (`registry.py`) — *tracking live sessions*: the `dict[UUID, LiveSession]` + one lock, and
  `register`/`get`/`pop`/`snapshot`/`drain`. No ports, no capacity, no reserved set, no per-deployment
  lock dance — those concerns are deleted, so the registry shrinks to a guarded map.
- **workdir/source** (`workdir.py`) — *where source lives on disk*: `session_workdir`,
  `prepare_workdir`, `write_marimo_project_config`, `read_current_source`.
- **readiness** (`readiness.py`) — *is it serving yet*: `wait_until_ready`, `probe`.

`SubprocessSessionManager` (`manager.py`) implements the Protocol by composing these; it holds a
`LiveSession` record that keeps `process`, `workdir`, `token`, `base_url`, and the private `port`.

##### KubeSessionManager — CR/Secret/Service mapping

Constructed from `kubernetes_asyncio` clients (`CustomObjectsApi` for `marimosessions`, `CoreV1Api` for
Secrets/Services), a `NotebookStorageService` is **not** needed (source flows through DT-8's init
container, not the manager). It holds no session dict — every lookup is a labelled `get`/`list`.

| seam method | kube action | notes |
|---|---|---|
| `spawn(nb, mode, creator)` | `id=uuid4()`; create CR `msess`=`id` (`spec.mode`, `baseUrl=/api/proxy/{id}`, labels `notebook/workspace/mode`); read back `uid`; create Secret `msess-{id}-env` `{MARIMO_TOKEN, SESSION_TOKEN(nb.id)}` ownerRef=CR; **poll** `status.phase` → `Ready`/`Failed` | CR-first so the Secret can ownerRef it (DT-8 GC). Poll bounded by `SESSION_READY_TIMEOUT_SECONDS`. |
| `spawn_deployment(nb, dep_id, slug)` | CR name = `dep_id`; `create` — `AlreadyExists` is the idempotency barrier (**replaces the lock dance**). If existing CR `phase==Sleeping`: PATCH Secret `SESSION_TOKEN` (fresh mint) → PATCH annotation `marimohub.io/wake` → poll to `Ready`. If `Ready`: return. | Wake-on-annotation lives here: `spawn_deployment` on a sleeping CR **is** the wake, so the existing `_wake_deployment` caller shape is unchanged. |
| `get(id)` | `get` CR by name → map `status`/`spec` → `SessionInfo`; `404`→`None` | No dict; `creator_id` from `spec.creatorId`. |
| `target(id)` | `get` CR; if `phase!=Ready` or no `serviceName` → `None`; else `read` Secret `MARIMO_TOKEN`; build `SessionTarget` | Any replica can build this (see contract) — this is why the token is read from the Secret, not held in memory. |
| `read_source(id)` | returns `None` | Source read-back is a workdir affordance; in kube, edit autosaves persist through the proxied `api/kernel/save` interception (DT-11), so there is no API-readable file. |
| `mark_active(id)` | **coalesced** PATCH of annotation `marimohub.io/last-activity` on the CR (main resource — backend has `patch marimosessions`, never `/status`, per DT-8) | In-process throttle (≤1 PATCH / session / ~15s); calls inside the window early-return with no I/O, so the hot WS relay may call it per frame safely. |
| `stop(id)` | `delete` CR by name; ownerRef GC cascades Pod/Service/Secret; `404`→`SessionNotFoundError` | Full teardown. Idle-*sleep* (Pod-only delete) is the controller's job, not the API's. |
| `shutdown()` | no-op (state is in etcd, survives API restart) | The subprocess backend drains live processes here; kube has nothing local to drain. |

Labels `marimohub.io/{notebook,workspace,mode}` (DT-8) back any list-by-label needs; the public seam
needs no `list`/`sessions` accessor (no caller uses today's `.sessions` property — it is dropped).

##### How callers change

| file | today | after |
|---|---|---|
| `schemas/session.py` | `from ...process_manager import SessionMode` | `from ...session_manager import SessionMode`; `SessionOut` unchanged |
| `api/sessions.py` | `Depends(get_process_manager)`, `ProcessManager`; `manager.get(...)`, `manager.current_source(...)` (sync) | `Depends(get_session_manager)`, `SessionManager`; `await manager.get(...)`, `await manager.read_source(...)`; `(SessionCapacityError, PortAllocationError)`→`SessionCapacityError` only |
| `api/deployments.py` | `ProcessManager`; `manager.target(...)`, `manager.touch(...)`, `manager.spawn_deployment(...)` | `SessionManager`; `await manager.target(...)`, `await manager.mark_active(...)`; drop `PortAllocationError`; `deployment.port = session.port` line removed by **DT-10** with the column |
| `api/proxy.py` | `ProcessManager`; `manager.target(...)`, `manager.touch(...)`, `manager.get(...)` (sync) | `SessionManager`; `await` those; wake/consolidation is **DT-9** |
| `services/marimo_proxy.py` | imports `ProcessManager, SessionTarget` from `process_manager`; `manager.touch(session_id)` (sync, per WS frame) | imports `SessionManager, SessionTarget` from `session_manager`; `await manager.mark_active(session_id)` — safe hot because kube coalesces; final wiring is **DT-9** |
| `main.py` | `shutdown_process_manager()` in lifespan | `shutdown_session_manager()`; reaper removal is **DT-10** |

##### Explicit contracts

- **`spawn` / `spawn_deployment`** — return only a `Ready` `SessionInfo`; a session that never reaches
  readiness raises rather than returning a half-built object. `spawn` validates `mode in {edit, run}`
  (deploy is internal). `spawn_deployment` is **idempotent by CR name** = `deployment_id`: a concurrent
  or repeated call yields the same session, never a second runtime (subprocess: registry check under
  lock; kube: `AlreadyExists` on create). Readiness is bounded by `SESSION_READY_TIMEOUT_SECONDS`.
- **`get` / `target`** — pure reads, no side effects. `get` returns `None` for an unknown id; `target`
  returns `None` when the session exists but is not currently serving (`phase != Ready`, e.g. `Sleeping`
  or `Starting`) — callers treat `None` as "not routable right now". In kube these are stateless: **any
  API replica** answers them from the cluster, which is the crash-safety the CRD buys and the reason the
  `MARIMO_TOKEN` is read from the Secret on demand rather than cached at spawn (this generalises DT-8's
  single-replica "no read-back" note to the multi-replica reality; an optional per-replica token cache is
  a latency optimisation, not a correctness requirement).
- **`read_source`** — best-effort snapshot of the notebook as the runtime last autosaved it; `None` when
  unavailable. Subprocess reads `workdir/notebook.py`; kube returns `None` because persistence flows
  through the proxied save path (DT-11), not a file the API can read. Callers already tolerate `None`.
- **`mark_active`** — idempotent, coalescing, and cheap enough to call on every proxied HTTP request and
  WS frame. Never raises for an unknown/stopped session (silent no-op) so it can never fault a live proxy
  stream. Subprocess: O(1) in-memory timestamp. Kube: throttled annotation PATCH.
- **`stop`** — removes the session and all its runtime resources; raises `SessionNotFoundError` if the id
  is unknown. Idempotency at the caller is via that typed error (`deployments._stop_if_running` suppresses
  it). Subprocess terminates the process and cleans the workdir; kube deletes the CR and lets ownerRef GC
  reap Pod/Service/Secret.
- **Error behaviour (both backends, DT-13 renders).** `SessionCapacityError`→429 (kube: the controller's
  Pod create is rejected by the namespace `ResourceQuota`, surfaced to the poller as a non-scheduling
  terminal condition; subprocess dev backend never raises it — no cap). `NotebookStartupError`→502
  (deterministic exit before ready; kube: `status.phase==Failed` with a container-terminated message).
  `SessionStartError`→503 (ready-timeout without a terminal failure). `SessionNotFoundError`→404. These
  are the same typed errors both backends raise, so DT-13 maps once.
- **Lifecycle assumptions.** Subprocess: sessions live only in-process; an API restart loses them
  (`shutdown` drains). Kube: sessions live in etcd; the API is stateless and horizontally scalable, the
  controller (DT-8) owns idle-sleep/restart/GC, and `shutdown` is a no-op. `edit`/`run` are ephemeral in
  both (deleted on idle by the controller / dropped on process exit); only `deploy` reaches `SLEEPING`.

##### Deletions (by symbol / file)

- **`process_manager.py` — deleted outright.** Redistributed to `subprocess_backend/*` and
  `session_manager.py`; removed with no replacement: `_parse_port_range`, `_allocate_port`,
  `_reserve_port`, `_reserved_ports`, `_pending_spawns`, `_deployment_spawn_locks` and its lock dance
  (`spawn_deployment`'s double-lock), `_max_concurrent_sessions`/capacity check, `MARIMO_HOST`-based port
  probe URL constant usage tied to ranges, `MIN_TCP_PORT`/`MAX_TCP_PORT`, `_SpawnPrep.stderr` plumbing
  stays but moves. `PortAllocationError` class deleted. `ManagedSession.port`/`.pid`, `SessionInfo.port`/
  `.pid` removed. `get_process_manager`/`shutdown_process_manager`/`_manager_state` replaced by
  `get_session_manager`/`shutdown_session_manager` in `session_manager.py`.
- **Config (named here; DT-12 owns the surface):** delete `MARIMO_PORT_RANGE`,
  `MAX_CONCURRENT_SESSIONS`; `MARIMO_READY_TIMEOUT_SECONDS`→`SESSION_READY_TIMEOUT_SECONDS`. Add
  `SESSION_BACKEND: Literal["subprocess","kube"] = "subprocess"` and kube keys `SESSION_NAMESPACE`,
  `MARIMO_RUNTIME_IMAGE`, `SESSION_TOKEN_TTL` (from DT-8). `IDLE_TIMEOUT_MINUTES` removal is DT-10/DT-12.
- **Callers:** `deployments.py:106 deployment.port = session.port` and the `deployment.port = None`
  writes go with the dropped column (DT-1/DT-10). `manager.touch` (proxy ×1, deployments ×1,
  marimo_proxy ×2) → `await manager.mark_active`. `manager.current_source` (sessions ×1) →
  `await manager.read_source`.

##### Dependency to add

`kubernetes-asyncio` (async client over the FastAPI event loop; `CustomObjectsApi` handles the
`marimosessions` CRD, `CoreV1Api` the Secret/Service) — added to `backend/pyproject.toml`
`[project].dependencies`. In-cluster config via `kubernetes_asyncio.config.load_incluster_config()`;
`load_kube_config()` for local `kube` testing.

##### Data flow — caller → seam → backend

```mermaid
flowchart TD
    subgraph callers
      S[api/sessions.py]
      D[api/deployments.py]
      P[api/proxy.py]
      MP[services/marimo_proxy.py]
    end
    S -->|Depends| SM{{SessionManager Protocol}}
    D -->|Depends| SM
    P -->|Depends| SM
    MP -->|await mark_active| SM

    SM -->|SESSION_BACKEND=subprocess| SUB[SubprocessSessionManager]
    SM -->|SESSION_BACKEND=kube| KUBE[KubeSessionManager]

    subgraph subprocess_backend
      SUB --> REG[registry: _sessions dict]
      SUB --> RT[runtime: spawn/terminate + ephemeral port]
      SUB --> WD[workdir: source read/write]
      SUB --> RD[readiness: probe/wait]
      RT --> PROC[(marimo subprocess<br/>127.0.0.1:ephemeral)]
    end

    subgraph kube path
      KUBE -->|create/get/delete/patch CR + Secret| API[(kube-apiserver / etcd)]
      API -. reconcile .-> CTL[marimohub-operator]
      CTL --> POD[(session Pod<br/>msess-id.svc:8080)]
      KUBE -->|target: serviceName + MARIMO_TOKEN| POD
    end
```

Subprocess path (edit/run create): `sessions.create_session` → `await manager.spawn` → registry reserves
an ephemeral port → runtime starts marimo → readiness waits → `SessionInfo(READY)`; proxy resolves
`await manager.target` → loopback URL. Kube path (deploy wake): `deployments._wake_deployment` →
`await manager.spawn_deployment` → `create` CR (or wake a `Sleeping` one via Secret-refresh + wake
annotation) → poll `status.phase` → `Ready`; `await manager.target` reads `serviceName` + Secret token →
`http://msess-{id}.svc:8080/api/deployments/{slug}`.

##### Rejected alternatives

- **Keep `port`/`pid` on `SessionInfo` and let the kube backend fill dummies.** Rejected: they are
  subprocess leakage; the seam should not carry fields one backend must fake. Ephemeral OS ports keep the
  subprocess backend working without a range allocator or the field.
- **A single unified `create(mode, name)` replacing `spawn`/`spawn_deployment`.** Rejected: the two differ
  in name policy (uuid4 vs deployment-id idempotency) and base-url derivation; two named methods match the
  existing caller shape and make the idempotency barrier explicit rather than a flag.
- **`lightkube` instead of `kubernetes-asyncio`.** Rejected as the default: `kubernetes-asyncio` is the
  spec-doc steer and covers Core + Custom objects with one dependency; `lightkube` is a lighter typed
  option worth revisiting if the client surface stays small (flagged, not adopted).

##### Open questions

- **`read_source` in kube leaves `POST /api/sessions/{id}/save` inert.** With autosave persisted through
  the proxied `api/kernel/save` path (DT-11), the explicit save endpoint has nothing to read on the kube
  backend. DT-10/DT-11 must decide whether the endpoint survives (proxy-driven) or is removed; DT-7 keeps
  `read_source` on the seam for the subprocess backend and flags the redundancy.
- **`mark_active` throttle vs. idle accuracy.** A ~15s coalescing window means `status.lastActivity` can
  lag real traffic by up to the throttle interval, which the controller's idle check must tolerate
  (`idleTimeoutSeconds` ≫ throttle). Confirm the window with DT-8's idle semantics, or make the throttle a
  config knob (DT-12).
- **Capacity signal fidelity in kube.** `SessionCapacityError`→429 depends on the manager distinguishing a
  `ResourceQuota` Pod-create rejection from other non-ready states while polling `status`. If the
  controller does not surface a machine-readable quota reason in `status.message`/a CR Event, the manager
  can only time out into `SessionStartError` (503). Flagged for DT-8/DT-10 to guarantee a quota condition
  on the CR.

---

### DT-8 — MarimoSession CRD, controller, and internal source endpoint
**Status:** complete

Design the `MarimoSession` CRD (spec/status per the CRD doc), the reconcile loop (Secret/Service/Pod creation, `Pending/Starting/Ready/Sleeping/Failed` phases, idle handling, wake-on-annotation, ownerRef GC), and the OpenShift constraints (dedicated `marimohub-sessions` namespace, `restricted-v2` SCC compliance, NetworkPolicy, ResourceQuota/LimitRange). Recommend the controller stack (kubebuilder/Go vs kopf/Python) with rationale. Design the backend-side internal endpoint `GET /api/internal/notebooks/{id}/source` (service-account-token auth, NetworkPolicy-restricted) that the init container fetches, replacing the tempdir write.

- **Acceptance criteria:** finalised CRD schema; reconcile pseudocode covering create/ready/fail/idle/wake/delete; per-session Pod/Service/Secret manifests aligned to the SCC; controller-stack recommendation; internal source endpoint contract (auth, path, response, error cases). Note the controller lives outside `backend/app` (separate component/repo) while the endpoint is in the backend.
- **Likely files:** `marimosession-crd-spec.md`, new controller component (out of `backend/app`), new `backend/app/api/internal.py`, `backend/app/main.py`.
- **Relates to:** DT-7, DT-10, DT-11.

#### DT-8 Design

Two components move in lockstep here but ship separately. **(1) The controller** — a standalone
Kubernetes operator, outside `backend/app`, that reconciles `MarimoSession` custom resources into a
Secret-consuming Pod + Service and owns every lifecycle transition (idle-sleep, crash-fail,
wake-on-annotation, GC). **(2) The backend's internal endpoint surface** — `backend/app/api/internal.py`,
a new NetworkPolicy-restricted router the session pod calls to fetch its source and persist its data,
authenticated by a per-session **scoped bearer token** the backend mints and binds to exactly one
notebook. The cluster (etcd) is the source of truth; the backend stops managing processes and the
controller stops touching application data.

The single load-bearing decision that DT-3/DT-6 left open is the **service-token mechanism**: it is a
backend-signed, notebook-scoped JWT delivered via the per-session Secret, resolved below.

##### Controller component layout (outside `backend/app`)

A separate repository / module `marimohub-operator/` (Go, kubebuilder scaffold), deployed as one
Deployment in the `marimohub-controller` namespace with a ServiceAccount granted a namespaced Role in
`marimohub-sessions`:

```
marimohub-operator/                 # separate repo; NOT in backend/app
├── api/v1alpha1/
│   ├── marimosession_types.go      # Spec/Status structs → CRD generated via controller-gen
│   └── groupversion_info.go        # group marimohub.io, version v1alpha1
├── internal/controller/
│   └── marimosession_controller.go # Reconcile(): the loop below
├── internal/session/
│   ├── secret.go                   # Secret precondition check (backend-owned; see auth)
│   ├── pod.go                      # buildPod() — restricted-v2 compliant spec
│   └── service.go                  # buildService()
├── config/crd/                     # generated CRD manifest (marimosessions.marimohub.io)
├── config/rbac/                    # Role: get/list/watch/create/delete pods,services; get secrets;
│                                   #        marimosessions + marimosessions/status full
├── config/samples/
└── config/manager/                 # controller Deployment
```

**RBAC (namespaced to `marimohub-sessions`):** `marimosessions` + `marimosessions/status` (all
verbs); `pods`, `services` (get/list/watch/create/delete); `secrets` (get/list/watch — read only, the
backend owns Secret writes); `events` (create). No cluster-scoped grants; no `secrets` write; no SCC
elevation (session pods run under the namespace-default `restricted-v2`).

##### Finalised CRD schema

Extends the spec doc with a **finalizer**, the **wake annotation** convention, a `Pending` initial
phase, `status.podName`, and `status.observedGeneration` for staleness detection. `spec` is immutable
after create except `idleTimeoutSeconds` and `resources` (the backend never mutates
`notebookId`/`mode`).

```yaml
apiVersion: apiextensions.k8s.io/v1
kind: CustomResourceDefinition
metadata:
  name: marimosessions.marimohub.io
spec:
  group: marimohub.io
  scope: Namespaced
  names: { kind: MarimoSession, plural: marimosessions, singular: marimosession, shortNames: [msess] }
  versions:
    - name: v1alpha1
      served: true
      storage: true
      subresources: { status: {} }
      additionalPrinterColumns:
        - { name: Mode,     type: string, jsonPath: .spec.mode }
        - { name: Phase,    type: string, jsonPath: .status.phase }
        - { name: Notebook, type: string, jsonPath: .spec.notebookId }
        - { name: Service,  type: string, jsonPath: .status.serviceName }
        - { name: Age,      type: date,   jsonPath: .metadata.creationTimestamp }
      schema:
        openAPIV3Schema:
          type: object
          required: [spec]
          properties:
            spec:
              type: object
              required: [notebookId, workspaceId, mode]
              properties:
                notebookId:  { type: string, format: uuid }
                workspaceId: { type: string, format: uuid }
                creatorId:   { type: string, format: uuid, nullable: true }
                mode:        { type: string, enum: [edit, run, deploy] }
                image:
                  type: string
                  description: Marimo runtime image; defaults from controller config when empty.
                baseUrl:
                  type: string
                  description: Proxy path prefix marimo is served under; maps to --base-url.
                idleTimeoutSeconds: { type: integer, default: 600, minimum: 30 }
                resources:
                  type: object
                  x-kubernetes-preserve-unknown-fields: true
                  description: corev1.ResourceRequirements passthrough.
            status:
              type: object
              properties:
                phase:              { type: string, enum: [Pending, Starting, Ready, Sleeping, Failed] }
                serviceName:        { type: string }
                podName:            { type: string }
                lastActivity:       { type: string, format: date-time }
                message:            { type: string }
                observedGeneration: { type: integer }
```

**Conventions layered on the schema (not new fields):**
- **Finalizer** `marimohub.io/session-cleanup` is added on first reconcile; the owned Pod/Service GC
  via ownerRefs, so the finalizer exists only to let the controller emit a terminal Event / drain
  before the object vanishes. It is removed once cleanup is acknowledged.
- **Wake annotation** `marimohub.io/wake: "<rfc3339>"` — set by the backend gateway to wake a
  `Sleeping` deploy session; the controller consumes and clears it.
- **Activity annotation** `marimohub.io/last-activity: "<rfc3339>"` — written by the gateway on
  proxied traffic (a cheap main-resource patch, no `status` RBAC for the API), promoted into
  `status.lastActivity` by the controller each reconcile. This resolves the spec's "PATCH status *or*
  annotation" choice in favour of the annotation: the backend gets `patch marimosessions` only, never
  `marimosessions/status`.

CR name = **deployment id** for `mode: deploy` (idempotent create is the dedup barrier that replaces
`spawn_deployment`'s lock dance) and **uuid4** for ephemeral `edit`/`run`. Labels
`marimohub.io/{notebook,workspace,mode}` drive the backend's list-by-label lookups (DT-7).

##### Per-session manifests (restricted-v2 SCC compliant)

The controller renders these from the CR. All three carry `ownerReferences: [MarimoSession]` for
cascade GC. **The Secret is authored by the backend, not the controller** (see auth); the controller
treats it as a precondition.

```yaml
# ── Secret: msess-<id>-env — WRITTEN BY THE BACKEND (KubeSessionManager.create, DT-7),
#    ownerRef'd to the CR for GC. Controller only reads it / gates the Pod on its presence. ──
apiVersion: v1
kind: Secret
metadata:
  name: msess-7f3e9a2c-env
  namespace: marimohub-sessions
  ownerReferences: [ { apiVersion: marimohub.io/v1alpha1, kind: MarimoSession, name: 7f3e9a2c-…, uid: … } ]
type: Opaque
stringData:
  MARIMO_TOKEN:  <random 32B urlsafe>     # marimo --token-password; the runtime access gate
  SESSION_TOKEN: <backend-signed scoped JWT>  # Authorization: Bearer to /api/internal/* (see auth)
---
apiVersion: v1
kind: Pod
metadata:
  name: msess-7f3e9a2c
  namespace: marimohub-sessions
  labels: { app: marimo-session, marimohub.io/session: 7f3e9a2c-… }
  ownerReferences: [ { … MarimoSession 7f3e9a2c-… } ]
spec:
  automountServiceAccountToken: false          # pod holds no k8s identity; auth is the SESSION_TOKEN
  restartPolicy: Always
  securityContext:
    runAsNonRoot: true                          # SCC assigns the uid from the namespace range — do NOT pin
    seccompProfile: { type: RuntimeDefault }
  initContainers:
    - name: fetch-source
      image: <spec.image | controller default>
      command: ["sh", "-c"]
      args:
        - >
          curl -sf --retry 5 --retry-connrefused
          -H "Authorization: Bearer $SESSION_TOKEN"
          "$API_URL/api/internal/notebooks/$NOTEBOOK_ID/source"
          -o /work/notebook.py
      env:
        - { name: API_URL,     value: "http://marimohub-api.marimohub.svc:8000" }
        - { name: NOTEBOOK_ID, value: "4c1d…" }                      # = spec.notebookId
      envFrom: [ { secretRef: { name: msess-7f3e9a2c-env } } ]      # SESSION_TOKEN, MARIMO_TOKEN
      securityContext:
        allowPrivilegeEscalation: false
        capabilities: { drop: [ALL] }
        readOnlyRootFilesystem: true
      volumeMounts: [ { name: work, mountPath: /work } ]
  containers:
    - name: marimo
      image: <spec.image | controller default>
      args:
        - marimo
        - run                                   # or: edit (mode-dependent)
        - /work/notebook.py
        - --host=0.0.0.0
        - --port=8080
        - --token-password=$(MARIMO_TOKEN)
        - --base-url=$(BASE_URL)
        - --headless
      env:
        - { name: BASE_URL, value: "/api/deployments/plasma-dashboard" }  # = spec.baseUrl
      envFrom: [ { secretRef: { name: msess-7f3e9a2c-env } } ]
      ports: [ { containerPort: 8080 } ]
      readinessProbe:
        tcpSocket: { port: 8080 }               # TCP, not httpGet: marimo's root is token-gated (403≠ready)
        periodSeconds: 2
        failureThreshold: 3
      resources: { … from spec.resources … }
      securityContext:
        allowPrivilegeEscalation: false
        capabilities: { drop: [ALL] }
        readOnlyRootFilesystem: true            # /work (emptyDir) is the only writable path
      volumeMounts: [ { name: work, mountPath: /work } ]
  volumes: [ { name: work, emptyDir: {} } ]
---
apiVersion: v1
kind: Service
metadata:
  name: msess-7f3e9a2c
  namespace: marimohub-sessions
  ownerReferences: [ { … MarimoSession 7f3e9a2c-… } ]
spec:
  selector: { marimohub.io/session: 7f3e9a2c-… }
  ports: [ { port: 8080, targetPort: 8080 } ]
```

The gateway proxies to `http://msess-<id>.marimohub-sessions.svc:8080<baseUrl>`. Every session listens
on 8080 behind its own Service — the port allocator, `_reserved_ports`, and `MARIMO_PORT_RANGE`
disappear. **Readiness is a TCP-socket probe**, not the spec doc's `httpGet <baseUrl>/health`: marimo's
routes sit behind `--token-password`, so an unauthenticated `httpGet` returns 403 (which the probe
reads as *not* ready) — a TCP probe is the token-agnostic, SCC-safe correct signal that the server is
listening (see open questions if marimo later exposes an unauthenticated health route).

##### Namespace guardrails (`marimohub-sessions`)

```yaml
# ── default-deny, then allow only gateway↔session and session→API egress ──
apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata: { name: session-isolation, namespace: marimohub-sessions }
spec:
  podSelector: { matchLabels: { app: marimo-session } }
  policyTypes: [Ingress, Egress]
  ingress:                                       # only the API/gateway may reach a session on 8080
    - from: [ { namespaceSelector: { matchLabels: { kubernetes.io/metadata.name: marimohub } },
                podSelector: { matchLabels: { app: marimohub-api } } } ]
      ports: [ { port: 8080, protocol: TCP } ]
  egress:
    - to: [ { namespaceSelector: { matchLabels: { kubernetes.io/metadata.name: marimohub } },
              podSelector: { matchLabels: { app: marimohub-api } } } ]
      ports: [ { port: 8000, protocol: TCP } ]   # /api/internal/* (source fetch, data persist)
    - to: [ { namespaceSelector: {} } ]          # DNS
      ports: [ { port: 53, protocol: UDP }, { port: 53, protocol: TCP } ]
    # + optional egress to internal PyPI if notebooks install deps (out of v1alpha1 scope)
---
apiVersion: v1
kind: ResourceQuota                              # replaces MAX_CONCURRENT_SESSIONS as the hard cap
metadata: { name: session-quota, namespace: marimohub-sessions }
spec:
  hard: { pods: "50", requests.cpu: "12", requests.memory: 24Gi, limits.cpu: "50", limits.memory: 100Gi }
---
apiVersion: v1
kind: LimitRange                                 # per-pod defaults/ceilings so a CR can't request the world
metadata: { name: session-limits, namespace: marimohub-sessions }
spec:
  limits:
    - type: Container
      default:        { cpu: "1",   memory: 2Gi }
      defaultRequest: { cpu: 250m,  memory: 512Mi }
      max:            { cpu: "2",   memory: 4Gi }
```

A quota-exceeded Pod create surfaces to the backend as a `create` failure on the CR's Pod — DT-7's
`SessionCapacityError` maps to it, giving a clean **429** instead of the old in-process
`MAX_CONCURRENT_SESSIONS` gate. No `Route` per session — only the gateway is exposed externally.

##### Reconcile loop (create / ready / fail / idle / wake / delete)

```
Reconcile(req):
  cr = get MarimoSession(req)                      # not found → nothing to do (already GC'd)
  if cr is None: return done

  # ── delete path ──────────────────────────────────────────────────────────
  if cr.deletionTimestamp is set:
      # Pod/Service GC automatically via ownerRefs; Secret GCs with the CR (its owner).
      emit Event("SessionDeleted"); remove finalizer "marimohub.io/session-cleanup"
      return done

  ensure finalizer "marimohub.io/session-cleanup" present            # else add + return (requeue)
  cr.status.observedGeneration = cr.generation
  promote annotation marimohub.io/last-activity → status.lastActivity  # gateway's cheap activity edge

  # ── Secret precondition (backend-owned) ──────────────────────────────────
  secret = get Secret("msess-<id>-env")
  if secret is None:
      status.phase = Pending; status.message = "awaiting session credentials"
      return requeue(2s)                            # backend writes it right after CR create

  ensure Service("msess-<id>") exists (ownerRef cr)   # idempotent create
  status.serviceName = "msess-<id>"

  # ── wake gate for sleeping deploy sessions ───────────────────────────────
  wake = annotation marimohub.io/wake present
  pod  = get Pod("msess-<id>")

  if pod is None:
      if cr.spec.mode == deploy and status.phase == Sleeping and not wake:
          return done                              # stay asleep; no requeue until API bumps wake
      if wake: clear annotation marimohub.io/wake
      create Pod (ownerRef cr, from buildPod(cr, secret))
      status.phase = Starting; status.podName = pod.name
      return requeue(2s)

  # ── running pod: derive phase from pod status ────────────────────────────
  if pod.Ready:
      status.phase = Ready
  elif pod terminated non-zero OR CrashLoopBackOff:
      status.phase = Failed
      status.message = tail(pod.containerStatuses…terminated.message)
      return done                                  # deterministic failure: do NOT recreate
  else:
      status.phase = Starting
      return requeue(2s)

  # ── idle handling (only meaningful once Ready) ───────────────────────────
  if status.lastActivity and (now - status.lastActivity) > cr.spec.idleTimeoutSeconds:
      if cr.spec.mode == deploy:
          delete Pod("msess-<id>")                 # CR + Secret + Service persist; wake on demand
          status.phase = Sleeping; status.podName = ""
      else:
          delete MarimoSession(cr)                 # edit/run are ephemeral; delete path GCs the rest
      return done

  return requeue(30s)
```

**Wake path in full:** the gateway (DT-9), seeing `status.phase == Sleeping`, first asks the backend
to **refresh `SESSION_TOKEN` in the Secret** (so the restarted pod boots with a non-expired token),
then patches `marimohub.io/wake` on the CR and polls `status.phase` until `Ready` (bounded by the same
ready-timeout the subprocess model used). The reconcile above recreates the Pod, clears the annotation,
and drives `Starting → Ready`.

##### Internal endpoint surface — `backend/app/api/internal.py`

New router `prefix="/api/internal"`, wired in `main.py` (`app.include_router(internal_router)`). Three
endpoints, all guarded by one dependency that validates the session token **and binds it to the path
notebook**. Two of them (data write + read-back) are the DT-6 handoff; the source endpoint replaces the
tempdir write.

```python
# ── auth: scoped session token (see "Service-token auth contract" below) ────
@dataclass(frozen=True, slots=True)
class SessionPrincipal:
    session_id: UUID
    notebook_id: UUID

def require_session_notebook(notebook_id: UUID, token: str = Depends(session_bearer)) -> SessionPrincipal:
    """Validate the SESSION_TOKEN JWT and require it be bound to `notebook_id`.
    401 on missing/invalid/expired token; 403 when the token is valid but bound
    to a different notebook. No DB or k8s I/O — a signature + claim check."""
    principal = decode_session_token(token)          # verify sig, exp, typ == "session"
    if principal is None:
        raise HTTPException(401, "Invalid session token", headers={"WWW-Authenticate": "Bearer"})
    if principal.notebook_id != notebook_id:
        raise HTTPException(403, "Session token not scoped to this notebook")
    return principal

SessionBound = Annotated[SessionPrincipal, Depends(require_session_notebook)]

# 1) SOURCE — init container fetch, replaces _prepare_workdir's tempdir write
@router.get("/notebooks/{notebook_id}/source", response_class=PlainTextResponse)
async def read_source(notebook_id: UUID, _: SessionBound, db, storage) -> PlainTextResponse:
    notebook = await db.get(Notebook, notebook_id)
    if notebook is None:
        raise HTTPException(404, "Notebook not found")
    return PlainTextResponse(await storage.get(notebook) or "", media_type="text/x-python")

# 2) DATA WRITE — relocated from the removed public POST /{id}/data (DT-6)
@router.post("/notebooks/{notebook_id}/data",
             response_model=NotebookDataCreated, status_code=201)
async def write_data(notebook_id: UUID, _: SessionBound, db,
                     payload: Annotated[JsonValue, Body()],
                     source: Annotated[str | None, Query(max_length=255)] = None) -> NotebookDataCreated:
    data = NotebookData(notebook_id=notebook_id, payload=payload, source=source)
    db.add(data); await db.commit(); await db.refresh(data)
    return NotebookDataCreated(id=data.id)

# 3) DATA READ-BACK — pod reads its own state, no public visibility gate
@router.get("/notebooks/{notebook_id}/data", response_model=NotebookDataOut)
async def read_data(notebook_id: UUID, _: SessionBound, db) -> NotebookDataOut:
    data = await db.scalar(
        select(NotebookData).where(NotebookData.notebook_id == notebook_id)
        .order_by(NotebookData.created_at.desc(), NotebookData.id.desc()).limit(1))
    if data is None:
        raise HTTPException(404, "Notebook data not found")
    return NotebookDataOut.model_validate(data, from_attributes=True)
```

`session_bearer` is an `HTTPBearer(auto_error=False)` scheme (distinct from `deps.oauth2_scheme`, which
resolves *user* JWTs). `NotebookData`, `NotebookDataCreated`, `NotebookDataOut` are reused verbatim from
`schemas/data.py` — DT-6 kept them for exactly this.

##### Service-token auth contract (resolves the DT-3/DT-6 open item)

The session token is a **backend-signed, notebook-scoped JWT**, minted by the backend (never the
controller) and delivered through the per-session Secret. Reusing `core/security.py`'s HS256 +
`SECRET_KEY` means no new dependency and no signing key in the controller.

- **Mint** (backend, at `KubeSessionManager.create` and at every wake): `create_session_token(session_id,
  notebook_id)` → HS256 JWT `{ "typ": "session", "sid": <session_id>, "nid": <notebook_id>, "iat": …,
  "exp": now + SESSION_TOKEN_TTL }`. The `typ: "session"` claim makes it un-substitutable for a user
  token (and vice-versa) even though both are HS256/`SECRET_KEY`.
- **Deliver:** written into `Secret msess-<id>-env` key `SESSION_TOKEN`; the Pod's init + main
  containers receive it via `envFrom`. Because the backend authors the Secret, it also holds
  `MARIMO_TOKEN` and needs no read-back to build `SessionTarget` (DT-7).
- **Verify** (backend internal endpoints): `decode_session_token` checks signature, `exp`, and
  `typ == "session"`, yielding `SessionPrincipal(session_id, notebook_id)`. The endpoint then requires
  `principal.notebook_id == path notebook_id`. **Notebook binding is intrinsic to the token** — a
  session serving notebook A cannot read or write notebook B's source/data even if it reaches the URL,
  with zero DB/k8s calls on the hot path.
- **Defense in depth:** the NetworkPolicy already means only session pods can reach `/api/internal/*`;
  the token adds per-notebook scoping on top, so a compromised pod is confined to its own notebook.
- **Why not the k8s ServiceAccount token / TokenReview** (the task's literal "service-account-token"):
  rejected below — it needs per-request cluster I/O and a SA→CR→notebook lookup to achieve the binding
  the JWT gets from one claim.

New config (DT-12 owns the surface; named here): `SESSION_TOKEN_TTL` (default e.g. 24h; always re-minted
fresh at pod start/wake so a bounded TTL never bites an active pod). `core/security.py` gains
`create_session_token` / `decode_session_token` beside the existing user-token helpers — the DT-4 JWT
seam guarantee is unaffected (user tokens are byte-identical; this is an additive, disjoint token type).

##### Explicit contracts

- **Phase-transition invariants.** `Pending → Starting → Ready` on the happy path; `Ready → Sleeping`
  only for `mode: deploy` on idle (Pod deleted, CR/Secret/Service retained); `Sleeping → Starting` only
  when the wake annotation is present; any state `→ Failed` on a deterministic non-zero container exit
  or CrashLoopBackOff (no auto-recreate — the backend surfaces it and the user must redeploy). `edit`/`run`
  never reach `Sleeping`: on idle they are deleted outright. `observedGeneration` always trails the
  reconciled `metadata.generation`, so a consumer can detect a not-yet-reconciled spec change.
- **GC / ownership guarantees.** Pod, Service, and Secret each carry an ownerReference to the
  `MarimoSession`; deleting the CR cascades all three via Kubernetes GC — no orphan cleanup code. The
  finalizer only gates a terminal Event, never resource deletion. Deleting a Pod alone (idle-sleep)
  leaves the CR/Secret/Service intact so a wake is a pure Pod re-create. CR name = deployment id makes
  `create` idempotent: a duplicate deploy request is an `AlreadyExists` no-op, not a second pod.
- **Auth contract (internal endpoints).** Every `/api/internal/*` call requires a valid `SESSION_TOKEN`
  bound to the path `notebook_id`. 401 = missing/invalid/expired/wrong-`typ`; 403 = valid token bound to
  a *different* notebook; 404 = notebook (or, for read-back, its data) absent. There is **no visibility
  gate** — the token *is* the authorization, and the pod is trusted to act only on the notebook it was
  minted for. Anonymous end-users of a public deployment never authenticate here; their data is
  persisted by the pod under the pod's token.
- **Source endpoint.** `GET …/source` returns `200 text/x-python` with the stored source, or the empty
  string for a notebook with no source yet (a fresh `edit` session) — never 404 for "empty", only for
  "no such notebook". Idempotent, read-only, safe to `--retry` from the init container.
- **Error behaviour.** Deterministic pod failures set `status.phase=Failed` + `status.message` and stop;
  transient absences (evicted pod, node loss) reconcile back to `Starting` because the Pod is simply
  re-created when missing and not `Failed`. Quota exhaustion is a Pod-create error the backend renders
  as 429.

##### Deletions (what this obsoletes — coordinated, not designed here)

- **DT-7** deletes `process_manager.py`'s `_prepare_workdir`, `_session_workdir`,
  `_write_marimo_project_config`, the `secrets.token_urlsafe` mint, the tempdir `notebook.py` write, and
  `MARIMO_PORT_RANGE`/`_reserved_ports`/`_allocate_port`. This design supplies their replacements (init
  container source fetch; backend-minted `SESSION_TOKEN`/`MARIMO_TOKEN` in the Secret; Service-per-session
  on 8080). DT-8 names them; DT-7 removes them.
- **DT-6** already removed the public `POST /api/notebooks/{id}/data`; DT-8 lands its replacement
  (`POST /api/internal/notebooks/{id}/data`) and the internal read-back. `create_notebook_data` in
  `data.py` is gone; `data.py` keeps only the READ-gated public GET.
- **DT-10** deletes `deployment_lifecycle.py` (`IdleDeploymentReaper`, `mark_running_deployments_sleeping`
  boot reset) — its idle/restart/orphan responsibilities are the reconcile loop above.
- **DT-11** owns the `NotebookStorageService` seam the source endpoint reads through; DT-8 consumes
  `storage.get(notebook)` and does not redesign it.
- `main.py` lifespan loses `start_deployment_lifecycle` / `shutdown_process_manager` (DT-10/DT-7); it
  gains only `app.include_router(internal_router)`.

##### Data flow — create → ready → proxy → idle → wake

```mermaid
sequenceDiagram
    participant API as backend (KubeSessionManager)
    participant K as kube-apiserver (etcd)
    participant Ctl as marimohub-operator
    participant Pod as session Pod
    participant GW as gateway (marimo_proxy)

    API->>K: create MarimoSession (name=deployment id)
    API->>K: create Secret msess-<id>-env {MARIMO_TOKEN, SESSION_TOKEN(nid)} ownerRef=CR
    Ctl->>K: reconcile → ensure Service; create Pod (envFrom Secret); phase=Starting
    Pod->>API: GET /api/internal/notebooks/{nid}/source  (Bearer SESSION_TOKEN)
    API-->>Pod: 200 text/x-python (source)
    Pod->>Pod: marimo run /work/notebook.py --token-password=$MARIMO_TOKEN
    Ctl->>K: pod Ready → status.phase=Ready, serviceName set
    GW->>K: resolve status.serviceName + MARIMO_TOKEN
    GW->>Pod: proxy http/ws to msess-<id>.svc:8080<baseUrl>
    GW->>K: patch annotation marimohub.io/last-activity=now (per traffic)
    Note over Pod,Ctl: idle > idleTimeoutSeconds
    Ctl->>K: delete Pod; phase=Sleeping (CR/Secret/Service retained)
    Note over GW,API: request arrives for a Sleeping deploy
    GW->>API: refresh SESSION_TOKEN in Secret
    GW->>K: patch annotation marimohub.io/wake=now
    Ctl->>K: recreate Pod; phase=Starting → Ready
    GW->>Pod: proxy resumes
```

##### Rejected alternatives

- **Kubernetes ServiceAccount token + TokenReview for internal auth** (the task's literal wording).
  Rejected: notebook binding would require a per-request `TokenReview` (or JWKS verify) plus a
  SA→session→CR→`notebookId` lookup, i.e. cluster I/O on every source/data call; a backend-signed JWT
  carries `nid` in one claim and verifies with a signature check and no I/O.
- **Controller mints the session token / creates the Secret** (as the spec doc implies). Rejected: the
  binding token must be signed with the backend's `SECRET_KEY`, and putting that key in the controller
  couples two components and widens the blast radius; the backend already has cluster access (DT-7), so
  it owns Secret authorship and the controller merely gates on the Secret's presence.
- **kopf (Python) controller in the monorepo.** Rejected in favour of **kubebuilder/controller-runtime
  (Go)**: the CRD types generate the schema and typed clients via `controller-gen`, controller-runtime's
  cache/workqueue/leader-election are battle-tested for the idle/wake requeue model, and it matches the
  team's existing Go operator experience. kopf's appeal was monorepo simplicity, but the controller is a
  separate deploy artifact regardless.

##### Open questions

- **Marimo readiness signal.** The design uses a TCP-socket probe because marimo's HTTP routes are
  token-gated. If a given marimo version exposes an *unauthenticated* `<baseUrl>/health`, an `httpGet`
  probe would catch app-level readiness (not just a listening socket) — worth confirming against the
  pinned runtime image; TCP is the safe default until then.
- **`/api/internal` exposure.** Baseline is one ASGI app with the internal router protected by
  NetworkPolicy + token. A stronger posture is a second listener/port bound only to the cluster network
  (never the gateway Route) so `/api/internal/*` is physically unreachable from outside. Flagged as a
  hardening decision for DT-12 (config/runtime surface).
- **`SESSION_TOKEN_TTL` vs. long deploy sleeps.** Re-minting on wake covers it, but if a wake races a
  just-expired token there is a one-reconcile retry. Confirm the TTL (24h default) and the refresh-on-wake
  ordering are acceptable, or make the token effectively non-expiring and rely on Secret GC for
  revocation.

---

### DT-9 — Proxy / gateway consolidation
**Status:** complete

Design one gateway abstraction unifying the near-duplicate HTTP and WebSocket proxy paths in `proxy.py` (sessions) and `deployments.py` (deployments): target resolution, activity update, `build_target_url`, `forward_http`, `relay_websocket`. Fold in the wake-on-request behaviour so "resolve-or-wake → mark active → proxy" is expressed once, and isolate the two entry points (session id vs deployment slug) to a small resolver. Define the activity edge so it works for both the subprocess `touch` and the CRD `status.lastActivity`/annotation model.

- **Acceptance criteria:** a single gateway module covering HTTP + WS for both entry points, with session/deployment differences isolated to a resolver; elimination of the duplicated `deployment_ws`/`proxy_ws` and `deployment_http`/`proxy_http` bodies; an activity-tracking seam expressible for both backends.
- **Likely files:** `backend/app/services/marimo_proxy.py`, `backend/app/api/{proxy,deployments}.py`.
- **Depends on:** DT-7.

#### DT-9 Design

The two proxy paths (`proxy.py` sessions, `deployments.py` deployments) are the same three steps —
**resolve-or-wake → mark active → forward/relay** — differing only in how the entry-point key becomes a
routable session. DT-9 makes `marimo_proxy.py` the single **gateway module** that expresses those three
steps exactly once for HTTP and once for WS, and pushes the per-entry-point difference behind one tiny
`Resolver` seam. Each router keeps its concrete resolver (where the domain knowledge already lives) and
shrinks to a two-line route. `marimo_proxy.py` stays domain-neutral — it imports only `SessionManager`/
`SessionTarget` (DT-7), never `Deployment`/`Notebook`.

##### Serving-access decision (DT-3 handoff, recorded)

**Deployment serving via slug stays public and unauthenticated; deployment *management* (deploy/delete)
hides for non-members (DT-3, 404).** The slug **is** the shareable public URL of a published app, so
`GET/POST … /api/deployments/{slug}` carries no authz and no `get_current_user_optional` dependency (the
current unused `_current_user` param is deleted). This is deliberately asymmetric with management, which
routes through DT-3's `authorize_notebook(…, WRITE)` and 404s the notebook for non-members.

**Session serving is capability-based, not policy-based.** `/api/proxy/{session_id}/…` carries no authz:
`session_id` is an unguessable `uuid4` minted at `POST /api/sessions` to an already-authorized creator, so
holding it *is* the authorization. The gateway preserves this — `SessionResolver` takes no actor. If
per-request session authz is ever wanted it is a resolver concern (it can consult DT-3's `get_role` /
DT-10's creator check), never the gateway's. Both decisions are final for DT-9.

##### Target design — the gateway (`backend/app/services/marimo_proxy.py`)

```python
PROXY_METHODS = ["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"]   # unchanged; routers register it

# ── resolution result + the one per-entry-point seam ────────────────────────
@dataclass(frozen=True, slots=True)
class GatewayRoute:
    session_id: UUID          # resolved runtime id — the mark_active + WS-relay key
    target: SessionTarget     # upstream URLs + access token (DT-7, three-field shape unchanged)

class Resolver(Protocol):
    """Map one entry-point key to a routable session, waking it if the backend can sleep.
    Raise a GatewayError when no routable upstream can be produced."""
    async def resolve(self) -> GatewayRoute: ...

# ── typed errors — DT-13 renders HTTP; ws_close_code is gateway-owned ────────
class GatewayError(Exception):
    detail: str
    http_status: int          # consumed by DT-13's exception handler
    ws_close_code: int        # consumed by proxy_websocket (WS has no HTTP status)

class UpstreamNotFound(GatewayError):    # 404 / WS 1008 — unknown session id, unknown/stopped slug
class UpstreamNotReady(GatewayError):    # 503 / WS 1011 — exists but not serving after a wake attempt
class UpstreamUnreachable(GatewayError): # 502 / WS 1011 — transport error reaching a resolved target

# ── the ONE place "resolve-or-wake → mark active → proxy" lives ─────────────
async def proxy_http(
    request: Request,
    manager: SessionManager,
    resolver: Resolver,
    path: str,
    *,
    response_body_callback: ResponseBodyCallback | None = None,   # DT-11 injects the edit-save hook here
    follow_redirects: bool = False,
) -> Response:
    route = await resolver.resolve()               # resolve-or-wake (raises GatewayError / SessionManagerError)
    await manager.mark_active(route.session_id)    # activity edge — one call, both backends (DT-7)
    return await _forward_http(
        request, route.target, path,
        response_body_callback=response_body_callback,
        follow_redirects=follow_redirects,
    )

async def proxy_websocket(
    websocket: WebSocket, manager: SessionManager, resolver: Resolver
) -> None:
    try:
        route = await resolver.resolve()
    except UpstreamNotFound:
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
        return
    except (GatewayError, SessionManagerError):    # not-ready / unreachable / capacity / start failure
        await websocket.close(code=status.WS_1011_INTERNAL_ERROR)
        return
    await manager.mark_active(route.session_id)    # connect edge; _relay_websocket marks per frame after
    target_url = _build_target_url(
        route.target.ws_base_url, "ws",
        cast("bytes", websocket.scope.get("query_string", b"")),
        route.target.access_token,
    )
    try:
        await _relay_websocket(websocket, target_url, manager, route.session_id)
    except (ConnectionClosed, WebSocketDisconnect, OSError):
        return

# ── transport internals: unchanged bodies, now private (only the gateway calls them) ─
def _build_target_url(base_url, path, query_string, access_token) -> str: ...        # was build_target_url
async def _forward_http(request, target, path, *, response_body_callback=None,       # was forward_http
                        follow_redirects=False) -> Response:
    #   only change: httpx.HTTPError → `raise UpstreamUnreachable(...)` (was HTTPException 502)
async def _relay_websocket(websocket, target_url, manager, session_id) -> None:      # was relay_websocket
    #   only change: per-frame `manager.touch(id)` → `await manager.mark_active(id)` (DT-7 rename)
```

`ResponseBodyCallback` stays as today. `build_target_url`/`forward_http`/`relay_websocket` lose their
public names — after consolidation nothing outside the module calls them.

##### Resolvers — the isolated entry-point difference

```python
# backend/app/api/proxy.py — edit/run sessions (no sleep, so no wake)
@dataclass(slots=True)
class SessionResolver:
    manager: SessionManager
    session_id: UUID
    async def resolve(self) -> GatewayRoute:
        target = await self.manager.target(self.session_id)
        if target is None:                                  # unknown or not serving → gone
            raise UpstreamNotFound("Session not found")
        return GatewayRoute(self.session_id, target)

# backend/app/api/deployments.py — deploy sessions (sleep + wake-on-request)
@dataclass(slots=True)
class DeploymentResolver:
    db: AsyncSession
    manager: SessionManager
    slug: str
    async def resolve(self) -> GatewayRoute:
        deployment, notebook = await _load_active_deployment(self.db, self.slug)   # UpstreamNotFound if missing/stopped
        target = await self.manager.target(deployment.id)
        if target is None:                                  # sleeping/absent → wake, idempotent by CR name (DT-7)
            await self.manager.spawn_deployment(notebook, deployment.id, deployment.slug)
            target = await self.manager.target(deployment.id)
            if target is None:
                raise UpstreamNotReady("Deployment is unavailable")
        return GatewayRoute(deployment.id, target)
```

`DeploymentResolver` is where "resolve-or-wake" is realized: a cheap `target()` fast-path (the common,
already-awake case — no wake call per proxied asset), then `spawn_deployment` only when not routable.
`spawn_deployment` is DT-7's idempotent, wake-on-annotation entry, so this single call both wakes a
`Sleeping` CR and no-ops a concurrent warm-up — the old `_wake_deployment` lock/fast-path/DB-write dance
collapses into it. `SessionResolver` has no wake branch because edit/run sessions never sleep (the
controller deletes them on idle; the subprocess exits) — a missing target is terminal.

##### The routers after consolidation

```python
# proxy.py — sessions (capability-scoped; db only for DT-11's save hook)
@router.api_route("/{session_id}/{path:path}", methods=marimo_proxy.PROXY_METHODS, name="proxy_http")
async def proxy_http(session_id, path, request, manager=Depends(get_session_manager), db=Depends(get_db)):
    callback = edit_save_callback(manager, db, session_id) \
        if request.method == "POST" and path == "api/kernel/save" else None    # callback body is DT-11
    return await marimo_proxy.proxy_http(
        request, manager, SessionResolver(manager, session_id), path, response_body_callback=callback)

@router.websocket("/{session_id}/ws")
async def proxy_ws(session_id, websocket, manager=Depends(get_session_manager)):
    await marimo_proxy.proxy_websocket(websocket, manager, SessionResolver(manager, session_id))

# deployments.py — public serving (no actor dependency)
@router.websocket("/api/deployments/{slug}/ws")
async def deployment_ws(slug, websocket, db=Depends(get_db), manager=Depends(get_session_manager)):
    await marimo_proxy.proxy_websocket(websocket, manager, DeploymentResolver(db, manager, slug))

@router.api_route("/api/deployments/{slug}", methods=marimo_proxy.PROXY_METHODS, name="deployment_http_root")
@router.api_route("/api/deployments/{slug}/{path:path}", methods=marimo_proxy.PROXY_METHODS, name="deployment_http")
async def deployment_http(slug, request, db=Depends(get_db), manager=Depends(get_session_manager), path=""):
    return await marimo_proxy.proxy_http(
        request, manager, DeploymentResolver(db, manager, slug), path,
        follow_redirects=request.method == "GET" and path == "")
```

The two route-registration shapes are all that stay distinct (path grammar `{session_id}` vs `{slug}`,
plus the deployment root-path pair kept ahead of the management `DELETE`). The `edit_save_callback` body
belongs to DT-11; DT-9 only owns the `response_body_callback` injection point. `follow_redirects` on the
deployment root GET is preserved.

##### Explicit contracts

- **`resolve()` (resolve-or-wake).** Returns a `GatewayRoute` whose `target` is serving *now*, or raises a
  typed error; it never returns a half-open route. Wake is internal to the deployment resolver and
  idempotent (DT-7 `spawn_deployment` by CR name), so concurrent first-hits on a sleeping deployment share
  one wake, never spawn twice. `SessionManagerError`s from the wake (`SessionCapacityError`→429,
  `NotebookStartupError`→502, `SessionStartError`→503) propagate untouched for DT-13 to render.
- **Activity edge.** `await manager.mark_active(route.session_id)` is the sole activity signal, expressed
  once for both backends and both entry points: once per HTTP request in `proxy_http`, once at WS connect
  plus per frame inside `_relay_websocket`. DT-7 guarantees it is idempotent, coalescing (kube throttles
  the annotation PATCH), and never raises for an unknown/stopped session — so it is safe on the hot relay
  loop and can never fault a live stream. The **per-request DB `deployment.last_active` write + commit**
  that the old serving path performed is deleted (a DB write per proxied asset); the read-model mirror of
  activity/status is DT-10's concern, fed from CR status, not written on the proxy hot path.
- **Error behavior (typed; DT-13 renders HTTP).** `UpstreamNotFound`→404 (unknown `session_id`; unknown or
  `STOPPED` deployment slug), `UpstreamNotReady`→503 (woke but still not serving), `UpstreamUnreachable`→502
  (`httpx` transport error against a resolved target). These join DT-3's `AccessError` and DT-7's
  `SessionManagerError` under DT-13's single exception-handler seam; until DT-13 lands the handler is a few
  lines in `main.py`. Routers never build an `HTTPException` for the proxy path.
- **WebSocket lifecycle.** DT-13's HTTP handler cannot render a WS close, so `proxy_websocket` owns the WS
  failure mapping: resolution `UpstreamNotFound`→close `1008` (policy violation), any other resolution
  failure (`UpstreamNotReady`/`UpstreamUnreachable`/`SessionManagerError`)→close `1011` (internal error);
  a mid-stream `ConnectionClosed`/`WebSocketDisconnect`/`OSError` ends the relay silently. `accept()` still
  happens inside `_relay_websocket`, i.e. only after a successful resolve, so a rejected session is closed
  pre-accept exactly as today.

##### Deletions (by file / symbol)

- **`marimo_proxy.py`:** `build_target_url`, `forward_http`, `relay_websocket` demoted to private
  `_build_target_url`/`_forward_http`/`_relay_websocket`; the module gains `Resolver`, `GatewayRoute`, the
  `GatewayError` hierarchy, `proxy_http`, `proxy_websocket`. Import swap (DT-7): `ProcessManager,
  SessionTarget` from `process_manager` → `SessionManager, SessionTarget` from `session_manager`; per-frame
  `manager.touch` → `await manager.mark_active`.
- **`proxy.py`:** the entire `proxy_http` body (inline `manager.target` + `manager.touch` + `forward_http`)
  and the entire `proxy_ws` body (inline `target` + `build_target_url` + `relay_websocket`) — replaced by
  the `Resolver` + `marimo_proxy.proxy_http`/`proxy_websocket` two-liners above. The inline
  `persist_marimo_save` closure moves to DT-11's `edit_save_callback`; DT-9 keeps only its injection.
- **`deployments.py`:** `_wake_deployment` (folded into `DeploymentResolver` via idempotent
  `spawn_deployment`; its `deployment.port`/`status`/`last_active` DB writes go with it — DT-10 owns the
  read-model), the entire `deployment_ws` and `deployment_http` bodies (inline load-wake-commit-target-touch
  -forward/relay), the unused `_current_user: Depends(get_current_user_optional)` param on both serving
  routes. `_load_deployment` is renamed `_load_active_deployment` and raises `UpstreamNotFound` instead of
  `_not_found()`; `deploy_notebook`, `delete_deployment`, the slug helpers, and `_deployment_out_after_conflict`
  are **out of DT-9 scope** (DT-10 lifecycle / DT-3 authz).

##### Data flow — request → resolver → wake → proxy (both entry points)

```mermaid
flowchart TD
    subgraph clients
      CS[client: /api/proxy/&#123;session_id&#125;/…]
      CD[client: /api/deployments/&#123;slug&#125;/…]
    end
    CS --> PR[proxy.py route]
    CD --> DR[deployments.py route]
    PR -->|SessionResolver| GW
    DR -->|DeploymentResolver| GW

    subgraph gw [marimo_proxy.py — single gateway]
      GW{{proxy_http / proxy_websocket}} --> RES[await resolver.resolve]
      RES --> MA[await manager.mark_active session_id]
      MA --> FWD[_forward_http / _relay_websocket]
    end

    subgraph resolve [resolve-or-wake, per entry point]
      SR[SessionResolver.resolve\nawait manager.target] -->|None| SNF[UpstreamNotFound → 404 / WS1008]
      DRr[DeploymentResolver.resolve] --> LD[_load_active_deployment slug]
      LD -->|missing/STOPPED| DNF[UpstreamNotFound → 404 / WS1008]
      LD --> T1[await manager.target]
      T1 -->|routable| OK[GatewayRoute]
      T1 -->|None: sleeping| WK[await manager.spawn_deployment\nidempotent wake — DT-7]
      WK --> T2[await manager.target]
      T2 -->|None| NR[UpstreamNotReady → 503 / WS1011]
      T2 --> OK
      WK -.capacity/start.-> SME[SessionManagerError → 429/502/503]
    end

    RES -.-> SR
    RES -.-> DRr
    FWD --> UP[(session runtime\nSessionTarget — DT-7)]
    MA -.subprocess: in-mem ts / kube: coalesced annotation PATCH.-> UP
```

Session path: `SessionResolver.resolve` → `target` (no wake) → `mark_active` → forward/relay. Deployment
path: `DeploymentResolver.resolve` → load slug → `target` fast-path, else idempotent `spawn_deployment`
wake → re-`target` → `mark_active` → forward/relay. One gateway body serves both.

##### Rejected alternatives

- **Put both resolvers inside `marimo_proxy.py`.** Rejected: it would drag `Deployment`/`Notebook` models
  and DB queries into the transport module; keeping each resolver in its own router leaves the gateway
  domain-neutral and each entry point's knowledge where it already lives.
- **Call `spawn_deployment` unconditionally (no `target` fast-path).** Rejected: every proxied asset would
  hit the wake path even when awake; the cheap `target()` probe first keeps the hot path a single read.
- **One `resolve(kind, key)` function with an `if session/deployment` branch instead of a `Resolver`
  protocol.** Rejected: reintroduces the entry-point conditional the seam exists to remove and blocks DT-11
  from varying only the session path's save hook.

##### Open questions

- **`UpstreamUnreachable` vs DT-13 ownership.** DT-9 defines the three `GatewayError` subtypes and their
  status intents; DT-13 must register the single handler that renders them (and reconcile with the
  identical 502 `forward_http` used to raise inline). Flagged for DT-13 to absorb, not re-decide.
- **Read-model of wake outcome.** Folding `_wake_deployment` into `spawn_deployment` removes the old
  on-failure `status=SLEEPING` DB write. DT-10 must confirm the read-model reflects wake success/failure
  from CR status (read-through) rather than a proxy-path write, so a failed wake never leaves the DB
  showing `RUNNING`.

---

### DT-10 — Deployment/session lifecycle & status source-of-truth
**Status:** complete

Design where deployment/session lifecycle state lives after the clean sweep. Today `IdleDeploymentReaper` (asyncio) + `mark_running_deployments_sleeping` (boot reset) + `deployments.status`/`port`/`last_active` in Postgres form the source of truth; the CRD doc moves idleness/restart/orphan handling to the controller and makes Postgres a read-model cache (read-through first). Define target running/sleeping/stopped semantics, wake-on-request (bounded ready timeout, annotation bump then poll), idle-sleep, and stop flows against the DT-7 seam; rewrite `deployments.py`/`sessions.py` accordingly (remove port/lock/capacity paths and re-key edit-save persistence); confirm the already-port-free `SessionOut`/`DeploymentOut` contracts; specify deletion of `deployment_lifecycle.py` and the boot reset.

- **Acceptance criteria:** stated target source-of-truth (controller CR status vs DB) and what the API keeps as a read-model; per-endpoint changes for `deployments.py`/`sessions.py`; wake/idle/stop flows defined against DT-7; removed error types/paths and files listed (`deployment_lifecycle.py`, `IdleDeploymentReaper`, boot reset, `port`); schema changes stated; authz delegated to DT-3.
- **Likely files:** `backend/app/services/deployment_lifecycle.py` (delete), `backend/app/api/{deployments,sessions}.py`, `backend/app/schemas/{deployment,session}.py`, `backend/app/models/__init__.py`, `backend/app/main.py`, `marimosession-crd-spec.md`.
- **Depends on:** DT-7, DT-8, DT-3.

#### DT-10 Design

Lifecycle state splits cleanly in two after the sweep. **Runtime phase** (is a pod serving, waking,
idled, or crashed) lives **only in the `MarimoSession` CR status** — the controller (DT-8) owns every
transition, and the API reads it through the DT-7 seam (`manager.get`/`manager.target`). **Durable
catalog + intent** (that a deployment exists, its `slug`, and whether the owner has **stopped** it)
lives in the Postgres `deployments` row, which is the only lifecycle fact the cluster cannot hold
(a `stopped` deployment has no CR to reflect it). The row is therefore a durable registry whose
`status` column persists exactly two resting states — `sleeping` and `stopped` — while `running` is
**never written**: it is derived read-through from CR phase `Ready` at the moment a response is built.
The idle reaper, the boot reset, and every per-request DB write on the serving path are deleted.

##### Source-of-truth statement

| Lifecycle fact | Authoritative in | API keeps as |
|---|---|---|
| Runtime phase (`running`/`sleeping`/waking/failed) | CR `status.phase` (controller-owned) | read-through projection at response time |
| Activity / idleness | CR `status.lastActivity` (gateway annotation → controller) | nothing on the hot path; optional `last_active` cache mirror |
| Idle-sleep / restart / orphan GC | Controller reconcile (DT-8) | — (reaper deleted) |
| Deployment existence + `slug` | `deployments` row | durable registry |
| **Stopped** (owner intent, no CR) | `deployments.status = 'stopped'` | authoritative, short-circuits read-through |
| Wake success/failure | CR phase after `spawn_deployment` (DT-7) | never mutates the row → cannot show `running` falsely |

The read-model is **read-through first** (compute from the CR per read; no watch, no write-back). The
CRD doc's optional materialized cache (`add the cache if listing gets hot`) is a later step, not v1alpha1.

##### Read-model projection rule (resolves DT-9 handoff #1)

One helper materializes the client-facing status; it is the sole place the CR phase becomes a
`DeploymentStatus`, and it is used wherever a `DeploymentOut` is built for an existing row.

```python
# api/deployments.py
async def resolved_status(manager: SessionManager, deployment: Deployment) -> DeploymentStatus:
    """Project the durable row + live CR onto the coarse client status.
    STOPPED is authoritative in the DB (no CR); otherwise read through to the runtime."""
    if deployment.status is DeploymentStatus.STOPPED:
        return DeploymentStatus.STOPPED            # owner intent — never consult the cluster
    info = await manager.get(deployment.id)        # CR (kube) / registry (subprocess); DT-7 seam
    if info is not None and info.phase is SessionPhase.READY:
        return DeploymentStatus.RUNNING
    # None (no live runtime), STARTING (waking), SLEEPING (idled), FAILED (deterministic) all
    # present as SLEEPING: not serving now; the next request re-attempts a wake and surfaces any
    # 502/503 there. FAILED→SLEEPING is what guarantees a failed wake never reads back as RUNNING.
    return DeploymentStatus.SLEEPING
```

Because the serving path performs **no** DB write (DT-9 deleted the per-asset `last_active` commit) and
`running` is never persisted, a failed, timed-out, or capacity-rejected wake mutates no row — the read
model is correct by construction, not by clean-up. This is identical on both backends: `manager.get`
returns `None` for a GC'd CR or an exited subprocess, mapping to `sleeping` either way.

##### Per-endpoint changes

| Endpoint | Today | After (DT-10) |
|---|---|---|
| `POST /api/notebooks/{id}/deploy` | `_load_owned_notebook`; upsert row; `port=None`; `last_active=None`; `_stop_if_running` (sync `manager.target`) | `NotebookWrite` dep (DT-3, WRITE); upsert row; `await manager.stop(dep.id)` to reset any stale CR (suppress `SessionNotFoundError`); `status=SLEEPING`; return `DeploymentOut(status=SLEEPING)` by construction (no CR yet). No `port`/`last_active` writes. |
| `GET /api/notebooks/{id}/deployment` **(new)** | — (no status read existed; the `{slug}` GET is the proxy) | `NotebookRead` dep (DT-3); load the notebook's deployment; `status=await resolved_status(manager, dep)`; `DeploymentOut`. The concrete read-model reader; 404 if the notebook has no deployment. |
| `DELETE /api/deployments/{slug}` | `_load_deployment` (404s STOPPED) + inline `notebook.user_id` check; `_stop_if_running`; `status=STOPPED`; `port/last_active=None` | load deployment+notebook by slug (any status, `ResourceHidden` if absent); `authorize_notebook(…, WRITE)` (DT-3); `await manager.stop(dep.id)` (idempotent, suppress `SessionNotFoundError` — CR delete GC-cascades Pod/Service/Secret, DT-8); `status=STOPPED`; commit; 204. |
| `GET/WS /api/deployments/{slug}` (serving) | inline load→wake→`last_active` commit→target→touch→forward/relay | **owned by DT-9** (`DeploymentResolver` + gateway). DT-10 only asserts: no DB `status`/`last_active` write on this path; activity is the CR annotation; a failed wake raises (DT-7 typed error) and touches no row. |
| `POST /api/sessions` | `_authorize_create`; `manager.spawn` (sync); `except (SessionCapacityError, PortAllocationError)`→503 | actor `get_current_user_optional`; `load_notebook_for(db, payload.notebook_id, actor, WRITE if mode=="edit" else READ)` (DT-3); `await manager.spawn(notebook, mode, actor.id?)`; no inline capacity catch — `SessionManagerError` propagates to the shared handler (`SessionCapacityError`→429, DT-13). |
| `POST /api/sessions/{id}/save` | persist via `manager.current_source` | **DELETED** (see handoff #2). |
| `DELETE /api/sessions/{id}` | `manager.get`; `_authorize_session` (creator); `_persist_edit_session`; `manager.stop` | capability-scoped (handoff #4): `await manager.stop(session_id)`; `SessionNotFoundError`→404 (DT-13). No actor, no DB, no persist — autosave already flows through DT-11's proxied `api/kernel/save`. |

##### Wake / idle-sleep / stop flows (against the DT-7 seam)

- **Wake-on-request** — not reimplemented here: it is DT-9's `DeploymentResolver.resolve` (cheap
  `await manager.target` fast-path, else idempotent `await manager.spawn_deployment(notebook, dep.id,
  slug)`), which is DT-7's bounded, annotation-bump-then-poll entry. DT-10's only stake is the read
  model: the wake writes nothing to Postgres, so its success/failure is reflected purely through the CR
  (resolved read-through), never a stale `running` row.
- **Idle-sleep** — entirely the controller (DT-8 reconcile: `now - lastActivity > idleTimeoutSeconds`
  → delete Pod, `phase=Sleeping`, CR/Secret/Service retained). No API code, no DB write; the durable
  row stays at its `sleeping` resting value throughout, and `resolved_status` reports `sleeping` off the
  CR phase. Edit/run sessions are deleted outright by the controller/process on idle (no durable row).
- **Stop** — owner-driven `DELETE …/{slug}`: `manager.stop` deletes the CR (GC cascade), the row flips
  to the authoritative `stopped` tombstone (slug reserved, identity remembered); a later `deploy` flips
  it back to `sleeping`.

##### Explicit contracts

- **Durable state machine (both backends).** `(no row) --deploy--> SLEEPING`;
  `SLEEPING --redeploy--> SLEEPING` (CR reset via `stop`); `SLEEPING --delete--> STOPPED` (CR deleted);
  `STOPPED --deploy--> SLEEPING`. The persisted `deployments.status` is only ever `{sleeping, stopped}`;
  `running` is a response-only projection and is never stored. `stopped` is set only by `delete` and
  cleared only by `deploy`.
- **Read-model consistency.** `resolved_status` is the one projection: `stopped` short-circuits (no
  cluster call); otherwise CR `Ready`→`running`, everything else (`None`/`Starting`/`Sleeping`/`Failed`)
  →`sleeping`. The serving/proxy path never writes the row (DT-9), so the projection cannot disagree
  with reality on account of a missed hot-path write; a failed/timed-out/capacity-rejected wake leaves
  the row untouched and therefore never reads back as `running` (DT-9 handoff #1, closed).
- **Ephemeral sessions.** Edit/run sessions have **no** persisted row and no durable state — the
  `session_id` (unguessable uuid4 minted to an authorized creator at `POST /api/sessions`) is the sole
  capability. `create` authorizes via the notebook policy (DT-3: edit→WRITE, run→READ); `delete` is
  capability-scoped (holding the id suffices), mirroring DT-9's serving decision. Idle reaping is the
  controller's (kube) / process exit's (subprocess) job.
- **Capacity → 429 (resolves DT-7 handoff #3).** Adopted mechanism: a machine-readable sentinel on the
  CR. When the controller's Pod create is rejected by the namespace `ResourceQuota` (`Forbidden`), it
  sets `status.message = "QuotaExceeded: <detail>"` while leaving `phase` at `Pending`/`Starting`
  (transient, still retriable — not `Failed`). `KubeSessionManager`'s spawn/wake poll treats a
  `QuotaExceeded:`-prefixed message as terminal **for this call** and raises `SessionCapacityError`
  (→429, DT-13) instead of polling to timeout. This lives in the operator (separate repo) + the DT-7
  manager poll; DT-10 fixes the sentinel contract. **Fallback:** until the controller emits it, capacity
  degrades gracefully to a `SessionStartError` timeout (→503) — flagged, not blocking.
- **Error behaviour.** `deploy`/`delete` raise DT-3 `AccessError` for authz (404/401/403) and let DT-7
  `SessionManagerError` propagate; `create_session` lets `SessionManagerError` propagate
  (429/502/503/404 via DT-13); `delete_session` lets `SessionNotFoundError`→404. No router builds an
  `HTTPException` for a lifecycle/authz decision; the shared handler seam (interim in `main.py` until
  DT-13) renders them once.

##### Handoff resolutions

1. **(DT-9) read-model reflects wake outcome** — closed by the read-through projection above: no
   serving-path DB write + `running` never persisted ⇒ a failed wake cannot leave `status=running`;
   `resolved_status` maps CR `Failed`→`sleeping`.
2. **(DT-7) fate of `POST /api/sessions/{id}/save`** — **removed.** On the kube (prod) backend
   `read_source` returns `None`, so the endpoint is inert; on both backends autosaved edits already
   persist through DT-11's proxied `api/kernel/save` interception (`edit_save_callback`, injected by
   DT-9). Keeping a save endpoint that silently no-ops in production is a footgun the sweep forbids.
   `_persist_edit_session` and the persist-on-`delete` are removed with it (the subprocess
   `read_source` flush read only what marimo had already autosaved to the workdir — no durability the
   continuous proxied save does not already provide). **Consequence:** the DT-7 seam's `read_source`
   is now caller-less; recommend DT-11 drop it when it lands the single persistence path (DT-11 owns
   that seam; DT-10 owns the endpoint's removal). The "re-key edit-save persistence" the task names is
   thus resolved by *deleting* the sessions.py path and leaving DT-11's callback — keyed on
   `session_id` and resolving the notebook via `manager.get(session_id).notebook_id`, not a workdir
   file — as the one persistence path for both backends.
3. **(DT-7) capacity→429** — resolved above (CR `QuotaExceeded:` sentinel + manager poll; 503 fallback).
4. **(DT-3) `_authorize_session` target form** — **removed; replaced by capability scoping.** An
   edit/run session is authorized by possession of its uuid4 `session_id`, exactly as DT-9 made serving
   capability-based ("holding the id is the authorization"). `delete_session` therefore takes no actor
   and performs no `creator_id` check; the prior asymmetry (open serving vs. creator-gated delete) is
   gone. `creator_id` stays on `SessionInfo`/`spec.creatorId` for attribution and any future
   role-based policy, but is not an access gate. Role-based management of a co-member's session is
   explicitly rejected for v1alpha1 (needs a session→notebook→role lookup the capability model avoids).

##### Deletions (files / symbols)

- **`backend/app/services/deployment_lifecycle.py` — deleted outright:** `IdleDeploymentReaper`,
  `mark_running_deployments_sleeping` (boot reset), `start_deployment_lifecycle`,
  `REAPER_INTERVAL_SECONDS`. Idle/restart/orphan handling is the DT-8 reconcile loop.
- **`main.py`:** the `start_deployment_lifecycle` import and the lifespan `reaper = await …` /
  `await reaper.stop()` pair. Lifespan keeps only `await shutdown_session_manager()` (DT-7 rename) and
  gains `app.include_router(internal_router)` (DT-8). Boot reset gone — state survives API restarts.
- **`deployments.py`:** `_wake_deployment` (folded into DT-9's `DeploymentResolver`), every
  `deployment.port = …` and serving/deploy/delete `deployment.last_active = …` write, the
  `PortAllocationError` import, `_load_owned_notebook` and the inline `notebook.user_id …` delete check
  (→ DT-3). `_stop_if_running` collapses to `try: await manager.stop(id) except SessionNotFoundError:
  pass` (drops the sync `manager.target` pre-check). Slug helpers, `_deployment_out_after_conflict`,
  `_deployment_url`, `deploy_notebook`, `delete_deployment` are **kept** (DT-10-owned lifecycle) and
  re-wired to the async seam + DT-3.
- **`sessions.py`:** the `POST /{session_id}/save` route + `save_session`, `_persist_edit_session`,
  `_authorize_session`, the `PortAllocationError` import and the capacity `try/except`, and the
  `NotebookStorageService` / `get_notebook_storage` / `storage` dependencies. (`_is_owner`/`_can_view`/
  `_authorize_create`/`_not_found`/`_auth_error` are DT-3's deletions.)

##### Schema changes

- **`deployments.port`** — column already dropped by DT-1/DT-2; DT-10 removes the last code writers
  (above). `DeploymentOut` (`slug`/`status`/`url`) and `SessionOut`
  (`id`/`notebook_id`/`mode`/`proxy_url`) **never carried `port`** (DT-1 confirmed) — no schema field
  change; the task's "drop `port` from `SessionOut`/`DeploymentOut`" is satisfied by their already being
  port-free plus deleting the ORM writes.
- **`deployments.status`** — column unchanged (DT-1 enum `running|sleeping|stopped`); its *stored*
  domain narrows to `{sleeping, stopped}` by the state machine above (behavioural, not DDL).
- **`deployments.last_active`** — kept per DT-1's read-model-cache intent, but its only writers (reaper,
  serving hot path) are deleted; in read-through-first it is unwritten (mirror of CR `status.lastActivity`
  only if a materialized cache lands later). Flagged as a removal candidate below rather than dropped
  now, to avoid re-opening the completed DT-2 migration.
- **`schemas/session.py`** — `SessionMode` import moves `process_manager`→`session_manager` (DT-7); no
  field change.
- **Config `IDLE_TIMEOUT_MINUTES`** — orphaned by the reaper deletion (idle now `spec.idleTimeoutSeconds`
  on the CR); its removal is DT-12's surface, named here.

##### Authz (delegated to DT-3)

`deploy_notebook` → `NotebookWrite` dep; `delete_deployment` → `authorize_notebook(…, WRITE)` (slug
entry point, actor from `get_current_user_optional`); `GET …/deployment` → `NotebookRead`;
`create_session` → `load_notebook_for(…, WRITE if edit else READ)`. Serving (`{slug}` GET/WS/HTTP) stays
public/unauthenticated (DT-9). Ephemeral-session `delete` is capability-scoped, not policy (handoff #4).

##### Data flow — deploy → serve → idle-sleep → wake → stop

```mermaid
flowchart TD
    subgraph db [Postgres — durable catalog + intent]
      ROW[deployments row\nstatus ∈ sleeping | stopped\nslug, notebook_id]
    end
    subgraph api [backend API — stateless]
      DEP[POST /notebooks/&#123;id&#125;/deploy]
      GET[GET /notebooks/&#123;id&#125;/deployment\nread-through]
      DEL[DELETE /deployments/&#123;slug&#125;]
      GW[serving proxy — DT-9\nDeploymentResolver]
    end
    subgraph cluster [Kubernetes — runtime source of truth]
      CR[MarimoSession CR\nstatus.phase / lastActivity]
      CTL[controller — DT-8]
      POD[(session Pod)]
    end

    DEP -->|DT-3 WRITE; status=SLEEPING| ROW
    DEP -.await manager.stop reset stale CR.-> CR
    GW -->|resolve-or-wake: spawn_deployment DT-7| CR
    CR --> CTL --> POD
    GW -->|mark_active → annotation| CR
    CTL -->|idle > idleTimeoutSeconds:\ndelete Pod, phase=Sleeping| CR
    GW -->|wake annotation, poll Ready| CR
    DEL -->|DT-3 WRITE; status=STOPPED| ROW
    DEL -->|manager.stop → delete CR + GC| CR
    GET -->|await manager.get → phase| CR
    GET -.STOPPED short-circuit.-> ROW
    GET --> OUT[DeploymentOut\nrunning | sleeping | stopped]
```

Deploy resets the row to `sleeping` and clears any stale CR; the first request wakes it via DT-9/DT-7;
the controller sleeps it on idle with no API involvement; a subsequent request re-wakes; stop deletes
the CR and tombstones the row `stopped`. Status is always projected read-through from the CR, so the
API and cluster never disagree.

##### Rejected alternatives

- **Keep the DB row authoritative and sync `running`/`sleeping` from a controller→DB watch.** Rejected:
  reintroduces the second source of truth and the sync path the CRD design exists to remove;
  read-through is always-consistent and needs no watch.
- **Materialize `running`/`sleeping` on the serving hot path (as today).** Rejected: a DB write per
  proxied asset (DT-9 deleted it); the CR activity annotation is the edge, projection is read-time.
- **Keep `POST /sessions/{id}/save` + persist-on-delete via `read_source`.** Rejected: inert on the
  kube backend and redundant with DT-11's proxied `api/kernel/save` persistence, which covers both
  backends.

##### Open questions

- **Capacity sentinel dependency.** 429 fidelity requires the operator (separate repo) to emit
  `status.message = "QuotaExceeded: …"` and `KubeSessionManager` to read it while polling. Until both
  land, capacity degrades to 503. Flagged for the controller component + DT-7's manager poll.
- **`deployments.last_active` removal.** Read-through-first leaves it with no writer. Drop it (a
  follow-up DT-2 migration) if no materialized-cache listing ever needs it, or keep as the cache seed
  DT-1 intended. Deferred, not decided, to avoid re-opening a completed migration.
- **`read_source` on the DT-7 seam.** Now caller-less (handoff #2). Recommend DT-11 remove it with the
  single-persistence-path design it owns.
- **`GET /api/notebooks/{id}/deployment` namespacing.** New read-model reader under the notebooks path;
  no collision found with DT-6's notebook router — confirm when DT-6/DT-10 land together.

---

### DT-11 — Notebook source storage & edit-session persistence
**Status:** complete

Design the notebook-source seam so it (a) removes the duplicated edit-save persistence (`sessions._persist_edit_session` reading the workdir file vs. `proxy.persist_marimo_save` intercepting `api/kernel/save`), (b) stays ready for the MinIO/object-store swap the `NotebookStorageService` ABC anticipates, and (c) supplies the CRD init-container's `GET /api/internal/notebooks/{id}/source` fetch. Define one persistence path for autosaved edits agnostic to whether the runtime is a local workdir file or a remote pod, and state how source flows to a session at start for each backend.

- **Acceptance criteria:** a single edit-save persistence design usable by both the save endpoint and the proxied `api/kernel/save` interception (or a decision to keep only one); the storage seam surface (get/put/delete + internal-source endpoint) defined against both backends; how source reaches a session at start per backend.
- **Likely files:** `backend/app/services/notebook_storage.py`, `backend/app/api/{sessions,proxy}.py`, `backend/app/services/kube_session_manager.py`, `marimosession-crd-spec.md`.
- **Depends on:** DT-7, DT-9. **Relates to:** DT-8 (internal endpoint).

#### DT-11 Design

After DT-10 removed `POST /api/sessions/{id}/save`, `_persist_edit_session`, and persist-on-delete,
there is exactly **one** edit-save persistence path left: the proxied `api/kernel/save` interception
DT-9 exposes as a `response_body_callback` injection point. DT-11 lands that callback
(`edit_save_callback`) as the single, backend-neutral persistence path, and confirms it works for both
runtimes for a structural reason — **persistence lives at the gateway, which every backend's traffic
flows through**, so it is blind to whether the upstream is a loopback subprocess or a pod Service. The
`NotebookStorageService` ABC is already the object-store swap seam and is **kept verbatim**; DT-11 only
routes every source read/write through it and deletes the now-dead workdir read-back
(`read_source`, DT-7 handoff #1). Source reaches a session differently per backend (subprocess writes
the workdir at spawn; kube's init container fetches through the internal endpoint), but both read
*through the same seam*.

##### Target design — storage seam (unchanged surface, the swap point)

```python
# backend/app/services/notebook_storage.py — SURFACE UNCHANGED (already MinIO-ready)
class NotebookStorageService(ABC):
    async def get(self, notebook: Notebook) -> str | None: ...    # read stored source
    async def put(self, notebook: Notebook, source: str | None) -> None: ...   # stage/write source
    async def delete(self, notebook_id: UUID) -> None: ...        # remove stored source

class PostgresNotebookStorage(NotebookStorageService):
    """dev/default: source lives on the notebook row.
    get → notebook.source; put → notebook.source = source; db.add(notebook) (caller commits);
    delete → clear the column. put/delete STAGE onto the caller's transaction — they never commit."""

# future, no seam change: ObjectStoreNotebookStorage(NotebookStorageService)
#   get/put/delete against MinIO keyed by notebook.id; put is durable on return, delete removes the object.

def get_notebook_storage(db: AsyncSession = Depends(get_db)) -> NotebookStorageService:
    """Select by NOTEBOOK_STORAGE_BACKEND (config key owned by DT-12; default → PostgresNotebookStorage(db))."""
```

##### Target design — the one edit-save persistence path

```python
# backend/app/api/proxy.py — the single edit-save path; DT-9 injects it as response_body_callback
def edit_save_callback(
    manager: SessionManager,
    storage: NotebookStorageService,
    db: AsyncSession,
    session_id: UUID,
) -> ResponseBodyCallback:
    """Persist a proxied `POST api/kernel/save` through the storage seam.
    Backend-neutral: the gateway sees the same save body for subprocess and pod upstreams."""
    async def persist(response: httpx.Response, body: bytes) -> None:
        if response.status_code >= HTTPStatus.BAD_REQUEST:
            return                                    # marimo rejected the save — nothing durable to mirror
        info = await manager.get(session_id)          # DT-7 async seam (CR read / registry read)
        if info is None or info.mode != "edit":
            return                                    # only edit sessions own source (run/deploy never save)
        notebook = await db.get(Notebook, info.notebook_id)
        if notebook is None:
            return
        try:
            await storage.put(notebook, body.decode("utf-8"))   # response body IS the serialized source
            await db.commit()
        except Exception:                             # transient store/DB fault
            await db.rollback()                       # keep the request's session usable
            logger.exception("edit-save persist failed for session %s", session_id)
            # swallowed by design: marimo's ~1s autosave re-persists on the next save; do not fault the editor

    return persist
```

```python
# backend/app/api/proxy.py — DT-9's route, with DT-11's storage injection added
@router.api_route("/{session_id}/{path:path}", methods=marimo_proxy.PROXY_METHODS, name="proxy_http")
async def proxy_http(session_id, path, request,
                     manager=Depends(get_session_manager),
                     storage=Depends(get_notebook_storage),
                     db=Depends(get_db)):
    callback = (edit_save_callback(manager, storage, db, session_id)
                if request.method == "POST" and path == "api/kernel/save" else None)
    return await marimo_proxy.proxy_http(
        request, manager, SessionResolver(manager, session_id), path, response_body_callback=callback)
```

`edit_save_callback` lives in `proxy.py` (beside its only injection site) so `marimo_proxy.py` stays
domain-neutral (DT-9's rule) and `notebook_storage.py` stays pure storage — neither imports
`SessionManager`. The response body being the saved source is existing, working behaviour
(`persist_marimo_save` today); DT-11 preserves it and only swaps the hardcoded
`PostgresNotebookStorage(db)` construction for the injected seam and adds fault tolerance.

##### Session-start source flow (per backend)

- **Subprocess (dev).** `SubprocessSessionManager.spawn` writes the stored source into
  `workdir/notebook.py` **fresh every spawn**, then launches `marimo edit|run`. The source is the
  notebook row (`notebook.source`, already loaded on the passed ORM object) — which, for the
  `PostgresNotebookStorage` backend that pairs with the dev runtime, *is* the store. The old
  `_prepare_workdir` "reuse the autosaved `notebook.py` if it exists" branch is **deleted**: with
  continuous save-through-the-gateway, the stored source is always the latest autosave, so a fresh
  write from storage is never staler than the on-disk file. The workdir file becomes pure session
  scratch with no durability role.
- **Kube (prod).** `KubeSessionManager.spawn`/`spawn_deployment` write **no** source — they create the
  CR + the Secret carrying the notebook-scoped `SESSION_TOKEN` (DT-8). The controller's init container
  `fetch-source` calls `GET /api/internal/notebooks/{id}/source` (DT-8), which returns
  `await storage.get(notebook)` — **the seam-respecting read** that transparently serves Postgres or a
  future object store — into `/work/notebook.py`. This is the sole path that must stay swap-ready, and
  it does, because it reads through the ABC.

##### Explicit contracts

- **One persistence path (definitive).** Edit durability flows *only* through `edit_save_callback` on
  the proxied `api/kernel/save`, for both backends. There is no save endpoint (DT-10 removed it), no
  persist-on-delete, and no manager-mediated file flush. Confirmed: the DT-10 "keep only one" decision
  is final — the callback is that one.
- **Durability guarantee.** An edit is durable once its `api/kernel/save` response returns `2xx/3xx`
  **and** `storage.put` + `db.commit` succeed. Because marimo autosaves on a ~1s delay (the workdir
  `pyproject.toml` `[tool.marimo.save]` config, retained), the durable copy trails the in-editor state
  by at most one autosave interval. The runtime's own working file (subprocess workdir / pod emptyDir)
  always holds the latest in-session edits regardless of persistence outcome, so a persist miss never
  loses *in-session* work — only the cross-session stored copy lags by one save.
- **Error behaviour on save failure mid-proxy.** A failing `storage.put`/`commit` is caught, rolled
  back, logged, and **swallowed** — the proxied `2xx` from marimo is returned to the editor unchanged.
  Rationale: the upstream already saved to its working file, and marimo's next autosave re-attempts
  persistence, so faulting the editor stream for a transient store blip would degrade UX without
  improving durability. The residual risk (a persist failure on the *final* save before the session
  ends is not retried) is accepted and flagged below; it is strictly smaller than today's, since save
  is now continuous rather than only on an explicit endpoint call.
- **`put`/`delete` transaction contract.** `put` and `delete` **stage** onto the caller's
  `AsyncSession` for `PostgresNotebookStorage` (the caller owns the `commit`); for an object-store
  implementation they are durable on return and the caller's subsequent `commit` is a harmless no-op.
  This one leak (Postgres `put` needs a caller commit) is intentional: `put` must not self-commit, or
  it would flush half-built notebooks in the DT-6 create/update transactions that also call it.
- **Lifecycle per backend.** Subprocess: source is written locally at spawn and served from loopback;
  the workdir is torn down on stop with no read-back. Kube: source is never handled by the manager —
  the pod self-fetches via its scoped token and the init endpoint; the CR/Secret carry no source, and
  GC (DT-8 ownerRefs) reaps everything on stop.

##### Deletions (by symbol / file)

- **DT-7 seam — `SessionManager.read_source` removed** (handoff #1 resolved: **drop it**). It is
  caller-less after DT-10 deleted `save_session`/`_persist_edit_session`; the single persistence path
  reads the save body off the proxy, never a manager file read. Remove from the `SessionManager`
  Protocol (`session_manager.py`), from `SubprocessSessionManager`, and the `return None` stub in
  `KubeSessionManager`.
- **`subprocess_backend/workdir.py`:** `read_current_source` (the `workdir/notebook.py` read backing
  the dropped `read_source`) and the `_prepare_workdir` "reuse autosaved file on disk" branch — always
  write stored source fresh.
- **`api/proxy.py`:** the inline `persist_marimo_save` closure and its hardcoded
  `PostgresNotebookStorage(db)` construction — replaced by the injected `edit_save_callback`
  (DT-9 kept only the injection point; DT-11 supplies the body).
- **No change to `NotebookStorageService`/`PostgresNotebookStorage` surface**; `delete` is retained
  for object-store parity and is called on notebook deletion (DT-6), not by any DT-11 path.

##### Data flow

Edit-save persistence (one path, both backends):

```mermaid
sequenceDiagram
    participant Ed as marimo editor (browser)
    participant GW as gateway (proxy.py + marimo_proxy)
    participant RT as runtime (subprocess loopback / pod Service)
    participant ST as NotebookStorageService
    participant DB as Postgres / object store

    Ed->>GW: POST /api/proxy/{id}/api/kernel/save  (autosave, ~1s)
    GW->>RT: forward (buffered: response_body_callback set)
    RT-->>GW: 200 + serialized source (response body)
    GW->>GW: edit_save_callback → manager.get(id) → notebook_id (edit only)
    GW->>ST: storage.put(notebook, body)
    ST->>DB: stage row source / write object
    GW->>DB: db.commit()  (no-op for object store)
    GW-->>Ed: 200 (unchanged; persist error swallowed + logged, next autosave retries)
```

Session-start source flow (per backend):

```mermaid
flowchart TD
    subgraph sub [subprocess backend — dev]
      S1[spawn] --> S2[storage.get / notebook.source]
      S2 --> S3[write workdir/notebook.py fresh]
      S3 --> S4[marimo edit|run /work/notebook.py]
    end
    subgraph kube [kube backend — prod]
      K1[spawn: create CR + Secret SESSION_TOKEN nid] --> K2[controller creates Pod]
      K2 --> K3[initContainer: GET /api/internal/notebooks/id/source\nBearer SESSION_TOKEN]
      K3 --> K4[endpoint → storage.get notebook\nPostgres row OR object store]
      K4 --> K5[write /work/notebook.py] --> K6[marimo run /work/notebook.py]
    end
```

##### Rejected alternatives

- **Keep a `read_source`-based save path (endpoint or persist-on-delete) alongside the callback.**
  Rejected: DT-10 already removed the endpoint; on kube `read_source` returns `None`, so it is inert in
  prod, and the continuous callback already covers both backends — a second path is dead weight.
- **Put `edit_save_callback` in `marimo_proxy.py` or `notebook_storage.py`.** Rejected: the former
  drags `SessionManager`/`Notebook` into the domain-neutral transport module (DT-9 forbids); the latter
  pollutes the storage seam with session/proxy concerns. It belongs beside its injection in `proxy.py`.
- **Fault the proxied save (return 5xx) when persistence fails.** Rejected: the upstream already saved
  to its working file and marimo's next autosave retries persistence, so erroring the editor stream
  trades UX for no durability gain; swallow-and-log with rollback is the correct posture.

##### Open questions

- **Last-save durability.** With persist-on-delete gone (DT-10) and persist errors swallowed, a store
  fault on the *final* autosave before a session ends is not retried and loses that one delta (the
  runtime working file is torn down with the session). Accepted for v1alpha1 given continuous autosave;
  revisit if a "flush on graceful editor close" signal is wanted.
- **Subprocess source read not seam-routed.** The dev backend reads `notebook.source` off the row
  rather than through `storage.get`, because `SubprocessSessionManager` is a process-wide singleton
  without a request-scoped `db`. Acceptable because dev is Postgres-only and the swap-critical read
  (kube's internal endpoint) *is* seam-routed; flagged only so it is a conscious non-goal, not an
  oversight.
- **`NOTEBOOK_STORAGE_BACKEND` config surface.** Named here; DT-12 owns the key, the object-store
  connection settings, and whether the selector is per-deployment or global.

---

### DT-12 — Configuration & runtime-settings surface
**Status:** complete

Design the rationalised backend configuration surface. Remove settings obsoleted by the CRD design (`MARIMO_PORT_RANGE`, `IDLE_TIMEOUT_MINUTES`; revisit `MAX_CONCURRENT_SESSIONS` as an optional soft cap for nicer 429s vs. deletion in favour of ResourceQuota); introduce workspace archive retention, trusted-email-linking provider policy, the `SESSION_BACKEND` selector, and kube/controller settings (sessions namespace, runtime image, service DNS suffix, ready timeout). Define how config is grouped and validated, and reconcile the k8s-client dependency addition.

- **Acceptance criteria:** target settings layout with obsolete keys removed and new session-backend keys added; a single source-of-truth statement for env defaults; consistency with DT-7's backend-selection seam; the dependency addition noted.
- **Likely files:** `backend/app/core/config.py`, `backend/pyproject.toml`.
- **Depends on:** DT-7.

#### DT-12 Design

The config surface stays a **single flat `Settings`** class with SCREAMING_CASE env keys (the current
convention, and what `.env.example` already speaks) — grouping is expressed by ordered, commented
sections and cross-field validation, **not** by nested sub-models (which would rename every env var to
`SESSION__BACKEND` and churn `.env`). The one nested structure is the OIDC provider list, because it is
a genuinely variable-length collection that cannot flatten. Field defaults in `config.py` are the
**single source of truth**; `.env.example` is a documentation mirror that sets only the two required,
no-default keys (`DATABASE_URL`, `SECRET_KEY`) to dev values and shows the rest as commented defaults.

##### Target design — `backend/app/core/config.py`

```python
from __future__ import annotations

import re
from enum import StrEnum
from functools import lru_cache
from typing import Literal

from pydantic import AnyHttpUrl, BaseModel, Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

_DNS_LABEL_RE = re.compile(r"^[a-z0-9]([-a-z0-9]*[a-z0-9])?$")   # RFC-1123, <=63 (see slug.py)


class SessionBackend(StrEnum):
    SUBPROCESS = "subprocess"        # dev: in-process marimo subprocesses (SubprocessSessionManager)
    KUBE = "kube"                    # prod: MarimoSession CRs (KubeSessionManager)


class OIDCProvider(BaseModel):
    """One configured external identity provider. Element of Settings.OIDC_PROVIDERS.
    The redirect URI is NOT stored — it is derived from PUBLIC_API_URL (single source)."""
    kind: Literal["google", "oidc", "saml"]   # 'saml' is broker-fronted OIDC (DT-4 SAML decision)
    slug: str                                 # DNS-label; used in /oidc/{slug}/… routes
    display_name: str
    issuer: AnyHttpUrl                         # discovery base: {issuer}/.well-known/openid-configuration
    client_id: str
    client_secret: str
    scopes: list[str] = ["openid", "email", "profile"]
    trusted_email_linking: bool = False          # requires verified email claim; explicit opt-in

    @property
    def provider_value(self) -> str:           # the identities.provider string (DT-1/DT-4 scheme)
        return "google" if self.kind == "google" else f"{self.kind}:{self.slug}"


class Settings(BaseSettings):
    """Application settings loaded from the environment and ``.env``.

    Field defaults below are the single source of truth for every non-required key.
    """

    # ── database ─────────────────────────────────────────────────────────────
    DATABASE_URL: str                                             # required, no default

    # ── auth / identity ──────────────────────────────────────────────────────
    SECRET_KEY: str                                              # required; signs user + session JWTs
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 60
    PUBLIC_API_URL: AnyHttpUrl = "http://localhost:8000"          # external base; OIDC redirect root
    OIDC_PROVIDERS: list[OIDCProvider] = Field(default_factory=list)   # JSON env value; [] → local-only

    # ── workspace lifecycle ──────────────────────────────────────────────────
    WORKSPACE_ARCHIVE_RETENTION_DAYS: int = 30

    # ── session / runtime (DT-7 seam selector + backend settings) ────────────
    SESSION_BACKEND: SessionBackend = SessionBackend.SUBPROCESS
    SESSION_READY_TIMEOUT_SECONDS: float = 30.0                  # spawn/wake poll bound (both backends)
    SESSION_TOKEN_TTL_SECONDS: int = 86_400                     # 24h; re-minted each pod start/wake (kube)
    #   kube-only below — inert under SESSION_BACKEND=subprocess
    SESSION_NAMESPACE: str = "marimohub-sessions"                # namespace holding session CRs/pods
    SESSION_SERVICE_DNS_SUFFIX: str = "svc"                      # {serviceName}.{ns}.{suffix}
    SESSION_SERVICE_PORT: int = 8080                            # marimo container port on the Service
    SESSION_RUNTIME_IMAGE: str | None = None                    # None → CR omits spec.image, controller defaults

    # ── notebook source storage (DT-11 seam selector) ────────────────────────
    NOTEBOOK_STORAGE_BACKEND: Literal["postgres"] = "postgres"   # object store added when its backend lands

    # embeddings: intentionally no config surface — see "Embeddings group" below.

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    @model_validator(mode="after")
    def _validate(self) -> "Settings":
        if self.SESSION_READY_TIMEOUT_SECONDS <= 0:
            raise ValueError("SESSION_READY_TIMEOUT_SECONDS must be > 0")
        if self.SESSION_TOKEN_TTL_SECONDS <= 0:
            raise ValueError("SESSION_TOKEN_TTL_SECONDS must be > 0")
        if self.WORKSPACE_ARCHIVE_RETENTION_DAYS <= 0:
            raise ValueError("WORKSPACE_ARCHIVE_RETENTION_DAYS must be > 0")
        slugs = [p.slug for p in self.OIDC_PROVIDERS]
        if len(set(slugs)) != len(slugs):
            raise ValueError("OIDC_PROVIDERS slugs must be unique")
        for p in self.OIDC_PROVIDERS:
            if not _DNS_LABEL_RE.match(p.slug):
                raise ValueError(f"OIDC provider slug {p.slug!r} must be a DNS label")
        if self.SESSION_BACKEND is SessionBackend.KUBE:
            if not _DNS_LABEL_RE.match(self.SESSION_NAMESPACE):
                raise ValueError("SESSION_NAMESPACE must be a DNS-1123 label when SESSION_BACKEND=kube")
            if not 1 <= self.SESSION_SERVICE_PORT <= 65535:
                raise ValueError("SESSION_SERVICE_PORT out of range")
        return self


@lru_cache
def get_settings() -> Settings:
    """Return the cached application settings, loading them on first use."""
    return Settings()  # pyright: ignore[reportCallIssue]
```

`OIDC_PROVIDERS` is a JSON-valued env var (pydantic-settings parses a complex field from JSON), e.g.
`OIDC_PROVIDERS='[{"kind":"google","slug":"google","display_name":"Google","issuer":"https://accounts.google.com","client_id":"…","client_secret":"…","trusted_email_linking":true}]'`.
DT-4's `get_oidc_auth_service(provider)` / `get_oidc_verifier(provider)` look a provider up by
`slug`; the verifier builds `redirect_uri = f"{PUBLIC_API_URL}/api/auth/oidc/{slug}/callback"` — derived,
never stored, so the callback URL has one source.

##### Target design — `.env.example` (rewritten)

```dotenv
# ── database ───────────────────────────────────────────────
DATABASE_URL=postgresql+asyncpg://molab:molab@localhost:5432/molab

# ── auth / identity ────────────────────────────────────────
SECRET_KEY=dev-secret-change-me
# ACCESS_TOKEN_EXPIRE_MINUTES=60
PUBLIC_API_URL=http://localhost:8000
# OIDC_PROVIDERS=[]        # JSON list; trusted_email_linking defaults false

# ── workspace lifecycle ────────────────────────────────────
# WORKSPACE_ARCHIVE_RETENTION_DAYS=30

# ── session / runtime ──────────────────────────────────────
SESSION_BACKEND=subprocess
# SESSION_READY_TIMEOUT_SECONDS=30.0
# SESSION_TOKEN_TTL_SECONDS=86400
# kube backend only (SESSION_BACKEND=kube):
# SESSION_NAMESPACE=marimohub-sessions
# SESSION_SERVICE_DNS_SUFFIX=svc
# SESSION_SERVICE_PORT=8080
# SESSION_RUNTIME_IMAGE=          # empty → controller default (CR spec.image omitted)

# ── notebook source storage ────────────────────────────────
# NOTEBOOK_STORAGE_BACKEND=postgres
```

##### `mark_active` coalescing window — handoff decision (DT-7/DT-8)

**Decision: a module constant, not a setting.**
`MARK_ACTIVE_COALESCE_SECONDS = 15.0` lives in `kube_session_manager.py` (it has no meaning for the
subprocess backend, which marks activity by an O(1) in-memory timestamp). It is an internal throttle
protecting the apiserver from per-WS-frame PATCH traffic, not an operator-facing tuning knob; exposing
it invites a misconfiguration that silently degrades idle accuracy for no operational benefit.

**Recorded relationship constraint:** the coalescing window must stay strictly below the CRD's minimum
`spec.idleTimeoutSeconds` so `status.lastActivity` never lags far enough to trip a premature idle-sleep.
The CRD (DT-8) pins `idleTimeoutSeconds` `default: 600, minimum: 30`; the 15 s window sits at half the
floor and 1/40 of the default — comfortable margin. Invariant to preserve if either value changes:
`MARK_ACTIVE_COALESCE_SECONDS < min(idleTimeoutSeconds)` (currently `15 < 30`). Documented at the
constant's definition so the two files stay in agreement.

##### Explicit contracts

- **Env default source of truth.** The `Settings` field defaults are authoritative at runtime;
  `.env.example` is a non-authoritative mirror (dev values for the two required keys, commented defaults
  for the rest). Nothing reads a default from `.env.example`, so the two cannot drift into a second
  source. `get_settings()` stays `@lru_cache`d — one validated `Settings` per process, matching DT-7's
  process-wide `get_session_manager()` singleton (both resolve `SESSION_BACKEND` exactly once at first
  use).
- **Backend-selection consistency (DT-7).** `SESSION_BACKEND: SessionBackend` is the one seam selector;
  `get_session_manager()` switches on it. The value type is the `SessionBackend` StrEnum (not a bare
  `Literal`) so the selector is a single named symbol shared by config and manager, and an invalid value
  fails at settings load, not at first spawn.
- **Backend-conditional requirements.** Every kube key carries a working in-cluster default, so no key is
  *unset-required*; instead, validation is **conditional correctness**: when `SESSION_BACKEND=kube`,
  `SESSION_NAMESPACE` must be a DNS-1123 label and `SESSION_SERVICE_PORT` in range (these compose the
  Service DNS name `KubeSessionManager` builds — `{serviceName}.{SESSION_NAMESPACE}.{SESSION_SERVICE_DNS_SUFFIX}:{SESSION_SERVICE_PORT}`,
  DT-7). Under `subprocess`, the kube keys are ignored and never validated. OIDC keys are validated only
  for the providers actually listed (unique DNS-label slugs); an empty list disables OIDC with no auth
  config required — local username/password always works.
- **Runtime image ownership (reconciles the CRD).** The runtime image's single source of truth is the
  **controller** (CRD `spec.image` "defaults from controller config when empty", DT-8). `SESSION_RUNTIME_IMAGE`
  defaults to `None`, in which case `KubeSessionManager` omits `spec.image` and the controller supplies it;
  a non-null value is an explicit per-backend override stamped into the CR. This avoids two authorities
  pinning the image by default.
- **Token settings coupling.** `SESSION_TOKEN_TTL_SECONDS` feeds `core/security.py`'s
  `create_session_token` (DT-8); it is a session-runtime concern grouped with the other session keys, and
  because the token is re-minted on every pod start/wake, a bounded TTL never bites an active pod. It uses
  the same `SECRET_KEY` as user JWTs (disjoint by the `typ:"session"` claim, DT-8), so no second signing
  key enters config.
- **Embeddings group.** Deliberately **empty**. `MODEL_NAME="all-MiniLM-L6-v2"` and
  `EMBEDDING_DIMENSIONS=384` stay module constants in `embedding_service.py` because the model is coupled
  to the 384-dim `pgvector` column and a runtime dimension assertion; making the model an env var would let
  a deploy silently select a different-dimensioned model and break inserts. The "embeddings" group is thus
  answered by *not* adding a surface — a conscious non-goal, not an omission.

##### Deletions (settings keys + related code)

| removed / changed | replacement | why |
|---|---|---|
| `IDLE_TIMEOUT_MINUTES` | — (none) | idle is the CRD `spec.idleTimeoutSeconds` + controller reconcile (DT-8/DT-10); the in-process reaper is deleted, so the key is orphaned. |
| `MAX_CONCURRENT_SESSIONS` | — (none) | capacity is the namespace `ResourceQuota` → `QuotaExceeded` sentinel → 429 (DT-10); subprocess dev has no cap (DT-7). No soft cap reintroduced. |
| `MARIMO_PORT_RANGE` | — (none) | ephemeral OS-assigned ports in the subprocess backend (DT-7); no allocator, no range. |
| `MARIMO_READY_TIMEOUT_SECONDS` | `SESSION_READY_TIMEOUT_SECONDS` | renamed to the backend-neutral name; same role (spawn/wake readiness bound). |

Code removed with the keys: `deployment_lifecycle.py`'s `settings.IDLE_TIMEOUT_MINUTES` read (whole file
deleted, DT-10); `process_manager.py`'s `from_settings` reads of `MARIMO_PORT_RANGE` /
`MAX_CONCURRENT_SESSIONS` / `MARIMO_READY_TIMEOUT_SECONDS` (whole file deleted, DT-7). `PUBLIC_API_URL`
is **added** to `Settings` (it lived only in `.env.example` before, read by nothing).

##### Dependency additions — `backend/pyproject.toml`

Add to `[project].dependencies`:
- `kubernetes-asyncio>=32.0.0` — async k8s client for `KubeSessionManager` (`CustomObjectsApi` for
  `marimosessions`, `CoreV1Api` for Secret/Service), driven on the FastAPI event loop; in-cluster via
  `config.load_incluster_config()`, `load_kube_config()` for local `kube` testing (DT-7/DT-8).
- `authlib>=1.3.0` — OIDC discovery/JWKS/id-token verification adapter behind DT-4's `get_oidc_verifier`;
  without it `OIDC_PROVIDERS` is inert config. Named here because DT-12 introduces the OIDC settings the
  adapter consumes.

Neither is a `dev` group dependency — both run in production. `pydantic`/`pydantic-settings` (already
present) cover `AnyHttpUrl`, `BaseModel`, and JSON parsing of `OIDC_PROVIDERS`; no addition needed there.

##### Rejected alternatives

- **Nested `BaseSettings` sub-models (env `SESSION__BACKEND`).** Rejected: renames every env var, churns
  `.env`/`.env.example`, and buys only cosmetic grouping that ordered comment sections already give.
- **Keep `MAX_CONCURRENT_SESSIONS` as an optional soft cap for nicer 429s.** Rejected: DT-10's
  `QuotaExceeded` sentinel already yields a clean 429 from the authoritative `ResourceQuota`; a second
  app-side cap would be a competing capacity authority that can disagree with the cluster.
- **`NOTEBOOK_STORAGE_BACKEND` as `Literal["postgres","objectstore"]` now, with `OBJECT_STORE_*` keys.**
  Rejected: the object-store backend does not exist yet (DT-11); pre-adding its connection settings is the
  speculative config the clean-sweep forbids. The key is a one-value `Literal` until that backend lands.

##### Open questions

- **Second listener for `/api/internal/*` (DT-8 hardening).** DT-8 flagged a stronger posture: bind the
  internal router to a cluster-only port rather than protecting it on the main app with NetworkPolicy+token.
  If adopted, it adds an `INTERNAL_BIND_PORT`/listener setting here. Deferred as a human security decision;
  the token+NetworkPolicy baseline needs no config, so no key is added speculatively.
- **`SESSION_TOKEN_TTL_SECONDS` = effectively non-expiring?** DT-8's open question (wake racing a
  just-expired token → one-reconcile retry) offers an alternative of a non-expiring token revoked only via
  Secret GC. If chosen, this key is dropped, not merely re-defaulted. Flagged for the same human decision as
  DT-8, not settled here.

---

### DT-13 — Error taxonomy & service→HTTP translation
**Status:** complete

Design a shared error model that removes the ad-hoc, per-router mapping of service exceptions to `HTTPException`. Today each router hand-maps `ProcessManagerError` subclasses, `GitLabImportError`, `DuplicateUserError`, and `IntegrityError` with duplicated status codes and detail strings. Define a core error hierarchy (or exception→response contract) and a single translation layer (e.g. FastAPI exception handlers) so services raise domain errors and the boundary renders HTTP once, consistent with DT-3's typed denials.

- **Acceptance criteria:** a core error taxonomy (client-safe detail + status intent) and a single translation seam; a mapping of every current inline `HTTPException` raise to the new model, or a rationale for those that stay local; consistency with DT-3.
- **Likely files:** `backend/app/services/{process_manager,gitlab_import,auth_service}.py`, `backend/app/api/*.py`, `backend/app/main.py`.
- **Depends on:** DT-3.

#### DT-13 Design

One `DomainError` base, one registration seam, one HTTP renderer. Every domain task raises typed
errors derived from this base and lets them propagate; the boundary renders them to HTTP exactly
once. This section defines the shared base for the four hierarchies used by `AccessError` (DT-3),
`SessionManagerError` (DT-7), `GatewayError` (DT-9), and
`DuplicateUserError`/`GitLabImportError` (DT-4), and adds the
two generic leaves the remaining inline `HTTPException`s collapse into (`ConflictError`, `NotFoundError`),
with one shared renderer in `main.py`.

##### Target design — new module `backend/app/core/errors.py`

The base carries three things the boundary needs: a client-safe `detail` (the only string that ever
reaches the client), an HTTP `status` intent, and a `ws_close_code` for the WebSocket path (HTTP status
has no meaning on a socket). `str(exc)` is the *internal* message — logged, never sent.

```python
# app/core/errors.py
import logging
from fastapi import FastAPI, Request, status
from fastapi.responses import JSONResponse

logger = logging.getLogger("app.errors")

class DomainError(Exception):
    """Base for every error the HTTP/WS boundary renders.

    `str(self)` is the internal, loggable message; `self.detail` is the
    client-safe response body. Subclasses set `status` (and, on the gateway
    path, `ws_close_code`) as class attributes; `GitLabImportError` overrides
    `status` per instance.
    """
    status: int = status.HTTP_500_INTERNAL_SERVER_ERROR
    ws_close_code: int = status.WS_1011_INTERNAL_ERROR

    def __init__(self, message: str, *, detail: str | None = None) -> None:
        super().__init__(message)
        self.detail = detail if detail is not None else message

# ── generic leaves (the home for the ad-hoc inline 409/404 conflicts) ────────
class ConflictError(DomainError):     # domain-state conflict; NEVER an access decision
    status = status.HTTP_409_CONFLICT

class NotFoundError(DomainError):     # resource genuinely absent (NOT existence-hiding — cf. ResourceHidden)
    status = status.HTTP_404_NOT_FOUND

class Unauthenticated(DomainError):   # no/invalid credentials presented (login, token decode)
    status = status.HTTP_401_UNAUTHORIZED

# ── the single seam: one registration call, one renderer ─────────────────────
def register_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(DomainError)
    async def _render(request: Request, exc: DomainError) -> JSONResponse:
        if exc.status >= 500:
            # internal message (str(exc)), never the client detail; no stack — these are expected states
            logger.warning("%s %s → %d: %s", request.method, request.url.path, exc.status, exc)
        headers = {"WWW-Authenticate": "Bearer"} if exc.status == status.HTTP_401_UNAUTHORIZED else None
        return JSONResponse(status_code=exc.status, content={"detail": exc.detail}, headers=headers)

    @app.exception_handler(Exception)
    async def _unexpected(request: Request, exc: Exception) -> JSONResponse:
        logger.exception("unhandled error on %s %s", request.method, request.url.path)
        return JSONResponse(status_code=500, content={"detail": "Internal server error"})
```

##### Domain error hierarchies (signatures unchanged)

```python
# services/access.py (DT-3)
class AccessError(DomainError): ...                         # abstract; each leaf sets `status`
class ResourceHidden(AccessError):         status = 404
class AuthenticationRequired(AccessError): status = 401     # resource readable, write needs login
class PermissionDenied(AccessError):       status = 403

# services/session_manager.py (DT-7)
class SessionManagerError(DomainError): ...
class SessionCapacityError(SessionManagerError):  status = 429
class SessionStartError(SessionManagerError):     status = 503   # keeps (message, *, detail) ctor
class NotebookStartupError(SessionStartError):    status = 502
class SessionNotFoundError(SessionManagerError):  status = 404

# services/marimo_proxy.py (DT-9) — RECONCILED: DT-9's `http_status` field is renamed to the
#   unified `status`; `ws_close_code` stays. No other DT-9 change.
class GatewayError(DomainError): ...
class UpstreamNotFound(GatewayError):    status = 404; ws_close_code = 1008   # WS_1008_POLICY_VIOLATION
class UpstreamNotReady(GatewayError):    status = 503                         # ws_close_code default 1011
class UpstreamUnreachable(GatewayError): status = 502                         # ws_close_code default 1011

# services/auth_service.py (DT-4)
class DuplicateUserError(ConflictError):   # 409; default detail so `raise DuplicateUserError` still works
    def __init__(self) -> None:
        super().__init__("Username or email already exists")

# services/gitlab_import.py — per-instance status (422/413/404/502); constructor shape unchanged
class GitLabImportError(DomainError):
    def __init__(self, status_code: int, detail: str) -> None:
        super().__init__(detail, detail=detail)
        self.status = status_code
```

##### `main.py` wiring — the whole seam

```python
from app.core.errors import register_error_handlers
app = FastAPI(title="MarimoHub API", lifespan=lifespan)
register_error_handlers(app)
```

##### WebSocket path (DT-9 reconciliation)

FastAPI exception handlers do **not** fire for WebSocket routes, so the single HTTP renderer cannot
touch a socket. DT-13 makes `ws_close_code` a first-class attribute of `DomainError` so DT-9's
`proxy_websocket` collapses its two-branch close into one table-driven line:

```python
try:
    route = await resolver.resolve()
except DomainError as exc:           # GatewayError OR SessionManagerError from a wake
    await websocket.close(code=exc.ws_close_code)
    return
```

`UpstreamNotFound.ws_close_code == 1008`; every other domain error inherits the `1011` default, exactly
reproducing DT-9's contract (`UpstreamNotFound`→1008, all else→1011) with no `isinstance` ladder.

##### Explicit contracts

- **Client-safe detail policy.** The response body is always `{"detail": exc.detail}` and nothing else.
  `exc.detail` is the only client-facing string; `str(exc)` (the internal message, e.g.
  `SessionStartError`'s `message` vs its `detail`) is server-only. No exception ever renders its class
  name, args, `__cause__`, or a stack trace into the response.
- **Status is a property of the type** (class attribute), except `GitLabImportError`, whose upstream
  status is genuinely dynamic and rides on the instance. Adding a status/error means one new subclass
  with one `status` line — never a new mapping table or a new handler.
- **WS close codes.** `ws_close_code` defaults to `1011`; only `UpstreamNotFound` overrides it to `1008`.
  Owned by `proxy_websocket` (DT-9), fed by this attribute. HTTP `status` is ignored on the WS path.
- **401 carries `WWW-Authenticate: Bearer`.** Emitted by the renderer for *any* 401
  (`Unauthenticated`, `AuthenticationRequired`), so the header rule lives in one place, not per-raise.
- **Logging.** `status >= 500` → `logger.warning` with method/path/status/`str(exc)` (internal message),
  no stack (these are expected operational states: 502/503 wake failures, unreachable upstreams). 4xx →
  not logged (client-driven). Truly unexpected exceptions hit the `Exception` handler → `logger.exception`
  (full stack) + generic `{"detail": "Internal server error"}`, the only place a stack is captured and the
  guarantee no internal detail leaks on an unmapped path.
- **`IntegrityError` stays a local backstop.** It is caught at the raise site (which owns the
  `rollback()` and any re-query) and re-raised as the matching `DomainError` — `DuplicateUserError`
  (DT-4 `_provision`), `ConflictError` (DT-5 `add_member`, deployments slug race). It is never handled
  centrally: the boundary cannot know what to roll back or which detail to render.
- **Consistency with DT-3.** `AccessError` is a `DomainError` subtree rendered by the shared handler.
  The hide-vs-forbid decision (404/401/403) remains in DT-3; DT-13 only renders it.

##### Mapping — every current inline `HTTPException` raise → new model

| File · symbol | Current raise | New model |
|---|---|---|
| `auth.py` register try/except | `DuplicateUserError`→409 | delete try/except; `DuplicateUserError` (ConflictError) propagates |
| `auth.py` login `user is None` | 401 "Invalid username or password" | `raise Unauthenticated("Invalid username or password")` |
| `deps.py` `_auth_error` | 401 "Invalid authentication credentials" | delete `_auth_error`; `raise Unauthenticated(...)` |
| `notebooks.py` `_not_found`, owner 403 | 404 / 403 | deleted by DT-3/DT-6 (`ResourceHidden` / `PermissionDenied`) |
| `notebooks.py` import try/except | `GitLabImportError`→`exc.status_code` | delete try/except; `GitLabImportError` (DomainError) propagates |
| `data.py` `_ensure_notebook_exists` | notebook 404 | deleted by DT-6 (`load_notebook_for` READ → `ResourceHidden`) |
| `data.py` get-latest `data is None` | 404 "Notebook data not found" | `raise NotFoundError("Notebook data not found")` |
| `sessions.py` `_not_found`/`_auth_error`, create 503 catch | 404 / 401 / 503 | deleted by DT-3/DT-10; `SessionManagerError` propagates (429/502/503) |
| `sessions.py` save/delete `session is None`, `SessionNotFoundError` | 404 | `SessionNotFoundError`→404 via handler (DT-10) |
| `deployments.py` `_load_owned_notebook` | 404 / 403 | deleted by DT-3 (`authorize_notebook` WRITE) |
| `deployments.py` `_wake_deployment` 502/503, serving 503, WS closes | wake/serve errors | deleted by DT-9 (`GatewayError`/`SessionManagerError` + `proxy_websocket`) |
| `deployments.py` `_unique_slug` exhaustion 400 | "Unable to generate slug" | deleted with old `_unique_slug` (DT-5 shared `unique_slug`) |
| `deployments.py` slug-in-use (pre-check + `IntegrityError`) | 409 | `raise ConflictError("Deployment slug is already in use")` |
| `marimo_proxy.py` `_forward_http` httpx error | 502 | `raise UpstreamUnreachable(...)` (DT-9) → handler 502 |
| `proxy.py` WS 1008 | serving WS | deleted by DT-9 (`proxy_websocket` + `ws_close_code`) |
| `workspaces.py` slug-taken, already-member, last-owner, archive-state/user-delete conflicts (DT-5) | 409 | `raise ConflictError(...)` |
| `workspaces.py` user-not-found, member-not-found (DT-5) | 404 | `raise NotFoundError(...)` |

##### Taxonomy tree

```
DomainError                       (core/errors.py)          detail · status · ws_close_code
├── AccessError                   (services/access.py, DT-3)   hide-vs-forbid
│   ├── ResourceHidden            → 404
│   ├── AuthenticationRequired    → 401 (+WWW-Authenticate)
│   └── PermissionDenied          → 403
├── SessionManagerError           (services/session_manager.py, DT-7)
│   ├── SessionCapacityError      → 429
│   ├── SessionStartError         → 503
│   │   └── NotebookStartupError  → 502
│   └── SessionNotFoundError      → 404
├── GatewayError                  (services/marimo_proxy.py, DT-9)   +ws_close_code
│   ├── UpstreamNotFound          → 404 / WS 1008
│   ├── UpstreamNotReady          → 503 / WS 1011
│   └── UpstreamUnreachable       → 502 / WS 1011
├── ConflictError                 → 409   (DuplicateUserError, DT-4; workspace/deploy conflicts, DT-5/DT-10)
├── NotFoundError                 → 404   (genuinely-absent sub-resource)
├── Unauthenticated               → 401 (+WWW-Authenticate)   (login, token decode)
└── GitLabImportError             → 422/413/404/502 (per-instance)   (services/gitlab_import.py)
```

##### Deletions (by file / symbol)

- `main.py`: per-family exception handlers, replaced by `register_error_handlers(app)`.
- `api/deps.py`: `_auth_error`.
- `api/auth.py`: the `try/except DuplicateUserError → HTTPException` in `register`; the `HTTPException`
  import once no other raise remains.
- `api/notebooks.py`: the `try/except GitLabImportError → HTTPException(exc.status_code, …)` in import.
- `api/data.py`: the local `HTTPException(404, "Notebook data not found")` → `NotFoundError`.
- `services/gitlab_import.py`: `GitLabImportError.status_code` renamed into the `DomainError` shape
  (`status` + `detail`); base swaps `Exception` → `DomainError`.
- `services/auth_service.py`: `DuplicateUserError(Exception)` → `DuplicateUserError(ConflictError)`.
- Everything else in the mapping table is deleted by its owning task (DT-3/DT-6/DT-9/DT-10); DT-13 only
  guarantees the destination type exists and is rendered.

##### Rejected alternatives

- **A `dict[type[Exception], int]` registry + one handler** instead of a `DomainError` base — rejected:
  a registry can't carry per-instance status (`GitLabImportError`) or `ws_close_code`, and every new
  error edits a central map instead of declaring itself.
- **Per-family handlers** (`@app.exception_handler(AccessError)`, `(SessionManagerError)`, …) — rejected:
  that is the "translate in N places" the task exists to remove; the shared base gives one handler.
- **RFC-7807 `application/problem+json` bodies** — rejected: the current API returns `{"detail": …}`
  (FastAPI default) and clients depend on it; a media-type change is out of scope and buys nothing here.

##### Open questions

- **Two 401 classes** (`Unauthenticated` for authN failure vs DT-3 `AuthenticationRequired` for
  "resource readable, write needs login"). Both render identically (401 + Bearer); kept distinct for
  semantic clarity. Merging into one is a defensible simplification — flagged, non-blocking.
- **Blanket `@app.exception_handler(Exception)`.** Guarantees a JSON body and no leak on unmapped paths,
  but swallows the raw 500 some integration tests assert via `raise_server_exceptions=True`. If the test
  client relies on propagation, register it behind a settings flag (prod-on, test-off). Flagged for DT-12
  config / the test-rewrite fallout to confirm.
- **`SessionStartError.status = 503` as a class attribute** while the class also carries a per-instance
  `detail` — status is fixed, detail varies. Confirmed consistent (only `GitLabImportError` varies
  status per instance); noted so DT-7's `(message, *, detail)` ctor is not "upgraded" to also vary status.

---

## Design task index

| ID | Title | Status |
|---|---|---|
| DT-1 | Domain data-model restructure (workspaces, identities, visibility) | complete |
| DT-2 | Squashed Alembic baseline | complete |
| DT-3 | Shared authorization & access-control policy module | complete |
| DT-4 | Auth & identity service seam (JWT → OIDC/SAML) | complete |
| DT-5 | Workspace & membership management API | complete |
| DT-6 | Notebook & data API redesign onto workspaces (visibility & forking) | complete |
| DT-7 | Session runtime manager seam (subprocess ↔ Kube) | complete |
| DT-8 | MarimoSession CRD, controller, and internal source endpoint | complete |
| DT-9 | Proxy / gateway consolidation | complete |
| DT-10 | Deployment/session lifecycle & status source-of-truth | complete |
| DT-11 | Notebook source storage & edit-session persistence | complete |
| DT-12 | Configuration & runtime-settings surface | complete |
| DT-13 | Error taxonomy & service→HTTP translation | complete |

### Dependency sketch

```mermaid
flowchart TD
    DT1[DT-1 data model] --> DT2[DT-2 migration]
    DT1 --> DT3[DT-3 authz]
    DT1 --> DT4[DT-4 auth seam]
    DT1 --> DT6[DT-6 notebook/data API]
    DT3 --> DT5[DT-5 workspace API]
    DT3 --> DT6
    DT8[DT-8 CRD + controller + internal src] --> DT7[DT-7 session seam]
    DT7 --> DT9[DT-9 proxy consolidation]
    DT7 --> DT10[DT-10 lifecycle/status]
    DT8 --> DT10
    DT3 --> DT10
    DT7 --> DT11[DT-11 source storage]
    DT9 --> DT11
    DT7 --> DT12[DT-12 config]
    DT3 --> DT13[DT-13 error taxonomy]
```
