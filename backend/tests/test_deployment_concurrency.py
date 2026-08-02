"""Real-Postgres concurrency races for the Deployment Runtime lifecycle.

Every test here drives two (or more) independent `AsyncSession` objects
concurrently through `asyncio.gather`, each backed by its own pooled
connection to the real test database. That independence is what actually
exercises the `SELECT ... FOR UPDATE` row locking in
`app.services.deployment_lifecycle`: two coroutines sharing one Python-level
`AsyncSession` (as `db_session` gives every other test file) can never
truly race each other, since SQLAlchemy sessions are not concurrency-safe.
Racing through two real connections is also indistinguishable, from
Postgres's point of view, from racing two separate backend replicas -- the
database is the only thing serializing them either way.
"""

import asyncio
from collections.abc import AsyncGenerator
import contextlib
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.commands.reconcile_runtimes import reconcile_stale_runtimes
from app.core.errors import ConflictError, NotFoundError
from app.models import Deployment, DeploymentDesiredState, Notebook, Workspace
from app.services import deployment_lifecycle, workspace_service
from app.services.notebook_storage import NotebookStorageService
from app.services.session_manager import RuntimeRef, SessionInfo, SessionNotFoundError, SessionPhase

_RUNTIME_IMAGE = "registry.example/marimo-runtime@sha256:" + "0" * 64


class _RaceManager:
    """A `SessionManager` double that tracks exactly what is "running" in the cluster.

    Deliberately synchronous bookkeeping with no locking of its own: the
    correctness this suite is checking for comes entirely from
    `deployment_lifecycle`'s database locking, not from anything this fake
    does to protect itself.
    """

    def __init__(self) -> None:
        self.running: set[UUID] = set()
        self.spawn_calls: list[UUID] = []
        self.stop_calls: list[UUID] = []

    async def spawn(self, notebook: object, mode: object, creator_id: object = None) -> SessionInfo:
        raise NotImplementedError

    async def spawn_deployment(self, notebook: Notebook, deployment: Deployment) -> SessionInfo:
        self.spawn_calls.append(deployment.id)
        await asyncio.sleep(0)  # yield control, widening the race window
        self.running.add(deployment.id)
        return SessionInfo(
            id=deployment.id,
            notebook_id=notebook.id,
            mode="deploy",
            phase=SessionPhase.READY,
            last_active=datetime.now(UTC),
            creator_id=None,
            deployment_revision=deployment.revision,
        )

    async def get(self, session_id: UUID) -> SessionInfo | None:
        return None

    async def target(self, session_id: UUID) -> None:
        return None

    async def mark_active(self, session_id: UUID) -> None:
        return None

    async def stop(self, session_id: UUID) -> None:
        self.stop_calls.append(session_id)
        await asyncio.sleep(0)
        if session_id not in self.running:
            raise SessionNotFoundError("Runtime not found")
        self.running.discard(session_id)

    async def stop_workspace_sessions(self, workspace_id: UUID) -> None:
        return None

    async def reconcilable_runtimes(self) -> list[RuntimeRef]:
        return []

    async def shutdown(self) -> None:
        return None


@pytest_asyncio.fixture
async def race_engine(test_database_url: str) -> AsyncGenerator[AsyncEngine, None]:
    engine = create_async_engine(test_database_url, pool_pre_ping=True)
    yield engine
    await engine.dispose()


def _sessions(engine: AsyncEngine, count: int) -> list[AsyncSession]:
    """Return `count` independent `AsyncSession`s sharing `engine`'s connection pool.

    Independent sessions from one engine's pool are exactly as concurrency-
    safe as sessions from separate engines/processes: each checks out its
    own physical connection, and Postgres row locks serialize across
    connections regardless of which engine object created them.
    """
    factory = async_sessionmaker(engine, expire_on_commit=False)
    return [factory() for _ in range(count)]


