from datetime import UTC, datetime, timedelta
from uuid import UUID

import bcrypt
import jwt
from jwt import InvalidTokenError

from app.core.config import get_settings


def hash_password(password: str) -> str:
    """Return a bcrypt hash for the given plaintext password."""
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def verify_password(password: str, password_hash: str) -> bool:
    """Return whether the plaintext password matches the stored bcrypt hash."""
    try:
        return bcrypt.checkpw(password.encode("utf-8"), password_hash.encode("utf-8"))
    except ValueError:
        return False


def create_access_token(user_id: UUID) -> str:
    """Return a signed JWT access token for the given user."""
    settings = get_settings()
    expires_at = datetime.now(UTC) + timedelta(minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES)
    # PyJWT's key parameter type depends on cryptography types pyright cannot
    # fully resolve, so the bound method reads as partially unknown.
    return jwt.encode(  # pyright: ignore[reportUnknownMemberType]
        {"sub": str(user_id), "exp": expires_at},
        settings.SECRET_KEY,
        algorithm="HS256",
    )


def decode_token(token: str) -> UUID | None:
    """Return the user id encoded in a valid token, or ``None`` if invalid."""
    try:
        payload: dict[str, object] = jwt.decode(  # pyright: ignore[reportUnknownMemberType]
            token, get_settings().SECRET_KEY, algorithms=["HS256"]
        )
        subject = payload.get("sub")
        if not isinstance(subject, str):
            return None
        return UUID(subject)
    except (InvalidTokenError, ValueError):
        return None
