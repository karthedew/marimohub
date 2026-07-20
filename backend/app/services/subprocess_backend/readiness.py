import asyncio
from http import HTTPStatus
from http.client import HTTPResponse
import logging
from pathlib import Path
from typing import cast
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import urlopen
from uuid import UUID

from app.services.session_manager import NotebookStartupError, SessionStartError
from app.services.subprocess_backend.runtime import MARIMO_HOST
from app.services.subprocess_backend.workdir import SpawnWorkdir

logger = logging.getLogger(__name__)

# Bound how much captured stderr reaches the server log on a failed start.
STDERR_LOG_LIMIT = 4000
NOTEBOOK_START_DETAIL = "The notebook could not start. Check that it is a valid marimo notebook."


def _read_stderr(path: Path) -> str:
    try:
        text = path.read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return ""
    if len(text) > STDERR_LOG_LIMIT:
        return f"...{text[-STDERR_LOG_LIMIT:]}"
    return text


async def probe(port: int, token: str, base_url: str) -> bool:
    """Check whether the marimo server at ``port`` is answering requests."""
    try:
        response_status = await asyncio.to_thread(_probe_sync, port, token, base_url)
    except (HTTPError, TimeoutError, URLError):
        return False
    else:
        return response_status == HTTPStatus.OK


def _probe_sync(port: int, token: str, base_url: str) -> int:
    # URL is built from a fixed http:// scheme and the loopback host constant, so
    # there is no user-controlled scheme for the S310 audit to be concerned with.
    url = f"http://{MARIMO_HOST}:{port}{base_url}/?access_token={quote(token)}"
    response = cast("HTTPResponse", urlopen(url, timeout=1.0))  # noqa: S310
    with response:
        return response.status


async def wait_until_ready(
    process: asyncio.subprocess.Process,
    port: int,
    prep: SpawnWorkdir,
    *,
    session_id: UUID,
    ready_timeout: float,
) -> None:
    """Poll a spawned marimo process until it serves or fails."""
    deadline = asyncio.get_running_loop().time() + ready_timeout
    while True:
        if process.returncode is not None:
            logger.error(
                "Notebook process %s (%s) exited with code %s before ready; stderr:\n%s",
                session_id,
                prep.base_url,
                process.returncode,
                _read_stderr(prep.stderr_path),
            )
            raise NotebookStartupError(
                "Notebook process exited before it was ready",
                detail=NOTEBOOK_START_DETAIL,
            )
        if await probe(port, prep.token, prep.base_url):
            return
        if asyncio.get_running_loop().time() >= deadline:
            logger.error(
                "Notebook process %s (%s) did not become ready within %.1fs; stderr:\n%s",
                session_id,
                prep.base_url,
                ready_timeout,
                _read_stderr(prep.stderr_path),
            )
            raise SessionStartError(
                "Notebook process did not become ready in time",
                detail="Deployment did not start in time.",
            )
        await asyncio.sleep(0.1)
