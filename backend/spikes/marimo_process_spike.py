from __future__ import annotations

import argparse
import contextlib
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import urlopen


HOST = "127.0.0.1"
TOKEN = "molab-spike-token"


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Spawn marimo edit/run subprocesses and verify readiness/shutdown."
    )
    parser.add_argument(
        "--mode",
        choices=("edit", "run", "both"),
        default="both",
        help="Which marimo mode to exercise.",
    )
    args = parser.parse_args()

    with tempfile.TemporaryDirectory(prefix="molab-marimo-spike-") as temp_dir:
        notebook = Path(temp_dir) / "spike_notebook.py"
        notebook.write_text(notebook_source(), encoding="utf-8")

        modes = ["edit", "run"] if args.mode == "both" else [args.mode]
        for mode in modes:
            exercise_mode(mode, notebook)

    return 0


def exercise_mode(mode: str, notebook: Path) -> None:
    port = free_port()
    command = command_for(mode, notebook, port)
    print(f"starting {mode}: {' '.join(command)}", flush=True)
    process = subprocess.Popen(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    try:
        status = wait_until_ready(port)
        print(f"{mode} ready: HTTP {status} at http://{HOST}:{port}/", flush=True)
    finally:
        shutdown(mode, process)


def command_for(mode: str, notebook: Path, port: int) -> list[str]:
    base = [
        "marimo",
        mode,
        str(notebook),
        "--headless",
        "--host",
        HOST,
        "--port",
        str(port),
        "--token-password",
        TOKEN,
    ]
    if mode == "edit":
        base.append("--skip-update-check")
    return base


def wait_until_ready(port: int, timeout_seconds: float = 20.0) -> int:
    deadline = time.monotonic() + timeout_seconds
    url = f"http://{HOST}:{port}/?access_token={TOKEN}"
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        try:
            with urlopen(url, timeout=1.0) as response:
                return response.status
        except HTTPError as error:
            if error.code in {200, 302, 401, 403}:
                return error.code
            last_error = error
        except URLError as error:
            last_error = error
        time.sleep(0.25)
    raise TimeoutError(f"marimo did not become ready on port {port}: {last_error}")


def shutdown(mode: str, process: subprocess.Popen[str]) -> None:
    if process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)
            raise RuntimeError(f"{mode} did not exit after SIGTERM; killed it")

    output = process.stdout.read() if process.stdout else ""
    if output:
        print(f"{mode} output:\n{output}", flush=True)
    if process.returncode not in {0, -15}:
        raise RuntimeError(f"{mode} exited with unexpected code {process.returncode}")
    print(f"{mode} stopped cleanly with exit code {process.returncode}", flush=True)


def free_port() -> int:
    with contextlib.closing(socket.socket(socket.AF_INET, socket.SOCK_STREAM)) as sock:
        sock.bind((HOST, 0))
        return int(sock.getsockname()[1])


def notebook_source() -> str:
    return '''import marimo

__generated_with = "0.23.10"
app = marimo.App()


@app.cell
def _():
    "MoLab marimo process spike"
    return


if __name__ == "__main__":
    app.run()
'''


if __name__ == "__main__":
    sys.exit(main())
