from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.models import WorkspaceRole
from app.services.slug import DNS_LABEL_RE


class WorkspaceCreate(BaseModel):
    """Create a workspace. `slug` optional; derived from `name` when omitted."""

    name: str = Field(min_length=1, max_length=255)
    slug: str | None = Field(default=None, max_length=63)

    @field_validator("slug")
    @classmethod
    def validate_slug(cls, value: str | None) -> str | None:
        """Reject a slug that isn't a valid RFC-1123 DNS label."""
        if value is not None and not DNS_LABEL_RE.match(value):
            raise ValueError("slug must be a lowercase DNS label (RFC-1123, <=63 chars)")
        return value


class WorkspaceUpdate(BaseModel):
    """Rename a workspace. Slug is immutable (it may appear in k8s names)."""

    name: str = Field(min_length=1, max_length=255)


class WorkspaceOut(BaseModel):
    """A workspace as seen by a member, annotated with the caller's own role."""

    id: UUID
    slug: str
    name: str
    role: WorkspaceRole  # the caller's role in this workspace, supplied explicitly
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)


class WorkspaceArchiveOut(WorkspaceOut):
    """A workspace as seen through the owner-only archive surface."""

    archived_at: datetime
    purge_after: datetime


class WorkspaceMemberCreate(BaseModel):
    """Add a member to a workspace by id."""

    user_id: UUID
    role: WorkspaceRole = WorkspaceRole.EDITOR


class WorkspaceMemberUpdate(BaseModel):
    """Change a member's role."""

    role: WorkspaceRole


class WorkspaceMemberOut(BaseModel):
    """A workspace membership, joined with the member's identity for display."""

    workspace_id: UUID
    user_id: UUID
    username: str
    email: str
    role: WorkspaceRole
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)
