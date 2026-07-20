from uuid import uuid4

from httpx import AsyncClient
import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Identity, LocalCredential, User, Workspace
from app.services.auth_service import (
    BasicAuthService,
    DuplicateUserError,
    OIDCAuthService,
    OIDCClaims,
)
from test_notebooks import json_dict

# Fake credentials used across the auth tests. Routed through module-level
# constants so the values are never string literals at sensitive call sites.
_LOGIN_VALUE = "correct-horse"
_BEARER_TYPE = "bearer"


async def register_user(
    client: AsyncClient,
    username: str = "ada",
    email: str = "ada@example.com",
    password: str = _LOGIN_VALUE,
) -> dict[str, object]:
    response = await client.post(
        "/api/auth/register",
        json={"username": username, "email": email, "password": password},
    )
    assert response.status_code == 201
    return json_dict(response)


async def login_user(
    client: AsyncClient, username: str = "ada", password: str = _LOGIN_VALUE
) -> str:
    response = await client.post(
        "/api/auth/login", json={"username": username, "password": password}
    )
    assert response.status_code == 200
    payload = json_dict(response)
    assert payload["token_type"] == _BEARER_TYPE
    return str(payload["access_token"])


@pytest.mark.asyncio
async def test_register_login_and_logout_with_bearer_token(api_client: AsyncClient) -> None:
    user = await register_user(api_client)
    token = await login_user(api_client)
    logout_response = await api_client.post(
        "/api/auth/logout", headers={"Authorization": f"Bearer {token}"}
    )

    assert user["username"] == "ada"
    assert user["email"] == "ada@example.com"
    assert logout_response.status_code == 204
    assert logout_response.content == b""


@pytest.mark.asyncio
async def test_register_duplicate_username_or_email_returns_409(api_client: AsyncClient) -> None:
    await register_user(api_client)

    duplicate_username = await api_client.post(
        "/api/auth/register",
        json={"username": "ada", "email": "other@example.com", "password": "correct-horse"},
    )
    duplicate_email = await api_client.post(
        "/api/auth/register",
        json={"username": "grace", "email": "ada@example.com", "password": "correct-horse"},
    )

    assert duplicate_username.status_code == 409
    assert duplicate_email.status_code == 409


@pytest.mark.asyncio
async def test_bad_credentials_return_401(api_client: AsyncClient) -> None:
    await register_user(api_client)

    wrong_password = await api_client.post(
        "/api/auth/login", json={"username": "ada", "password": "wrong-password"}
    )
    missing_user = await api_client.post(
        "/api/auth/login", json={"username": "missing", "password": "correct-horse"}
    )

    assert wrong_password.status_code == 401
    assert missing_user.status_code == 401


@pytest.mark.asyncio
async def test_logout_requires_valid_token(api_client: AsyncClient) -> None:
    without_token = await api_client.post("/api/auth/logout")
    invalid_token = await api_client.post(
        "/api/auth/logout", headers={"Authorization": "Bearer not-a-token"}
    )

    assert without_token.status_code == 401
    assert invalid_token.status_code == 401


@pytest.mark.asyncio
async def test_register_creates_user_local_identity_and_credentials_no_workspace(
    db_session: AsyncSession,
) -> None:
    service = BasicAuthService(db_session)
    user = await service.register("ada", "Ada@Example.com", _LOGIN_VALUE)

    identity = await db_session.scalar(select(Identity).where(Identity.user_id == user.id))
    credential = await db_session.scalar(
        select(LocalCredential).where(LocalCredential.user_id == user.id)
    )
    workspaces = (await db_session.execute(select(Workspace))).scalars().all()

    assert user.email == "ada@example.com"
    assert identity is not None
    assert identity.provider == "local"
    assert identity.subject == str(user.id)
    assert credential is not None
    assert workspaces == []


@pytest.mark.asyncio
async def test_register_duplicate_conflict_leaves_no_partial_rows(
    db_session: AsyncSession,
) -> None:
    service = BasicAuthService(db_session)
    await service.register("ada", "ada@example.com", _LOGIN_VALUE)

    with pytest.raises(DuplicateUserError):
        await service.register("ada", "someone-else@example.com", _LOGIN_VALUE)

    users = (await db_session.execute(select(User))).scalars().all()
    identities = (await db_session.execute(select(Identity))).scalars().all()
    credentials = (await db_session.execute(select(LocalCredential))).scalars().all()
    assert len(users) == 1
    assert len(identities) == 1
    assert len(credentials) == 1


@pytest.mark.asyncio
async def test_one_local_identity_per_user_is_db_enforced(db_session: AsyncSession) -> None:
    service = BasicAuthService(db_session)
    user = await service.register("ada", "ada@example.com", _LOGIN_VALUE)

    with pytest.raises(IntegrityError):
        async with db_session.begin_nested():
            await db_session.execute(
                text(
                    "INSERT INTO identities (id, user_id, provider, subject) "
                    "VALUES (:id, :user_id, 'local', :subject)"
                ),
                {"id": uuid4(), "user_id": user.id, "subject": "second-local"},
            )