async def _seed_active_deployment(
    db: AsyncSession, *, slug: str, revision: int = 1
) -> tuple[UUID, UUID, UUID]:
    """Commit a Workspace/Notebook/active Deployment row set; return their ids."""
    workspace = Workspace(slug=f"{slug}-ws", name="Race Workspace")
    db.add(workspace)
    await db.flush()
    notebook = Notebook(workspace_id=workspace.id, title="Race Notebook", source="x = 1")
    db.add(notebook)
    await db.flush()
    deployment = Deployment(
        notebook_id=notebook.id,
        slug=slug,
        desired_state=DeploymentDesiredState.ACTIVE,
        source_snapshot="x = 1",
        source_sha256="deadbeef",
        runtime_image=_RUNTIME_IMAGE,
        revision=revision,
    )
    db.add(deployment)
    await db.commit()
    return workspace.id, notebook.id, deployment.id


@pytest.mark.asyncio
async def test_concurrent_stop_and_wake_never_leaves_a_running_runtime(
    race_engine: AsyncEngine,
) -> None:
    seed_db, wake_db, stop_db, check_db = _sessions(race_engine, 4)
    _, notebook_id, deployment_id = await _seed_active_deployment(
        seed_db, slug=f"race-stop-wake-{uuid4().hex[:8]}"
    )
    await seed_db.close()
    manager = _RaceManager()

    async def do_wake() -> None:
        notebook = await wake_db.get(Notebook, notebook_id)
        assert notebook is not None
        with contextlib.suppress(NotFoundError):
            await deployment_lifecycle.ensure_running(wake_db, manager, notebook, deployment_id)

    async def do_stop() -> None:
        notebook = await stop_db.get(Notebook, notebook_id)
        assert notebook is not None
        await deployment_lifecycle.stop(stop_db, manager, notebook, deployment_id)

    await asyncio.gather(do_wake(), do_stop())
    await wake_db.close()
    await stop_db.close()

    deployment = await check_db.get(Deployment, deployment_id)
    assert deployment is not None
    # Stop's durable intent always wins: a wake that raced it can start the
    # Runtime, but can never leave it running once stop has also run.
    assert deployment.desired_state is DeploymentDesiredState.STOPPED
    assert manager.running == set()
    if manager.spawn_calls:
        assert deployment_id in manager.stop_calls
    await check_db.close()


@pytest.mark.asyncio
async def test_concurrent_stop_and_create_never_leaves_a_running_runtime(
    race_engine: AsyncEngine,
) -> None:
    """A stop racing the very first wake (no Runtime has ever existed) is still safe."""
    seed_db, wake_db, stop_db, check_db = _sessions(race_engine, 4)
    _, notebook_id, deployment_id = await _seed_active_deployment(
        seed_db, slug=f"race-stop-create-{uuid4().hex[:8]}"
    )
    await seed_db.close()
    manager = _RaceManager()

    async def do_wake() -> None:
        notebook = await wake_db.get(Notebook, notebook_id)
        assert notebook is not None
        with contextlib.suppress(NotFoundError):
            await deployment_lifecycle.ensure_running(wake_db, manager, notebook, deployment_id)

    async def do_stop() -> None:
        notebook = await stop_db.get(Notebook, notebook_id)
        assert notebook is not None
        await deployment_lifecycle.stop(stop_db, manager, notebook, deployment_id)

    await asyncio.gather(do_wake(), do_stop())
    await wake_db.close()
    await stop_db.close()

    deployment = await check_db.get(Deployment, deployment_id)
    assert deployment is not None
    assert deployment.desired_state is DeploymentDesiredState.STOPPED
    assert manager.running == set()
    await check_db.close()


