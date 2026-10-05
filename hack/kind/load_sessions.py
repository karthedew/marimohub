"""Drive N concurrent users through a full edit Session on the kind environment.

Each simulated user registers, creates a Workspace and a Notebook, starts an
edit Session (one Runtime Pod), connects marimo's kernel WebSocket through the
public proxy, and runs the notebook. The notebook writes a file into
`$MARIMOHUB_WORKSPACE_DIR`, and this script then confirms that file landed in
that Workspace's directory on the host, proving the
Session -> Pod -> Workspace storage path end to end for every user at once.

Run from the repository root after `make kind-up`:

    uv run --project backend python hack/kind/load_sessions.py --users 50

Exits non-zero if any user fails a step.
"""

from __future__ import annotations

import argparse
import asyncio
from dataclasses import dataclass, field
import json
from pathlib import Path
import re
import ssl
import statistics
import sys
import time
import uuid

import httpx
import websockets

REPO_ROOT = Path(__file__).resolve().parents[2]
SERVER_TOKEN_RE = re.compile(r'"serverToken":\s*"([^"]+)"')
NOTEBOOK_SOURCE = """import marimo

app = marimo.App()


@app.cell
def _():
    import os
    import pathlib

    target = pathlib.Path(os.environ["MARIMOHUB_WORKSPACE_DIR"]) / "hello.txt"
    target.write_text("{marker}")
    return


if __name__ == "__main__":
    app.run()
"""


@dataclass
class Result:
    """One simulated user's outcome and per-step timings in seconds."""

    user: int
    timings: dict[str, float] = field(default_factory=dict)
    error: str | None = None


async def timed(result: Result, step: str, coro):  # noqa: ANN001, ANN201 -- thin timing wrapper
    """Await `coro`, recording its wall-clock duration under `step`."""
    start = time.monotonic()
    try:
        return await coro
    finally:
        result.timings[step] = time.monotonic() - start


