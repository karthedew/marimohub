from abc import ABC, abstractmethod

from sqlalchemy import or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import hash_password, verify_password
from app.models import User


class DuplicateUserError(Exception):
    pass


class AuthService(ABC):
    @abstractmethod
    async def register(self, username: str, email: str, password: str) -> User:
        raise NotImplementedError

    @abstractmethod
    async def authenticate(self, username: str, password: str) -> User | None:
        raise NotImplementedError


class BasicAuthService(AuthService):
    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    async def register(self, username: str, email: str, password: str) -> User:
        existing = await self.db.scalar(select(User).where(or_(User.username == username, User.email == email)))
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

    async def authenticate(self, username: str, password: str) -> User | None:
        user = await self.db.scalar(select(User).where(User.username == username))
        if user is None or not verify_password(password, user.password_hash):
            return None
        return user
