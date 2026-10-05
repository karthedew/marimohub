from datetime import datetime
from typing import Annotated
from uuid import UUID

from pydantic import (
    AfterValidator,
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
)

from app.models import WorkspaceRole
from app.services.identity_text import has_control_character
from app.services.member_candidates import MAX_QUERY_LENGTH, MIN_QUERY_LENGTH
from app.services.slug import DNS_LABEL_RE


def _strip(value: object) -> object:
    # Python's str.strip, exactly as `find_member_candidates` trims: pydantic's
    # own strip_whitespace keeps characters such as "\x1c" that str.strip
    # removes, so a query could pass here yet be too short for the service.
    return value.strip() if isinstance(value, str) else value


def _refuse_control_characters(value: str) -> str:
    # No name holds one, and PostgreSQL cannot store NUL: searching for it
    # would fail the query with a 500 rather than this 422.
    if has_control_character(value):
        raise ValueError("a person search must not contain control characters")
    return value


# The person-search text (`?q=`): trimmed first (the outer validator runs
# first), then length-checked, then checked for control characters.
MemberCandidateQuery = Annotated[
    str,
    StringConstraints(min_length=MIN_QUERY_LENGTH, max_length=MAX_QUERY_LENGTH),
    AfterValidator(_refuse_control_characters),
    BeforeValidator(_strip),
]


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
    """Add a member to a workspace by id (find the id with the member-candidates search)."""

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
    display_name: str | None
    # In full only on the caller's own membership; anyone else's is masked
    # like a person-search hint ("k•••@example.com"). Adding a member needs
    # no consent, so a full address here would hand every Owner the email of
    # anyone the person search finds (docs/adr/0004).
    email: str
    role: WorkspaceRole
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)


class MemberCandidateOut(BaseModel):
    """A person an Owner could add to the workspace, as the person search reveals them."""

    user_id: UUID
    username: str
    display_name: str | None
    # The full email only when the search was that exact address; otherwise
    # masked as its first character, "•••@" and the domain.
    email_hint: str
