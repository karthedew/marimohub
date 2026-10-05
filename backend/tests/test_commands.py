from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from types import ModuleType

import pytest

from app.commands import purge_archived_workspaces, reconcile_runtimes


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("command", "sweep_name"),
    [
        pytest.param(reconcile_runtimes, "reconcile_stale_runtimes", id="reconcile-runtimes"),
        pytest.param(purge_archived_workspaces, "purge_due_workspaces", id="purge-workspaces"),
    ],
)
@pytest.mark.parametrize(
    "sweep_error",
    [
        pytest.param(None, id="sweep-finishes"),
        pytest.param(RuntimeError("cluster unavailable"), id="sweep-raises"),
    ],
)
async def test_command_shuts_the_session_manager_down_on_every_exit_path(
    monkeypatch: pytest.MonkeyPatch,
    command: ModuleType,
    sweep_name: str,
    sweep_error: Exception | None,
) -> None:
    """Each CronJob run must release the manager's Kubernetes client before exiting.

    Without that, every run ended by logging "Unclosed client session".
    """
    events: list[str] = []

    @asynccontextmanager
    async def _db_session() -> AsyncIterator[object]:
        yield object()

    async def _sweep(*args: object) -> int:
        events.append("sweep")
        if sweep_error is not None:
            raise sweep_error
        return 3

    async def _shutdown_session_manager() -> None:
        events.append("shutdown")

    monkeypatch.setattr(command, "get_session_manager", object)
    monkeypatch.setattr(command, "get_sessionmaker", lambda: _db_session)
    monkeypatch.setattr(command, sweep_name, _sweep)
    monkeypatch.setattr(command, "shutdown_session_manager", _shutdown_session_manager)

    if sweep_error is None:
        assert await command._run() == 3
    else:
        with pytest.raises(RuntimeError, match="cluster unavailable"):
            await command._run()

    assert events == ["sweep", "shutdown"]
