import asyncio
from datetime import UTC, datetime
import shutil
from uuid import UUID, uuid4

from app.core.config import Settings
from app.models import Deployment, Notebook
from app.services.session_manager import (
    RuntimeMode,
    RuntimeRef,
    SessionInfo,
    SessionManagerError,
    SessionMode,
    SessionNotFoundError,
    SessionStartError,
    SessionTarget,
)
from app.services.subprocess_backend import readiness, runtime, workdir
from app.services.subprocess_backend.registry import LiveSession, LiveSessionRegistry
from app.services.subprocess_backend.runtime import MARIMO_HOST, CommandFactory


class SubprocessSessionManager:
    """Spawns, tracks, and tears down marimo notebook subprocesses.

    Implements the ``SessionManager`` protocol for local/dev use: sessions live
    only in this process's memory, so an API restart loses them (``shutdown``
    drains what it can). There is no capacity gate — ports are ephemeral
    OS-assigned loopback ports, not a finite reserved range.
    """

    def __init__(
        self,
        *,
        ready_timeout: float = 15.0,
        command_factory: CommandFactory | None = None,
    ) -> None:
        """Configure the readiness timeout and command builder."""
        self._registry = LiveSessionRegistry()
        self._ready_timeout = ready_timeout
        self._command_factory = command_factory or runtime.marimo_command
        # Held across an entire deployment spawn (not just the registry check)
        # so a concurrent call for the same deployment id waits for the first
        # to register, rather than racing a second subprocess into existence.
        self._deployment_spawn_lock = asyncio.Lock()
        self._shutting_down = False

    @classmethod
    def from_settings(cls, settings: Settings) -> "SubprocessSessionManager":
        """Build a subprocess session manager from application settings."""
        return cls(ready_timeout=settings.SESSION_READY_TIMEOUT_SECONDS)

    async def spawn(
        self, notebook: Notebook, mode: SessionMode, creator_id: UUID | None = None
    ) -> SessionInfo:
        """Spawn an edit or run session for a notebook and return its info."""
        if mode not in ("edit", "run"):
            raise ValueError("mode must be 'edit' or 'run'")
        return await self._spawn(
            notebook, mode, creator_id=creator_id, session_id=uuid4(), base_url=None
        )

    async def spawn_deployment(self, notebook: Notebook, deployment: Deployment) -> SessionInfo:
        """Spawn (or return the existing) deploy session for a deployment.

        Dev-mode only: unlike the Kube backend, this never distinguishes
        deploy revisions or a resolved image digest, so `deployment.revision`
        and `deployment.runtime_image` are unused here -- the subprocess
        always runs the notebook's current source.
        """
        async with self._deployment_spawn_lock:
            existing = await self._registry.get(deployment.id)
            if existing is not None:
                return existing.info()
            return await self._spawn(
                notebook,
                "deploy",
                creator_id=None,
                session_id=deployment.id,
                base_url=f"/api/deployments/{deployment.slug}",
            )

    def _ensure_not_shutting_down(self) -> None:
        if self._shutting_down:
            raise SessionStartError("Session manager is shutting down")

    async def _spawn(
        self,
        notebook: Notebook,
        mode: RuntimeMode,
        *,
        creator_id: UUID | None,
        session_id: UUID,
        base_url: str | None,
    ) -> SessionInfo:
        self._ensure_not_shutting_down()

        port = runtime.reserve_ephemeral_port()
        prep = workdir.prepare_workdir(notebook, mode, session_id, base_url)
        command = list(
            self._command_factory(prep.notebook_path, mode, port, prep.token, prep.base_url)
        )
        process: asyncio.subprocess.Process | None = None

        try:
            process = await runtime.start_marimo_process(command, prep.workdir, prep.stderr_path)
            await readiness.wait_until_ready(
                process,
                port,
                prep,
                session_id=session_id,
                ready_timeout=self._ready_timeout,
            )
            self._ensure_not_shutting_down()
            live = LiveSession(
                id=session_id,
                notebook_id=notebook.id,
                mode=mode,
                port=port,
                last_active=datetime.now(UTC),
                process=process,
                workdir=prep.workdir,
                token=prep.token,
                creator_id=creator_id,
                base_url=prep.base_url,
                cleanup_workdir=prep.cleanup_workdir,
            )
            await self._registry.register(live)
            return live.info()
        except BaseException as exc:
            if process is not None:
                await runtime.terminate_process(process)
            if prep.cleanup_workdir:
                shutil.rmtree(prep.workdir, ignore_errors=True)
            if isinstance(exc, SessionManagerError):
                raise
            if not isinstance(exc, Exception):
                raise
            raise SessionStartError("Notebook process failed to start") from exc

    async def get(self, session_id: UUID) -> SessionInfo | None:
        """Return a snapshot of the session, or ``None`` if not tracked."""
        session = await self._registry.get(session_id)
        return session.info() if session is not None else None

    async def target(self, session_id: UUID) -> SessionTarget | None:
        """Return proxy URLs and token for the session, or ``None``."""
        session = await self._registry.get(session_id)
        if session is None:
            return None
        return SessionTarget(
            http_base_url=f"http://{MARIMO_HOST}:{session.port}{session.base_url}",
            ws_base_url=f"ws://{MARIMO_HOST}:{session.port}{session.base_url}",
            access_token=session.token,
        )

    async def mark_active(self, session_id: UUID) -> None:
        """Mark the session as active as of now, if it is tracked."""
        session = await self._registry.get(session_id)
        if session is not None:
            session.last_active = datetime.now(UTC)

    async def stop(self, session_id: UUID) -> None:
        """Stop and remove a session, raising if it is not found."""
        session = await self._registry.pop(session_id)
        if session is None:
            raise SessionNotFoundError("Session not found")
        await self._stop_live(session)

    async def stop_workspace_sessions(self, workspace_id: UUID) -> None:
        """No-op: the local dev registry does not track a session's workspace.

        Local subprocess sessions are not workspace-labelled the way a Kube
        Runtime CR is, and this backend is dev-only, so bulk workspace
        cleanup is not implemented here; `app.services.workspace_service`
        still stops every tracked Deployment individually through `stop`.
        """
        return

    async def reconcilable_runtimes(self) -> list[RuntimeRef]:
        """Always empty: local dev sessions are not subject to the maintenance sweep."""
        return []

    async def shutdown(self) -> None:
        """Stop all tracked sessions.

        A spawn already in flight is not tracked here (registration happens
        only after readiness); it self-terminates via the ``_shutting_down``
        check in ``_spawn`` once it would otherwise succeed.
        """
        self._shutting_down = True
        sessions = await self._registry.drain()
        await asyncio.gather(
            *(self._stop_live(session) for session in sessions), return_exceptions=True
        )

    async def _stop_live(self, session: LiveSession) -> None:
        await runtime.terminate_process(session.process)
        if session.cleanup_workdir:
            shutil.rmtree(session.workdir, ignore_errors=True)
