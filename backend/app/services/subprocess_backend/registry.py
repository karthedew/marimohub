import asyncio
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from uuid import UUID

from app.services.session_manager import RuntimeMode, SessionInfo, SessionPhase


@dataclass
class LiveSession:
    """A live notebook subprocess together with its bookkeeping state."""

    id: UUID
    notebook_id: UUID
    mode: RuntimeMode
    port: int
    last_active: datetime
    process: asyncio.subprocess.Process
    workdir: Path
    token: str
    creator_id: UUID | None
    base_url: str
    cleanup_workdir: bool

    def info(self) -> SessionInfo:
        """Return an immutable, backend-neutral snapshot of this session."""
        return SessionInfo(
            id=self.id,
            notebook_id=self.notebook_id,
            mode=self.mode,
            # Only ever registered after readiness — see LiveSessionRegistry.
            phase=SessionPhase.READY,
            last_active=self.last_active,
            creator_id=self.creator_id,
        )


class LiveSessionRegistry:
    """Guarded map of live sessions — the sole in-memory source of truth."""

    def __init__(self) -> None:
        """Start with an empty session map."""
        self._sessions: dict[UUID, LiveSession] = {}
        self._lock = asyncio.Lock()

    async def register(self, session: LiveSession) -> None:
        """Track a session that has already reached readiness."""
        async with self._lock:
            self._sessions[session.id] = session

    async def get(self, session_id: UUID) -> LiveSession | None:
        """Return the live session, or ``None`` if it is not tracked."""
        async with self._lock:
            return self._sessions.get(session_id)

    async def pop(self, session_id: UUID) -> LiveSession | None:
        """Remove and return the live session, or ``None`` if untracked."""
        async with self._lock:
            return self._sessions.pop(session_id, None)

    async def drain(self) -> tuple[LiveSession, ...]:
        """Remove and return every tracked session."""
        async with self._lock:
            sessions = tuple(self._sessions.values())
            self._sessions.clear()
            return sessions
