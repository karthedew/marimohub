"""Authentication routes: local accounts, and sign-in through external OIDC providers.

An OIDC sign-in is a browser redirect round trip that ends in a handoff, so
the bearer access token never travels in a URL:

1. The SPA keeps a random PKCE ``verifier`` in sessionStorage and navigates
   to ``GET /api/auth/oidc/{slug}/login?challenge=S256(verifier)``.
2. ``oidc_login`` redirects to the provider and sets one signed login cookie
   holding that attempt's state, nonce, the backend's own PKCE verifier and
   the SPA's challenge.
3. The provider returns to ``GET /api/auth/oidc/{slug}/callback``, which
   checks the cookie, verifies the login and redirects to
   ``{PUBLIC_APP_URL}/auth/callback#handoff=H`` (or to
   ``{PUBLIC_APP_URL}/auth/login?error=CODE``).
4. The SPA posts ``{handoff, verifier}`` to ``/api/auth/oidc/exchange``. The
   handoff only counts together with the verifier whose challenge it is
   bound to, which never left the tab that started the sign-in.
"""

from dataclasses import dataclass, fields
from datetime import timedelta
from enum import StrEnum
import logging
import re
from typing import Annotated, Self
from urllib.parse import urlencode
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request, Response, status
from fastapi.responses import RedirectResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user
from app.core.config import OIDCProvider, Settings, get_settings
from app.core.errors import DomainError, NotFoundError, Unauthenticated
from app.core.security import (
    TokenPurpose,
    constant_time_equals,
    create_access_token,
    create_purpose_token,
    decode_purpose_token,
    pkce_s256_challenge,
)
from app.db.database import get_db
from app.models import User
from app.schemas import (
    LoginRequest,
    OIDCCallbackParams,
    OIDCExchangeOut,
    OIDCExchangeRequest,
    OIDCProviderOut,
    Token,
    UserCreate,
    UserOut,
)
from app.services.auth_service import (
    BasicAuthService,
    OIDCAccountExistsError,
    OIDCAuthService,
)
from app.services.oidc_verifier import (
    OIDCDomainNotAllowedError,
    OIDCHttpOptions,
    OIDCProviderConfig,
    OIDCProviderError,
    OIDCVerifier,
)

router = APIRouter(prefix="/api/auth", tags=["auth"])
logger = logging.getLogger(__name__)

_LOGIN_COOKIE_TTL = timedelta(minutes=10)
_HANDOFF_TTL = timedelta(seconds=60)
# base64url without padding of a SHA-256 digest: exactly 43 characters.
_CHALLENGE_RE = re.compile(r"[A-Za-z0-9_-]{43}")
_INVALID_HANDOFF = "Invalid or expired sign-in handoff"


class OIDCErrorCode(StrEnum):
    """The ``?error=`` an OIDC sign-in can end with on the SPA's login page."""

    DENIED = "oidc_denied"  # the user declined at the provider
    EXPIRED = "oidc_expired"  # no valid login cookie, or state/provider mismatch
    FAILED = "oidc_failed"  # the provider, the token exchange or verification failed
    ACCOUNT_EXISTS = "oidc_account_exists"  # the email's account may not be linked
    DOMAIN_NOT_ALLOWED = "oidc_domain_not_allowed"  # outside the hosted domain
    INVALID_REQUEST = "oidc_invalid_request"  # the SPA sent no valid challenge


SettingsDep = Annotated[Settings, Depends(get_settings)]
DbDep = Annotated[AsyncSession, Depends(get_db)]


def get_auth_service(db: DbDep) -> BasicAuthService:
    """Provide an auth service bound to the request's database session."""
    return BasicAuthService(db)


def get_oidc_http_options(settings: SettingsDep) -> OIDCHttpOptions:
    """Provide how the OIDC verifier reaches identity providers.

    The one seam tests override to put a fake provider behind the real verifier.
    """
    proxy = settings.OIDC_HTTP_PROXY_URL
    return OIDCHttpOptions(proxy=str(proxy) if proxy is not None else None)


OIDCHttpDep = Annotated[OIDCHttpOptions, Depends(get_oidc_http_options)]


