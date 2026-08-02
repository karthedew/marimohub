"""Deployment runtime lifecycle: durable intent, locking, and cleanup.

Every mutation that can create or wake a Runtime -- deploy, redeploy, the
public wake-on-first-visit path, and stop -- follows the same protocol: lock
`Workspace` then `Deployment` (always in that order, so two concurrent
operations on the same pair can never deadlock waiting on each other),
persist the durable intent that decision requires, release the lock, and
only then perform the (possibly slow) cluster mutation and wait.

A database lock must never be held across a Kubernetes wait: Workspace
archival, a competing redeploy, and a competing stop all need to be able to
acquire the same locks promptly, and a multi-second Pod-creation wait held
under `SELECT ... FOR UPDATE` would serialize unrelated operations behind it
on a busy cluster. Real Postgres row locking, not an application-level
mutex, is what makes the recheck safe across backend replicas: two replicas
racing the same row serialize on the database itself.
"""

import contextlib
import hashlib
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings, get_settings
from app.core.errors import ConflictError, NotFoundError
from app.models import Deployment, DeploymentDesiredState, Notebook, Workspace
from app.services.notebook_storage import NotebookStorageService
from app.services.session_manager import SessionInfo, SessionManager, SessionNotFoundError

_UNCONFIGURED_RUNTIME_IMAGE = "unconfigured"


def resolve_runtime_image(settings: Settings | None = None) -> str:
    """Return the digest-qualified Runtime image a new deploy snapshot is pinned to.

    Phase 6's chart introduces a per-flavor digest map; until then this
    reuses the single configured `SESSION_RUNTIME_IMAGE`. The sentinel
    fallback keeps a snapshot's `runtime_image` non-empty -- the database
    requires that whenever a deployment is active -- even in a profile that
    never configures a real digest (local/dev). A real cluster would reject
    the resulting CR spec at admission, which is the correct failure mode for
    a misconfigured deployment rather than a silent success.
    """
    settings = settings or get_settings()
    return settings.SESSION_RUNTIME_IMAGE or _UNCONFIGURED_RUNTIME_IMAGE


async def lock_workspace(db: AsyncSession, workspace_id: UUID) -> Workspace:
    """Row-lock `workspace_id` without requiring it be active.

    Used by `stop`: stopping a Deployment must succeed even if the owning
    Workspace was archived a moment ago (archival already implies the same
    outcome), so only `lock_active_workspace` rejects an archived Workspace.
    """
    workspace = (
        await db.execute(select(Workspace).where(Workspace.id == workspace_id).with_for_update())
    ).scalar_one_or_none()
    if workspace is None:
        raise NotFoundError("Workspace not found")
    return workspace


async def lock_active_workspace(db: AsyncSession, workspace_id: UUID) -> Workspace:
    """Row-lock `workspace_id` and require it not be archived.

    Used by every operation that may *start* compute (deploy, redeploy, wake):
    once an archive commits, no later recheck here may pass, however far that
    archive's own cluster cleanup has gotten.
    """
    workspace = await lock_workspace(db, workspace_id)
    if workspace.archived_at is not None:
        raise ConflictError("Workspace is archived")
    return workspace


async def lock_deployment(db: AsyncSession, deployment_id: UUID) -> Deployment:
    """Row-lock `deployment_id`. Callers must lock the owning Workspace first."""
    deployment = (
        await db.execute(select(Deployment).where(Deployment.id == deployment_id).with_for_update())
    ).scalar_one_or_none()
    if deployment is None:
        raise NotFoundError("Deployment not found")
    return deployment


async def stop_if_running(manager: SessionManager, deployment_id: UUID) -> None:
    """Foreground-delete-and-wait a Deployment's Runtime, tolerating "already gone"."""
    with contextlib.suppress(SessionNotFoundError):
        await manager.stop(deployment_id)


