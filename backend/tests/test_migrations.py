from uuid import uuid4

from alembic import command
from alembic.config import Config
import pytest
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Deployment, DeploymentDesiredState, Notebook, Workspace

# The revision `users.display_name` (20261004_0004) builds on.
PRE_DISPLAY_NAME_REVISION = "20261004_0003"


@pytest.mark.asyncio
async def test_workspaces_archive_columns_nullable_with_paired_check(
    db_session: AsyncSession,
) -> None:
    columns = (
        await db_session.execute(
            text(
                "SELECT column_name, is_nullable FROM information_schema.columns "
                "WHERE table_name = 'workspaces' AND column_name IN ('archived_at', 'purge_after')"
            )
        )
    ).all()
    assert {row.column_name: row.is_nullable for row in columns} == {
        "archived_at": "YES",
        "purge_after": "YES",
    }

    constraint = (
        await db_session.execute(
            text(
                "SELECT pg_get_constraintdef(oid) AS definition FROM pg_constraint "
                "WHERE conrelid = 'workspaces'::regclass AND conname = 'ck_workspaces_archive_pair'"
            )
        )
    ).one()
    assert "archived_at IS NULL" in constraint.definition
    assert "purge_after IS NULL" in constraint.definition


@pytest.mark.asyncio
async def test_workspace_members_user_id_fk_is_restrict(db_session: AsyncSession) -> None:
    result = (
        await db_session.execute(
            text(
                "SELECT confdeltype::text FROM pg_constraint "
                "WHERE conrelid = 'workspace_members'::regclass AND contype = 'f' "
                "AND confrelid = 'users'::regclass"
            )
        )
    ).one()
    assert result.confdeltype == "r"


@pytest.mark.asyncio
async def test_local_identity_partial_unique_index_exists(db_session: AsyncSession) -> None:
    result = (
        await db_session.execute(
            text(
                "SELECT ix.indisunique, ix.indpred IS NOT NULL AS is_partial "
                "FROM pg_index ix JOIN pg_class c ON c.oid = ix.indexrelid "
                "WHERE c.relname = 'uq_identities_one_local_per_user'"
            )
        )
    ).one()
    assert result.indisunique is True
    assert result.is_partial is True


@pytest.mark.asyncio
async def test_local_identity_partial_unique_index_enforces_one_per_user(
    db_session: AsyncSession,
) -> None:
    user_id = uuid4()
    await db_session.execute(
        text("INSERT INTO users (id, username, email) VALUES (:id, :username, :email)"),
        {"id": user_id, "username": "one-local", "email": "one-local@example.com"},
    )
    await db_session.execute(
        text(
            "INSERT INTO identities (id, user_id, provider, subject) "
            "VALUES (:id, :user_id, 'local', :subject)"
        ),
        {"id": uuid4(), "user_id": user_id, "subject": str(user_id)},
    )

    with pytest.raises(IntegrityError):
        async with db_session.begin_nested():
            await db_session.execute(
                text(
                    "INSERT INTO identities (id, user_id, provider, subject) "
                    "VALUES (:id, :user_id, 'local', :subject)"
                ),
                {"id": uuid4(), "user_id": user_id, "subject": "second-local"},
            )

    # A second identity under a different provider for the same user is unaffected.
    await db_session.execute(
        text(
            "INSERT INTO identities (id, user_id, provider, subject) "
            "VALUES (:id, :user_id, 'google', :subject)"
        ),
        {"id": uuid4(), "user_id": user_id, "subject": "google-subject"},
    )


