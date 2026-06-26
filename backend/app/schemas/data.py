from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, JsonValue


class NotebookDataCreated(BaseModel):
    """Identifier of a newly stored notebook data record."""

    id: UUID


class NotebookDataOut(BaseModel):
    """A stored notebook data payload and its provenance."""

    payload: JsonValue
    source: str | None
    created_at: datetime
