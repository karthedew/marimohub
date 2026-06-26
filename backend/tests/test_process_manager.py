import asyncio
from collections.abc import Sequence
import os
from pathlib import Path
import shutil
import socket
import sys
from uuid import uuid4

import pytest

from app.models import Notebook
from app.services import process_manager as process_manager_module
from app.services.process_manager import (
    ProcessManager,
    SessionCapacityError,
    SessionNotFoundError,
    SessionStartError,
)


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _stub_command(notebook_path: Path, mode: str, port: int, token: str, base_url: str) -> Sequence[str]:
    code = """
import http.server
import socketserver
import sys

port = int(sys.argv[1])

class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"ready")

    def log_message(self, format, *args):
        pass

with socketserver.TCPServer(("127.0.0.1", port), Handler) as server:
    server.serve_forever()
"""
    return [sys.executable, "-c", code, str(port)]


def _notebook(source: str = "print('hello')") -> Notebook:
    return Notebook(id=uuid4(), user_id=uuid4(), title="Runtime", source=source)


def _process_is_running(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


@pytest.mark.asyncio
async def test_spawn_tracks_session_and_stop_terminates_process() -> None:
    port = _free_port()
    manager = ProcessManager(port_range=f"{port}-{port}", max_concurrent_sessions=1, command_factory=_stub_command)

    session = await manager.spawn(_notebook("x = 1"), "run")
    tracked = manager.get(session.id)
    workdirs = [managed.workdir for managed in manager._sessions.values()]

    assert tracked == session
    assert session.mode == "run"
    assert session.port == port
    assert session.pid > 0
    assert len(manager.sessions) == 1
    assert workdirs[0].joinpath("notebook.py").read_text(encoding="utf-8") == "x = 1"
    assert manager.target(session.id) is not None
    assert manager.target(session.id).http_base_url == f"http://127.0.0.1:{port}/api/proxy/{session.id}"
    assert manager.target(session.id).ws_base_url == f"ws://127.0.0.1:{port}/api/proxy/{session.id}"

    await manager.stop(session.id)

    assert manager.get(session.id) is None
    assert not workdirs[0].exists()


@pytest.mark.asyncio
async def test_spawn_enforces_max_concurrent_sessions() -> None:
    first_port = _free_port()
    manager = ProcessManager(
        port_range=f"{first_port}-{first_port + 1}",
        max_concurrent_sessions=1,
        command_factory=_stub_command,
    )

    session = await manager.spawn(_notebook(), "edit")
    try:
        with pytest.raises(SessionCapacityError):
            await manager.spawn(_notebook(), "run")
    finally:
        await manager.shutdown()

    assert manager.get(session.id) is None


@pytest.mark.asyncio
async def test_concurrent_deployment_spawn_returns_existing_start(monkeypatch: pytest.MonkeyPatch) -> None:
    first_port = _free_port()
    ready = asyncio.Event()
    probe_started = asyncio.Event()
    commands: list[Sequence[str]] = []

    def command_factory(notebook_path: Path, mode: str, port: int, token: str, base_url: str) -> Sequence[str]:
        command = _stub_command(notebook_path, mode, port, token, base_url)
        commands.append(command)
        return command

    async def delayed_probe(_port: int, _token: str, _base_url: str) -> bool:
        probe_started.set()
        await ready.wait()
        return True

    monkeypatch.setattr(process_manager_module, "_probe", delayed_probe)
    manager = ProcessManager(
        port_range=f"{first_port}-{first_port + 1}",
        max_concurrent_sessions=2,
        command_factory=command_factory,
    )
    notebook = _notebook()
    deployment_id = uuid4()

    first = asyncio.create_task(manager.spawn_deployment(notebook, deployment_id, "runtime"))
    await probe_started.wait()
    second = asyncio.create_task(manager.spawn_deployment(notebook, deployment_id, "runtime"))
    await asyncio.sleep(0)
    ready.set()

    try:
        first_session, second_session = await asyncio.gather(first, second)
    finally:
        await manager.shutdown()

    assert first_session == second_session
    assert first_session.id == deployment_id
    assert len(commands) == 1


@pytest.mark.asyncio
async def test_shutdown_terminates_running_process_and_removes_workdir() -> None:
    port = _free_port()
    manager = ProcessManager(port_range=f"{port}-{port}", max_concurrent_sessions=1, command_factory=_stub_command)

    session = await manager.spawn(_notebook(), "run")
    managed = manager._sessions[session.id]
    pid = managed.pid
    workdir = managed.workdir

    assert _process_is_running(pid)
    assert workdir.exists()

    await manager.shutdown()

    assert manager.get(session.id) is None
    assert not workdir.exists()
    assert not _process_is_running(pid)


@pytest.mark.asyncio
async def test_shutdown_waits_for_spawn_that_becomes_ready_during_shutdown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    port = _free_port()
    manager = ProcessManager(port_range=f"{port}-{port}", max_concurrent_sessions=1, command_factory=_stub_command)
    pid: int | None = None
    workdir: Path | None = None
    shutdown_task: asyncio.Task[None] | None = None

    async def ready_after_shutdown_starts(_port: int, _token: str, _base_url: str) -> bool:
        nonlocal pid, workdir, shutdown_task
        managed = next(iter(manager._sessions.values()))
        pid = managed.pid
        workdir = managed.workdir
        shutdown_task = asyncio.create_task(manager.shutdown())
        await asyncio.sleep(0)
        return True

    monkeypatch.setattr(process_manager_module, "_probe", ready_after_shutdown_starts)

    spawn_task = asyncio.create_task(manager.spawn(_notebook(), "run"))

    with pytest.raises(SessionStartError):
        await spawn_task
    assert shutdown_task is not None
    await shutdown_task

    assert pid is not None
    assert workdir is not None
    assert len(manager.sessions) == 0
    assert not workdir.exists()
    assert not _process_is_running(pid)


@pytest.mark.asyncio
async def test_stop_unknown_session_raises_domain_error() -> None:
    port = _free_port()
    manager = ProcessManager(port_range=f"{port}-{port}", max_concurrent_sessions=1, command_factory=_stub_command)

    with pytest.raises(SessionNotFoundError):
        await manager.stop(uuid4())


@pytest.mark.integration
@pytest.mark.asyncio
async def test_spawn_real_marimo_run_session() -> None:
    if shutil.which("marimo") is None:
        pytest.skip("marimo executable is not available")

    port = _free_port()
    manager = ProcessManager(port_range=f"{port}-{port}", max_concurrent_sessions=1)
    notebook = _notebook(
        "import marimo\n\n"
        "__generated_with = '0.23.10'\n"
        "app = marimo.App()\n\n"
        "@app.cell\n"
        "def _():\n"
        "    'MoLab marimo process test'\n"
        "    return\n\n"
        "if __name__ == '__main__':\n"
        "    app.run()\n"
    )

    session = await manager.spawn(notebook, "run")
    try:
        assert session.port == port
        assert session.pid > 0
    finally:
        await manager.shutdown()
