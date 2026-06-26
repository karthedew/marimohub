import asyncio
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from http import HTTPStatus
from http.client import HTTPResponse
import logging
import os
from pathlib import Path
import secrets
import shutil
import sys
import tempfile
from typing import Literal, cast
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import urlopen
from uuid import UUID, uuid4

from app.core.config import Settings, get_settings
from app.models import Notebook

logger = logging.getLogger(__name__)

SessionMode = Literal["edit", "run"]
RuntimeMode = Literal["edit", "run", "deploy"]
CommandFactory = Callable[[Path, RuntimeMode, int, str, str], Sequence[str]]

MARIMO_HOST = "127.0.0.1"
SHUTDOWN_TIMEOUT_SECONDS = 10.0
# Bound how much captured stderr reaches the server log on a failed start.
STDERR_LOG_LIMIT = 4000
MIN_TCP_PORT = 1
MAX_TCP_PORT = 65535
NOTEBOOK_START_DETAIL = "The notebook could not start. Check that it is a valid marimo notebook."


class ProcessManagerError(Exception):
    """Base class for process manager failures."""


class SessionCapacityError(ProcessManagerError):
    """Raised when the maximum number of concurrent sessions is reached."""


class PortAllocationError(ProcessManagerError):
    """Raised when no port is available in the configured range."""


class SessionStartError(ProcessManagerError):
    """A notebook process never reached a ready state.

    Carries a client-safe ``detail`` so the API layer can return an actionable
    message without leaking server internals (raw stderr stays in the log).
    """

    DEFAULT_DETAIL = "Deployment failed to wake"

    def __init__(self, message: str, *, detail: str | None = None) -> None:
        """Store the log message and the client-safe detail for the failure."""
        super().__init__(message)
        self.detail = detail or self.DEFAULT_DETAIL


class NotebookStartupError(SessionStartError):
    """The notebook process exited during startup.

    A deterministic failure (e.g. the source is not a runnable marimo notebook)
    that retrying won't fix.
    """


class SessionNotFoundError(ProcessManagerError):
    """Raised when an operation references a session that does not exist."""


@dataclass(frozen=True)
class SessionInfo:
    """Immutable snapshot of a managed session's identity and state."""

    id: UUID
    notebook_id: UUID
    mode: RuntimeMode
    port: int
    pid: int
    last_active: datetime
    creator_id: UUID | None


@dataclass(frozen=True)
class SessionTarget:
    """Upstream URLs and access token for proxying to a session."""

    http_base_url: str
    ws_base_url: str
    access_token: str


@dataclass
class ManagedSession:
    """A live notebook subprocess together with its bookkeeping state."""

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
    cleanup_workdir: bool

    def info(self) -> SessionInfo:
        """Return an immutable snapshot of this session's public state."""
        return SessionInfo(
            id=self.id,
            notebook_id=self.notebook_id,
            mode=self.mode,
            port=self.port,
            pid=self.pid,
            last_active=self.last_active,
            creator_id=self.creator_id,
        )


@dataclass(frozen=True)
class _SpawnPrep:
    workdir: Path
    cleanup_workdir: bool
    notebook_path: Path
    token: str
    base_url: str
    stderr_path: Path


