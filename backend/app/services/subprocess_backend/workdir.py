from dataclasses import dataclass
import os
from pathlib import Path
import secrets
import tempfile
from uuid import UUID

from app.models import Notebook
from app.services.session_manager import RuntimeMode


@dataclass(frozen=True)
class SpawnWorkdir:
    """Everything ``prepare_workdir`` produces for a single spawn attempt."""

    workdir: Path
    cleanup_workdir: bool
    notebook_path: Path
    token: str
    base_url: str
    stderr_path: Path


def session_workdir(notebook: Notebook, mode: RuntimeMode) -> tuple[Path, bool]:
    """Return the working directory for a session and whether it is disposable.

    An edit session gets a notebook-keyed directory that outlives the process (pure
    scratch space, not read back — the stored source is always written fresh at spawn);
    every other mode gets a throwaway tempdir.
    """
    if mode == "edit":
        root = Path(os.environ.get("MOLAB_NOTEBOOK_WORKDIR", ".molab-notebooks"))
        workdir = root / str(notebook.id)
        workdir.mkdir(parents=True, exist_ok=True)
        return workdir.resolve(), False
    return Path(tempfile.mkdtemp(prefix="molab-marimo-")), True


def write_marimo_project_config(workdir: Path) -> None:
    """Write the per-workdir marimo autosave config."""
    workdir.joinpath("pyproject.toml").write_text(
        '[tool.marimo.save]\nautosave = "after_delay"\nautosave_delay = 1000\n',
        encoding="utf-8",
    )


def prepare_workdir(
    notebook: Notebook, mode: RuntimeMode, session_id: UUID, base_url: str | None
) -> SpawnWorkdir:
    """Prepare the on-disk working directory and per-spawn secrets for a session."""
    workdir, cleanup_workdir = session_workdir(notebook, mode)
    notebook_path = workdir / "notebook.py"
    notebook_path.write_text(notebook.source or "", encoding="utf-8")
    write_marimo_project_config(workdir)
    return SpawnWorkdir(
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
