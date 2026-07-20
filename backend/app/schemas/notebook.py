from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.models import NotebookVisibility


class NotebookCreate(BaseModel):
    """Request body for creating a new notebook."""

    title: str = Field(min_length=1)
    description: str | None = None
    tags: list[str] = Field(default_factory=list)
    source: str | None = None
    workspace_id: UUID


class NotebookImport(BaseModel):
    """Request body for importing a notebook from an external URL."""

    url: str = Field(min_length=1)
    pat: str | None = None
    workspace_id: UUID


class NotebookFork(BaseModel):
    """Request body naming the target workspace for a fork."""

    workspace_id: UUID


class NotebookUpdate(BaseModel):
    """Partial update for an existing notebook; unset fields are left unchanged."""

    title: str | None = Field(default=None, min_length=1)
    description: str | None = None
    tags: list[str] | None = None
    source: str | None = None

    @field_validator("title", "tags", mode="before")
    @classmethod
    def reject_null_required_columns(cls, value: object) -> object:
        """Reject explicit nulls for columns that are not nullable."""
        if value is None:
            raise ValueError("Field cannot be null")
        return value


class NotebookPublish(BaseModel):
    """Request body for changing a notebook's visibility."""

    visibility: NotebookVisibility


class NotebookOut(BaseModel):
    """Public representation of a notebook."""

    id: UUID
    workspace_id: UUID
    created_by: UUID | None
    parent_id: UUID | None
    parent_title: str | None = None
    parent_workspace_id: UUID | None = None
    parent_workspace_slug: str | None = None
    title: str
    description: str | None
    tags: list[str]
    visibility: NotebookVisibility
    fork_count: int
    source: str | None = None
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)


class NotebookListOut(BaseModel):
    """A paginated page of notebooks."""

    items: list[NotebookOut]
    total: int
    page: int
    page_size: int
