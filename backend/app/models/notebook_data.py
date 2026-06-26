from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import DateTime, ForeignKey, String, func
from sqlalchemy.dialects.postgresql import JSONB, UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.database import Base


class NotebookData(Base):
    __tablename__ = "notebook_data"

    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid4)
    notebook_id: Mapped[UUID] = mapped_column(ForeignKey("notebooks.id", ondelete="CASCADE"), nullable=False)
    payload: Mapped[Any] = mapped_column(JSONB, nullable=False)
    source: Mapped[str | None] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    notebook: Mapped["Notebook"] = relationship(back_populates="data")
