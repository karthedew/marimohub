import enum
from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import DateTime, Enum, ForeignKey, Integer, String, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.database import Base


class DeploymentStatus(str, enum.Enum):
    RUNNING = "running"
    SLEEPING = "sleeping"
    STOPPED = "stopped"


class Deployment(Base):
    __tablename__ = "deployments"
    __table_args__ = (UniqueConstraint("notebook_id", name="uq_deployments_notebook_id"),)

    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid4)
    notebook_id: Mapped[UUID] = mapped_column(ForeignKey("notebooks.id", ondelete="CASCADE"), nullable=False)
    slug: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)
    status: Mapped[DeploymentStatus] = mapped_column(
        Enum(DeploymentStatus, name="deployment_status", values_callable=lambda enum_: [item.value for item in enum_]),
        nullable=False,
        default=DeploymentStatus.SLEEPING,
    )
    port: Mapped[int | None] = mapped_column(Integer)
    last_active: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    notebook: Mapped["Notebook"] = relationship(back_populates="deployments")
