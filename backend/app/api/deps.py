from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Annotated
from uuid import UUID

from fastapi import Depends
from fastapi.security import OAuth2PasswordBearer
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import Unauthenticated
from app.core.security import decode_token
from app.db.database import get_db
from app.models import Notebook, User, Workspace, WorkspaceRole
from app.services.access import Action, load_notebook_for, load_workspace_for

oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/api/auth/login", auto_error=False)


async def get_current_user_optional(
    token: Annotated[str | None, Depends(oauth2_scheme)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> User | None:
    """Return the authenticated user for the bearer token, or ``None``."""
    if token is None:
        return None

    subject_id = decode_token(token)
    if subject_id is None:
        return None

    return await db.get(User, subject_id)


async def get_current_user(
    current_user: Annotated[User | None, Depends(get_current_user_optional)],
) -> User:
    """Return the authenticated user, raising 401 if not authenticated."""
    if current_user is None:
        raise Unauthenticated("Invalid authentication credentials")
    return current_user


@dataclass(frozen=True, slots=True)
class NotebookContext:
    """A notebook resolved and authorized for the current request."""

    notebook: Notebook
    actor: User | None
    role: WorkspaceRole | None  # actor's role in the notebook's workspace, or None


def require_notebook(action: Action) -> Callable[..., Awaitable[NotebookContext]]:
    """Build a dependency that loads and authorizes the `notebook_id` path param for `action`."""

    async def dep(
        notebook_id: UUID,
        db: Annotated[AsyncSession, Depends(get_db)],
        actor: Annotated[User | None, Depends(get_current_user_optional)],
    ) -> NotebookContext:
        notebook, role = await load_notebook_for(db, notebook_id, actor, action)
        return NotebookContext(notebook, actor, role)

    return dep


NotebookRead = Annotated[NotebookContext, Depends(require_notebook(Action.READ))]
NotebookWrite = Annotated[NotebookContext, Depends(require_notebook(Action.WRITE))]


@dataclass(frozen=True, slots=True)
class WorkspaceContext:
    """A workspace resolved and authorized for the current request."""

    workspace: Workspace
    actor: User
    role: WorkspaceRole  # caller's role, >= the endpoint's required minimum


def require_workspace(minimum: WorkspaceRole) -> Callable[..., Awaitable[WorkspaceContext]]:
    """Build a dependency that loads and authorizes the `workspace_id` path param."""

    async def dep(
        workspace_id: UUID,
        db: Annotated[AsyncSession, Depends(get_db)],
        actor: Annotated[User, Depends(get_current_user)],
    ) -> WorkspaceContext:
        workspace, role = await load_workspace_for(db, workspace_id, actor, minimum)
        return WorkspaceContext(workspace, actor, role)

    return dep


WorkspaceMemberDep = Annotated[WorkspaceContext, Depends(require_workspace(WorkspaceRole.VIEWER))]
WorkspaceOwnerDep = Annotated[WorkspaceContext, Depends(require_workspace(WorkspaceRole.OWNER))]
