import asyncio
from dataclasses import dataclass
import secrets
import unicodedata
from uuid import uuid4

from sqlalchemy import func, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import ConflictError
from app.core.security import hash_password, verify_password
from app.models import Identity, LocalCredential, User
from app.services.slug import to_dns_label

GOOGLE_PROVIDER = "google"  # the identities.provider value of every kind-"google" provider


@dataclass(frozen=True, slots=True)
class OIDCClaims:
    """A normalized, already-verified external identity assertion."""

    provider: str
    subject: str
    email: str
    email_verified: bool
    preferred_username: str | None = None
    # The `name` claim: trimmed, non-blank, short enough to store and usable
    # as a Display Name (no "@", see `oidc_verifier._display_name`), or None.
    name: str | None = None
    hosted_domain: str | None = None  # Google's `hd` claim: the account's Workspace domain

    @property
    def email_is_authoritative(self) -> bool:
        """Return whether the provider vouches for who owns ``email`` right now.

        A verified email is not always proof of current ownership. Google
        only vouches for ``@gmail.com`` addresses and for addresses of a
        Workspace domain (an ``hd`` claim); for any other address it only
        checked ownership at some past point. Other providers are trusted
        on ``email_verified`` alone.
        """
        if not self.email_verified:
            return False
        if self.provider != GOOGLE_PROVIDER:
            return True
        return normalize_email(self.email).endswith("@gmail.com") or bool(self.hosted_domain)


class DuplicateUserError(ConflictError):
    """Raised when a username or email is already registered."""

    def __init__(self) -> None:
        """Set the default client-safe detail for a registration conflict."""
        super().__init__("Username or email already exists")


class OIDCAccountExistsError(ConflictError):
    """An external login's email belongs to an account it may not be linked to."""

    def __init__(self) -> None:
        """Set the client-safe detail; it never says which account matched."""
        super().__init__("An account with this email address already exists")


def normalize_email(email: str) -> str:
    """Canonicalize an email for storage, verified-email linking and exact-email lookup."""
    return email.strip().lower()


def _username_label(seed: str | None) -> str | None:
    """Return ``seed`` as a lowercase DNS-label username, or None when it cannot seed one.

    An email address never seeds a username (see
    `OIDCAuthService._available_username`), and accents are folded to plain
    letters first ("José Núñez" -> "jose-nunez"); a seed left with no ASCII
    letter or digit, such as a name written only in another script, is None.
    """
    if seed is None or "@" in seed:
        return None
    plain = unicodedata.normalize("NFKD", seed).encode("ascii", "ignore").decode("ascii")
    return to_dns_label(plain) if any(char.isalnum() for char in plain) else None


