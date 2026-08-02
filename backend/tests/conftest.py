from collections.abc import AsyncGenerator, Generator
import os

from alembic import command
from alembic.config import Config
from httpx import ASGITransport, AsyncClient
import psycopg
from psycopg import sql
import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.api.notebooks import get_embedding_service
from app.core.config import get_settings
from app.db.database import get_db
from app.internal_main import app as internal_app
from app.main import app
from app.services.embedding_service import EMBEDDING_DIMENSIONS

DEFAULT_TEST_DATABASE_URL = "postgresql+asyncpg://molab:molab@localhost:5432/molab_test"


class FakeEmbeddingService:
    def __init__(self) -> None:
        super().__init__()
        self.calls: list[str] = []

    async def embed(self, text: str) -> list[float]:
        self.calls.append(text)
        normalized = text.lower()
        vector = [0.01] * EMBEDDING_DIMENSIONS
        if any(term in normalized for term in ["rocket", "propulsion", "engine"]):
            vector[0] = 1.0
        elif any(term in normalized for term in ["climate", "weather", "atmosphere"]):
            vector[1] = 1.0
        elif any(term in normalized for term in ["music", "audio", "melody"]):
            vector[2] = 1.0
        elif any(term in normalized for term in ["finance", "market", "trading"]):
            vector[3] = 1.0
        return vector


def _sync_url(database_url: str, database: str | None = None) -> str:
    url = make_url(database_url).set(drivername="postgresql")
    if database is not None:
        url = url.set(database=database)
    return url.render_as_string(hide_password=False)


def _restore_env(name: str, original: str | None) -> None:
    if original is None:
        os.environ.pop(name, None)
    else:
        os.environ[name] = original


@pytest.fixture(scope="session")
def test_database_url() -> Generator[str, None, None]:
    database_url = os.environ.get("TEST_DATABASE_URL", DEFAULT_TEST_DATABASE_URL)
    parsed = make_url(database_url)
    database_name = parsed.database
    if database_name is None:
        raise RuntimeError("TEST_DATABASE_URL must include a database name")

    with psycopg.connect(_sync_url(database_url, "postgres"), autocommit=True) as conn:
        exists = conn.execute(
            "SELECT 1 FROM pg_database WHERE datname = %s", (database_name,)
        ).fetchone()
        if exists is None:
            conn.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(database_name)))

    # Import tests exercise a mocked gitlab.example.com fetch; the allowlist
    # gate defaults to fully closed (see Settings.GITLAB_IMPORT_ENABLED), so
    # the test environment opts that one placeholder host in explicitly
    # rather than every test doing it individually.
    env_defaults = {
        "DATABASE_URL": database_url,
        "SECRET_KEY": "test-secret-with-at-least-32-bytes",
        "GITLAB_IMPORT_ENABLED": "true",
        "GITLAB_IMPORT_ALLOWED_HOSTS": '["gitlab.example.com"]',
    }
    originals = {name: os.environ.get(name) for name in env_defaults}
    os.environ["DATABASE_URL"] = database_url
    for name, value in env_defaults.items():
        if name != "DATABASE_URL":
            os.environ.setdefault(name, value)
    get_settings.cache_clear()
    command.upgrade(Config("alembic.ini"), "head")

    yield database_url

    for name, original in originals.items():
        _restore_env(name, original)
    get_settings.cache_clear()


@pytest_asyncio.fixture
async def db_session(test_database_url: str) -> AsyncGenerator[AsyncSession, None]:
    engine = create_async_engine(test_database_url, pool_pre_ping=True)
    sessionmaker = async_sessionmaker(engine, expire_on_commit=False)

    async with engine.begin() as conn:
        await conn.execute(
            text(
                "TRUNCATE notebook_data, deployments, notebooks, workspace_members, "
                "workspaces, identities, local_credentials, users RESTART IDENTITY CASCADE"
            )
        )

    async with sessionmaker() as session:
        yield session
        await session.rollback()

    async with engine.begin() as conn:
        await conn.execute(
            text(
                "TRUNCATE notebook_data, deployments, notebooks, workspace_members, "
                "workspaces, identities, local_credentials, users RESTART IDENTITY CASCADE"
            )
        )

    await engine.dispose()


@pytest_asyncio.fixture
async def fake_embedding_service() -> FakeEmbeddingService:
    return FakeEmbeddingService()


@pytest_asyncio.fixture
async def api_client(
    db_session: AsyncSession,
    fake_embedding_service: FakeEmbeddingService,
) -> AsyncGenerator[AsyncClient, None]:
    async def override_db() -> AsyncGenerator[AsyncSession, None]:
        yield db_session

    app.dependency_overrides[get_db] = override_db
    app.dependency_overrides[get_embedding_service] = lambda: fake_embedding_service

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        yield client

    app.dependency_overrides.clear()


@pytest_asyncio.fixture
async def internal_api_client(db_session: AsyncSession) -> AsyncGenerator[AsyncClient, None]:
    """A client against the separate internal app, sharing the same test database session.

    Distinct from `api_client`: the internal app has its own `get_db`
    override but never gets `get_embedding_service` overridden, since none of
    its routes touch embeddings. Tests set their own
    `get_runtime_credential_verifier` override per fake cluster state.
    """

    async def override_db() -> AsyncGenerator[AsyncSession, None]:
        yield db_session

    internal_app.dependency_overrides[get_db] = override_db

    async with AsyncClient(
        transport=ASGITransport(app=internal_app), base_url="http://test"
    ) as client:
        yield client

    internal_app.dependency_overrides.clear()
