from abc import ABC, abstractmethod
from typing import override

from sqlalchemy import or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import hash_password, verify_password
from app.models import User


class DuplicateUserError(Exception):
    """Raised when a username or email is already registered."""


class AuthService(ABC):
    """Abstract service for registering and authenticating users."""

    @abstractmethod
    async def register(self, username: str, email: str, password: str) -> User:
        """Create a new user, raising ``DuplicateUserError`` on conflict."""
        raise NotImplementedError

    @abstractmethod
    async def authenticate(self, username: str, password: str) -> User | None:
        """Return the matching user for valid credentials, else ``None``."""
        raise NotImplementedError


class BasicAuthService(AuthService):
    """Username/password auth backed by a database and bcrypt hashing."""

    def __init__(self, db: AsyncSession) -> None:
        """Bind the service to an active database session."""
        super().__init__()
        self.db = db

    @override
    async def register(self, username: str, email: str, password: str) -> User:
        """Create a new user, raising ``DuplicateUserError`` on conflict."""
        existing = await self.db.scalar(
            select(User).where(or_(User.username == username, User.email == email))
        )
        if existing is not None:
            raise DuplicateUserError

        user = User(username=username, email=email, password_hash=hash_password(password))
        self.db.add(user)
        try:
            await self.db.commit()
        except IntegrityError as exc:
            await self.db.rollback()
            raise DuplicateUserError from exc

        await self.db.refresh(user)
        return user

    @override
    async def authenticate(self, username: str, password: str) -> User | None:
        """Return the matching user for valid credentials, else ``None``."""
        user = await self.db.scalar(select(User).where(User.username == username))
        if user is None or not verify_password(password, user.password_hash):
            return None
        return user
