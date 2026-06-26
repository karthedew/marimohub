import asyncio
import contextlib
from datetime import UTC, datetime, timedelta

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import Settings, get_settings
from app.db.database import get_sessionmaker
from app.models import Deployment, DeploymentStatus
from app.services.process_manager import ProcessManager, SessionNotFoundError, get_process_manager

REAPER_INTERVAL_SECONDS = 60.0


async def mark_running_deployments_sleeping(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """Reset any deployments left ``RUNNING`` by a previous process to sleeping."""
    async with sessionmaker() as db:
        await db.execute(
            update(Deployment)
            .where(Deployment.status == DeploymentStatus.RUNNING)
            .values(status=DeploymentStatus.SLEEPING, port=None)
        )
        await db.commit()


class IdleDeploymentReaper:
    """Background task that puts idle deployments to sleep on an interval."""

    def __init__(
        self,
        *,
        sessionmaker: async_sessionmaker[AsyncSession],
        manager: ProcessManager,
        settings: Settings,
        interval_seconds: float = REAPER_INTERVAL_SECONDS,
    ) -> None:
        """Configure the reaper with its dependencies and idle timeout."""
        super().__init__()
        self._sessionmaker = sessionmaker
        self._manager = manager
        self._timeout = timedelta(minutes=settings.IDLE_TIMEOUT_MINUTES)
        self._interval_seconds = interval_seconds
        self._task: asyncio.Task[None] | None = None

    def start(self) -> None:
        """Start the background reaping loop if it is not already running."""
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        """Cancel the background reaping loop and wait for it to finish."""
        if self._task is None:
            return
        self._task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await self._task

    async def reap_once(self) -> None:
        """Sleep deployments whose last activity is older than the idle timeout."""
        cutoff = datetime.now(UTC) - self._timeout
        async with self._sessionmaker() as db:
            result = await db.scalars(
                select(Deployment).where(
                    Deployment.status == DeploymentStatus.RUNNING,
                    Deployment.last_active.is_not(None),
                    Deployment.last_active < cutoff,
                )
            )
            deployments = list(result)
            for deployment in deployments:
                live_session = self._manager.get(deployment.id)
                if live_session is not None and live_session.last_active >= cutoff:
                    deployment.last_active = live_session.last_active
                    db.add(deployment)
                    continue

                if live_session is not None:
                    with contextlib.suppress(SessionNotFoundError):
                        await self._manager.stop(deployment.id)
                deployment.status = DeploymentStatus.SLEEPING
                deployment.port = None
                db.add(deployment)
            await db.commit()

    async def _run(self) -> None:
        while True:
            await asyncio.sleep(self._interval_seconds)
            await self.reap_once()


async def start_deployment_lifecycle() -> IdleDeploymentReaper:
    """Reset stale deployments and start the idle reaper; return the reaper."""
    sessionmaker = get_sessionmaker()
    await mark_running_deployments_sleeping(sessionmaker)
    reaper = IdleDeploymentReaper(
        sessionmaker=sessionmaker,
        manager=get_process_manager(),
        settings=get_settings(),
    )
    reaper.start()
    return reaper