@dataclass(frozen=True, slots=True)
class NewUser:
    """Who a provisioned account is, apart from how it signs in."""

    username: str
    email: str  # as asserted; the users row stores it normalized
    display_name: str | None = None  # already trimmed, non-blank and at most 255 characters


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

    async def _touch_identity(
        self, provider: str, subject: str, email: str | None, *, email_authoritative: bool
    ) -> None:
        """Bump last_login_at, and record this sign-in's email and whether it was vouched for."""
        await self.db.execute(
            update(Identity)
            .where(Identity.provider == provider, Identity.subject == subject)
            .values(last_login_at=func.now(), email=email, email_authoritative=email_authoritative)
        )
        await self.db.commit()

    async def _name_or_email_taken(self, username: str, email: str) -> bool:
        """Return whether the username or normalized email is already registered."""
        existing = await self.db.scalar(
            select(User.id).where(
                or_(User.username == username, User.email == normalize_email(email))
            )
        )
        return existing is not None

    async def _provision(
        self,
        new_user: NewUser,
        *,
        provider: str,
        password_hash: str | None,
        subject: str | None = None,
        email_authoritative: bool = False,
    ) -> User:
        """Atomically create user + identity (+ local_credentials), never a workspace.

        The user id is minted up front so a ``local`` identity's subject is
        exactly ``str(user.id)`` regardless of what the caller passes;
        non-local providers must supply their own verified ``subject``, and
        say whether they vouched for ``new_user.email`` (see
        `OIDCAuthService._may_link`).
        """
        if await self._name_or_email_taken(new_user.username, new_user.email):
            raise DuplicateUserError
        user_id = uuid4()
        identity_subject = str(user_id) if provider == "local" else subject
        if identity_subject is None:
            raise ValueError("subject is required for non-local providers")
        user = User(
            id=user_id,
            username=new_user.username,
            email=normalize_email(new_user.email),
            display_name=new_user.display_name,
        )
        self.db.add_all(
            [
                user,
                Identity(
                    user_id=user_id,
                    provider=provider,
                    subject=identity_subject,
                    email=new_user.email,
                    email_authoritative=email_authoritative,
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

    async def register(
        self, username: str, email: str, password: str, *, display_name: str | None = None
    ) -> User:
        """Create a new local user, raising ``DuplicateUserError`` on conflict.

        ``display_name`` must already be trimmed, non-blank and at most 255
        characters (`UserCreate` guarantees it), or None.
        """
        return await self._provision(
            NewUser(username=username, email=email, display_name=display_name),
            provider="local",
            # bcrypt is deliberately CPU-expensive (~250ms per hash) and would
            # otherwise stall the event loop, and with it every proxied notebook
            # connection and health probe, for the whole duration. It releases
            # the GIL, so a worker thread also lets hashes run in parallel.
            password_hash=await asyncio.to_thread(hash_password, password),
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
        if not await asyncio.to_thread(verify_password, password, password_hash):
            return None
        await self._touch_identity("local", str(user.id), user.email, email_authoritative=False)
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
        """Resolve, link, or JIT-provision the user for a verified OIDC assertion.

        Raises ``OIDCAccountExistsError`` when the asserted email already
        belongs to an account this identity may not be linked to (see
        `_may_link`), rather than silently provisioning a second account
        that could never own that email.

        A provisioned account takes its display name from the ``name``
        claim; an existing one only gets it while it has none (see
        `_fill_display_name`).
        """
        user = await self._resolve_identity(claims.provider, claims.subject)
        if user is not None:
            await self._fill_display_name(user, claims.name)
            await self._touch_identity(
                claims.provider,
                claims.subject,
                claims.email,
                email_authoritative=claims.email_is_authoritative,
            )
            return user
        existing = await self.db.scalar(
            select(User).where(User.email == normalize_email(claims.email))
        )
        if existing is not None:
            if not await self._may_link(existing, claims):
                raise OIDCAccountExistsError
            return await self._link_identity(existing, claims)
        try:
            return await self._provision(
                NewUser(
                    username=await self._available_username(claims),
                    email=claims.email,
                    display_name=claims.name,
                ),
                provider=claims.provider,
                subject=claims.subject,
                password_hash=None,
                email_authoritative=claims.email_is_authoritative,
            )
        except DuplicateUserError:
            # A concurrent first login of this same identity may have won the
            # insert race; that account is the right answer, not a conflict.
            winner = await self._resolve_identity(claims.provider, claims.subject)
            if winner is None:
                raise
            return winner

    async def _may_link(self, user: User, claims: OIDCClaims) -> bool:
        """Return whether a new identity may join the existing account owning its email.

        All must hold, or whoever first claimed the address keeps access to
        the account its real owner is linked into (pre-account hijacking):

        1. The provider is configured for trusted linking.
        2. The provider vouches for the email's current owner
           (`OIDCClaims.email_is_authoritative`).
        3. The account has no local password: local registration never
           verifies an email, so anyone could have pre-registered this address
           with a password of their own.
        4. Every identity on the account was vouched for as this email's owner
           at its latest sign-in (`Identity.email_authoritative`). An account
           first claimed through an unverified email, or through a Google
           account holding a third-party address, was never proven to be the
           address owner's.
        5. The account has no identity from this provider yet. A second
           account at the same provider asserting the same email (a Google
           consumer and a Workspace account can share one, and a recycled
           address belongs to a new account) need not be the same person.
        """
        if not (self.trusted_email_linking and claims.email_is_authoritative):
            return False
        credential = await self.db.scalar(
            select(LocalCredential.user_id).where(LocalCredential.user_id == user.id)
        )
        if credential is not None:
            return False
        identities = (
            await self.db.scalars(select(Identity).where(Identity.user_id == user.id))
        ).all()
        return bool(identities) and all(
            identity.provider != claims.provider
            and identity.email_authoritative
            and normalize_email(identity.email or "") == user.email
            for identity in identities
        )

    async def _link_identity(self, user: User, claims: OIDCClaims) -> User:
        """Attach the new (provider, subject) identity to ``user``."""
        # Before the identity is added: executing the update autoflushes
        # pending rows, and the identity's insert must fail inside the
        # IntegrityError guard below, not here.
        await self._fill_display_name(user, claims.name)
        self.db.add(
            Identity(
                user_id=user.id,
                provider=claims.provider,
                subject=claims.subject,
                email=claims.email,
                email_authoritative=claims.email_is_authoritative,
                last_login_at=func.now(),
            )
        )
        try:
            await self.db.commit()
        except IntegrityError as exc:
            await self.db.rollback()
            raise DuplicateUserError from exc
        return user

    async def _fill_display_name(self, user: User, name: str | None) -> None:
        """Give ``user`` the provider's ``name`` if the account has no display name yet.

        Never overwrites one already set, even by a concurrent sign-in: the
        update itself only matches a row whose display name is still NULL.
        Left uncommitted, to land with the sign-in's own commit.
        """
        if name is None or user.display_name is not None:
            return
        await self.db.execute(
            update(User)
            .where(User.id == user.id, User.display_name.is_(None))
            .values(display_name=name)
        )

    async def _available_username(self, claims: OIDCClaims) -> str:
        """Seed from preferred_username, else the name, else a random handle; suffix until free.

        Never from the email: the person search shows a username beside a
        hint holding the email's domain (docs/adr/0004), so a username that
        is the local part would give the whole address away. Google sends
        no preferred_username, and one that is an address is the email too.
        """
        base = next(
            (
                label
                for seed in (claims.preferred_username, claims.name)
                if (label := _username_label(seed)) is not None
            ),
            f"user-{secrets.token_hex(4)}",
        )
        candidate = base
        suffix = 2
        while await self.db.scalar(select(User.id).where(User.username == candidate)) is not None:
            candidate = f"{base}-{suffix}"
            suffix += 1
        return candidate
