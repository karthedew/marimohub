# MarimoHub Schema Redesign: Workspaces, Visibility, Identities

Target: one squashed Alembic baseline that creates workspace ownership, split credentials/identities, archive lifecycle, and notebook visibility directly. Existing development databases are recreated; no historical-data backfill or old-revision compatibility is required.

## Target ERD

```
users ────────────< identities
  │                    (provider, subject)
  ├──────────── local_credentials (1:0..1)
  │
  └───< workspace_members >─── workspaces
              (role)              │ archived_at / purge_after
                                  │
                                  └───< notebooks ───< deployments
                                          │  visibility      (1:0..1)
                                          │  created_by → users
                                          ├───< notebook_data
                                          └──o parent_id (self, fork lineage)
```

## Tables

### users (modified)
Credentials removed; identity-neutral account record.

| column | type | notes |
|---|---|---|
| id | uuid PK | unchanged |
| username | varchar(255) UNIQUE NOT NULL | display handle; no longer a login credential |
| email | varchar(255) UNIQUE NOT NULL | canonical email |
| created_at | timestamptz NOT NULL default now() | |

**Dropped:** `password_hash` (moves to `local_credentials`).

### identities (new)
One row per (provider, external subject). A user may have several — Google + SAML + local.

| column | type | notes |
|---|---|---|
| id | uuid PK | |
| user_id | uuid FK users ON DELETE CASCADE, NOT NULL | |
| provider | varchar(64) NOT NULL | `local`, `google`, `oidc:<issuer-slug>`, `saml:<idp-slug>` |
| subject | varchar(255) NOT NULL | OIDC `sub` / SAML NameID / user id for local |
| email | varchar(255) NULL | provider-asserted email at last login |
| created_at | timestamptz NOT NULL | |
| last_login_at | timestamptz NULL | |

Constraints: `UNIQUE(provider, subject)`; index on `user_id`; partial unique index on `user_id`
where `provider = 'local'` (at most one local identity per user).

### local_credentials (new)
Kept separate from `identities` so password material never rides along on identity queries.

| column | type | notes |
|---|---|---|
| user_id | uuid PK, FK users ON DELETE CASCADE | 1:0..1 with users |
| password_hash | varchar(255) NOT NULL | bcrypt, unchanged format |
| updated_at | timestamptz NOT NULL | |

### workspaces (new)

| column | type | notes |
|---|---|---|
| id | uuid PK | |
| slug | varchar(63) UNIQUE NOT NULL | URL-safe; DNS-label charset so it can appear in k8s names later |
| name | text NOT NULL | |
| archived_at | timestamptz NULL | explicit delete archives and hides the workspace |
| purge_after | timestamptz NULL | persisted archive deadline, indexed |
| created_at | timestamptz NOT NULL | |

There is no personal-workspace subtype and registration creates no workspace. A workspace is explicitly
created; its creator becomes the first owner. A check constraint requires `archived_at` and
`purge_after` to be either both NULL or both set. Slugs remain reserved while archived.

### workspace_members (new)

| column | type | notes |
|---|---|---|
| workspace_id | uuid FK workspaces ON DELETE CASCADE | |
| user_id | uuid FK users ON DELETE RESTRICT | forces guarded user-deletion workflow |
| role | workspace_role enum NOT NULL | `owner` \| `editor` \| `viewer` |
| created_at | timestamptz NOT NULL | |

PK `(workspace_id, user_id)`. Index on `user_id` (the "my workspaces" query).

Role semantics: **owner** manages members + archives/restores workspace; **editor** creates/edits/deploys notebooks; **viewer** reads private notebooks. Every workspace retains at least one owner. User deletion hard-deletes workspaces where that user is the sole member, but is blocked while they are the last owner of a multi-member workspace.

### notebooks (modified)

| change | detail |
|---|---|
| drop `user_id` | replaced by workspace ownership |
| add `workspace_id` | uuid FK workspaces ON DELETE CASCADE, NOT NULL |
| add `created_by` | uuid FK users ON DELETE SET NULL, NULL — attribution survives user deletion |
| visibility enum | rename `draft` → `private`; keep `unlisted`, `public` |

Everything else (tags array, search_vector, embedding, fork_count, parent_id) stays. `fork_count` remains a maintained counter — accepted denormalization. Add index on `workspace_id`.

Visibility × membership access matrix:

| | non-member | viewer | editor/owner |
|---|---|---|---|
| private | — | read | read/write |
| unlisted | read via direct link | read | read/write |
| public | read/search/fork | read | read/write |

Forking a public notebook requires an explicit target workspace, lands with `visibility=private`, and sets `parent_id`.

### deployments (modified, minimal)
Drop `port` (meaningless under kube; the session Service is addressed by name — see CRD doc). Keep `status`, `slug`, `last_active` as the API-facing cache of controller status. No other change in this revision.

## Squashed migration baseline

```python
revision = "20260713_0001"
down_revision = None

def upgrade() -> None:
    # Create extensions and final enums directly:
    # workspace_role = owner|editor|viewer
    # notebook_visibility = private|unlisted|public
    # deployment_status = running|sleeping|stopped
    # Create the final users, identities, local_credentials, workspaces,
    # workspace_members, notebooks, deployments, and notebook_data tables.
    # Include generated FTS/pgvector columns and all constraints/indexes above.
    # Never create users.password_hash, notebooks.user_id, draft visibility,
    # deployments.port, is_personal, or personal_owner_id.
```

Notes:
- Delete the two existing development revision files and recreate development/CI databases.
- Workspace slug generation exists only in the workspace-creation service.
- `downgrade()` structurally drops baseline objects in reverse dependency order; it does not recreate the historical schema or discarded data.

## Query changes (the two that matter)

Discover/search filter:
```sql
WHERE EXISTS (SELECT 1 FROM workspaces w WHERE w.id = n.workspace_id AND w.archived_at IS NULL)
  AND (n.visibility = 'public'
   OR n.workspace_id IN (
         SELECT wm.workspace_id FROM workspace_members wm WHERE wm.user_id = :me
      ))
```

Write authorization (service layer):
```sql
SELECT wm.role FROM workspace_members wm
WHERE wm.workspace_id = :ws AND wm.user_id = :me
-- require role IN ('owner','editor') for mutations
```

## AuthService seam

`BasicAuthService` writes user + local identity + credentials atomically, but no workspace. New
`OIDCAuthService(AuthService)` resolves `(provider, sub)` first; on a miss it may attach the identity
to an existing canonical-email account only when the configured trusted provider asserts
`email_verified=true`, otherwise it JIT-provisions user + identity. SAML shops are brokered through
Dex/Keycloak so the app implements only OIDC. `create_access_token` / `decode_token` are unchanged.