@pytest.mark.asyncio
async def test_concurrent_redeploys_each_bump_revision_exactly_once(
    race_engine: AsyncEngine,
) -> None:
    seed_db, a_db, b_db, check_db = _sessions(race_engine, 4)
    workspace = Workspace(slug=f"race-redeploy-ws-{uuid4().hex[:8]}", name="Race Redeploy")
    seed_db.add(workspace)
    await seed_db.flush()
    notebook = Notebook(workspace_id=workspace.id, title="Race Redeploy NB", source="x = 1")
    seed_db.add(notebook)
    await seed_db.commit()
    notebook_id = notebook.id
    await seed_db.close()
    manager = _RaceManager()

    class _Storage(NotebookStorageService):
        async def get(self, notebook: Notebook) -> str | None:
            return "x = 1"

        async def put(self, notebook: Notebook, source: str | None) -> None:
            return None

        async def delete(self, notebook_id: UUID) -> None:
            return None

    storage = _Storage()
    suffix = uuid4().hex[:8]

    async def do_deploy(db: AsyncSession, slug: str) -> None:
        nb = await db.get(Notebook, notebook_id)
        assert nb is not None
        await deployment_lifecycle.deploy(db, manager, nb, storage, slug=slug)

    await asyncio.gather(
        do_deploy(a_db, f"race-redeploy-a-{suffix}"), do_deploy(b_db, f"race-redeploy-b-{suffix}")
    )
    await a_db.close()
    await b_db.close()

    deployment = await check_db.scalar(
        select(Deployment).where(Deployment.notebook_id == notebook_id)
    )
    assert deployment is not None
    # Both concurrent deploys targeted the same one Deployment row (unique
    # per Notebook); each bump happens under the row lock, so two deploys
    # must land as two increments, never a lost update.
    assert deployment.revision == 2
    await check_db.close()


@pytest.mark.asyncio
async def test_archive_blocks_concurrent_wake_and_leaves_no_running_runtime(
    race_engine: AsyncEngine,
) -> None:
    seed_db, wake_db, archive_db, check_db = _sessions(race_engine, 4)
    workspace_id, notebook_id, deployment_id = await _seed_active_deployment(
        seed_db, slug=f"race-archive-{uuid4().hex[:8]}"
    )
    await seed_db.close()
    manager = _RaceManager()

    async def do_wake() -> None:
        notebook = await wake_db.get(Notebook, notebook_id)
        assert notebook is not None
        with contextlib.suppress(ConflictError, NotFoundError):
            await deployment_lifecycle.ensure_running(wake_db, manager, notebook, deployment_id)

    async def do_archive() -> None:
        await workspace_service.archive_workspace(archive_db, manager, workspace_id)

    await asyncio.gather(do_wake(), do_archive())
    await wake_db.close()
    await archive_db.close()

    workspace = await check_db.get(Workspace, workspace_id)
    assert workspace is not None
    assert workspace.archived_at is not None
    # Whichever order the two operations actually ran in, archive's own
    # sweep of every Deployment in the Workspace (taken *after* its own
    # commit) is what guarantees nothing is left running, not the ordering
    # of the race itself.
    assert manager.running == set()
    await check_db.close()


@pytest.mark.asyncio
async def test_reconcile_sweep_removes_runtime_left_by_a_crash_between_intent_and_cleanup(
    race_engine: AsyncEngine,
) -> None:
    """A crash after committing archive intent but before the cluster stop is not permanent.

    This simulates the crash point directly: durable intent (archived_at +
    every owned Deployment stopped) is committed exactly as
    `archive_workspace` would, but the matching cluster mutation never runs
    -- standing in for a process dying in between. The maintenance sweep
    must still recover it on its next run.
    """
    seed_db, check_db = _sessions(race_engine, 2)
    workspace_id, notebook_id, deployment_id = await _seed_active_deployment(
        seed_db, slug=f"race-crash-{uuid4().hex[:8]}"
    )
    manager = _RaceManager()
    manager.running.add(deployment_id)  # the Runtime a crashed archive never got to stop

    workspace = await seed_db.get(Workspace, workspace_id)
    assert workspace is not None
    now = datetime.now(UTC)
    workspace.archived_at = now
    workspace.purge_after = now + timedelta(days=30)
    deployment = await seed_db.get(Deployment, deployment_id)
    assert deployment is not None
    deployment.desired_state = DeploymentDesiredState.STOPPED
    await seed_db.commit()

    async def fake_reconcilable_runtimes() -> list[RuntimeRef]:
        return [
            RuntimeRef(
                id=deployment_id,
                mode="deploy",
                workspace_id=workspace_id,
                notebook_id=notebook_id,
                deployment_revision=1,
            )
        ]

    manager.reconcilable_runtimes = fake_reconcilable_runtimes  # ty: ignore[invalid-assignment]

    removed = await reconcile_stale_runtimes(check_db, manager)

    assert removed == 1
    assert manager.running == set()
    assert deployment_id in manager.stop_calls
    await seed_db.close()
    await check_db.close()
