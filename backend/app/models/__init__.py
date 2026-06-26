from datetime import datetime
import enum
from uuid import UUID, uuid4

from pgvector.sqlalchemy import Vector  # pyright: ignore[reportMissingTypeStubs]
from pydantic import JsonValue
from sqlalchemy import (
    DateTime,
    Enum,
    FetchedValue,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, TSVECTOR, UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.database import Base


def _enum_values(enum_class: type[enum.StrEnum]) -> list[str]:
    """Map a string enum to its values, for SQLAlchemy ``Enum(values_callable=...)``."""
    return [str(member) for member in enum_class]


class NotebookVisibility(enum.StrEnum):
    """How widely a notebook is shared."""

    DRAFT = "draft"
    UNLISTED = "unlisted"
    PUBLIC = "public"


class DeploymentStatus(enum.StrEnum):
    """Lifecycle state of a deployed notebook process."""

    RUNNING = "running"
    SLEEPING = "sleeping"
    STOPPED = "stopped"


class User(Base):
    """A registered user account."""

    __tablename__ = "users"

    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid4)
    username: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)
    email: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    notebooks: Mapped[list["Notebook"]] = relationship(back_populates="user")


class Notebook(Base):
    """A marimo notebook owned by a user, optionally forked from a parent."""

    __tablename__ = "notebooks"

    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid4)
    user_id: Mapped[UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    parent_id: Mapped[UUID | None] = mapped_column(ForeignKey("notebooks.id", ondelete="SET NULL"))
    title: Mapped[str] = mapped_column(Text, nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    tags: Mapped[list[str]] = mapped_column(
        ARRAY(Text),
        nullable=False,
        default=list,
        server_default=text("ARRAY[]::text[]"),
    )
    source: Mapped[str | None] = mapped_column(Text)
    visibility: Mapped[NotebookVisibility] = mapped_column(
        Enum(
            NotebookVisibility,
            name="notebook_visibility",
            values_callable=_enum_values,
        ),
        nullable=False,
        default=NotebookVisibility.DRAFT,
    )
    fork_count: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
        server_default=text("0"),
    )
    search_vector: Mapped[str | None] = mapped_column(
        TSVECTOR,
        server_default=FetchedValue(),
        server_onupdate=FetchedValue(),
        deferred=True,
    )
    embedding: Mapped[list[float] | None] = mapped_column(Vector(384))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    user: Mapped["User"] = relationship(back_populates="notebooks")
    parent: Mapped["Notebook | None"] = relationship(
        remote_side="Notebook.id", back_populates="forks"
    )
    forks: Mapped[list["Notebook"]] = relationship(back_populates="parent")
    deployments: Mapped[list["Deployment"]] = relationship(back_populates="notebook")
    data: Mapped[list["NotebookData"]] = relationship(back_populates="notebook")


class Deployment(Base):
    """A long-lived, externally addressable run of a notebook."""

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
            values_callable=_enum_values,
        ),
        nullable=False,
        default=DeploymentStatus.SLEEPING,
    )
    port: Mapped[int | None] = mapped_column(Integer)
    last_active: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    notebook: Mapped["Notebook"] = relationship(back_populates="deployments")


class NotebookData(Base):
    """A versioned JSON data payload attached to a notebook."""

    __tablename__ = "notebook_data"

    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid4)
    notebook_id: Mapped[UUID] = mapped_column(
        ForeignKey("notebooks.id", ondelete="CASCADE"), nullable=False
    )
    payload: Mapped[JsonValue] = mapped_column(JSONB, nullable=False)
    source: Mapped[str | None] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    notebook: Mapped["Notebook"] = relationship(back_populates="data")


__all__ = [
    "Deployment",
    "DeploymentStatus",
    "Notebook",
    "NotebookData",
    "NotebookVisibility",
    "User",
]
