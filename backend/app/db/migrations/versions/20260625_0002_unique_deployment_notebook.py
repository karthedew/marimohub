"""Enforce one deployment per notebook

Revision ID: 20260625_0002
Revises: 20260615_0001
Create Date: 2026-06-25
"""

from collections.abc import Sequence

from alembic import op


revision: str = "20260625_0002"
down_revision: str | None = "20260615_0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        DO $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1
                FROM pg_constraint
                WHERE conname = 'uq_deployments_notebook_id'
                  AND conrelid = 'deployments'::regclass
            ) THEN
                ALTER TABLE deployments
                ADD CONSTRAINT uq_deployments_notebook_id UNIQUE (notebook_id);
            END IF;
        END $$
        """
    )


def downgrade() -> None:
    op.execute("ALTER TABLE deployments DROP CONSTRAINT IF EXISTS uq_deployments_notebook_id")