def oidc_provider_config(settings: Settings, provider: OIDCProvider) -> OIDCProviderConfig:
    """Build the verifier-facing config of a configured provider.

    The redirect URI is derived from ``PUBLIC_API_URL``, never configured.
    """
    return OIDCProviderConfig(
        provider=provider.provider_value,
        kind=provider.kind,
        issuer=provider.issuer,
        client_id=provider.client_id,
        client_secret=provider.client_secret,
        redirect_uri=f"{settings.public_api_base_url}/api/auth/oidc/{provider.slug}/callback",
        scopes=tuple(provider.scopes),
        hosted_domain=provider.hosted_domain,
        prompt=provider.prompt,
    )


def _configured_provider(settings: Settings, slug: str) -> OIDCProvider | None:
    return next((p for p in settings.OIDC_PROVIDERS if p.slug == slug), None)


@dataclass(frozen=True, slots=True)
class _LoginCookie:
    """Where the login cookie lives: ``__Host-`` and Secure on https, path-scoped on http."""

    name: str
    path: str
    secure: bool

    @classmethod
    def for_settings(cls, settings: Settings) -> Self:
        if settings.PUBLIC_API_URL.scheme == "https":
            return cls(name="__Host-marimohub_oidc", path="/", secure=True)
        return cls(name="marimohub_oidc", path="/api/auth/oidc/", secure=False)

    def attach(self, response: Response, value: str) -> None:
        response.set_cookie(
            self.name,
            value,
            max_age=int(_LOGIN_COOKIE_TTL.total_seconds()),
            path=self.path,
            secure=self.secure,
            httponly=True,
            samesite="lax",
        )

    def delete(self, response: Response) -> None:
        response.delete_cookie(
            self.name, path=self.path, secure=self.secure, httponly=True, samesite="lax"
        )


@dataclass(frozen=True, slots=True)
class _LoginAttempt:
    """What the login cookie carries from the login redirect to the callback."""

    provider: str  # the slug the sign-in started with
    state: str
    nonce: str
    code_verifier: str  # the backend's own PKCE verifier for the provider
    challenge: str  # the SPA's PKCE challenge, bound into the handoff

    def seal(self, secret_key: str) -> str:
        return create_purpose_token(
            secret_key,
            TokenPurpose.OIDC_LOGIN_COOKIE,
            {
                "provider": self.provider,
                "state": self.state,
                "nonce": self.nonce,
                "code_verifier": self.code_verifier,
                "challenge": self.challenge,
            },
            _LOGIN_COOKIE_TTL,
        )

    @classmethod
    def unseal(cls, secret_key: str, token: str | None) -> Self | None:
        """Return the attempt in a valid, unexpired login cookie, else ``None``."""
        if token is None:
            return None
        claims = decode_purpose_token(secret_key, TokenPurpose.OIDC_LOGIN_COOKIE, token)
        if claims is None:
            return None
        values = [claims.get(field.name) for field in fields(cls)]
        strings = [value for value in values if isinstance(value, str)]
        return cls(*strings) if len(strings) == len(values) else None


def _create_handoff(secret_key: str, user_id: UUID, challenge: str) -> str:
    return create_purpose_token(
        secret_key,
        TokenPurpose.OIDC_HANDOFF,
        {"sub": str(user_id), "challenge": challenge},
        _HANDOFF_TTL,
    )


def _redeem_handoff(secret_key: str, handoff: str, verifier: str) -> UUID | None:
    """Return the user a handoff signs in, if it is valid and ``verifier`` matches it."""
    claims = decode_purpose_token(secret_key, TokenPurpose.OIDC_HANDOFF, handoff) or {}
    subject, challenge = claims.get("sub"), claims.get("challenge")
    if not isinstance(subject, str) or not isinstance(challenge, str):
        return None
    try:
        verified = constant_time_equals(pkce_s256_challenge(verifier), challenge)
        user_id = UUID(subject)
    except ValueError:  # a verifier with no UTF-8 encoding, or a subject that is no UUID
        return None
    return user_id if verified else None


def _see_other(url: str) -> RedirectResponse:
    """Redirect the browser, keeping this response out of caches and referrers."""
    response = RedirectResponse(url, status_code=status.HTTP_303_SEE_OTHER)
    response.headers["Cache-Control"] = "no-store"
    response.headers["Referrer-Policy"] = "no-referrer"
    return response