class ProcessManager:
    """Spawns, tracks, and tears down marimo notebook subprocesses."""

    def __init__(
        self,
        *,
        port_range: str,
        max_concurrent_sessions: int,
        ready_timeout: float = 15.0,
        command_factory: CommandFactory | None = None,
    ) -> None:
        """Configure port allocation, capacity, readiness timeout, and command builder."""
        super().__init__()
        self._ports = _parse_port_range(port_range)
        self._max_concurrent_sessions = max_concurrent_sessions
        self._ready_timeout = ready_timeout
        self._command_factory = command_factory or _marimo_command
        self._sessions: dict[UUID, ManagedSession] = {}
        self._reserved_ports: set[int] = set()
        self._pending_spawns: set[asyncio.Task[SessionInfo]] = set()
        self._deployment_spawn_locks: dict[UUID, asyncio.Lock] = {}
        self._shutting_down = False
        self._lock = asyncio.Lock()

    @classmethod
    def from_settings(cls, settings: Settings) -> "ProcessManager":
        """Build a process manager from application settings."""
        return cls(
            port_range=settings.MARIMO_PORT_RANGE,
            max_concurrent_sessions=settings.MAX_CONCURRENT_SESSIONS,
            ready_timeout=settings.MARIMO_READY_TIMEOUT_SECONDS,
        )

    async def spawn(
        self, notebook: Notebook, mode: SessionMode, creator_id: UUID | None = None
    ) -> SessionInfo:
        """Spawn an edit or run session for a notebook and return its info."""
        if mode not in ("edit", "run"):
            raise ValueError("mode must be 'edit' or 'run'")

        return await self._spawn(
            notebook, mode, creator_id=creator_id, session_id=uuid4(), base_url=None
        )

    async def spawn_deployment(
        self, notebook: Notebook, deployment_id: UUID, slug: str
    ) -> SessionInfo:
        """Spawn (or return the existing) deploy session for a deployment."""
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
                    if (
                        self._sessions.get(deployment_id) is None
                        and self._deployment_spawn_locks.get(deployment_id) is spawn_lock
                    ):
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

        port = await self._reserve_port(spawn_task)
        prep = _prepare_workdir(notebook, mode, session_id, base_url)
        command = list(
            self._command_factory(prep.notebook_path, mode, port, prep.token, prep.base_url)
        )
        process: asyncio.subprocess.Process | None = None
        session: ManagedSession | None = None

        try:
            process = await _start_marimo_process(command, prep.workdir, prep.stderr_path)
            session = ManagedSession(
                id=session_id,
                notebook_id=notebook.id,
                mode=mode,
                port=port,
                pid=process.pid,
                last_active=datetime.now(UTC),
                process=process,
                workdir=prep.workdir,
                token=prep.token,
                creator_id=creator_id,
                base_url=prep.base_url,
                cleanup_workdir=prep.cleanup_workdir,
            )
            await self._register_session(session)
            await self._wait_until_ready(
                process,
                port,
                prep.token,
                prep.base_url,
                session_id=session_id,
                stderr_path=prep.stderr_path,
            )
            return await self._finalize_started_session(session)
        except BaseException as exc:
            await self._cleanup_failed_spawn(
                port=port,
                session=session,
                process=process,
                workdir=prep.workdir,
                cleanup_workdir=prep.cleanup_workdir,
            )
            if isinstance(exc, ProcessManagerError):
                raise
            if not isinstance(exc, Exception):
                raise
            raise SessionStartError("Notebook process failed to start") from exc
        finally:
            async with self._lock:
                self._pending_spawns.discard(spawn_task)

    async def _reserve_port(self, spawn_task: asyncio.Task[SessionInfo]) -> int:
        async with self._lock:
            if self._shutting_down:
                raise SessionStartError("Process manager is shutting down")
            if len(self._sessions) + len(self._reserved_ports) >= self._max_concurrent_sessions:
                raise SessionCapacityError("Maximum concurrent sessions reached")
            port = self._allocate_port()
            self._reserved_ports.add(port)
            self._pending_spawns.add(spawn_task)
            return port

    async def _register_session(self, session: ManagedSession) -> None:
        async with self._lock:
            if self._shutting_down:
                raise SessionStartError("Process manager is shutting down")
            self._reserved_ports.discard(session.port)
            self._sessions[session.id] = session

    async def _finalize_started_session(self, session: ManagedSession) -> SessionInfo:
        async with self._lock:
            if self._shutting_down or self._sessions.get(session.id) is not session:
                self._sessions.pop(session.id, None)
                raise SessionStartError("Process manager is shutting down")
            return session.info()

    async def _cleanup_failed_spawn(
        self,
        *,
        port: int,
        session: ManagedSession | None,
        process: asyncio.subprocess.Process | None,
        workdir: Path,
        cleanup_workdir: bool,
    ) -> None:
        async with self._lock:
            self._reserved_ports.discard(port)
            if session is not None:
                self._sessions.pop(session.id, None)
        if process is not None:
            await _terminate_process(process)
        if cleanup_workdir:
            shutil.rmtree(workdir, ignore_errors=True)

    async def stop(self, session_id: UUID) -> None:
        """Stop and remove a session, raising if it is not found."""
        async with self._lock:
            session = self._sessions.pop(session_id, None)
            self._deployment_spawn_locks.pop(session_id, None)
        if session is None:
            raise SessionNotFoundError("Session not found")
        await self._stop_session(session)

    async def shutdown(self) -> None:
        """Stop all sessions and await any in-flight spawns."""
        async with self._lock:
            self._shutting_down = True
            sessions = list(self._sessions.values())
            pending_spawns = [
                task for task in self._pending_spawns if task is not asyncio.current_task()
            ]
            self._sessions.clear()
            self._reserved_ports.clear()
            self._deployment_spawn_locks.clear()
        await asyncio.gather(
            *(self._stop_session(session) for session in sessions), return_exceptions=True
        )
        await asyncio.gather(*pending_spawns, return_exceptions=True)

    def get(self, session_id: UUID) -> SessionInfo | None:
        """Return a snapshot of the session, or ``None`` if not tracked."""
        session = self._sessions.get(session_id)
        return session.info() if session is not None else None

    def current_source(self, session_id: UUID) -> str | None:
        """Read the notebook source as marimo has autosaved it to disk.

        Lets an edit session's changes be persisted before the process is stopped.
        """
        session = self._sessions.get(session_id)
        if session is None:
            return None
        try:
            return (session.workdir / "notebook.py").read_text(encoding="utf-8")
        except OSError:
            return None

    def target(self, session_id: UUID) -> SessionTarget | None:
        """Return proxy URLs and token for the session, or ``None``."""
        session = self._sessions.get(session_id)
        if session is None:
            return None
        return SessionTarget(
            http_base_url=f"http://{MARIMO_HOST}:{session.port}{session.base_url}",
            ws_base_url=f"ws://{MARIMO_HOST}:{session.port}{session.base_url}",
            access_token=session.token,
        )

    def touch(self, session_id: UUID) -> None:
        """Mark the session as active as of now, if it is tracked."""
        session = self._sessions.get(session_id)
        if session is not None:
            session.last_active = datetime.now(UTC)

    @property
    def sessions(self) -> tuple[SessionInfo, ...]:
        """Snapshots of all currently tracked sessions."""
        return tuple(session.info() for session in self._sessions.values())

    def _allocate_port(self) -> int:
        used_ports = {session.port for session in self._sessions.values()} | self._reserved_ports
        for port in self._ports:
            if port not in used_ports:
                return port
        raise PortAllocationError("No marimo ports are available")

    async def _wait_until_ready(
        self,
        process: asyncio.subprocess.Process,
        port: int,
        token: str,
        base_url: str,
        *,
        session_id: UUID,
        stderr_path: Path,
    ) -> None:
        deadline = asyncio.get_running_loop().time() + self._ready_timeout
        while True:
            if process.returncode is not None:
                logger.error(
                    "Notebook process %s (%s) exited with code %s before ready; stderr:\n%s",
                    session_id,
                    base_url,
                    process.returncode,
                    _read_stderr(stderr_path),
                )
                raise NotebookStartupError(
                    "Notebook process exited before it was ready",
                    detail=NOTEBOOK_START_DETAIL,
                )
            if await _probe(port, token, base_url):
                return
            if asyncio.get_running_loop().time() >= deadline:
                logger.error(
                    "Notebook process %s (%s) did not become ready within %.1fs; stderr:\n%s",
                    session_id,
                    base_url,
                    self._ready_timeout,
                    _read_stderr(stderr_path),
                )
                raise SessionStartError(
                    "Notebook process did not become ready in time",
                    detail="Deployment did not start in time.",
                )
            await asyncio.sleep(0.1)

    async def _stop_session(self, session: ManagedSession) -> None:
        await _terminate_process(session.process)
        if session.cleanup_workdir:
            shutil.rmtree(session.workdir, ignore_errors=True)


