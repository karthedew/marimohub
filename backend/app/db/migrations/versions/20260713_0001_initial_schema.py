"""Initial schema

Revision ID: 20260713_0001
Revises:
Create Date: 2026-07-13
"""

from collections.abc import Sequence

from alembic import op
from pgvector.sqlalchemy import Vector
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "20260713_0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")
    op.execute(
        "CREATE FUNCTION f_textarr2text(text[]) RETURNS text "
        "LANGUAGE sql IMMUTABLE AS $$ SELECT array_to_string($1, ' ') $$"
    )
    op.execute("CREATE TYPE workspace_role AS ENUM ('owner', 'editor', 'viewer')")
    op.execute("CREATE TYPE notebook_visibility AS ENUM ('private', 'unlisted', 'public')")
    op.execute("CREATE TYPE deployment_status AS ENUM ('running', 'sleeping', 'stopped')")

    op.create_table(
        "users",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column("username", sa.String(length=255), nullable=False),
        sa.Column("email", sa.String(length=255), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.UniqueConstraint("email", name="uq_users_email"),
        sa.UniqueConstraint("username", name="uq_users_username"),
    )

    op.create_table(
        "identities",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("provider", sa.String(length=64), nullable=False),
        sa.Column("subject", sa.String(length=255), nullable=False),
        sa.Column("email", sa.String(length=255), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("last_login_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], name="fk_identities_user_id", ondelete="CASCADE"
        ),
        sa.UniqueConstraint("provider", "subject", name="uq_identities_provider_subject"),
    )
    op.create_index("ix_identities_user_id", "identities", ["user_id"])
    op.create_index(
        "uq_identities_one_local_per_user",
        "identities",
        ["user_id"],
        unique=True,
        postgresql_where=sa.text("provider = 'local'"),
    )

    op.create_table(
        "local_credentials",
        sa.Column("user_id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column("password_hash", sa.String(length=255), nullable=False),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], name="fk_local_credentials_user_id", ondelete="CASCADE"
        ),
    )

    op.create_table(
        "workspaces",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column("slug", sa.String(length=63), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("archived_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("purge_after", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.UniqueConstraint("slug", name="uq_workspaces_slug"),
        sa.CheckConstraint(
            "(archived_at IS NULL) = (purge_after IS NULL)", name="ck_workspaces_archive_pair"
        ),
    )
    op.create_index("ix_workspaces_purge_after", "workspaces", ["purge_after"])

    op.create_table(
        "workspace_members",
        sa.Column("workspace_id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column(
            "role",
            postgresql.ENUM(
                "owner", "editor", "viewer", name="workspace_role", create_type=False
            ),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspaces.id"],
            name="fk_workspace_members_workspace_id",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name="fk_workspace_members_user_id",
            ondelete="RESTRICT",
        ),
    )
    op.create_index("ix_workspace_members_user_id", "workspace_members", ["user_id"])

    op.create_table(
        "notebooks",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column("workspace_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("created_by", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("parent_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column(
            "tags",
            postgresql.ARRAY(sa.Text()),
            server_default=sa.text("ARRAY[]::text[]"),
            nullable=False,
        ),
        sa.Column("source", sa.Text(), nullable=True),
        sa.Column(
            "visibility",
            postgresql.ENUM(
                "private", "unlisted", "public", name="notebook_visibility", create_type=False
            ),
            nullable=False,
            server_default="private",
        ),
        sa.Column("fork_count", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column(
            "search_vector",
            postgresql.TSVECTOR(),
            sa.Computed(
                "to_tsvector('english', "
                "coalesce(title, '') || ' ' || "
                "coalesce(description, '') || ' ' || "
                "coalesce(f_textarr2text(tags), ''))",
                persisted=True,
            ),
            nullable=True,
        ),
        sa.Column("embedding", Vector(384), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspaces.id"],
            name="fk_notebooks_workspace_id",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["created_by"], ["users.id"], name="fk_notebooks_created_by", ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(
            ["parent_id"], ["notebooks.id"], name="fk_notebooks_parent_id", ondelete="SET NULL"
        ),
    )
    op.create_index("ix_notebooks_workspace_id", "notebooks", ["workspace_id"])
    op.create_index(
        "ix_notebooks_search_vector", "notebooks", ["search_vector"], postgresql_using="gin"
    )

    op.create_table(
        "deployments",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column("notebook_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("slug", sa.String(length=255), nullable=False),
        sa.Column(
            "status",
            postgresql.ENUM(
                "running", "sleeping", "stopped", name="deployment_status", create_type=False
            ),
            nullable=False,
        ),
        sa.Column("last_active", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["notebook_id"], ["notebooks.id"], name="fk_deployments_notebook_id", ondelete="CASCADE"
        ),
        sa.UniqueConstraint("notebook_id", name="uq_deployments_notebook_id"),
        sa.UniqueConstraint("slug", name="uq_deployments_slug"),
    )

    op.create_table(
        "notebook_data",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column("notebook_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("payload", postgresql.JSONB(), nullable=False),
        sa.Column("source", sa.String(length=255), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["notebook_id"],
            ["notebooks.id"],
            name="fk_notebook_data_notebook_id",
            ondelete="CASCADE",
        ),
    )


def downgrade() -> None:
    op.drop_table("notebook_data")
    op.drop_table("deployments")
    op.drop_index("ix_notebooks_search_vector", table_name="notebooks", postgresql_using="gin")
    op.drop_index("ix_notebooks_workspace_id", table_name="notebooks")
    op.drop_table("notebooks")
    op.drop_index("ix_workspace_members_user_id", table_name="workspace_members")
    op.drop_table("workspace_members")
    op.drop_index("ix_workspaces_purge_after", table_name="workspaces")
    op.drop_table("workspaces")
    op.drop_table("local_credentials")
    op.drop_index("uq_identities_one_local_per_user", table_name="identities")
    op.drop_index("ix_identities_user_id", table_name="identities")
    op.drop_table("identities")
    op.drop_table("users")
    op.execute("DROP TYPE deployment_status")
    op.execute("DROP TYPE notebook_visibility")
    op.execute("DROP TYPE workspace_role")
    op.execute("DROP FUNCTION f_textarr2text(text[])")
    op.execute("DROP EXTENSION IF EXISTS vector")
