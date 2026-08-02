from collections.abc import AsyncGenerator
from functools import cache
import ssl
from typing import Protocol

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase

from app.core.config import get_settings


class Base(DeclarativeBase):
    """Declarative base class for all ORM models."""


class _HasDatabaseCAFile(Protocol):
    """The one field `database_connect_args` needs from a settings object.

    A `Protocol` rather than `app.core.config.Settings` directly, because
    Alembic's migration environment (`app/db/migrations/env.py`) loads its
    own narrow `MigrationSettings` rather than the full application
    `Settings`, and both must build the identical `connect_args` shape from
    the same `DATABASE_CA_FILE` field.
    """

    DATABASE_CA_FILE: str | None


def database_connect_args(settings: _HasDatabaseCAFile) -> dict[str, ssl.SSLContext]:
    """Return `asyncpg` connect kwargs verifying the server certificate when configured.

    Shared with Alembic's own engine (`app/db/migrations/env.py`) so the
    application and its migrations never disagree about whether a
    connection to PostgreSQL is verified. Unset `DATABASE_CA_FILE` returns no
    `ssl` kwarg at all, leaving the driver's own default in place -- the
    OpenShift profile is the one that requires this to be configured (see
    the chart's `database.tls` values); the portable profile's local/disposable
    Postgres fixture is not required to present a CA-verifiable certificate.
    """
    if not settings.DATABASE_CA_FILE:
        return {}
    context = ssl.create_default_context(cafile=settings.DATABASE_CA_FILE)
    return {"ssl": context}


@cache
def get_engine() -> AsyncEngine:
    """Return the process-wide async engine, creating it on first use."""
    settings = get_settings()
    return create_async_engine(
        settings.DATABASE_URL,
        pool_pre_ping=True,
        connect_args=database_connect_args(settings),
    )


@cache
def get_sessionmaker() -> async_sessionmaker[AsyncSession]:
    """Return the process-wide async session factory, creating it on first use."""
    return async_sessionmaker(get_engine(), expire_on_commit=False)


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    """Yield a database session for the lifetime of a request."""
    async with get_sessionmaker()() as session:
        yield session