def _parse_port_range(port_range: str) -> tuple[int, ...]:
    try:
        start_text, end_text = port_range.split("-", 1)
        start = int(start_text)
        end = int(end_text)
    except ValueError as exc:
        raise ValueError("MARIMO_PORT_RANGE must be formatted as start-end") from exc
    if start < MIN_TCP_PORT or end > MAX_TCP_PORT or start > end:
        raise ValueError("MARIMO_PORT_RANGE must describe valid TCP ports")
    return tuple(range(start, end + 1))


def _session_workdir(notebook: Notebook, mode: RuntimeMode) -> tuple[Path, bool]:
    if mode == "edit":
        root = Path(os.environ.get("MOLAB_NOTEBOOK_WORKDIR", ".molab-notebooks"))
        workdir = root / str(notebook.id)
        workdir.mkdir(parents=True, exist_ok=True)
        return workdir.resolve(), False
    return Path(tempfile.mkdtemp(prefix="molab-marimo-")), True


def _write_marimo_project_config(workdir: Path) -> None:
    workdir.joinpath("pyproject.toml").write_text(
        '[tool.marimo.save]\nautosave = "after_delay"\nautosave_delay = 1000\n',
        encoding="utf-8",
    )


def _prepare_workdir(
    notebook: Notebook, mode: RuntimeMode, session_id: UUID, base_url: str | None
) -> _SpawnPrep:
    workdir, cleanup_workdir = _session_workdir(notebook, mode)
    notebook_path = workdir / "notebook.py"
    # An edit session reuses an autosaved notebook on disk; every other mode
    # writes the stored source fresh.
    if not (mode == "edit" and notebook_path.exists()):
        notebook_path.write_text(notebook.source or "", encoding="utf-8")
    _write_marimo_project_config(workdir)
    return _SpawnPrep(
        workdir=workdir,
        cleanup_workdir=cleanup_workdir,
        notebook_path=notebook_path,
        token=secrets.token_urlsafe(24),
        base_url=base_url or f"/api/proxy/{session_id}",
        # Capture child stderr to a workdir file (not a pipe) so a long-lived
        # process can never block on a full pipe buffer, while a failed start
        # still leaves its diagnostics for the server log.
        stderr_path=workdir / "stderr.log",
    )


