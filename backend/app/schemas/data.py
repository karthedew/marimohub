from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel


class NotebookDataCreated(BaseModel):
    id: UUID


class NotebookDataOut(BaseModel):
    payload: Any
    source: str | None
    created_at: datetime
