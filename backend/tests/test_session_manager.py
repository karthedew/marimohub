import asyncio
from collections.abc import Sequence
import os
from pathlib import Path
import shutil
import sys
from typing import cast
from uuid import uuid4

import pytest

from app.models import Notebook
from app.services.session_manager import (
    NotebookStartupError,
    SessionNotFoundError,
    SessionPhase,
    SessionStartError,
)
from app.services.subprocess_backend import (
    SubprocessSessionManager,
    readiness as readiness_module,
    runtime as runtime_module,
)
from app.services.subprocess_backend.workdir import SpawnWorkdir


def _stub_command(
    notebook_path: Path, mode: str, port: int, token: str, base_url: str
) -> Sequence[str]:
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


def _never_ready_command(
    notebook_path: Path, mode: str, port: int, token: str, base_url: str
) -> Sequence[str]:
    return [sys.executable, "-c", "import time; time.sleep(60)"]


def _failing_command(
    notebook_path: Path, mode: str, port: int, token: str, base_url: str
) -> Sequence[str]:
    return [
        sys.executable,
        "-c",
        "import sys; sys.stderr.write('boom: dependency import failed'); sys.exit(1)",
    ]


def _notebook(source: str = "print('hello')") -> Notebook:
    return Notebook(id=uuid4(), workspace_id=uuid4(), title="Runtime", source=source)