async def wait_for_file(path: Path, expected: str, timeout: float) -> None:
    """Poll the host filesystem until `path` holds `expected`."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            if path.read_text() == expected:
                return
        except FileNotFoundError:
            pass
        await asyncio.sleep(0.5)
    raise TimeoutError(f"{path} never contained the expected marker")


async def run_user(  # noqa: PLR0913 -- one cohesive set of run parameters
    index: int,
    run_id: str,
    base_url: str,
    ssl_context: ssl.SSLContext,
    data_dir: Path,
    hold_seconds: float,
) -> Result:
    """Walk one user through register -> Session -> Workspace write -> stop."""
    result = Result(user=index)
    username = f"load-{run_id}-{index}"
    marker = f"{username}:{uuid.uuid4()}"
    async with httpx.AsyncClient(
        base_url=base_url, verify=ssl_context, timeout=180, follow_redirects=True
    ) as client:
        session_id = None
        try:
            password = "load-test-password"
            response = await client.post(
                "/api/auth/register",
                json={
                    "username": username,
                    "email": f"{username}@example.test",
                    "password": password,
                },
            )
            response.raise_for_status()
            response = await client.post(
                "/api/auth/login", json={"username": username, "password": password}
            )
            response.raise_for_status()
            client.headers["Authorization"] = (
                f"Bearer {response.json()['access_token']}"
            )

            response = await client.post(
                "/api/workspaces", json={"name": f"Load {run_id} {index}"}
            )
            response.raise_for_status()
            workspace_id = response.json()["id"]
            response = await client.post(
                "/api/notebooks",
                json={
                    "title": f"load {index}",
                    "workspace_id": workspace_id,
                    "source": NOTEBOOK_SOURCE.replace("{marker}", marker),
                },
            )
            response.raise_for_status()
            notebook_id = response.json()["id"]

            response = await timed(
                result,
                "spawn",
                client.post(
                    "/api/sessions", json={"notebook_id": notebook_id, "mode": "edit"}
                ),
            )
            response.raise_for_status()
            session_id = response.json()["id"]
            proxy = f"/api/proxy/{session_id}"

            page = await timed(result, "first_page", client.get(f"{proxy}/"))
            page.raise_for_status()
            token_match = SERVER_TOKEN_RE.search(page.text)
            if token_match is None:
                raise RuntimeError("marimo page carried no serverToken")

            kernel_session = str(uuid.uuid4())
            ws_url = (
                base_url.replace("https://", "wss://")
                + f"{proxy}/ws?session_id={kernel_session}"
            )
            async with websockets.connect(
                ws_url, ssl=ssl_context, max_size=None, open_timeout=60
            ) as ws:
                response = await client.post(
                    f"{proxy}/api/kernel/instantiate",
                    headers={
                        "Marimo-Session-Id": kernel_session,
                        "Marimo-Server-Token": token_match.group(1),
                    },
                    json={"objectIds": [], "values": [], "autoRun": True},
                )
                response.raise_for_status()
                target = data_dir / "workspaces" / workspace_id / "hello.txt"
                await timed(
                    result, "workspace_write", wait_for_file(target, marker, 120)
                )

                # Hold the kernel connection open like an active user, draining
                # whatever the kernel sends so its buffers never back up.
                hold_until = time.monotonic() + hold_seconds
                while (remaining := hold_until - time.monotonic()) > 0:
                    try:
                        await asyncio.wait_for(ws.recv(), timeout=min(remaining, 5))
                    except TimeoutError:
                        continue
        except Exception as exc:  # noqa: BLE001 -- every failure is reported, none should abort the others
            result.error = f"{type(exc).__name__}: {exc}"[:300]
        finally:
            if session_id is not None:
                stop = await client.delete(f"/api/sessions/{session_id}")
                if stop.status_code not in (204, 404) and result.error is None:
                    result.error = f"stop returned {stop.status_code}"
    return result


def summarize(results: list[Result], wall: float) -> int:
    """Print per-step latency percentiles and failures; return the exit code."""
    failed = [r for r in results if r.error]
    print(
        f"\n{len(results) - len(failed)}/{len(results)} users succeeded in {wall:.1f}s wall time"
    )
    for step in ("spawn", "first_page", "workspace_write"):
        values = sorted(
            r.timings[step] for r in results if step in r.timings and not r.error
        )
        if not values:
            continue
        p95 = values[min(len(values) - 1, int(len(values) * 0.95))]
        print(
            f"  {step:16} n={len(values):3}  p50={statistics.median(values):6.2f}s  "
            f"p95={p95:6.2f}s  max={values[-1]:6.2f}s"
        )
    for r in failed:
        print(f"  user {r.user}: {r.error}")
    return 1 if failed else 0


def _default_base_url() -> str:
    try:
        host = (
            (Path(__file__).resolve().parents[2] / ".kind" / "host").read_text().strip()
        )
    except OSError:
        host = ""
    return f"https://{host or 'localhost'}"


async def main() -> int:
    """Parse arguments, run every user concurrently, and report."""
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--users", type=int, default=50)
    parser.add_argument(
        "--hold-seconds",
        type=float,
        default=60,
        help="how long each user keeps its kernel open",
    )
    parser.add_argument(
        "--base-url",
        default=_default_base_url(),
        help="defaults to the host make kind-up last used (.kind/host), else localhost",
    )
    parser.add_argument(
        "--ca", type=Path, default=REPO_ROOT / ".kind" / "tls" / "ca.crt"
    )
    parser.add_argument(
        "--data-dir",
        type=Path,
        default=Path("/data1/marimohub"),
        help="host directory holding Workspace Files (MARIMOHUB_DATA_DIR for make kind-up)",
    )
    args = parser.parse_args()

    ssl_context = ssl.create_default_context(cafile=str(args.ca))
    run_id = uuid.uuid4().hex[:6]
    print(
        f"run {run_id}: {args.users} users, holding each kernel for {args.hold_seconds:.0f}s"
    )
    start = time.monotonic()
    results = await asyncio.gather(
        *(
            run_user(
                i, run_id, args.base_url, ssl_context, args.data_dir, args.hold_seconds
            )
            for i in range(args.users)
        )
    )
    code = summarize(list(results), time.monotonic() - start)
    (REPO_ROOT / ".kind").mkdir(exist_ok=True)
    report = REPO_ROOT / ".kind" / f"load-{run_id}.json"
    report.write_text(json.dumps([r.__dict__ for r in results], indent=2))
    print(f"  details: {report}")
    return code


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
