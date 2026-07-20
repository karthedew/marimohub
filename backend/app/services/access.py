"""Shared authorization policy: the access matrix and the 401/403/404 boundary.

Pure decision (`can_access`), membership lookup (`get_role`), and resource
loaders (`authorize_*`/`load_*_for`) live here so no router builds its own
ownership or visibility check. Request-scoped and read-only: every function
takes the caller's `AsyncSession` and never commits. No FastAPI imports —
those live in `app/api/deps.py`.
"""

import enum
from uuid import UUID

from sqlalchemy import ColumnElement, and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import DomainError
from app.models import Notebook, NotebookVisibility, User, Workspace, WorkspaceMember, WorkspaceRole

_ROLE_AUTHORITY = {WorkspaceRole.VIEWER: 0, WorkspaceRole.EDITOR: 1, WorkspaceRole.OWNER: 2}


class Action(enum.StrEnum):
    """The two access decisions the policy makes; every route reduces to one of these."""

    READ = "read"
    WRITE = "write"


class AccessError(DomainError):
    """Base for a denied access decision; `detail` is always client-safe."""


class ResourceHidden(AccessError):  # noqa: N818 -- named for the existence decision, not "Error" noise
    """The actor cannot read the resource, so its existence must not be revealed."""

    status = 404


class AuthenticationRequired(AccessError):  # noqa: N818 -- named for the decision, not "Error" noise
    """The resource is readable, but the action requires an identified actor."""

    status = 401


class PermissionDenied(AccessError):  # noqa: N818 -- named for the decision, not "Error" noise
    """The actor is identified but holds insufficient role for the action."""

    status = 403


def role_at_least(role: WorkspaceRole | None, minimum: WorkspaceRole) -> bool:
    """Return whether `role` meets `minimum` on the owner > editor > viewer ordering."""
    if role is None:
        return False
    return _ROLE_AUTHORITY[role] >= _ROLE_AUTHORITY[minimum]


def can_access(visibility: NotebookVisibility, role: WorkspaceRole | None, action: Action) -> bool:
    """The entire notebook access matrix: pure, total, no I/O."""
    if action is Action.READ:
        if visibility in (NotebookVisibility.PUBLIC, NotebookVisibility.UNLISTED):
            return True
        return role is not None
    return role_at_least(role, WorkspaceRole.EDITOR)


async def get_role(
    db: AsyncSession, workspace_id: UUID, user_id: UUID | None
) -> WorkspaceRole | None:
    """Return the caller's role in `workspace_id`, or `None` for anonymous/non-members alike."""
    if user_id is None:
        return None
    return await db.scalar(
        select(WorkspaceMember.role).where(
            WorkspaceMember.workspace_id == workspace_id, WorkspaceMember.user_id == user_id
        )
    )


def _deny(actor: User | None, can_read: bool, resource: str) -> AccessError:  # noqa: FBT001
    # Visibility-first: a missing id and a private resource you can't see are
    # indistinguishable, so the read decision alone picks 404 vs. 401/403.
    if not can_read:
        return ResourceHidden(f"{resource} not found")
    if actor is None:
        return AuthenticationRequired("Authentication required")
    return PermissionDenied(f"{resource} editor role required")


async def authorize_notebook(
    db: AsyncSession, notebook: Notebook, actor: User | None, action: Action
) -> WorkspaceRole | None:
    """Return the actor's role if `action` is allowed on `notebook`; else raise `AccessError`."""
    active = await db.scalar(
        select(Workspace.id).where(
            Workspace.id == notebook.workspace_id, Workspace.archived_at.is_(None)
        )
    )
    if active is None:
        raise ResourceHidden("Notebook not found")
    role = await get_role(db, notebook.workspace_id, actor.id if actor else None)
    if can_access(notebook.visibility, role, action):
        return role
    raise _deny(actor, can_access(notebook.visibility, role, Action.READ), "Notebook")


async def load_notebook_for(
    db: AsyncSession, notebook_id: UUID, actor: User | None, action: Action
) -> tuple[Notebook, WorkspaceRole | None]:
    """Load and authorize a notebook by id; missing and hidden both raise `ResourceHidden`."""
    notebook = await db.scalar(
        select(Notebook)
        .join(Workspace)
        .where(Notebook.id == notebook_id, Workspace.archived_at.is_(None))
    )
    if notebook is None:
        raise ResourceHidden("Notebook not found")
    return notebook, await authorize_notebook(db, notebook, actor, action)


async def authorize_workspace(
    db: AsyncSession, workspace_id: UUID, actor: User, minimum: WorkspaceRole
) -> WorkspaceRole:
    """Require `actor` to hold at least `minimum` role in `workspace_id`.

    Missing, archived, and non-member workspaces are indistinguishable 404s —
    a workspace has no public read axis, so membership is the existence gate.
    """
    active = await db.scalar(
        select(Workspace.id).where(Workspace.id == workspace_id, Workspace.archived_at.is_(None))
    )
    if active is None:
        raise ResourceHidden("Workspace not found")
    role = await get_role(db, workspace_id, actor.id)
    if role is None:
        raise ResourceHidden("Workspace not found")
    if not role_at_least(role, minimum):
        raise PermissionDenied("Insufficient workspace permissions")
    return role


async def load_workspace_for(
    db: AsyncSession, workspace_id: UUID, actor: User, minimum: WorkspaceRole
) -> tuple[Workspace, WorkspaceRole]:
    """Load and authorize a workspace by id, for callers that need the object."""
    workspace = await db.scalar(
        select(Workspace).where(Workspace.id == workspace_id, Workspace.archived_at.is_(None))
    )
    role = await get_role(db, workspace_id, actor.id) if workspace is not None else None
    if role is None or workspace is None:
        raise ResourceHidden("Workspace not found")
    if not role_at_least(role, minimum):
        raise PermissionDenied("Insufficient workspace permissions")
    return workspace, role


async def load_archived_workspace_for_owner(
    db: AsyncSession, workspace_id: UUID, actor: User
) -> Workspace:
    """Load an archived workspace for its owner; missing, active, or non-owner all deny.

    Normal policy helpers (`load_workspace_for`) intentionally never return
    archived rows, so the archive list/restore surface uses this separate
    owner-only loader instead.
    """
    workspace = await db.scalar(
        select(Workspace).where(Workspace.id == workspace_id, Workspace.archived_at.is_not(None))
    )
    role = await get_role(db, workspace_id, actor.id) if workspace is not None else None
    if workspace is None or role is None:
        raise ResourceHidden("Workspace not found")
    if not role_at_least(role, WorkspaceRole.OWNER):
        raise PermissionDenied("Insufficient workspace permissions")
    return workspace


def visible_notebooks(user_id: UUID | None) -> ColumnElement[bool]:
    """Discovery predicate: active workspace AND (public OR one of my memberships)."""
    public = Notebook.visibility == NotebookVisibility.PUBLIC
    active = Notebook.workspace.has(Workspace.archived_at.is_(None))
    if user_id is None:
        return and_(active, public)
    mine = select(WorkspaceMember.workspace_id).where(WorkspaceMember.user_id == user_id)
    return and_(active, or_(public, Notebook.workspace_id.in_(mine)))