def _login_error_url(settings: Settings, code: OIDCErrorCode) -> str:
    return f"{settings.public_app_base_url}/auth/login?{urlencode({'error': code.value})}"


def _error_code_for(exc: DomainError) -> OIDCErrorCode:
    if isinstance(exc, OIDCDomainNotAllowedError):
        return OIDCErrorCode.DOMAIN_NOT_ALLOWED
    if isinstance(exc, OIDCAccountExistsError):
        return OIDCErrorCode.ACCOUNT_EXISTS
    return OIDCErrorCode.FAILED


async def _sign_in(
    provider: OIDCProvider,
    verifier: OIDCVerifier,
    params: OIDCCallbackParams,
    attempt: _LoginAttempt,
    db: AsyncSession,
) -> User | OIDCErrorCode:
    """Verify the provider's answer and resolve, link or provision its user."""
    if params.error is not None:
        # Attacker-reachable input: logged as a bounded repr, never raw.
        logger.info("OIDC provider %s returned error %r", provider.slug, params.error[:64])
        return OIDCErrorCode.DENIED if params.error == "access_denied" else OIDCErrorCode.FAILED
    if not params.code:
        return OIDCErrorCode.FAILED
    try:
        claims = await verifier.verify_callback(
            code=params.code,
            iss=params.iss,
            nonce=attempt.nonce,
            code_verifier=attempt.code_verifier,
        )
        service = OIDCAuthService(
            db, provider.provider_value, trusted_email_linking=provider.trusted_email_linking
        )
        return await service.complete_login(claims)
    except DomainError as exc:
        logger.warning("OIDC sign-in with %s failed: %s", provider.slug, exc)
        return _error_code_for(exc)


async def _finish_login(
    slug: str,
    params: OIDCCallbackParams,
    attempt: _LoginAttempt | None,
    settings: Settings,
    db: AsyncSession,
    http: OIDCHttpOptions,
) -> str:
    """Return where the callback sends the browser: the SPA's callback or login page."""
    # The cookie binds this response to the browser and the provider that
    # started the sign-in (CSRF and mix-up defense); state is single-use
    # because the callback always deletes the cookie.
    if (
        attempt is None
        or attempt.provider != slug
        or not constant_time_equals(params.state or "", attempt.state)
    ):
        return _login_error_url(settings, OIDCErrorCode.EXPIRED)
    provider = _configured_provider(settings, slug)
    if provider is None:
        return _login_error_url(settings, OIDCErrorCode.FAILED)
    verifier = OIDCVerifier(oidc_provider_config(settings, provider), http)
    outcome = await _sign_in(provider, verifier, params, attempt, db)
    if isinstance(outcome, OIDCErrorCode):
        return _login_error_url(settings, outcome)
    handoff = _create_handoff(settings.SECRET_KEY, outcome.id, attempt.challenge)
    return f"{settings.public_app_base_url}/auth/callback#{urlencode({'handoff': handoff})}"


@router.post("/register", response_model=UserOut, status_code=status.HTTP_201_CREATED)
async def register(
    payload: UserCreate,
    auth_service: Annotated[BasicAuthService, Depends(get_auth_service)],
) -> User:
    """Register a new user and return the created account."""
    return await auth_service.register(
        payload.username,
        str(payload.email),
        payload.password,
        display_name=payload.display_name,
    )


@router.post("/login", response_model=Token)
async def login(
    payload: LoginRequest,
    auth_service: Annotated[BasicAuthService, Depends(get_auth_service)],
) -> Token:
    """Authenticate a user and return a bearer access token."""
    user = await auth_service.authenticate(payload.username, payload.password)
    if user is None:
        raise Unauthenticated("Invalid username or password")

    return Token(access_token=create_access_token(user.id))


@router.post(
    "/logout",
    status_code=status.HTTP_204_NO_CONTENT,
    response_class=Response,
    summary="End the current bearer-token session",
)
async def logout(_: Annotated[User, Depends(get_current_user)]) -> Response:
    """End the current bearer-token session.

    JWT bearer tokens are stateless; logout only validates the current token
    and returns no content.
    """
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get(
    "/providers",
    response_model=list[OIDCProviderOut],
    summary="List the external identity providers users can sign in with",
)
async def list_identity_providers(settings: SettingsDep) -> list[OIDCProviderOut]:
    """Return every configured provider's slug, display name and kind; never its secrets."""
    return [
        OIDCProviderOut(slug=provider.slug, display_name=provider.display_name, kind=provider.kind)
        for provider in settings.OIDC_PROVIDERS
    ]


