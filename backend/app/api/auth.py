from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user
from app.core.security import create_access_token
from app.db.database import get_db
from app.models import User
from app.schemas import LoginRequest, Token, UserCreate, UserOut
from app.services.auth_service import AuthService, BasicAuthService, DuplicateUserError

router = APIRouter(prefix="/api/auth", tags=["auth"])


def get_auth_service(db: Annotated[AsyncSession, Depends(get_db)]) -> AuthService:
    """Provide an auth service bound to the request's database session."""
    return BasicAuthService(db)


@router.post("/register", response_model=UserOut, status_code=status.HTTP_201_CREATED)
async def register(
    payload: UserCreate,
    auth_service: Annotated[AuthService, Depends(get_auth_service)],
) -> User:
    """Register a new user and return the created account."""
    try:
        return await auth_service.register(payload.username, str(payload.email), payload.password)
    except DuplicateUserError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="Username or email already exists"
        ) from exc


@router.post("/login", response_model=Token)
async def login(
    payload: LoginRequest,
    auth_service: Annotated[AuthService, Depends(get_auth_service)],
) -> Token:
    """Authenticate a user and return a bearer access token."""
    user = await auth_service.authenticate(payload.username, payload.password)
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid username or password"
        )

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
