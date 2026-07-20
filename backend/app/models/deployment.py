from datetime import datetime
import enum
from typing import TYPE_CHECKING
from uuid import UUID, uuid4

from sqlalchemy import DateTime, Enum, ForeignKey, String, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.database import Base
from app.models.base import enum_values

if TYPE_CHECKING:
    from app.models.notebook import Notebook


class DeploymentStatus(enum.StrEnum):
    """Lifecycle state of a deployed notebook process."""

    RUNNING = "running"
    SLEEPING = "sleeping"
    STOPPED = "stopped"


class Deployment(Base):
    """A long-lived, externally addressable run of a notebook; 1:0..1 with its notebook."""

    __tablename__ = "deployments"
    __table_args__: tuple[UniqueConstraint, ...] = (
        UniqueConstraint("notebook_id", name="uq_deployments_notebook_id"),
    )

    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid4)
    notebook_id: Mapped[UUID] = mapped_column(
        ForeignKey("notebooks.id", ondelete="CASCADE"), nullable=False
    )
    slug: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)
    status: Mapped[DeploymentStatus] = mapped_column(
        Enum(
            DeploymentStatus,
            name="deployment_status",
            values_callable=enum_values,
        ),
        nullable=False,
        default=DeploymentStatus.SLEEPING,
    )
    last_active: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    notebook: Mapped["Notebook"] = relationship(back_populates="deployment")
