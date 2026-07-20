from datetime import datetime
import enum
from typing import TYPE_CHECKING
from uuid import UUID, uuid4

from sqlalchemy import CheckConstraint, DateTime, Enum, ForeignKey, String, Text, func
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.database import Base
from app.models.base import enum_values

if TYPE_CHECKING:
    from app.models.notebook import Notebook
    from app.models.user import User


class WorkspaceRole(enum.StrEnum):
    """A member's authority within a workspace, ordered owner > editor > viewer."""

    OWNER = "owner"  # manage members, rename/archive/restore workspace, + editor operations
    EDITOR = "editor"  # create/edit/deploy/fork notebooks in the workspace
    VIEWER = "viewer"  # read private notebooks in the workspace; no writes


class Workspace(Base):
    """An explicitly-created notebook ownership boundary; no personal subtype."""

    __tablename__ = "workspaces"
    __table_args__ = (
        CheckConstraint(
            "(archived_at IS NULL) = (purge_after IS NULL)",
            name="ck_workspaces_archive_pair",
        ),
    )

    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid4)
    slug: Mapped[str] = mapped_column(String(63), unique=True, nullable=False)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    purge_after: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    members: Mapped[list["WorkspaceMember"]] = relationship(
        back_populates="workspace", cascade="all, delete-orphan"
    )
    notebooks: Mapped[list["Notebook"]] = relationship(back_populates="workspace")


class WorkspaceMember(Base):
    """Membership of a user in a workspace at a given role; composite PK forbids duplicates."""

    __tablename__ = "workspace_members"

    workspace_id: Mapped[UUID] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), primary_key=True
    )
    user_id: Mapped[UUID] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), primary_key=True, index=True
    )
    role: Mapped[WorkspaceRole] = mapped_column(
        Enum(WorkspaceRole, name="workspace_role", values_callable=enum_values), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    workspace: Mapped["Workspace"] = relationship(back_populates="members")
    user: Mapped["User"] = relationship(back_populates="memberships")
