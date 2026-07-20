from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession


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
