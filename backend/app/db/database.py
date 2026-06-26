from collections.abc import AsyncGenerator
from functools import cache

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


@cache
def get_engine() -> AsyncEngine:
    """Return the process-wide async engine, creating it on first use."""
    return create_async_engine(get_settings().DATABASE_URL, pool_pre_ping=True)


@cache
def get_sessionmaker() -> async_sessionmaker[AsyncSession]:
    """Return the process-wide async session factory, creating it on first use."""
    return async_sessionmaker(get_engine(), expire_on_commit=False)


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    """Yield a database session for the lifetime of a request."""
    async with get_sessionmaker()() as session:
        yield session
