import base64
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from enum import StrEnum
import hashlib
import hmac
from uuid import UUID

import bcrypt
import jwt
from jwt import InvalidTokenError

from app.core.config import get_settings


class TokenPurpose(StrEnum):
    """What a non-bearer signed token is for; also the value of its ``typ`` claim.

    Each purpose signs with its own key derived from ``SECRET_KEY`` and is
    stamped with its ``typ``, so a token minted for one purpose verifies for
    no other -- and never as a bearer access token, which carries no ``typ``
    and is signed with ``SECRET_KEY`` itself (see `decode_token`).
    """

    OIDC_LOGIN_COOKIE = "marimohub/oidc-login-cookie"
    OIDC_HANDOFF = "marimohub/oidc-handoff"


# How far apart two replicas' clocks may be for a purpose token one mints to
# verify on the other. Without it, a token minted by a replica whose clock
# runs even a second ahead is "not yet valid" (`iat` in the future) elsewhere.
PURPOSE_TOKEN_CLOCK_LEEWAY = timedelta(seconds=5)


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
    """Return the user id encoded in a valid access token, or ``None`` if invalid.

    Only access tokens qualify. Every other token the backend signs carries a
    ``typ`` claim (and is signed with a per-purpose key), so it is refused
    here even if it were signed with ``SECRET_KEY`` itself.
    """
    try:
        payload: dict[str, object] = jwt.decode(  # pyright: ignore[reportUnknownMemberType]
            token,
            get_settings().SECRET_KEY,
            algorithms=["HS256"],
            options={"require": ["exp", "sub"]},
        )
        subject = payload.get("sub")
        if "typ" in payload or not isinstance(subject, str):
            return None
        return UUID(subject)
    except (InvalidTokenError, ValueError):
        return None


def _purpose_key(secret_key: str, purpose: TokenPurpose) -> bytes:
    """Derive the signing key for one token purpose: HMAC-SHA256(SECRET_KEY, purpose)."""
    return hmac.new(
        secret_key.encode("utf-8"), purpose.value.encode("utf-8"), hashlib.sha256
    ).digest()


def create_purpose_token(
    secret_key: str, purpose: TokenPurpose, claims: Mapping[str, str], ttl: timedelta
) -> str:
    """Sign ``claims`` as a ``purpose`` token that expires after ``ttl``."""
    issued_at = datetime.now(UTC)
    payload: dict[str, object] = {
        **claims,
        "typ": purpose.value,
        "iat": issued_at,
        "exp": issued_at + ttl,
    }
    return jwt.encode(  # pyright: ignore[reportUnknownMemberType]
        payload, _purpose_key(secret_key, purpose), algorithm="HS256"
    )


def decode_purpose_token(
    secret_key: str, purpose: TokenPurpose, token: str
) -> dict[str, object] | None:
    """Return the claims of a valid, unexpired ``purpose`` token, else ``None``.

    Allows `PURPOSE_TOKEN_CLOCK_LEEWAY` of clock skew: one replica may mint a
    token that another, whose clock runs slightly behind, checks.
    """
    try:
        payload: dict[str, object] = jwt.decode(  # pyright: ignore[reportUnknownMemberType]
            token,
            _purpose_key(secret_key, purpose),
            algorithms=["HS256"],
            options={"require": ["exp", "iat", "typ"]},
            leeway=PURPOSE_TOKEN_CLOCK_LEEWAY,
        )
    except (InvalidTokenError, ValueError):  # ValueError: e.g. a lone surrogate
        return None
    return payload if payload.get("typ") == purpose.value else None


def pkce_s256_challenge(verifier: str) -> str:
    """Return the RFC 7636 S256 challenge: base64url(SHA-256(verifier)) without padding.

    Raises:
        ValueError: ``verifier`` has no UTF-8 encoding (it holds a lone surrogate).
    """
    digest = hashlib.sha256(verifier.encode("utf-8")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def constant_time_equals(left: str, right: str) -> bool:
    """Compare two strings without leaking where they first differ."""
    return hmac.compare_digest(left.encode("utf-8"), right.encode("utf-8"))
