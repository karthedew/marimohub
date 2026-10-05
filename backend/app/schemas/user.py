from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict


class UserOut(BaseModel):
    """Public-facing representation of a user account."""

    id: UUID
    username: str
    email: str
    display_name: str | None
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)
