"""Workspace lifecycle rules that are not access decisions.

Archive/restore/purge and the locked user-deletion orchestration. Every access
decision still routes through `services/access.py`; this module owns the
domain state transitions and their `409` conflicts.
"""

import contextlib
from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.errors import ConflictError
from app.models import Deployment, Notebook, User, Workspace, WorkspaceMember, WorkspaceRole
from app.services.session_manager import SessionManager, SessionNotFoundError


async def owner_count(db: AsyncSession, workspace_id: UUID) -> int:
    """Return the number of `owner`-role members in `workspace_id`."""
    return (
        await db.scalar(
            select(func.count())
            .select_from(WorkspaceMember)
            .where(
                WorkspaceMember.workspace_id == workspace_id,
                WorkspaceMember.role == WorkspaceRole.OWNER,
            )
        )
        or 0
    )


async def stop_workspace_runtimes(
    db: AsyncSession, manager: SessionManager, workspace_id: UUID
) -> None:
    """Stop the running deployment for every notebook in `workspace_id`.

    Edit/run sessions are ephemeral and capability-scoped with no registry
    lookup by workspace, so deployments are the honest scope here: their DB
    rows keep their resting status regardless, and an archived workspace is
    unreachable through the API either way.
    """
    deployment_ids = await db.scalars(
        select(Deployment.id)
        .join(Notebook, Deployment.notebook_id == Notebook.id)
        .where(Notebook.workspace_id == workspace_id)
    )
    for deployment_id in deployment_ids:
        with contextlib.suppress(SessionNotFoundError):
            await manager.stop(deployment_id)


async def archive_workspace(
    db: AsyncSession, manager: SessionManager, workspace: Workspace
) -> None:
    """Stamp `workspace` archived with a fixed purge deadline and stop its runtimes."""
    now = datetime.now(UTC)
    workspace.archived_at = now
    workspace.purge_after = now + timedelta(days=get_settings().WORKSPACE_ARCHIVE_RETENTION_DAYS)
    await stop_workspace_runtimes(db, manager, workspace.id)


def restore_workspace(workspace: Workspace) -> None:
    """Clear a workspace's archive stamps, making it active again."""
    workspace.archived_at = None
    workspace.purge_after = None


async def purge_due_workspaces(db: AsyncSession, now: datetime) -> int:
    """Hard-delete workspaces whose purge deadline has passed; safe to call repeatedly.

    FK cascades remove each purged workspace's notebooks, deployments, data,
    and memberships as part of the same statement.
    """
    due_ids = (await db.scalars(select(Workspace.id).where(Workspace.purge_after <= now))).all()
    if due_ids:
        await db.execute(delete(Workspace).where(Workspace.id.in_(due_ids)))
    await db.commit()
    return len(due_ids)


async def delete_user(db: AsyncSession, user_id: UUID) -> None:
    """Delete `user_id` per the locked workspace/user lifecycle.

    Locks the user's memberships, hard-deletes any workspace where they are
    the sole member, and rejects (409) if they are the sole owner of any
    multi-member workspace. Otherwise their remaining memberships and the
    user row are removed. `workspace_members.user_id` is `ON DELETE RESTRICT`,
    so a direct delete of the user row cannot bypass this orchestration.
    """
    memberships = (
        (
            await db.execute(
                select(WorkspaceMember).where(WorkspaceMember.user_id == user_id).with_for_update()
            )
        )
        .scalars()
        .all()
    )

    sole_member_workspace_ids: list[UUID] = []
    for membership in memberships:
        workspace_id = membership.workspace_id
        member_count = await db.scalar(
            select(func.count())
            .select_from(WorkspaceMember)
            .where(WorkspaceMember.workspace_id == workspace_id)
        )
        if member_count == 1:
            sole_member_workspace_ids.append(workspace_id)
            continue
        if membership.role == WorkspaceRole.OWNER and await owner_count(db, workspace_id) == 1:
            raise ConflictError("User is the sole owner of a shared workspace")

    for workspace_id in sole_member_workspace_ids:
        await db.execute(delete(Workspace).where(Workspace.id == workspace_id))
    await db.execute(delete(WorkspaceMember).where(WorkspaceMember.user_id == user_id))
    await db.execute(delete(User).where(User.id == user_id))
    await db.commit()
