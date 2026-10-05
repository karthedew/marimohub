from typing import Literal

from pydantic import BaseModel, Field, field_validator

from app.schemas.user import UserOut
from app.services.identity_text import check_display_name, check_username, has_control_character


class UserCreate(BaseModel):
    """Registration payload for creating a new user account."""

    username: str = Field(min_length=1, max_length=255)
    email: str = Field(min_length=1, max_length=255)
    password: str = Field(min_length=8)
    display_name: str | None = Field(default=None, max_length=255)

    @field_validator("username")
    @classmethod
    def validate_username(cls, value: str) -> str:
        """Refuse a username that could pass for someone else's (see `check_username`)."""
        return check_username(value)

    @field_validator("email")
    @classmethod
    def validate_email(cls, value: str) -> str:
        """Refuse control characters: no address holds one, and NUL cannot be stored."""
        if has_control_character(value):
            raise ValueError("an email address must not contain control characters")
        return value

    @field_validator("display_name", mode="before")
    @classmethod
    def trim_display_name(cls, value: object) -> object:
        """Trim the display name before its length is checked; a blank one means none."""
        if isinstance(value, str):
            return value.strip() or None
        return value

    @field_validator("display_name")
    @classmethod
    def validate_display_name(cls, value: str | None) -> str | None:
        """Refuse a name that would break or reorder what is shown around it."""
        return None if value is None else check_display_name(value)


class LoginRequest(BaseModel):
    """Credentials submitted to obtain an access token."""

    username: str
    password: str

    @field_validator("username")
    @classmethod
    def refuse_nul(cls, value: str) -> str:
        """Refuse NUL with a 422: PostgreSQL cannot store one, so the lookup itself would fail.

        Only NUL: accounts registered before usernames were checked may hold
        other control characters and must still be able to sign in.
        """
        if "\x00" in value:
            raise ValueError("a username must not contain NUL")
        return value


class Token(BaseModel):
    """An issued bearer access token."""

    access_token: str
    token_type: str = Field(default="bearer")


class OIDCProviderOut(BaseModel):
    """A configured external identity provider, as the sign-in page lists it.

    Deliberately only these three fields: issuer, client id and secret never
    leave the backend.
    """

    slug: str
    display_name: str
    kind: Literal["google", "oidc", "saml"]


class OIDCCallbackParams(BaseModel):
    """Query parameters an identity provider may send back to the OIDC callback.

    All optional: an error response carries no ``code``, and a forged or
    replayed request may carry nothing at all. Anything else the provider
    adds (``scope``, ``authuser``, ...) is ignored.
    """

    code: str | None = None
    state: str | None = None
    iss: str | None = None  # RFC 9207 issuer identification
    error: str | None = None
    error_description: str | None = None


class OIDCExchangeRequest(BaseModel):
    """The SPA's half of an OIDC login: the handoff plus its PKCE verifier."""

    handoff: str
    verifier: str


class OIDCExchangeOut(Token):
    """A bearer access token plus the account it signs in, after an OIDC login."""

    user: UserOut
