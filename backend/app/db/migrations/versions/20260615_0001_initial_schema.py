"""Initial schema

Revision ID: 20260615_0001
Revises:
Create Date: 2026-06-15
"""

from collections.abc import Sequence

from alembic import op
from pgvector.sqlalchemy import Vector
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "20260615_0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")
    op.execute(
        "CREATE FUNCTION f_textarr2text(text[]) RETURNS text "
        "LANGUAGE sql IMMUTABLE AS $$ SELECT array_to_string($1, ' ') $$"
    )
    op.execute("CREATE TYPE notebook_visibility AS ENUM ('draft', 'unlisted', 'public')")
    op.execute("CREATE TYPE deployment_status AS ENUM ('running', 'sleeping', 'stopped')")

    op.create_table(
        "users",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column("username", sa.String(length=255), nullable=False),
        sa.Column("email", sa.String(length=255), nullable=False),
        sa.Column("password_hash", sa.String(length=255), nullable=False),
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
        "notebooks",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
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
                "draft", "unlisted", "public", name="notebook_visibility", create_type=False
            ),
            nullable=False,
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
            ["parent_id"], ["notebooks.id"], name="fk_notebooks_parent_id", ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], name="fk_notebooks_user_id", ondelete="CASCADE"
        ),
    )
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
        sa.Column("port", sa.Integer(), nullable=True),
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
    op.drop_table("notebooks")
    op.drop_table("users")
    op.execute("DROP TYPE deployment_status")
    op.execute("DROP TYPE notebook_visibility")
    op.execute("DROP FUNCTION f_textarr2text(text[])")
    op.execute("DROP EXTENSION IF EXISTS vector")
