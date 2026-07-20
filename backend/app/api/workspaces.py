from datetime import datetime
from typing import Annotated, cast
from uuid import UUID

from fastapi import APIRouter, Depends, Response, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import WorkspaceMemberDep, WorkspaceOwnerDep, get_current_user
from app.core.errors import ConflictError, NotFoundError
from app.db.database import get_db
from app.models import User, Workspace, WorkspaceMember, WorkspaceRole
from app.schemas import (
    WorkspaceArchiveOut,
    WorkspaceCreate,
    WorkspaceMemberCreate,
    WorkspaceMemberOut,
    WorkspaceMemberUpdate,
    WorkspaceOut,
    WorkspaceUpdate,
)
from app.services import workspace_service
from app.services.access import get_role, load_archived_workspace_for_owner
from app.services.session_manager import SessionManager, get_session_manager
from app.services.slug import to_dns_label, unique_slug

router = APIRouter(prefix="/api/workspaces", tags=["workspaces"])


def _workspace_out(ws: Workspace, role: WorkspaceRole) -> WorkspaceOut:
    return WorkspaceOut(id=ws.id, slug=ws.slug, name=ws.name, role=role, created_at=ws.created_at)


def _workspace_archive_out(ws: Workspace, role: WorkspaceRole) -> WorkspaceArchiveOut:
    # archived_at/purge_after are nullable columns, but every caller of this helper
    # already filtered to archived_at IS NOT NULL, and the paired-null DB constraint
    # guarantees purge_after is set whenever archived_at is.
    return WorkspaceArchiveOut(
        id=ws.id,
        slug=ws.slug,
        name=ws.name,
        role=role,
        created_at=ws.created_at,
        archived_at=cast("datetime", ws.archived_at),
        purge_after=cast("datetime", ws.purge_after),
    )


async def _member_out(db: AsyncSession, member: WorkspaceMember) -> WorkspaceMemberOut:
    identity = (
        await db.execute(select(User.username, User.email).where(User.id == member.user_id))
    ).one()
    return WorkspaceMemberOut(
        workspace_id=member.workspace_id,
        user_id=member.user_id,
        username=identity.username,
        email=identity.email,
        role=member.role,
        created_at=member.created_at,
    )


@router.post("", response_model=WorkspaceOut, status_code=status.HTTP_201_CREATED)
async def create_workspace(
    payload: WorkspaceCreate,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
) -> WorkspaceOut:
    """Create a workspace; the caller becomes its sole owner in one transaction."""
    if payload.slug is not None:
        taken = await db.scalar(select(Workspace.id).where(Workspace.slug == payload.slug))
        if taken is not None:
            raise ConflictError("Workspace slug already in use")
        slug = payload.slug
    else:
        slug = await unique_slug(db, to_dns_label(payload.name), Workspace.slug)

    workspace = Workspace(slug=slug, name=payload.name)
    db.add(workspace)
    await db.flush()
    db.add(
        WorkspaceMember(
            workspace_id=workspace.id, user_id=current_user.id, role=WorkspaceRole.OWNER
        )
    )
    try:
        await db.commit()
    except IntegrityError as exc:
        await db.rollback()
        raise ConflictError("Workspace slug already in use") from exc
    await db.refresh(workspace)
    return _workspace_out(workspace, WorkspaceRole.OWNER)


@router.get("", response_model=list[WorkspaceOut])
async def list_my_workspaces(
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
) -> list[WorkspaceOut]:
    """List every active workspace the caller belongs to, oldest first."""
    rows = await db.execute(
        select(Workspace, WorkspaceMember.role)
        .join(WorkspaceMember, WorkspaceMember.workspace_id == Workspace.id)
        .where(WorkspaceMember.user_id == current_user.id, Workspace.archived_at.is_(None))
        .order_by(Workspace.created_at.asc())
    )
    return [_workspace_out(ws, role) for ws, role in rows.all()]


@router.get("/archived", response_model=list[WorkspaceArchiveOut])
async def list_archived_workspaces(
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
) -> list[WorkspaceArchiveOut]:
    """List archived workspaces the caller owns."""
    rows = await db.execute(
        select(Workspace, WorkspaceMember.role)
        .join(WorkspaceMember, WorkspaceMember.workspace_id == Workspace.id)
        .where(
            WorkspaceMember.user_id == current_user.id,
            WorkspaceMember.role == WorkspaceRole.OWNER,
            Workspace.archived_at.is_not(None),
        )
        .order_by(Workspace.archived_at.asc())
    )
    return [_workspace_archive_out(ws, role) for ws, role in rows.all()]


@router.get("/{workspace_id}", response_model=WorkspaceOut)
async def get_workspace(ctx: WorkspaceMemberDep) -> WorkspaceOut:
    """Return a single active workspace visible to the caller."""
    return _workspace_out(ctx.workspace, ctx.role)