async def _start_marimo_process(
    command: Sequence[str], workdir: Path, stderr_path: Path
) -> asyncio.subprocess.Process:
    with stderr_path.open("wb") as stderr_file:
        return await asyncio.create_subprocess_exec(
            *command,
            cwd=workdir,
            env=_marimo_env(),
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=stderr_file,
        )


def _marimo_env() -> dict[str, str]:
    # Keep the kernel on the backend venv, but don't let marimo mistake the
    # backend project itself for the notebook's dependency project. Sandbox mode
    # tracks notebook deps in PEP 723 metadata and uses uv-managed envs.
    env = dict(os.environ)
    for key in ("UV", "UV_PROJECT_ENVIRONMENT"):
        env.pop(key, None)
    venv = env.get("VIRTUAL_ENV") or _current_virtualenv()
    if venv is not None:
        env["VIRTUAL_ENV"] = venv
        env["PATH"] = f"{Path(venv) / 'bin'}{os.pathsep}{env.get('PATH', '')}"
    uv_cache = Path(tempfile.gettempdir()) / "molab-uv-cache"
    uv_cache.mkdir(parents=True, exist_ok=True)
    env.setdefault("UV_CACHE_DIR", str(uv_cache))
    return env


def _current_virtualenv() -> str | None:
    executable = Path(sys.executable).resolve()
    if executable.parent.name == "bin" and executable.parent.parent.name == ".venv":
        return str(executable.parent.parent)
    return None


def _marimo_command(
    notebook_path: Path, mode: RuntimeMode, port: int, token: str, base_url: str
) -> Sequence[str]:
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
        "--sandbox",
    ]
    if mode == "edit":
        command.append("--skip-update-check")
    return command


def _read_stderr(path: Path) -> str:
    try:
        text = path.read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return ""
    if len(text) > STDERR_LOG_LIMIT:
        return f"...{text[-STDERR_LOG_LIMIT:]}"
    return text


async def _probe(port: int, token: str, base_url: str) -> bool:
    try:
        status = await asyncio.to_thread(_probe_sync, port, token, base_url)
    except (HTTPError, TimeoutError, URLError):
        return False
    else:
        return status == HTTPStatus.OK


def _probe_sync(port: int, token: str, base_url: str) -> int:
    # URL is built from a fixed http:// scheme and the loopback host constant, so
    # there is no user-controlled scheme for the S310 audit to be concerned with.
    url = f"http://{MARIMO_HOST}:{port}{base_url}/?access_token={quote(token)}"
    response = cast("HTTPResponse", urlopen(url, timeout=1.0))  # noqa: S310
    with response:
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


_manager_state: dict[str, ProcessManager | None] = {"instance": None}


def get_process_manager() -> ProcessManager:
    """Return the process-wide process manager, creating it on first use."""
    manager = _manager_state["instance"]
    if manager is None:
        manager = ProcessManager.from_settings(get_settings())
        _manager_state["instance"] = manager
    return manager


async def shutdown_process_manager() -> None:
    """Shut down and clear the process-wide process manager, if any."""
    manager = _manager_state["instance"]
    if manager is not None:
        await manager.shutdown()
        _manager_state["instance"] = None
