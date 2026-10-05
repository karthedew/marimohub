"""Retryable maintenance sweep removing Runtimes whose owning state no longer justifies them.

Archive, stop, redeploy, notebook deletion, and workspace purge all commit
durable stop/archive intent and then foreground-delete the affected Runtime
themselves. This sweep is the backstop for the case those code paths cannot
handle alone: a process crash between committing that intent and performing
the matching cluster mutation. Run it on a schedule (cron, a Kubernetes
CronJob, ...) so compute a crash left behind never lingers indefinitely.

    uv run python -m app.commands.reconcile_runtimes
"""

import asyncio
import contextlib
import logging

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.database import get_sessionmaker
from app.models import Deployment, DeploymentDesiredState, Notebook, Workspace
from app.services.session_manager import (
    RuntimeRef,
    SessionManager,
    SessionNotFoundError,
    get_session_manager,
    shutdown_session_manager,
)

logger = logging.getLogger("app.commands.reconcile_runtimes")


async def _owner_missing_or_archived(db: AsyncSession, runtime: RuntimeRef) -> bool:
    """Whether `runtime`'s owning Workspace is gone/archived, or its Notebook is gone."""
    workspace = await db.get(Workspace, runtime.workspace_id)
    if workspace is None or workspace.archived_at is not None:
        return True
    notebook = await db.get(Notebook, runtime.notebook_id)
    return notebook is None


async def _deployment_stale(db: AsyncSession, runtime: RuntimeRef) -> bool:
    """Whether `runtime`'s bound Deployment is missing, stopped, or revision-stale."""
    deployment = await db.scalar(
        select(Deployment).where(Deployment.notebook_id == runtime.notebook_id)
    )
    if deployment is None or deployment.desired_state is not DeploymentDesiredState.ACTIVE:
        return True
    return deployment.revision != runtime.deployment_revision


async def _is_stale(db: AsyncSession, runtime: RuntimeRef) -> bool:
    """Whether `runtime`'s owning Workspace/Notebook/Deployment state no longer justifies it."""
    if await _owner_missing_or_archived(db, runtime):
        return True
    if runtime.mode != "deploy":
        return False
    return await _deployment_stale(db, runtime)


async def reconcile_stale_runtimes(db: AsyncSession, manager: SessionManager) -> int:
    """Delete every Runtime the manager can list whose owning state is stale.

    Safe to call repeatedly: a Runtime that is gone by the time it is
    checked, or that a concurrent operation has already stopped, is simply
    skipped rather than treated as an error.
    """
    removed = 0
    for runtime in await manager.reconcilable_runtimes():
        if await _is_stale(db, runtime):
            with contextlib.suppress(SessionNotFoundError):
                await manager.stop(runtime.id)
            removed += 1
    return removed


async def _run() -> int:
    manager = get_session_manager()
    try:
        async with get_sessionmaker()() as db:
            return await reconcile_stale_runtimes(db, manager)
    finally:
        # Releases the manager's Kubernetes client; without it every run
        # ends by logging "Unclosed client session".
        await shutdown_session_manager()


def main() -> None:
    """Run the reconciliation sweep once and log how many Runtimes were removed."""
    logging.basicConfig(level=logging.INFO)
    count = asyncio.run(_run())
    logger.info("removed %d stale runtime(s)", count)


if __name__ == "__main__":
    main()
