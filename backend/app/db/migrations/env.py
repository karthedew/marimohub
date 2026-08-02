from logging.config import fileConfig

from alembic import context
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy import pool
from sqlalchemy.ext.asyncio import async_engine_from_config

from app import models  # noqa: F401
from app.db.database import Base, database_connect_args


class MigrationSettings(BaseSettings):
    DATABASE_URL: str = "postgresql+asyncpg://molab:molab@localhost:5432/molab"
    # Mirrors app.core.config.Settings.DATABASE_CA_FILE: migrations run
    # against the same external PostgreSQL the application does and must
    # verify it the same way.
    DATABASE_CA_FILE: str | None = None

    model_config = SettingsConfigDict(env_file=("../.env", ".env"), extra="ignore")


config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name, disable_existing_loggers=False)

target_metadata = Base.metadata

migration_settings = MigrationSettings()
database_url = migration_settings.DATABASE_URL

config.set_main_option("sqlalchemy.url", database_url)


def run_migrations_offline() -> None:
    context.configure(
        url=database_url,
        target_metadata=target_metadata,
        literal_binds=True,
    )

    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection) -> None:
    context.configure(connection=connection, target_metadata=target_metadata)

    with context.begin_transaction():
        context.run_migrations()


async def run_migrations_online() -> None:
    connectable = async_engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
        connect_args=database_connect_args(migration_settings),
    )

    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)

    await connectable.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    import asyncio

    asyncio.run(run_migrations_online())
