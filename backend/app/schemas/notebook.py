from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.models import NotebookVisibility


class NotebookCreate(BaseModel):
    title: str = Field(min_length=1)
    description: str | None = None
    tags: list[str] = Field(default_factory=list)
    source: str | None = None


class NotebookImport(BaseModel):
    url: str = Field(min_length=1)
    pat: str | None = None


class NotebookUpdate(BaseModel):
    title: str | None = Field(default=None, min_length=1)
    description: str | None = None
    tags: list[str] | None = None
    source: str | None = None

    @field_validator("title", "tags", mode="before")
    @classmethod
    def reject_null_required_columns(cls, value: Any) -> Any:
        if value is None:
            raise ValueError("Field cannot be null")
        return value


class NotebookPublish(BaseModel):
    visibility: NotebookVisibility


class NotebookOut(BaseModel):
    id: UUID
    user_id: UUID
    parent_id: UUID | None
    parent_title: str | None = None
    parent_owner_id: UUID | None = None
    parent_owner_username: str | None = None
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
    items: list[NotebookOut]
    total: int
    page: int
    page_size: int
