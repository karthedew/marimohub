"""Workspace lifecycle rules that are not access decisions.

Archive/restore/purge and the locked user-deletion orchestration. Every access
decision still routes through `services/access.py`; this module owns the
domain state transitions and their `409` conflicts.
"""

from datetime import UTC, datetime, timedelta
import logging
from uuid import UUID

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.errors import ConflictError
from app.models import (
    Deployment,
    DeploymentDesiredState,
    Notebook,
    User,
    Workspace,
    WorkspaceMember,
    WorkspaceRole,
)
from app.services import deployment_lifecycle
from app.services.session_manager import SessionManager

logger = logging.getLogger(__name__)


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


async def _stop_workspace_deployments(
    db: AsyncSession, manager: SessionManager, workspace_id: UUID
) -> None:
    """Foreground-delete-and-wait the Runtime for every Deployment in `workspace_id`."""
    deployment_ids = await db.scalars(
        select(Deployment.id)
        .join(Notebook, Deployment.notebook_id == Notebook.id)
        .where(Notebook.workspace_id == workspace_id)
    )
    for deployment_id in deployment_ids:
        await deployment_lifecycle.stop_if_running(manager, deployment_id)


async def stop_workspace_runtimes(
    db: AsyncSession, manager: SessionManager, workspace_id: UUID
) -> None:
    """Stop every Runtime -- Deployments and edit/run Sessions alike -- for `workspace_id`.

    Best-effort for the bulk edit/run cleanup: a cluster error there must not
    prevent the (already durably committed) archive/purge/delete from
    completing. `app.commands.reconcile_runtimes` is the backstop that
    retries whatever this call could not finish.
    """
    await _stop_workspace_deployments(db, manager, workspace_id)
    try:
        await manager.stop_workspace_sessions(workspace_id)
    except Exception:
        logger.exception(
            "stop_workspace_sessions failed for workspace %s; the maintenance sweep will retry",
            workspace_id,
        )


def _require_not_archived(workspace: Workspace) -> None:
    if workspace.archived_at is not None:
        raise ConflictError("Workspace is already archived")


async def archive_workspace(
    db: AsyncSession, manager: SessionManager, workspace_id: UUID
) -> Workspace:
    """Lock, archive, and stop every Runtime for `workspace_id`.

    Durable archive intent (`archived_at`/`purge_after` plus every owned
    Deployment's `desired_state`) commits under the Workspace lock before any
    cluster mutation is attempted, so a crash here still leaves the intent in
    place for `reconcile_stale_runtimes` to finish enforcing. Once that
    commit lands, no later `lock_active_workspace` recheck can pass, however
    long the cluster cleanup below takes.
    """
    try:
        workspace = await deployment_lifecycle.lock_workspace(db, workspace_id)
        _require_not_archived(workspace)
        now = datetime.now(UTC)
        workspace.archived_at = now
        workspace.purge_after = now + timedelta(
            days=get_settings().WORKSPACE_ARCHIVE_RETENTION_DAYS
        )
        notebook_ids = select(Notebook.id).where(Notebook.workspace_id == workspace_id)
        deployments = (
            (
                await db.execute(
                    select(Deployment)
                    .where(Deployment.notebook_id.in_(notebook_ids))
                    .with_for_update()
                )
            )
            .scalars()
            .all()
        )
        for deployment in deployments:
            deployment.desired_state = DeploymentDesiredState.STOPPED
        await db.commit()
    except BaseException:
        await db.rollback()
        raise

    await stop_workspace_runtimes(db, manager, workspace_id)
    return workspace


def restore_workspace(workspace: Workspace) -> None:
    """Clear a workspace's archive stamps, making it active again."""
    workspace.archived_at = None
    workspace.purge_after = None


async def purge_due_workspaces(db: AsyncSession, manager: SessionManager, now: datetime) -> int:
    """Hard-delete workspaces whose purge deadline has passed; safe to call repeatedly.

    Every due workspace was already archived (and so already had its
    Runtimes stopped) at archive time; stopping them again here is a
    crash-safety backstop, not the primary cleanup path, in case that earlier
    cluster cleanup never finished. FK cascades remove each purged
    workspace's notebooks, deployments, data, and memberships as part of the
    same statement.
    """
    due_ids = (await db.scalars(select(Workspace.id).where(Workspace.purge_after <= now))).all()
    for workspace_id in due_ids:
        await stop_workspace_runtimes(db, manager, workspace_id)
    if due_ids:
        await db.execute(delete(Workspace).where(Workspace.id.in_(due_ids)))
    await db.commit()
    return len(due_ids)


async def delete_user(db: AsyncSession, manager: SessionManager, user_id: UUID) -> None:
    """Delete `user_id` per the locked workspace/user lifecycle.

    Locks the user's memberships, hard-deletes any workspace where they are
    the sole member, and rejects (409) if they are the sole owner of any
    multi-member workspace. Otherwise their remaining memberships and the
    user row are removed. `workspace_members.user_id` is `ON DELETE RESTRICT`,
    so a direct delete of the user row cannot bypass this orchestration.

    A workspace removed this way loses its last owning relationship, so its
    Runtimes are stopped before the row is dropped -- the same cleanup
    archive and purge perform, just triggered by user deletion instead.
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
        await stop_workspace_runtimes(db, manager, workspace_id)
    for workspace_id in sole_member_workspace_ids:
        await db.execute(delete(Workspace).where(Workspace.id == workspace_id))
    await db.execute(delete(WorkspaceMember).where(WorkspaceMember.user_id == user_id))
    await db.execute(delete(User).where(User.id == user_id))
    await db.commit()