@pytest.mark.asyncio
async def test_identity_email_authoritative_is_not_null_and_defaults_to_false(
    db_session: AsyncSession,
) -> None:
    column = (
        await db_session.execute(
            text(
                "SELECT is_nullable, column_default FROM information_schema.columns "
                "WHERE table_name = 'identities' AND column_name = 'email_authoritative'"
            )
        )
    ).one()
    assert column.is_nullable == "NO"
    assert column.column_default == "false"

    # A row written without the column never counts as a vouched-for email.
    user_id = uuid4()
    await db_session.execute(
        text("INSERT INTO users (id, username, email) VALUES (:id, :username, :email)"),
        {"id": user_id, "username": "unproven", "email": "unproven@example.com"},
    )
    await db_session.execute(
        text(
            "INSERT INTO identities (id, user_id, provider, subject) "
            "VALUES (:id, :user_id, 'oidc:corp', 'corp-subject')"
        ),
        {"id": uuid4(), "user_id": user_id},
    )
    authoritative = await db_session.scalar(
        text("SELECT email_authoritative FROM identities WHERE user_id = :user_id"),
        {"user_id": user_id},
    )
    assert authoritative is False


@pytest.mark.asyncio
async def test_users_display_name_is_an_optional_varchar_255(db_session: AsyncSession) -> None:
    column = (
        await db_session.execute(
            text(
                "SELECT data_type, character_maximum_length, is_nullable, column_default "
                "FROM information_schema.columns "
                "WHERE table_name = 'users' AND column_name = 'display_name'"
            )
        )
    ).one()
    assert column.data_type == "character varying"
    assert column.character_maximum_length == 255
    assert column.is_nullable == "YES"
    assert column.column_default is None

    # A row written without the column, as every pre-existing account was, has none.
    user_id = uuid4()
    await db_session.execute(
        text("INSERT INTO users (id, username, email) VALUES (:id, :username, :email)"),
        {"id": user_id, "username": "nameless", "email": "nameless@example.com"},
    )
    display_name = await db_session.scalar(
        text("SELECT display_name FROM users WHERE id = :id"), {"id": user_id}
    )
    assert display_name is None


def _users_columns(database_url: str) -> set[str]:
    engine = create_engine(make_url(database_url).set(drivername="postgresql+psycopg"))
    try:
        with engine.connect() as connection:
            return {column["name"] for column in inspect(connection).get_columns("users")}
    finally:
        engine.dispose()


def test_display_name_migration_round_trips(test_database_url: str) -> None:
    # Synchronous on purpose: Alembic's env.py drives its async engine with
    # asyncio.run, which cannot start inside a test's running event loop.
    config = Config("alembic.ini")
    try:
        assert "display_name" in _users_columns(test_database_url)

        command.downgrade(config, PRE_DISPLAY_NAME_REVISION)
        assert "display_name" not in _users_columns(test_database_url)

        command.upgrade(config, "head")
        assert "display_name" in _users_columns(test_database_url)
    finally:
        command.upgrade(config, "head")  # never leave later tests on an old schema


@pytest.mark.asyncio
async def test_notebooks_workspace_id_not_null_fk(db_session: AsyncSession) -> None:
    column = (
        await db_session.execute(
            text(
                "SELECT is_nullable FROM information_schema.columns "
                "WHERE table_name = 'notebooks' AND column_name = 'workspace_id'"
            )
        )
    ).one()
    assert column.is_nullable == "NO"

    result = (
        await db_session.execute(
            text(
                "SELECT confdeltype::text FROM pg_constraint "
                "WHERE conrelid = 'notebooks'::regclass AND contype = 'f' "
                "AND confrelid = 'workspaces'::regclass"
            )
        )
    ).one()
    assert result.confdeltype == "c"


@pytest.mark.asyncio
async def test_deployments_notebook_id_unique(db_session: AsyncSession) -> None:
    result = (
        await db_session.execute(
            text(
                "SELECT 1 FROM pg_constraint WHERE conrelid = 'deployments'::regclass "
                "AND contype = 'u' AND conname = 'uq_deployments_notebook_id'"
            )
        )
    ).one_or_none()
    assert result is not None


@pytest.mark.asyncio
async def test_users_has_no_password_hash_column(db_session: AsyncSession) -> None:
    result = (
        await db_session.execute(
            text(
                "SELECT 1 FROM information_schema.columns "
                "WHERE table_name = 'users' AND column_name = 'password_hash'"
            )
        )
    ).one_or_none()
    assert result is None


