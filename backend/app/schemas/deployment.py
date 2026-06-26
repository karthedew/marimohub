import re

from pydantic import BaseModel, Field, field_validator
from pydantic_core import PydanticCustomError

from app.models import DeploymentStatus

SLUG_PATTERN = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
SLUG_MESSAGE = "Use lowercase letters, numbers, and hyphens. Start and end with a letter or number."


class DeploymentCreate(BaseModel):
    """Request body for creating or updating a deployment."""

    slug: str | None = Field(default=None, min_length=1, max_length=255)

    @field_validator("slug")
    @classmethod
    def _check_slug_format(cls, value: str | None) -> str | None:
        if value is not None and SLUG_PATTERN.fullmatch(value) is None:
            raise PydanticCustomError("slug_format", SLUG_MESSAGE)
        return value


class DeploymentOut(BaseModel):
    """A deployment's public slug, status, and externally reachable URL."""

    slug: str
    status: DeploymentStatus
    url: str