@router.get(
    "/oidc/{provider}/login",
    status_code=status.HTTP_303_SEE_OTHER,
    response_class=RedirectResponse,
    summary="Start signing in with an external identity provider",
)
async def oidc_login(
    provider: str,
    settings: SettingsDep,
    http: OIDCHttpDep,
    challenge: str | None = None,
) -> Response:
    """Redirect to the provider's authorize URL, remembering the attempt in a signed cookie.

    ``challenge`` is the SPA's PKCE challenge; the handoff the callback mints
    is bound to it. Unknown providers are a 404; every other failure lands on
    the SPA's login page with an ``error`` code.
    """
    config = _configured_provider(settings, provider)
    if config is None:
        raise NotFoundError("Unknown identity provider")
    if challenge is None or not _CHALLENGE_RE.fullmatch(challenge):
        return _see_other(_login_error_url(settings, OIDCErrorCode.INVALID_REQUEST))
    verifier = OIDCVerifier(oidc_provider_config(settings, config), http)
    try:
        authorization = await verifier.authorization_request()
    except OIDCProviderError as exc:
        logger.warning("OIDC sign-in with %s could not start: %s", provider, exc)
        return _see_other(_login_error_url(settings, OIDCErrorCode.FAILED))
    attempt = _LoginAttempt(
        provider=provider,
        state=authorization.state,
        nonce=authorization.nonce,
        code_verifier=authorization.code_verifier,
        challenge=challenge,
    )
    response = _see_other(authorization.url)
    _LoginCookie.for_settings(settings).attach(response, attempt.seal(settings.SECRET_KEY))
    return response


@router.get(
    "/oidc/{provider}/callback",
    status_code=status.HTTP_303_SEE_OTHER,
    response_class=RedirectResponse,
    summary="Finish signing in with an external identity provider",
)
async def oidc_callback(
    provider: str,
    params: Annotated[OIDCCallbackParams, Query()],
    request: Request,
    settings: SettingsDep,
    db: DbDep,
    http: OIDCHttpDep,
) -> Response:
    """Verify the provider's answer and hand the sign-in back to the SPA.

    Always a 303 that deletes the login cookie: to
    ``{PUBLIC_APP_URL}/auth/callback#handoff=H`` on success, else to
    ``{PUBLIC_APP_URL}/auth/login?error=CODE`` (see `OIDCErrorCode`).
    """
    cookie = _LoginCookie.for_settings(settings)
    attempt = _LoginAttempt.unseal(settings.SECRET_KEY, request.cookies.get(cookie.name))
    try:
        target = await _finish_login(provider, params, attempt, settings, db, http)
    except Exception:
        # A top-level browser navigation: even an unexpected failure (e.g.
        # the database) must land on the SPA, not on a raw JSON error page.
        logger.exception("OIDC callback for %s failed unexpectedly", provider)
        target = _login_error_url(settings, OIDCErrorCode.FAILED)
    response = _see_other(target)
    cookie.delete(response)
    return response


@router.post(
    "/oidc/exchange",
    response_model=OIDCExchangeOut,
    summary="Trade an OIDC sign-in handoff for a bearer access token",
)
async def oidc_exchange(
    payload: OIDCExchangeRequest, settings: SettingsDep, db: DbDep, response: Response
) -> OIDCExchangeOut:
    """Return an access token and the user for a handoff plus its PKCE verifier.

    401 when the handoff is invalid or expired (60 s), or when
    base64url(SHA-256(verifier)) is not the challenge it is bound to.
    """
    user_id = _redeem_handoff(settings.SECRET_KEY, payload.handoff, payload.verifier)
    user = await db.get(User, user_id) if user_id is not None else None
    if user is None:
        raise Unauthenticated(_INVALID_HANDOFF)
    response.headers["Cache-Control"] = "no-store"  # carries a token (RFC 6749 §5.1)
    return OIDCExchangeOut(
        access_token=create_access_token(user.id), user=UserOut.model_validate(user)
    )
