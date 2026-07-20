import asyncio
from collections.abc import Callable, Sequence
import os
from pathlib import Path
import socket
import sys
import tempfile

from app.services.session_manager import RuntimeMode

MARIMO_HOST = "127.0.0.1"
SHUTDOWN_TIMEOUT_SECONDS = 10.0

CommandFactory = Callable[[Path, RuntimeMode, int, str, str], Sequence[str]]


def reserve_ephemeral_port() -> int:
    """Ask the OS for an unused loopback TCP port.

    Binding then immediately closing hands the port back to the kernel, which
    won't reissue it for the lifetime of this socket's TIME_WAIT window,
    making the handoff to the spawned subprocess race-free in practice.
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind((MARIMO_HOST, 0))
        return int(sock.getsockname()[1])


def marimo_command(
    notebook_path: Path, mode: RuntimeMode, port: int, token: str, base_url: str
) -> Sequence[str]:
    """Build the ``marimo`` CLI invocation for a runtime mode."""
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


def marimo_env() -> dict[str, str]:
    """Build the subprocess environment for a marimo runtime."""
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


async def start_marimo_process(
    command: Sequence[str], workdir: Path, stderr_path: Path
) -> asyncio.subprocess.Process:
    """Start the marimo subprocess, capturing stderr to a workdir file."""
    with stderr_path.open("wb") as stderr_file:
        return await asyncio.create_subprocess_exec(
            *command,
            cwd=workdir,
            env=marimo_env(),
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=stderr_file,
        )


async def terminate_process(process: asyncio.subprocess.Process) -> None:
    """Terminate a subprocess, escalating to kill if it ignores SIGTERM."""
    if process.returncode is not None:
        return
    process.terminate()
    try:
        await asyncio.wait_for(process.wait(), timeout=SHUTDOWN_TIMEOUT_SECONDS)
    except TimeoutError:
        process.kill()
        await process.wait()
