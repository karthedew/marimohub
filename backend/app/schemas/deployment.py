import enum
import re

from pydantic import BaseModel, Field, field_validator
from pydantic_core import PydanticCustomError

SLUG_PATTERN = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
SLUG_MESSAGE = "Use lowercase letters, numbers, and hyphens. Start and end with a letter or number."


class DeploymentStatus(enum.StrEnum):
    """The public, coarse-grained status a deployment projects to API callers.

    Never persisted: it is always computed from `Deployment.desired_state`
    plus the live Runtime's phase (see `resolved_status`), so a wake, a
    crash, or the Runtime's own health is reflected immediately rather than
    through a column this row would have to keep in sync.
    """

    RUNNING = "running"
    SLEEPING = "sleeping"
    FAILED = "failed"
    STOPPED = "stopped"


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
    """A deployment's public slug, status, and externally reachable URL.

    `failure_reason`/`message` are populated only for an authorized
    Notebook-management caller and only while `status` is `failed`; they
    carry a stable Condition Reason and bounded, sanitized text, never raw
    Pod logs or cluster internals. Anonymous Deployment traffic never sees
    this schema at all -- it gets a generic unavailable gateway response
    instead (see `app.services.marimo_proxy.UpstreamNotReady`).
    """

    slug: str
    status: DeploymentStatus
    url: str
    failure_reason: str | None = None
    message: str | None = None
