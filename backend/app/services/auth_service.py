from dataclasses import dataclass
from uuid import uuid4

from sqlalchemy import func, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import ConflictError
from app.core.security import hash_password, verify_password
from app.models import Identity, LocalCredential, User
from app.services.slug import to_dns_label


@dataclass(frozen=True, slots=True)
class OIDCClaims:
    """A normalized, already-verified external identity assertion."""

    provider: str
    subject: str
    email: str
    email_verified: bool
    preferred_username: str | None = None


class DuplicateUserError(ConflictError):
    """Raised when a username or email is already registered."""

    def __init__(self) -> None:
        """Set the default client-safe detail for a registration conflict."""
        super().__init__("Username or email already exists")


def _normalize_email(email: str) -> str:
    """Canonicalize an email for storage and verified-email matching."""
    return email.strip().lower()


class AuthService:
    """Shared provisioning + resolution kernel every provider builds on.

    Not instantiated directly: each provider subclass adds its own credential
    verification entry point. Those entry points differ too much in shape to
    share one signature (local verifies a password and never auto-provisions;
    OIDC resolves-or-JIT-provisions a verified assertion), so the polymorphism
    lives in the provisioning kernel below, not in a forced ``authenticate``
    method.
    """

    def __init__(self, db: AsyncSession) -> None:
        """Bind the service to an active database session."""
        self.db = db

    async def _resolve_identity(self, provider: str, subject: str) -> User | None:
        """Return the user owning identity (provider, subject), or None."""
        return await self.db.scalar(
            select(User)
            .join(Identity, Identity.user_id == User.id)
            .where(Identity.provider == provider, Identity.subject == subject)
        )

    async def _touch_identity(self, provider: str, subject: str, email: str | None) -> None:
        """Bump last_login_at (and provider-asserted email) on an existing identity."""
        await self.db.execute(
            update(Identity)
            .where(Identity.provider == provider, Identity.subject == subject)
            .values(last_login_at=func.now(), email=email)
        )
        await self.db.commit()

    async def _name_or_email_taken(self, username: str, email: str) -> bool:
        """Return whether the username or normalized email is already registered."""
        existing = await self.db.scalar(
            select(User.id).where(
                or_(User.username == username, User.email == _normalize_email(email))
            )
        )
        return existing is not None

    async def _provision(
        self,
        *,
        username: str,
        email: str,
        provider: str,
        password_hash: str | None,
        subject: str | None = None,
    ) -> User:
        """Atomically create user + identity (+ local_credentials), never a workspace.

        The user id is minted up front so a ``local`` identity's subject is
        exactly ``str(user.id)`` regardless of what the caller passes;
        non-local providers must supply their own verified ``subject``.
        """
        if await self._name_or_email_taken(username, email):
            raise DuplicateUserError
        user_id = uuid4()
        identity_subject = str(user_id) if provider == "local" else subject
        if identity_subject is None:
            raise ValueError("subject is required for non-local providers")
        user = User(id=user_id, username=username, email=_normalize_email(email))
        self.db.add_all(
            [
                user,
                Identity(
                    user_id=user_id,
                    provider=provider,
                    subject=identity_subject,
                    email=email,
                    last_login_at=func.now(),
                ),
            ]
        )
        if password_hash is not None:
            self.db.add(LocalCredential(user_id=user_id, password_hash=password_hash))
        try:
            await self.db.commit()
        except IntegrityError as exc:  # race backstop: username/email/subject/local guard
            await self.db.rollback()
            raise DuplicateUserError from exc
        await self.db.refresh(user)
        return user


class BasicAuthService(AuthService):
    """Username/password auth backed by ``local_credentials`` + ``identities``."""

    async def register(self, username: str, email: str, password: str) -> User:
        """Create a new local user, raising ``DuplicateUserError`` on conflict."""
        return await self._provision(
            username=username,
            email=email,
            provider="local",
            password_hash=hash_password(password),
        )

    async def authenticate(self, username: str, password: str) -> User | None:
        """Return the matching user for valid local credentials, else None."""
        row = (
            await self.db.execute(
                select(User, LocalCredential.password_hash)
                .join(LocalCredential, LocalCredential.user_id == User.id)
                .where(User.username == username)
            )
        ).first()
        if row is None:  # unknown username OR an SSO-only user (no local_credentials)
            return None
        user, password_hash = row
        if not verify_password(password, password_hash):
            return None
        await self._touch_identity("local", str(user.id), user.email)
        return user


class OIDCAuthService(AuthService):
    """External OIDC auth: resolve an existing identity, link, or JIT-provision."""

    def __init__(
        self, db: AsyncSession, provider: str, *, trusted_email_linking: bool = False
    ) -> None:
        """Bind the service to one configured provider and its trusted-linking policy."""
        super().__init__(db)
        self.provider = provider
        self.trusted_email_linking = trusted_email_linking

    async def complete_login(self, claims: OIDCClaims) -> User:
        """Resolve, link, or JIT-provision the user for a verified OIDC assertion."""
        user = await self._resolve_identity(claims.provider, claims.subject)
        if user is not None:
            await self._touch_identity(claims.provider, claims.subject, claims.email)
            return user
        if self.trusted_email_linking and claims.email_verified:
            linked = await self._link_by_verified_email(claims)
            if linked is not None:
                return linked
        return await self._provision(
            username=await self._available_username(claims),
            email=claims.email,
            provider=claims.provider,
            subject=claims.subject,
            password_hash=None,
        )

    async def _link_by_verified_email(self, claims: OIDCClaims) -> User | None:
        """Attach a new identity to the user whose canonical email matches, if any.

        Only reached when the provider is configured for trusted, verified-email
        linking and the claim itself asserts ``email_verified`` — an untrusted or
        unverified match would let anyone claim an existing account by simply
        asserting its email address.
        """
        normalized_email = _normalize_email(claims.email)
        user = await self.db.scalar(select(User).where(User.email == normalized_email))
        if user is None:
            return None
        self.db.add(
            Identity(
                user_id=user.id,
                provider=claims.provider,
                subject=claims.subject,
                email=claims.email,
                last_login_at=func.now(),
            )
        )
        try:
            await self.db.commit()
        except IntegrityError as exc:
            await self.db.rollback()
            raise DuplicateUserError from exc
        return user

    async def _available_username(self, claims: OIDCClaims) -> str:
        """Seed from preferred_username / email local-part, suffixed until free."""
        base = to_dns_label(claims.preferred_username or claims.email.split("@", 1)[0])
        candidate = base
        suffix = 2
        while await self.db.scalar(select(User.id).where(User.username == candidate)) is not None:
            candidate = f"{base}-{suffix}"
            suffix += 1
        return candidate
