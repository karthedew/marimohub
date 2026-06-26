from pydantic import BaseModel, Field

from app.models import DeploymentStatus


class DeploymentCreate(BaseModel):
    slug: str | None = Field(default=None, min_length=1, max_length=255, pattern=r"^[a-z0-9]+(?:-[a-z0-9]+)*$")


class DeploymentOut(BaseModel):
    slug: str
    status: DeploymentStatus
    url: str
