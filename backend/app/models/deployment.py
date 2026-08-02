from datetime import datetime
import enum
from typing import TYPE_CHECKING
from uuid import UUID, uuid4

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    Enum,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.database import Base
from app.models.base import enum_values

if TYPE_CHECKING:
    from app.models.notebook import Notebook


class DeploymentDesiredState(enum.StrEnum):
    """The owner's durable intent for a deployment, independent of live cluster state.

    This is the only fact about a deployment's runtime that this row is
    allowed to hold durably. Whether it is currently running, sleeping, or
    failed is always read through the live Runtime instead (see
    ``resolved_status`` in ``app.api.deployments``): a wake, a crash, or the
    Runtime's own health can change that from one moment to the next in ways
    a persisted "running" flag would immediately go stale on.
    """

    ACTIVE = "active"
    STOPPED = "stopped"


class Deployment(Base):
    """A durable deploy-time snapshot and its owner intent; 1:0..1 with its notebook.

    The snapshot fields (``source_snapshot``, ``source_sha256``,
    ``runtime_image``, ``revision``) are written only by deploy/redeploy —
    never by an ordinary Notebook source edit — so a Deployment keeps serving
    exactly what was deployed until an Editor or Owner explicitly redeploys.
    """

    __tablename__ = "deployments"
    __table_args__ = (
        UniqueConstraint("notebook_id", name="uq_deployments_notebook_id"),
        CheckConstraint(
            "desired_state <> 'active' OR ("
            "source_snapshot IS NOT NULL AND "
            "source_sha256 IS NOT NULL AND source_sha256 <> '' AND "
            "runtime_image IS NOT NULL AND runtime_image <> '' AND "
            "revision > 0)",
            name="ck_deployments_active_requires_snapshot",
        ),
    )

    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid4)
    notebook_id: Mapped[UUID] = mapped_column(
        ForeignKey("notebooks.id", ondelete="CASCADE"), nullable=False
    )
    slug: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)
    desired_state: Mapped[DeploymentDesiredState] = mapped_column(
        Enum(
            DeploymentDesiredState,
            name="deployment_desired_state",
            values_callable=enum_values,
        ),
        nullable=False,
        default=DeploymentDesiredState.STOPPED,
    )
    # The exact source served the moment this deployment was (re)deployed. An
    # empty string is a valid snapshot (an empty notebook); only NULL means
    # "never deployed", which the check constraint forbids while active.
    source_snapshot: Mapped[str | None] = mapped_column(Text)
    source_sha256: Mapped[str | None] = mapped_column(String(64))
    # An immutable digest reference, not a mutable tag: the Runtime this
    # deployment launches must always be the exact image validated at deploy
    # time, never whatever a tag happens to resolve to later.
    runtime_image: Mapped[str | None] = mapped_column(String(512))
    # Monotonically increasing per deployment; 0 means "never deployed" and is
    # the only value the active-state check constraint forbids. The operator
    # binds a deploy Runtime's source fetch to this exact value, and the
    # backend never routes a Ready Runtime whose CR reports a different one.
    revision: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_active: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    notebook: Mapped["Notebook"] = relationship(back_populates="deployment")
