from datetime import datetime
import enum
from typing import TYPE_CHECKING
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
    func,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, TSVECTOR, UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.database import Base
from app.models.base import enum_values

if TYPE_CHECKING:
    from app.models.deployment import Deployment
    from app.models.user import User
    from app.models.workspace import Workspace


class NotebookVisibility(enum.StrEnum):
    """How widely a notebook is shared."""

    PRIVATE = "private"
    UNLISTED = "unlisted"
    PUBLIC = "public"


class Notebook(Base):
    """A marimo notebook owned by a workspace, optionally forked from a parent."""

    __tablename__ = "notebooks"

    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid4)
    workspace_id: Mapped[UUID] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False, index=True
    )
    created_by: Mapped[UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
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
            values_callable=enum_values,
        ),
        nullable=False,
        default=NotebookVisibility.PRIVATE,
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

    workspace: Mapped["Workspace"] = relationship(back_populates="notebooks")
    creator: Mapped["User | None"] = relationship()
    parent: Mapped["Notebook | None"] = relationship(
        remote_side="Notebook.id", back_populates="forks"
    )
    forks: Mapped[list["Notebook"]] = relationship(back_populates="parent")
    deployment: Mapped["Deployment | None"] = relationship(back_populates="notebook", uselist=False)
    data: Mapped[list["NotebookData"]] = relationship(back_populates="notebook")


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