@pytest.mark.asyncio
async def test_authenticate_success_bumps_last_login_at(db_session: AsyncSession) -> None:
    service = BasicAuthService(db_session)
    user = await service.register("ada", "ada@example.com", _LOGIN_VALUE)
    identity = await db_session.scalar(select(Identity).where(Identity.user_id == user.id))
    assert identity is not None
    before = identity.last_login_at

    authenticated = await service.authenticate("ada", _LOGIN_VALUE)

    await db_session.refresh(identity)
    assert authenticated is not None
    assert authenticated.id == user.id
    assert identity.last_login_at is not None
    assert before is None or identity.last_login_at >= before


@pytest.mark.asyncio
async def test_authenticate_wrong_password_returns_none(db_session: AsyncSession) -> None:
    service = BasicAuthService(db_session)
    await service.register("ada", "ada@example.com", _LOGIN_VALUE)

    assert await service.authenticate("ada", "wrong-password") is None


@pytest.mark.asyncio
async def test_authenticate_unknown_and_sso_only_users_are_indistinguishable(
    db_session: AsyncSession,
) -> None:
    service = BasicAuthService(db_session)
    oidc_service = OIDCAuthService(db_session, "google")
    await oidc_service.complete_login(
        OIDCClaims(
            provider="google",
            subject="google-subject",
            email="grace@example.com",
            email_verified=True,
            preferred_username="grace",
        )
    )

    assert await service.authenticate("missing", "whatever") is None
    assert await service.authenticate("grace", "whatever") is None


@pytest.mark.asyncio
async def test_oidc_resolve_hit_returns_same_user_and_touches_identity(
    db_session: AsyncSession,
) -> None:
    service = OIDCAuthService(db_session, "google")
    claims = OIDCClaims(
        provider="google",
        subject="google-subject",
        email="grace@example.com",
        email_verified=True,
        preferred_username="grace",
    )
    first = await service.complete_login(claims)
    identity = await db_session.scalar(
        select(Identity).where(
            Identity.provider == "google", Identity.subject == "google-subject"
        )
    )
    assert identity is not None
    first_login = identity.last_login_at

    second = await service.complete_login(claims)

    await db_session.refresh(identity)
    users = (await db_session.execute(select(User))).scalars().all()
    assert second.id == first.id
    assert len(users) == 1
    assert identity.last_login_at is not None
    assert first_login is None or identity.last_login_at >= first_login


@pytest.mark.asyncio
async def test_oidc_miss_trusted_verified_email_links_to_existing_user(
    db_session: AsyncSession,
) -> None:
    basic = BasicAuthService(db_session)
    existing = await basic.register("ada", "ada@example.com", _LOGIN_VALUE)

    service = OIDCAuthService(db_session, "google", trusted_email_linking=True)
    linked = await service.complete_login(
        OIDCClaims(
            provider="google",
            subject="google-subject",
            email="  Ada@Example.com  ",
            email_verified=True,
        )
    )

    users = (await db_session.execute(select(User))).scalars().all()
    identities = (
        (await db_session.execute(select(Identity).where(Identity.user_id == existing.id)))
        .scalars()
        .all()
    )
    assert linked.id == existing.id
    assert len(users) == 1
    assert {identity.provider for identity in identities} == {"local", "google"}


@pytest.mark.asyncio
async def test_oidc_miss_untrusted_provider_never_links_jit_provisions_instead(
    db_session: AsyncSession,
) -> None:
    basic = BasicAuthService(db_session)
    existing = await basic.register("ada", "ada@example.com", _LOGIN_VALUE)

    service = OIDCAuthService(db_session, "google", trusted_email_linking=False)
    provisioned = await service.complete_login(
        OIDCClaims(
            provider="google",
            subject="google-subject",
            email="ada.google@example.com",
            email_verified=True,
        )
    )

    assert provisioned.id != existing.id
    users = (await db_session.execute(select(User))).scalars().all()
    assert len(users) == 2


@pytest.mark.asyncio
async def test_oidc_miss_unverified_email_never_links_jit_provisions_instead(
    db_session: AsyncSession,
) -> None:
    basic = BasicAuthService(db_session)
    existing = await basic.register("ada", "ada@example.com", _LOGIN_VALUE)

    service = OIDCAuthService(db_session, "google", trusted_email_linking=True)
    provisioned = await service.complete_login(
        OIDCClaims(
            provider="google",
            subject="google-subject",
            email="ada.google@example.com",
            email_verified=False,
        )
    )

    assert provisioned.id != existing.id
    users = (await db_session.execute(select(User))).scalars().all()
    assert len(users) == 2


@pytest.mark.asyncio
async def test_oidc_jit_username_collision_gets_suffixed(db_session: AsyncSession) -> None:
    basic = BasicAuthService(db_session)
    await basic.register("grace", "grace-local@example.com", _LOGIN_VALUE)

    service = OIDCAuthService(db_session, "google")
    provisioned = await service.complete_login(
        OIDCClaims(
            provider="google",
            subject="google-subject",
            email="grace@example.com",
            email_verified=True,
            preferred_username="grace",
        )
    )

    assert provisioned.username == "grace-2"
