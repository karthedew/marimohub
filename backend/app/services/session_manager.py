from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
import enum
from typing import Literal, Protocol, runtime_checkable
from uuid import UUID

from fastapi import status

from app.core.config import SessionBackend, get_settings
from app.core.errors import DomainError
from app.models import Deployment, Notebook

SessionMode = Literal["edit", "run"]
RuntimeMode = Literal["edit", "run", "deploy"]


class SessionPhase(enum.StrEnum):
    """Backend-neutral lifecycle phase of a session."""

    STARTING = "starting"
    READY = "ready"
    SLEEPING = "sleeping"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class SessionInfo:
    """Backend-neutral snapshot of a session's identity and lifecycle state.

    `failure_reason`/`message` are populated only when `phase` is `FAILED`
    and only by a backend that can classify failures against a stable
    contract (the Kube backend's Runtime Conditions); they carry a sanitized,
    bounded Reason/message an authorized caller may surface, never anything
    an anonymous Deployment visitor should see directly.

    `deployment_revision` mirrors the Runtime's bound deploy revision (deploy
    mode only) so a caller can detect a stale Runtime still serving a
    previous snapshot while a redeploy is in flight, without the manager
    itself needing to know about the backend's Deployment row.
    """

    id: UUID
    notebook_id: UUID
    mode: RuntimeMode
    phase: SessionPhase
    last_active: datetime | None
    creator_id: UUID | None
    failure_reason: str | None = None
    message: str | None = None
    deployment_revision: int | None = None


@dataclass(frozen=True, slots=True)
class SessionTarget:
    """Upstream URLs and access token for proxying to a session."""

    http_base_url: str
    ws_base_url: str
    access_token: str


@dataclass(frozen=True, slots=True)
class RuntimeRef:
    """A minimal, backend-neutral reference to a live Runtime for maintenance sweeps.

    Deliberately thinner than `SessionInfo`: the maintenance sweep only needs
    enough identity to decide whether the Runtime's owning state (Workspace,
    Notebook, Deployment) still justifies it, never its live phase or target.
    """

    id: UUID
    mode: RuntimeMode
    workspace_id: UUID
    notebook_id: UUID
    deployment_revision: int | None


@runtime_checkable
class SessionManager(Protocol):
    """The seam every caller depends on instead of a concrete backend."""

    async def spawn(
        self, notebook: Notebook, mode: SessionMode, creator_id: UUID | None = None
    ) -> SessionInfo:
        """Spawn an edit or run session for a notebook and return its info once ready."""
        ...

    async def spawn_deployment(self, notebook: Notebook, deployment: Deployment) -> SessionInfo:
        """Ensure `deployment`'s Runtime is running, waking or creating it as needed.

        Idempotent by `deployment.id`. Takes the whole row (not just its id
        and slug) because a Kube-backed manager must bind the Runtime spec to
        `deployment.revision` and `deployment.runtime_image`; a backend that
        doesn't distinguish deploy revisions may ignore those fields.
        """
        ...

    async def get(self, session_id: UUID) -> SessionInfo | None:
        """Return a snapshot of the session, or ``None`` if it is unknown."""
        ...

    async def target(self, session_id: UUID) -> SessionTarget | None:
        """Return proxy URLs and token for the session, or ``None`` if not serving."""
        ...

    async def mark_active(self, session_id: UUID) -> None:
        """Record activity for the session; a no-op for an unknown session."""
        ...

    async def stop(self, session_id: UUID) -> None:
        """Foreground-delete a session's Runtime and wait until it is gone.

        Raises ``SessionNotFoundError`` if the Runtime never existed. A
        caller stopping idempotently (it doesn't care whether one was
        running) suppresses that error rather than checking first, since
        checking-then-stopping is itself a race.
        """
        ...

    async def stop_workspace_sessions(self, workspace_id: UUID) -> None:
        """Best-effort stop every edit/run Runtime labelled with `workspace_id`.

        Deployment Runtimes are out of scope here -- callers stop those
        individually through `stop` using the Deployment rows they already
        track. Edit/run Sessions have no such row, so this is the only way to
        reach them in bulk; a backend with no such bulk primitive (or no
        Workspace-scoped Sessions at all) may no-op.
        """
        ...

    async def reconcilable_runtimes(self) -> Sequence[RuntimeRef]:
        """List every Runtime this manager can enumerate, for the maintenance sweep.

        A backend with no cluster-wide listing capability (or nothing worth
        reconciling this way, e.g. dev-mode subprocess) returns an empty
        sequence rather than raising.
        """
        ...

    async def shutdown(self) -> None:
        """Tear down every session this manager is responsible for."""
        ...


class SessionManagerError(DomainError):
    """Base for every session-manager failure."""


class SessionCapacityError(SessionManagerError):
    """Raised when the backend has no room to start another session."""

    status = status.HTTP_429_TOO_MANY_REQUESTS


class SessionStartError(SessionManagerError):
    """A session never reached a ready state.

    Carries a client-safe ``detail`` so the API layer can return an actionable
    message without leaking server internals (raw stderr stays in the log).
    """

    DEFAULT_DETAIL = "Deployment failed to wake"
    status = status.HTTP_503_SERVICE_UNAVAILABLE

    def __init__(self, message: str, *, detail: str | None = None) -> None:
        """Store the log message and the client-safe detail for the failure."""
        super().__init__(message, detail=detail or self.DEFAULT_DETAIL)


class NotebookStartupError(SessionStartError):
    """The runtime exited during startup.

    A deterministic failure (e.g. the source is not a runnable marimo notebook)
    that retrying won't fix.
    """

    status = status.HTTP_502_BAD_GATEWAY


class SessionNotFoundError(SessionManagerError):
    """Raised when an operation references a session that does not exist."""

    status = status.HTTP_404_NOT_FOUND


_manager_state: dict[str, SessionManager | None] = {"instance": None}


def get_session_manager() -> SessionManager:
    """Return the process-wide session manager, creating it on first use."""
    manager = _manager_state["instance"]
    if manager is None:
        settings = get_settings()
        # Imported lazily: both backends import this module for the protocol,
        # value objects, and errors they implement, so a top-level import here
        # would be circular.
        if settings.SESSION_BACKEND is SessionBackend.KUBE:
            from app.services.kube_session_manager import KubeSessionManager  # noqa: PLC0415

            manager = KubeSessionManager.from_settings(settings)
        else:
            from app.services.subprocess_backend import SubprocessSessionManager  # noqa: PLC0415

            manager = SubprocessSessionManager.from_settings(settings)
        _manager_state["instance"] = manager
    return manager


async def shutdown_session_manager() -> None:
    """Shut down and clear the process-wide session manager, if any."""
    manager = _manager_state["instance"]
    if manager is not None:
        await manager.shutdown()
        _manager_state["instance"] = None
