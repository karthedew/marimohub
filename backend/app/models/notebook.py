import enum
from datetime import datetime
from uuid import UUID, uuid4

from pgvector.sqlalchemy import Vector
from sqlalchemy import DateTime, Enum, FetchedValue, ForeignKey, Integer, Text, func, text
from sqlalchemy.dialects.postgresql import ARRAY, TSVECTOR, UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.database import Base


class NotebookVisibility(str, enum.Enum):
    DRAFT = "draft"
    UNLISTED = "unlisted"
    PUBLIC = "public"


class Notebook(Base):
    __tablename__ = "notebooks"

    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid4)
    user_id: Mapped[UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
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
            values_callable=lambda enum_: [item.value for item in enum_],
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
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    user: Mapped["User"] = relationship(back_populates="notebooks")
    parent: Mapped["Notebook | None"] = relationship(remote_side=[id], back_populates="forks")
    forks: Mapped[list["Notebook"]] = relationship(back_populates="parent")
    deployments: Mapped[list["Deployment"]] = relationship(back_populates="notebook")
    data: Mapped[list["NotebookData"]] = relationship(back_populates="notebook")
