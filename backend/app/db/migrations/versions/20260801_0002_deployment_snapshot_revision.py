"""Deployment snapshot, digest, and revision

Revision ID: 20260801_0002
Revises: 20260713_0001
Create Date: 2026-08-01
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "20260801_0002"
down_revision: str | None = "20260713_0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_DESIRED_STATE_ENUM = postgresql.ENUM(
    "active", "stopped", name="deployment_desired_state", create_type=False
)


def upgrade() -> None:
    op.execute("CREATE TYPE deployment_desired_state AS ENUM ('active', 'stopped')")

    # Every existing row has no deploy-time snapshot or resolved image digest
    # to satisfy the active-state check constraint below, so it lands
    # `stopped`: an Editor or Owner must explicitly redeploy it before it can
    # run again. `revision` starts at 0, the "never deployed" sentinel the
    # constraint also enforces.
    op.add_column(
        "deployments",
        sa.Column("desired_state", _DESIRED_STATE_ENUM, nullable=False, server_default="stopped"),
    )
    op.add_column("deployments", sa.Column("source_snapshot", sa.Text(), nullable=True))
    op.add_column("deployments", sa.Column("source_sha256", sa.String(length=64), nullable=True))
    op.add_column("deployments", sa.Column("runtime_image", sa.String(length=512), nullable=True))
    op.add_column(
        "deployments",
        sa.Column("revision", sa.Integer(), nullable=False, server_default="0"),
    )

    op.drop_column("deployments", "status")
    op.execute("DROP TYPE deployment_status")

    op.create_check_constraint(
        "ck_deployments_active_requires_snapshot",
        "deployments",
        "desired_state <> 'active' OR ("
        "source_snapshot IS NOT NULL AND "
        "source_sha256 IS NOT NULL AND source_sha256 <> '' AND "
        "runtime_image IS NOT NULL AND runtime_image <> '' AND "
        "revision > 0)",
    )

    # The server defaults above exist only to backfill existing rows; the ORM
    # model sets both columns explicitly on every new row.
    op.alter_column("deployments", "desired_state", server_default=None)
    op.alter_column("deployments", "revision", server_default=None)


def downgrade() -> None:
    op.drop_constraint("ck_deployments_active_requires_snapshot", "deployments", type_="check")

    op.execute("CREATE TYPE deployment_status AS ENUM ('running', 'sleeping', 'stopped')")
    op.add_column(
        "deployments",
        sa.Column(
            "status",
            postgresql.ENUM(
                "running", "sleeping", "stopped", name="deployment_status", create_type=False
            ),
            nullable=False,
            server_default="sleeping",
        ),
    )
    op.alter_column("deployments", "status", server_default=None)

    op.drop_column("deployments", "revision")
    op.drop_column("deployments", "runtime_image")
    op.drop_column("deployments", "source_sha256")
    op.drop_column("deployments", "source_snapshot")
    op.drop_column("deployments", "desired_state")
    op.execute("DROP TYPE deployment_desired_state")