async def deploy(
    db: AsyncSession,
    manager: SessionManager,
    notebook: Notebook,
    storage: NotebookStorageService,
    *,
    slug: str,
) -> Deployment:
    """Commit a new deploy-time snapshot and tear down any previous Runtime.

    The new CR is not created eagerly here -- the next visitor's wake (or an
    explicit follow-up request) creates it lazily, exactly like a first-ever
    deploy already does, so a deploy nobody visits costs no compute. What
    must happen synchronously is retiring the *old* Runtime: a redeploy
    reuses the same CR name, so the previous Pod (still serving the old
    snapshot) must be fully gone -- not just stale -- before anything can
    safely reuse that name.
    """
    source = await storage.get(notebook)
    source_sha256 = hashlib.sha256((source or "").encode("utf-8")).hexdigest()
    runtime_image = resolve_runtime_image()

    try:
        await lock_active_workspace(db, notebook.workspace_id)
        existing = (
            await db.execute(
                select(Deployment).where(Deployment.notebook_id == notebook.id).with_for_update()
            )
        ).scalar_one_or_none()
        is_redeploy = existing is not None
        deployment = existing or Deployment(notebook_id=notebook.id, slug=slug)
        if is_redeploy:
            deployment.slug = slug
        else:
            db.add(deployment)
            await db.flush()
        deployment.source_snapshot = source or ""
        deployment.source_sha256 = source_sha256
        deployment.runtime_image = runtime_image
        deployment.revision += 1
        deployment.desired_state = DeploymentDesiredState.ACTIVE
        await db.commit()
    except BaseException:
        await db.rollback()
        raise

    if is_redeploy:
        await stop_if_running(manager, deployment.id)
    return deployment


async def stop(
    db: AsyncSession, manager: SessionManager, notebook: Notebook, deployment_id: UUID
) -> None:
    """Commit `stopped` intent, then foreground-delete/wait the Runtime outside the lock.

    Intent commits first, unconditionally: a wake that manages to re-acquire
    the lock afterward will see `stopped` and refuse to run, whatever the
    cluster mutation below turns out to do.
    """
    try:
        await lock_workspace(db, notebook.workspace_id)
        deployment = await lock_deployment(db, deployment_id)
        deployment.desired_state = DeploymentDesiredState.STOPPED
        await db.commit()
    except BaseException:
        await db.rollback()
        raise

    await stop_if_running(manager, deployment_id)


async def ensure_running(
    db: AsyncSession, manager: SessionManager, notebook: Notebook, deployment_id: UUID
) -> SessionInfo:
    """Recheck durable intent, then wake or create the Runtime outside the lock.

    Only the recheck happens under the Workspace/Deployment lock; the
    cluster mutation and readiness wait -- which can take seconds -- happen
    after it is released, so a concurrent stop or archive is never blocked
    behind this call.
    """
    try:
        await lock_active_workspace(db, notebook.workspace_id)
        deployment = await lock_deployment(db, deployment_id)
        _require_active(deployment)
        await db.commit()
    except BaseException:
        await db.rollback()
        raise

    return await manager.spawn_deployment(notebook, deployment)


def _require_active(deployment: Deployment) -> None:
    if deployment.desired_state is not DeploymentDesiredState.ACTIVE:
        raise NotFoundError("Deployment is stopped")


async def resolve_target_info(
    manager: SessionManager, deployment: Deployment
) -> SessionInfo | None:
    """Return the Runtime's info, or `None` if it is not a routable match.

    A Runtime bound to a different, since-superseded revision than the
    locked Deployment row must never be treated as routable: routing it
    would serve a stale snapshot while a redeploy is in flight. A backend
    with no revision concept (dev-mode subprocess) reports `None` for
    `deployment_revision` and is never subject to this check.
    """
    info = await manager.get(deployment.id)
    if info is None:
        return None
    if info.deployment_revision is not None and info.deployment_revision != deployment.revision:
        return None
    return info
