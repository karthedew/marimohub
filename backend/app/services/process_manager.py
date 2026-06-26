import asyncio
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
import shutil
import tempfile
from typing import Literal
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import urlopen
from uuid import UUID, uuid4
import secrets

from app.core.config import Settings, get_settings
from app.models import Notebook

SessionMode = Literal["edit", "run"]
RuntimeMode = Literal["edit", "run", "deploy"]
CommandFactory = Callable[[Path, RuntimeMode, int, str, str], Sequence[str]]

MARIMO_HOST = "127.0.0.1"
READINESS_TIMEOUT_SECONDS = 10.0
SHUTDOWN_TIMEOUT_SECONDS = 10.0


class ProcessManagerError(Exception):
    pass


class SessionCapacityError(ProcessManagerError):
    pass


class PortAllocationError(ProcessManagerError):
    pass


class SessionStartError(ProcessManagerError):
    pass


class SessionNotFoundError(ProcessManagerError):
    pass


@dataclass(frozen=True)
class SessionInfo:
    id: UUID
    notebook_id: UUID
    mode: RuntimeMode
    port: int
    pid: int
    last_active: datetime
    creator_id: UUID | None


@dataclass(frozen=True)
class SessionTarget:
    http_base_url: str
    ws_base_url: str
    access_token: str


@dataclass
class ManagedSession:
    id: UUID
    notebook_id: UUID
    mode: RuntimeMode
    port: int
    pid: int
    last_active: datetime
    process: asyncio.subprocess.Process
    workdir: Path
    token: str
    creator_id: UUID | None
    base_url: str

    def info(self) -> SessionInfo:
        return SessionInfo(
            id=self.id,
            notebook_id=self.notebook_id,
            mode=self.mode,
            port=self.port,
            pid=self.pid,
            last_active=self.last_active,
            creator_id=self.creator_id,
        )