def _process_is_running(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


@pytest.mark.asyncio
async def test_spawn_tracks_session_and_stop_terminates_process() -> None:
    ports: list[int] = []

    def command_factory(
        notebook_path: Path, mode: str, port: int, token: str, base_url: str
    ) -> Sequence[str]:
        ports.append(port)
        return _stub_command(notebook_path, mode, port, token, base_url)

    manager = SubprocessSessionManager(command_factory=command_factory)

    session = await manager.spawn(_notebook("x = 1"), "run")
    tracked = await manager.get(session.id)
    live = await manager._registry.get(session.id)  # pyright: ignore[reportPrivateUsage]
    assert live is not None
    workdir = live.workdir

    assert tracked == session
    assert session.mode == "run"
    assert session.phase == SessionPhase.READY
    assert workdir.joinpath("notebook.py").read_text(encoding="utf-8") == "x = 1"
    target = await manager.target(session.id)
    assert target is not None
    assert target.http_base_url == f"http://127.0.0.1:{ports[0]}/api/proxy/{session.id}"
    assert target.ws_base_url == f"ws://127.0.0.1:{ports[0]}/api/proxy/{session.id}"

    await manager.stop(session.id)

    assert await manager.get(session.id) is None
    assert not workdir.exists()


@pytest.mark.asyncio
async def test_edit_session_uses_durable_autosave_workdir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MOLAB_NOTEBOOK_WORKDIR", str(tmp_path / "notebooks"))
    notebook = _notebook("x = 1")
    manager = SubprocessSessionManager(command_factory=_stub_command)

    session = await manager.spawn(notebook, "edit")
    live = await manager._registry.get(session.id)  # pyright: ignore[reportPrivateUsage]
    assert live is not None
    notebook_path = live.workdir / "notebook.py"
    notebook_path.write_text("x = 2  # autosaved by marimo", encoding="utf-8")
    assert live.workdir == (tmp_path / "notebooks" / str(notebook.id)).resolve()
    assert live.workdir.joinpath("pyproject.toml").read_text(encoding="utf-8") == (
        '[tool.marimo.save]\nautosave = "after_delay"\nautosave_delay = 1000\n'
    )

    await manager.stop(session.id)

    assert notebook_path.exists()
    assert notebook_path.read_text(encoding="utf-8") == "x = 2  # autosaved by marimo"


@pytest.mark.asyncio
async def test_concurrent_deployment_spawn_returns_existing_start(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ready = asyncio.Event()
    probe_started = asyncio.Event()
    commands: list[Sequence[str]] = []

    def command_factory(
        notebook_path: Path, mode: str, port: int, token: str, base_url: str
    ) -> Sequence[str]:
        command = _stub_command(notebook_path, mode, port, token, base_url)
        commands.append(command)
        return command

    async def delayed_probe(_port: int, _token: str, _base_url: str) -> bool:
        probe_started.set()
        await ready.wait()
        return True

    monkeypatch.setattr(readiness_module, "probe", delayed_probe)
    manager = SubprocessSessionManager(command_factory=command_factory)
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
    manager = SubprocessSessionManager(command_factory=_stub_command)

    session = await manager.spawn(_notebook(), "run")
    live = await manager._registry.get(session.id)  # pyright: ignore[reportPrivateUsage]
    assert live is not None
    pid = live.process.pid
    workdir = live.workdir

    assert _process_is_running(pid)
    assert workdir.exists()

    await manager.shutdown()

    assert await manager.get(session.id) is None
    assert not workdir.exists()
    assert not _process_is_running(pid)


@pytest.mark.asyncio
async def test_spawn_during_shutdown_terminates_late_arriving_process(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = SubprocessSessionManager(command_factory=_stub_command)
    pid: int | None = None
    workdir: Path | None = None
    shutdown_task: asyncio.Task[None] | None = None

    async def ready_once_shutdown_has_started(
        process: asyncio.subprocess.Process,
        _port: int,
        prep: object,
        *,
        session_id: object,
        ready_timeout: float,
    ) -> None:
        nonlocal pid, workdir, shutdown_task
        pid = process.pid
        workdir = cast("SpawnWorkdir", prep).stderr_path.parent
        shutdown_task = asyncio.create_task(manager.shutdown())
        await asyncio.sleep(0)

    monkeypatch.setattr(readiness_module, "wait_until_ready", ready_once_shutdown_has_started)

    spawn_task = asyncio.create_task(manager.spawn(_notebook(), "run"))

    with pytest.raises(SessionStartError):
        await spawn_task
    assert shutdown_task is not None
    await shutdown_task

    assert pid is not None
    assert workdir is not None
    assert not await asyncio.to_thread(workdir.exists)
    assert not _process_is_running(pid)


@pytest.mark.asyncio
async def test_wait_until_ready_honors_configured_timeout() -> None:
    manager = SubprocessSessionManager(ready_timeout=0.5, command_factory=_never_ready_command)

    started = asyncio.get_running_loop().time()
    try:
        with pytest.raises(SessionStartError) as exc_info:
            await manager.spawn(_notebook(), "run")
    finally:
        await manager.shutdown()
    elapsed = asyncio.get_running_loop().time() - started

    assert exc_info.value.detail == "Deployment did not start in time."
    assert elapsed < 5.0


@pytest.mark.asyncio
async def test_exited_process_logs_stderr_and_raises(caplog: pytest.LogCaptureFixture) -> None:
    manager = SubprocessSessionManager(ready_timeout=5.0, command_factory=_failing_command)

    with (
        caplog.at_level("ERROR", logger="app.services.subprocess_backend.readiness"),
        pytest.raises(NotebookStartupError) as exc_info,
    ):
        await manager.spawn(_notebook(), "run")

    assert (
        exc_info.value.detail
        == "The notebook could not start. Check that it is a valid marimo notebook."
    )
    assert "boom: dependency import failed" in caplog.text


def test_marimo_env_drops_uv_project_markers(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("UV", "/usr/bin/uv")
    monkeypatch.setenv("UV_PROJECT_ENVIRONMENT", "/srv/app/.venv")
    monkeypatch.setenv("VIRTUAL_ENV", "/srv/app/.venv")
    monkeypatch.delenv("UV_CACHE_DIR", raising=False)

    env = runtime_module.marimo_env()

    assert "UV" not in env
    assert "UV_PROJECT_ENVIRONMENT" not in env
    assert env["VIRTUAL_ENV"] == "/srv/app/.venv"
    assert env["PATH"].startswith(f"/srv/app/.venv/bin{os.pathsep}")
    assert env["UV_CACHE_DIR"].endswith("molab-uv-cache")


def test_marimo_command_runs_notebooks_in_uv_sandbox() -> None:
    notebook_path = "/work/notebook.py"
    command = runtime_module.marimo_command(
        Path(notebook_path),
        "edit",
        9000,
        "token",
        "/api/proxy/session-id",
    )

    assert "--sandbox" in command
    assert command[:3] == ["marimo", "edit", notebook_path]


def test_reserve_ephemeral_port_returns_bindable_port() -> None:
    port = runtime_module.reserve_ephemeral_port()

    assert 0 < port <= 65535


@pytest.mark.asyncio
async def test_stop_unknown_session_raises_domain_error() -> None:
    manager = SubprocessSessionManager(command_factory=_stub_command)

    with pytest.raises(SessionNotFoundError):
        await manager.stop(uuid4())


@pytest.mark.integration
@pytest.mark.asyncio
async def test_spawn_real_marimo_run_session() -> None:
    if shutil.which("marimo") is None:
        pytest.skip("marimo executable is not available")

    manager = SubprocessSessionManager()
    notebook = _notebook(
        """import marimo

__generated_with = '0.23.10'
app = marimo.App()

@app.cell
def _():
    'MarimoHub marimo process test'
    return

if __name__ == '__main__':
    app.run()
"""
    )

    session = await manager.spawn(notebook, "run")
    try:
        live = await manager._registry.get(session.id)  # pyright: ignore[reportPrivateUsage]
        assert live is not None
        assert live.process.pid > 0
    finally:
        await manager.shutdown()
