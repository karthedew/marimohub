from pydantic import BaseModel, Field


class UserCreate(BaseModel):
    """Registration payload for creating a new user account."""

    username: str = Field(min_length=1, max_length=255)
    email: str = Field(min_length=1, max_length=255)
    password: str = Field(min_length=8)


class LoginRequest(BaseModel):
    """Credentials submitted to obtain an access token."""

    username: str
    password: str


class Token(BaseModel):
    """An issued bearer access token."""

    access_token: str
    token_type: str = Field(default="bearer")
