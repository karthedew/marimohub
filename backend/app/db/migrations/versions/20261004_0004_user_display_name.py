"""Give users an optional display name

Revision ID: 20261004_0004
Revises: 20261004_0003
Create Date: 2026-10-04
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa

revision: str = "20261004_0004"
down_revision: str | None = "20261004_0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # A person's own name, shown beside their username and matched by the
    # Owner-only person search (docs/adr/0004). Optional and not unique:
    # existing accounts start without one, and an OIDC account gets one from
    # its provider's `name` claim on its next sign-in.
    op.add_column("users", sa.Column("display_name", sa.String(length=255), nullable=True))


def downgrade() -> None:
    op.drop_column("users", "display_name")