@pytest.mark.asyncio
async def test_notebook_visibility_enum_labels(db_session: AsyncSession) -> None:
    rows = (
        await db_session.execute(
            text(
                "SELECT e.enumlabel FROM pg_enum e "
                "JOIN pg_type t ON e.enumtypid = t.oid "
                "WHERE t.typname = 'notebook_visibility'"
            )
        )
    ).all()
    assert {row.enumlabel for row in rows} == {"private", "unlisted", "public"}


@pytest.mark.asyncio
async def test_deployment_desired_state_enum_labels(db_session: AsyncSession) -> None:
    rows = (
        await db_session.execute(
            text(
                "SELECT e.enumlabel FROM pg_enum e "
                "JOIN pg_type t ON e.enumtypid = t.oid "
                "WHERE t.typname = 'deployment_desired_state'"
            )
        )
    ).all()
    assert {row.enumlabel for row in rows} == {"active", "stopped"}


@pytest.mark.asyncio
async def test_old_deployment_status_column_and_type_are_gone(db_session: AsyncSession) -> None:
    column = (
        await db_session.execute(
            text(
                "SELECT 1 FROM information_schema.columns "
                "WHERE table_name = 'deployments' AND column_name = 'status'"
            )
        )
    ).one_or_none()
    assert column is None

    enum_type = (
        await db_session.execute(text("SELECT 1 FROM pg_type WHERE typname = 'deployment_status'"))
    ).one_or_none()
    assert enum_type is None


@pytest.mark.asyncio
async def test_deployment_snapshot_columns_exist_and_default_to_stopped_zero(
    db_session: AsyncSession,
) -> None:
    workspace = Workspace(slug="migration-snapshot-ws", name="Migration Snapshot")
    db_session.add(workspace)
    await db_session.flush()
    notebook = Notebook(workspace_id=workspace.id, title="Migration Snapshot NB")
    db_session.add(notebook)
    await db_session.flush()
    deployment = Deployment(notebook_id=notebook.id, slug="migration-snapshot-deploy")
    db_session.add(deployment)
    await db_session.commit()
    await db_session.refresh(deployment)

    assert deployment.desired_state is DeploymentDesiredState.STOPPED
    assert deployment.revision == 0
    assert deployment.source_snapshot is None
    assert deployment.source_sha256 is None
    assert deployment.runtime_image is None


@pytest.mark.asyncio
async def test_active_deployment_requires_snapshot_digest_and_positive_revision(
    db_session: AsyncSession,
) -> None:
    workspace = Workspace(slug="migration-active-ws", name="Migration Active")
    db_session.add(workspace)
    await db_session.flush()
    notebook = Notebook(workspace_id=workspace.id, title="Migration Active NB")
    db_session.add(notebook)
    await db_session.flush()
    deployment = Deployment(
        notebook_id=notebook.id,
        slug="migration-active-deploy",
        desired_state=DeploymentDesiredState.ACTIVE,
    )
    db_session.add(deployment)

    with pytest.raises(IntegrityError, match="ck_deployments_active_requires_snapshot"):
        await db_session.commit()
    await db_session.rollback()


@pytest.mark.asyncio
async def test_active_deployment_with_full_snapshot_is_accepted(
    db_session: AsyncSession,
) -> None:
    workspace = Workspace(slug="migration-active-ok-ws", name="Migration Active Ok")
    db_session.add(workspace)
    await db_session.flush()
    notebook = Notebook(workspace_id=workspace.id, title="Migration Active Ok NB")
    db_session.add(notebook)
    await db_session.flush()
    deployment = Deployment(
        notebook_id=notebook.id,
        slug="migration-active-ok-deploy",
        desired_state=DeploymentDesiredState.ACTIVE,
        source_snapshot="",  # an empty notebook is a valid snapshot
        source_sha256="deadbeef",
        runtime_image="registry.example/marimo-runtime@sha256:" + "0" * 64,
        revision=1,
    )
    db_session.add(deployment)

    await db_session.commit()  # must not raise
