"""Record whether an identity's provider vouched for its email

Revision ID: 20261004_0003
Revises: 20260801_0002
Create Date: 2026-10-04
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa

revision: str = "20261004_0003"
down_revision: str | None = "20260801_0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Trusted email linking may only join an account whose email its
    # identities were vouched for (OIDCClaims.email_is_authoritative). No
    # existing row recorded that, so every one starts out unproven: a local
    # password identity never is, and an external identity is re-evaluated
    # on its next sign-in. The server default stays, so a row inserted
    # without the column can never count as proof either.
    op.add_column(
        "identities",
        sa.Column("email_authoritative", sa.Boolean(), nullable=False, server_default=sa.false()),
    )


def downgrade() -> None:
    op.drop_column("identities", "email_authoritative")
