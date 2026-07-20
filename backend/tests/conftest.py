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

    original_database_url = os.environ.get("DATABASE_URL")
    original_secret_key = os.environ.get("SECRET_KEY")
    os.environ["DATABASE_URL"] = database_url
    os.environ.setdefault("SECRET_KEY", "test-secret-with-at-least-32-bytes")
    get_settings.cache_clear()
    command.upgrade(Config("alembic.ini"), "head")

    yield database_url

    if original_database_url is None:
        os.environ.pop("DATABASE_URL", None)
    else:
        os.environ["DATABASE_URL"] = original_database_url
    if original_secret_key is None:
        os.environ.pop("SECRET_KEY", None)
    else:
        os.environ["SECRET_KEY"] = original_secret_key
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