@router.patch("/{workspace_id}", response_model=WorkspaceOut)
async def rename_workspace(
    payload: WorkspaceUpdate,
    ctx: WorkspaceOwnerDep,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> WorkspaceOut:
    """Rename a workspace the caller owns; the slug never changes."""
    ctx.workspace.name = payload.name
    await db.commit()
    await db.refresh(ctx.workspace)
    return _workspace_out(ctx.workspace, ctx.role)


@router.delete("/{workspace_id}", status_code=status.HTTP_204_NO_CONTENT, response_class=Response)
async def delete_workspace(
    ctx: WorkspaceOwnerDep,
    db: Annotated[AsyncSession, Depends(get_db)],
    manager: Annotated[SessionManager, Depends(get_session_manager)],
) -> Response:
    """Archive a workspace and stop its deployment runtimes."""
    await workspace_service.archive_workspace(db, manager, ctx.workspace)
    await db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/{workspace_id}/restore", response_model=WorkspaceOut)
async def restore_archived_workspace(
    workspace_id: UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
) -> WorkspaceOut:
    """Restore an archived workspace the caller owns, before its purge deadline."""
    workspace = await load_archived_workspace_for_owner(db, workspace_id, current_user)
    workspace_service.restore_workspace(workspace)
    await db.commit()
    await db.refresh(workspace)
    return _workspace_out(workspace, WorkspaceRole.OWNER)


@router.get("/{workspace_id}/members", response_model=list[WorkspaceMemberOut])
async def list_members(
    ctx: WorkspaceMemberDep, db: Annotated[AsyncSession, Depends(get_db)]
) -> list[WorkspaceMemberOut]:
    """List a workspace's members, oldest first."""
    rows = await db.execute(
        select(WorkspaceMember, User.username, User.email)
        .join(User, User.id == WorkspaceMember.user_id)
        .where(WorkspaceMember.workspace_id == ctx.workspace.id)
        .order_by(WorkspaceMember.created_at.asc())
    )
    return [
        WorkspaceMemberOut(
            workspace_id=member.workspace_id,
            user_id=member.user_id,
            username=username,
            email=email,
            role=member.role,
            created_at=member.created_at,
        )
        for member, username, email in rows.all()
    ]


@router.post(
    "/{workspace_id}/members",
    response_model=WorkspaceMemberOut,
    status_code=status.HTTP_201_CREATED,
)
async def add_member(
    payload: WorkspaceMemberCreate,
    ctx: WorkspaceOwnerDep,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> WorkspaceMemberOut:
    """Add a member to a workspace the caller owns, at any role including owner."""
    target = await db.get(User, payload.user_id)
    if target is None:
        raise NotFoundError("User not found")
    if await get_role(db, ctx.workspace.id, target.id) is not None:
        raise ConflictError("User is already a member")

    member = WorkspaceMember(workspace_id=ctx.workspace.id, user_id=target.id, role=payload.role)
    db.add(member)
    try:
        await db.commit()
    except IntegrityError as exc:
        await db.rollback()
        raise ConflictError("User is already a member") from exc
    await db.refresh(member)
    return WorkspaceMemberOut(
        workspace_id=member.workspace_id,
        user_id=member.user_id,
        username=target.username,
        email=target.email,
        role=member.role,
        created_at=member.created_at,
    )


@router.patch("/{workspace_id}/members/{user_id}", response_model=WorkspaceMemberOut)
async def change_member_role(
    user_id: UUID,
    payload: WorkspaceMemberUpdate,
    ctx: WorkspaceOwnerDep,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> WorkspaceMemberOut:
    """Change a member's role; the last owner cannot be demoted."""
    member = await db.get(WorkspaceMember, (ctx.workspace.id, user_id))
    if member is None:
        raise NotFoundError("Member not found")
    if (
        member.role == WorkspaceRole.OWNER
        and payload.role != WorkspaceRole.OWNER
        and await workspace_service.owner_count(db, ctx.workspace.id) == 1
    ):
        raise ConflictError("Workspace must retain at least one owner")

    member.role = payload.role
    await db.commit()
    return await _member_out(db, member)


@router.delete(
    "/{workspace_id}/members/{user_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    response_class=Response,
)
async def remove_member(
    user_id: UUID, ctx: WorkspaceOwnerDep, db: Annotated[AsyncSession, Depends(get_db)]
) -> Response:
    """Remove a member; the last owner cannot be removed."""
    member = await db.get(WorkspaceMember, (ctx.workspace.id, user_id))
    if member is None:
        raise NotFoundError("Member not found")
    is_sole_owner = member.role == WorkspaceRole.OWNER and (
        await workspace_service.owner_count(db, ctx.workspace.id) == 1
    )
    if is_sole_owner:
        raise ConflictError("Workspace must retain at least one owner")

    await db.delete(member)
    await db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)
