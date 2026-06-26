from httpx import AsyncClient
import pytest


async def register_user(
    client: AsyncClient,
    username: str = "ada",
    email: str = "ada@example.com",
    password: str = "correct-horse",
) -> dict[str, str]:
    response = await client.post(
        "/api/auth/register",
        json={"username": username, "email": email, "password": password},
    )
    assert response.status_code == 201
    return response.json()


async def login_user(client: AsyncClient, username: str = "ada", password: str = "correct-horse") -> str:
    response = await client.post("/api/auth/login", json={"username": username, "password": password})
    assert response.status_code == 200
    payload = response.json()
    assert payload["token_type"] == "bearer"
    return payload["access_token"]


@pytest.mark.asyncio
async def test_register_login_and_logout_with_bearer_token(api_client: AsyncClient) -> None:
    user = await register_user(api_client)
    token = await login_user(api_client)
    logout_response = await api_client.post("/api/auth/logout", headers={"Authorization": f"Bearer {token}"})

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

    wrong_password = await api_client.post("/api/auth/login", json={"username": "ada", "password": "wrong-password"})
    missing_user = await api_client.post("/api/auth/login", json={"username": "missing", "password": "correct-horse"})

    assert wrong_password.status_code == 401
    assert missing_user.status_code == 401


@pytest.mark.asyncio
async def test_logout_requires_valid_token(api_client: AsyncClient) -> None:
    without_token = await api_client.post("/api/auth/logout")
    invalid_token = await api_client.post("/api/auth/logout", headers={"Authorization": "Bearer not-a-token"})

    assert without_token.status_code == 401
    assert invalid_token.status_code == 401