class ProcessManager:
    def __init__(
        self,
        *,
        port_range: str,
        max_concurrent_sessions: int,
        command_factory: CommandFactory | None = None,
    ) -> None:
        self._ports = _parse_port_range(port_range)
        self._max_concurrent_sessions = max_concurrent_sessions
        self._command_factory = command_factory or _marimo_command
        self._sessions: dict[UUID, ManagedSession] = {}
        self._reserved_ports: set[int] = set()
        self._pending_spawns: set[asyncio.Task[SessionInfo]] = set()
        self._deployment_spawn_locks: dict[UUID, asyncio.Lock] = {}
        self._shutting_down = False
        self._lock = asyncio.Lock()

    @classmethod
    def from_settings(cls, settings: Settings) -> "ProcessManager":
        return cls(
            port_range=settings.MARIMO_PORT_RANGE,
            max_concurrent_sessions=settings.MAX_CONCURRENT_SESSIONS,
        )

    async def spawn(self, notebook: Notebook, mode: SessionMode, creator_id: UUID | None = None) -> SessionInfo:
        if mode not in ("edit", "run"):
            raise ValueError("mode must be 'edit' or 'run'")

        return await self._spawn(notebook, mode, creator_id=creator_id, session_id=uuid4(), base_url=None)

    async def spawn_deployment(self, notebook: Notebook, deployment_id: UUID, slug: str) -> SessionInfo:
        async with self._lock:
            spawn_lock = self._deployment_spawn_locks.get(deployment_id)
            if spawn_lock is None:
                spawn_lock = asyncio.Lock()
                self._deployment_spawn_locks[deployment_id] = spawn_lock

        async with spawn_lock:
            async with self._lock:
                session = self._sessions.get(deployment_id)
                if session is not None:
                    return session.info()

            try:
                return await self._spawn(
                    notebook,
                    "deploy",
                    creator_id=None,
                    session_id=deployment_id,
                    base_url=f"/api/deployments/{slug}",
                )
            finally:
                async with self._lock:
                    if self._sessions.get(deployment_id) is None and self._deployment_spawn_locks.get(deployment_id) is spawn_lock:
                        self._deployment_spawn_locks.pop(deployment_id, None)

    async def _spawn(
        self,
        notebook: Notebook,
        mode: RuntimeMode,
        *,
        creator_id: UUID | None,
        session_id: UUID,
        base_url: str | None,
    ) -> SessionInfo:
        spawn_task = asyncio.current_task()
        if spawn_task is None:
            raise SessionStartError("Notebook process must be spawned from a task")

        async with self._lock:
            if self._shutting_down:
                raise SessionStartError("Process manager is shutting down")
            if len(self._sessions) + len(self._reserved_ports) >= self._max_concurrent_sessions:
                raise SessionCapacityError("Maximum concurrent sessions reached")
            port = self._allocate_port()
            self._reserved_ports.add(port)
            self._pending_spawns.add(spawn_task)

        workdir = Path(tempfile.mkdtemp(prefix="molab-marimo-"))
        notebook_path = workdir / "notebook.py"
        notebook_path.write_text(notebook.source or "", encoding="utf-8")
        token = secrets.token_urlsafe(24)
        runtime_base_url = base_url or f"/api/proxy/{session_id}"
        command = list(self._command_factory(notebook_path, mode, port, token, runtime_base_url))
        process: asyncio.subprocess.Process | None = None
        session: ManagedSession | None = None

        try:
            process = await asyncio.create_subprocess_exec(
                *command,
                cwd=workdir,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            now = datetime.now(UTC)
            session = ManagedSession(
                id=session_id,
                notebook_id=notebook.id,
                mode=mode,
                port=port,
                pid=process.pid,
                last_active=now,
                process=process,
                workdir=workdir,
                token=token,
                creator_id=creator_id,
                base_url=runtime_base_url,
            )
            async with self._lock:
                if self._shutting_down:
                    raise SessionStartError("Process manager is shutting down")
                self._reserved_ports.discard(port)
                self._sessions[session.id] = session
            await self._wait_until_ready(process, port, token, runtime_base_url)
            async with self._lock:
                if self._shutting_down or self._sessions.get(session.id) is not session:
                    self._sessions.pop(session.id, None)
                    raise SessionStartError("Process manager is shutting down")
                return session.info()
        except BaseException as exc:
            async with self._lock:
                self._reserved_ports.discard(port)
                if session is not None:
                    self._sessions.pop(session.id, None)
            if process is not None:
                await _terminate_process(process)
            shutil.rmtree(workdir, ignore_errors=True)
            if isinstance(exc, ProcessManagerError):
                raise
            if not isinstance(exc, Exception):
                raise
            raise SessionStartError("Notebook process failed to start") from exc
        finally:
            async with self._lock:
                self._pending_spawns.discard(spawn_task)

    async def stop(self, session_id: UUID) -> None:
        async with self._lock:
            session = self._sessions.pop(session_id, None)
            self._deployment_spawn_locks.pop(session_id, None)
        if session is None:
            raise SessionNotFoundError("Session not found")
        await self._stop_session(session)

    async def shutdown(self) -> None:
        async with self._lock:
            self._shutting_down = True
            sessions = list(self._sessions.values())
            pending_spawns = [task for task in self._pending_spawns if task is not asyncio.current_task()]
            self._sessions.clear()
            self._reserved_ports.clear()
            self._deployment_spawn_locks.clear()
        await asyncio.gather(*(self._stop_session(session) for session in sessions), return_exceptions=True)
        await asyncio.gather(*pending_spawns, return_exceptions=True)

    def get(self, session_id: UUID) -> SessionInfo | None:
        session = self._sessions.get(session_id)
        return session.info() if session is not None else None

    def target(self, session_id: UUID) -> SessionTarget | None:
        session = self._sessions.get(session_id)
        if session is None:
            return None
        return SessionTarget(
            http_base_url=f"http://{MARIMO_HOST}:{session.port}{session.base_url}",
            ws_base_url=f"ws://{MARIMO_HOST}:{session.port}{session.base_url}",
            access_token=session.token,
        )

    def touch(self, session_id: UUID) -> None:
        session = self._sessions.get(session_id)
        if session is not None:
            session.last_active = datetime.now(UTC)

    @property
    def sessions(self) -> tuple[SessionInfo, ...]:
        return tuple(session.info() for session in self._sessions.values())

    def _allocate_port(self) -> int:
        used_ports = {session.port for session in self._sessions.values()} | self._reserved_ports
        for port in self._ports:
            if port not in used_ports:
                return port
        raise PortAllocationError("No marimo ports are available")

    async def _wait_until_ready(self, process: asyncio.subprocess.Process, port: int, token: str, base_url: str) -> None:
        deadline = asyncio.get_running_loop().time() + READINESS_TIMEOUT_SECONDS
        while True:
            if process.returncode is not None:
                raise SessionStartError("Notebook process exited before it was ready")
            if await _probe(port, token, base_url):
                return
            if asyncio.get_running_loop().time() >= deadline:
                raise SessionStartError("Notebook process did not become ready in time")
            await asyncio.sleep(0.1)

    async def _stop_session(self, session: ManagedSession) -> None:
        await _terminate_process(session.process)
        shutil.rmtree(session.workdir, ignore_errors=True)


def _parse_port_range(port_range: str) -> tuple[int, ...]:
    try:
        start_text, end_text = port_range.split("-", 1)
        start = int(start_text)
        end = int(end_text)
    except ValueError as exc:
        raise ValueError("MARIMO_PORT_RANGE must be formatted as start-end") from exc
    if start < 1 or end > 65535 or start > end:
        raise ValueError("MARIMO_PORT_RANGE must describe valid TCP ports")
    return tuple(range(start, end + 1))


def _marimo_command(notebook_path: Path, mode: RuntimeMode, port: int, token: str, base_url: str) -> Sequence[str]:
    marimo_mode = "run" if mode == "deploy" else mode
    command = [
        "marimo",
        marimo_mode,
        str(notebook_path),
        "--headless",
        "--host",
        MARIMO_HOST,
        "--port",
        str(port),
        "--token-password",
        token,
        "--base-url",
        base_url,
    ]
    if mode == "edit":
        command.append("--skip-update-check")
    return command


async def _probe(port: int, token: str, base_url: str) -> bool:
    try:
        status = await asyncio.to_thread(_probe_sync, port, token, base_url)
        return status == 200
    except (HTTPError, TimeoutError, URLError):
        return False


def _probe_sync(port: int, token: str, base_url: str) -> int:
    url = f"http://{MARIMO_HOST}:{port}{base_url}/?access_token={quote(token)}"
    with urlopen(url, timeout=1.0) as response:
        return response.status


async def _terminate_process(process: asyncio.subprocess.Process) -> None:
    if process.returncode is not None:
        return
    process.terminate()
    try:
        await asyncio.wait_for(process.wait(), timeout=SHUTDOWN_TIMEOUT_SECONDS)
    except TimeoutError:
        process.kill()
        await process.wait()


_process_manager: ProcessManager | None = None


def get_process_manager() -> ProcessManager:
    global _process_manager
    if _process_manager is None:
        _process_manager = ProcessManager.from_settings(get_settings())
    return _process_manager


async def shutdown_process_manager() -> None:
    global _process_manager
    if _process_manager is not None:
        await _process_manager.shutdown()
        _process_manager = None
