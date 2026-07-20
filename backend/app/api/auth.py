import secrets
from typing import Annotated

from fastapi import APIRouter, Depends, Request, Response, status
from fastapi.responses import RedirectResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user
from app.core.config import OIDCProvider, get_settings
from app.core.errors import NotFoundError, Unauthenticated
from app.core.security import create_access_token
from app.db.database import get_db
from app.models import User
from app.schemas import LoginRequest, Token, UserCreate, UserOut
from app.services.auth_service import BasicAuthService, OIDCAuthService
from app.services.oidc_verifier import OIDCProviderConfig, OIDCVerifier

router = APIRouter(prefix="/api/auth", tags=["auth"])

_OIDC_STATE_COOKIE = "oidc_state"
_OIDC_NONCE_COOKIE = "oidc_nonce"


def get_auth_service(db: Annotated[AsyncSession, Depends(get_db)]) -> BasicAuthService:
    """Provide an auth service bound to the request's database session."""
    return BasicAuthService(db)


def _find_oidc_provider(slug: str) -> OIDCProvider:
    """Resolve a configured provider slug from `Settings.OIDC_PROVIDERS`, or 404."""
    for provider in get_settings().OIDC_PROVIDERS:
        if provider.slug == slug:
            return provider
    raise NotFoundError(f"Unknown OIDC provider: {slug}")


def _lookup_oidc_provider(slug: str) -> OIDCProviderConfig:
    """Build the verifier-facing config for a configured provider slug."""
    provider = _find_oidc_provider(slug)
    base_url = str(get_settings().PUBLIC_API_URL).rstrip("/")
    redirect_uri = f"{base_url}/api/auth/oidc/{slug}/callback"
    return OIDCProviderConfig(
        provider=provider.provider_value,
        issuer=str(provider.issuer),
        client_id=provider.client_id,
        client_secret=provider.client_secret,
        redirect_uri=redirect_uri,
        scopes=provider.scopes,
    )


def get_oidc_verifier(provider: str) -> OIDCVerifier:
    """Provide the token-verification adapter for a configured OIDC provider."""
    return OIDCVerifier(_lookup_oidc_provider(provider))


def get_oidc_auth_service(
    provider: str, db: Annotated[AsyncSession, Depends(get_db)]
) -> OIDCAuthService:
    """Provide an OIDC auth service bound to a configured provider and this request's db session."""
    config = _find_oidc_provider(provider)  # 404s before touching the db when unconfigured
    return OIDCAuthService(
        db, config.provider_value, trusted_email_linking=config.trusted_email_linking
    )


@router.post("/register", response_model=UserOut, status_code=status.HTTP_201_CREATED)
async def register(
    payload: UserCreate,
    auth_service: Annotated[BasicAuthService, Depends(get_auth_service)],
) -> User:
    """Register a new user and return the created account."""
    return await auth_service.register(payload.username, str(payload.email), payload.password)


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


@router.get("/oidc/{provider}/login")
async def oidc_login(verifier: Annotated[OIDCVerifier, Depends(get_oidc_verifier)]) -> Response:
    """Redirect to the IdP authorize URL, stashing state/nonce in short-lived cookies."""
    state = secrets.token_urlsafe(32)
    nonce = secrets.token_urlsafe(32)
    url = await verifier.authorize_redirect_url(state=state, nonce=nonce)
    response = RedirectResponse(url, status_code=status.HTTP_307_TEMPORARY_REDIRECT)
    response.set_cookie(_OIDC_STATE_COOKIE, state, httponly=True, max_age=600, samesite="lax")
    response.set_cookie(_OIDC_NONCE_COOKIE, nonce, httponly=True, max_age=600, samesite="lax")
    return response


@router.get("/oidc/{provider}/callback", response_model=Token)
async def oidc_callback(
    code: str,
    state: str,
    request: Request,
    verifier: Annotated[OIDCVerifier, Depends(get_oidc_verifier)],
    auth_service: Annotated[OIDCAuthService, Depends(get_oidc_auth_service)],
) -> Token:
    """Complete an OIDC login and return a bearer access token, same shape as `/login`."""
    expected_state = request.cookies.get(_OIDC_STATE_COOKIE)
    nonce = request.cookies.get(_OIDC_NONCE_COOKIE)
    if expected_state is None or nonce is None or state != expected_state:
        raise Unauthenticated("Invalid or expired OIDC login attempt")

    claims = await verifier.verify_callback(code=code, nonce=nonce)
    user = await auth_service.complete_login(claims)
    return Token(access_token=create_access_token(user.id))
