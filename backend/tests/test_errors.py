from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
import pytest

from app.core.errors import (
    ConflictError,
    DomainError,
    NotFoundError,
    Unauthenticated,
    register_error_handlers,
)


def _build_app() -> FastAPI:
    app = FastAPI()
    register_error_handlers(app)

    @app.get("/conflict")
    async def _conflict() -> None:
        raise ConflictError("duplicate row", detail="Resource already exists")

    @app.get("/not-found")
    async def _not_found() -> None:
        raise NotFoundError("row missing")

    @app.get("/unauthenticated")
    async def _unauthenticated() -> None:
        raise Unauthenticated("bad token")

    @app.get("/unmapped-domain-error")
    async def _unmapped_domain_error() -> None:
        raise DomainError("internal wiring failure")

    @app.get("/unexpected")
    async def _unexpected() -> None:
        raise ValueError("db connection string leaked here")

    return app


@pytest.fixture
def client() -> AsyncClient:
    transport = ASGITransport(app=_build_app(), raise_app_exceptions=False)
    return AsyncClient(transport=transport, base_url="http://test")


@pytest.mark.asyncio
async def test_conflict_error_renders_409_with_client_safe_detail(client: AsyncClient) -> None:
    async with client:
        response = await client.get("/conflict")

    assert response.status_code == 409
    assert response.json() == {"detail": "Resource already exists"}


@pytest.mark.asyncio
async def test_not_found_error_renders_404_with_message_as_detail(client: AsyncClient) -> None:
    async with client:
        response = await client.get("/not-found")

    assert response.status_code == 404
    assert response.json() == {"detail": "row missing"}


@pytest.mark.asyncio
async def test_unauthenticated_error_renders_401_with_bearer_challenge(
    client: AsyncClient,
) -> None:
    async with client:
        response = await client.get("/unauthenticated")

    assert response.status_code == 401
    assert response.json() == {"detail": "bad token"}
    assert response.headers["www-authenticate"] == "Bearer"


@pytest.mark.asyncio
async def test_non_401_errors_carry_no_www_authenticate_header(client: AsyncClient) -> None:
    async with client:
        response = await client.get("/conflict")

    assert "www-authenticate" not in response.headers


@pytest.mark.asyncio
async def test_domain_error_without_explicit_status_defaults_to_500(client: AsyncClient) -> None:
    async with client:
        response = await client.get("/unmapped-domain-error")

    assert response.status_code == 500
    assert response.json() == {"detail": "internal wiring failure"}


@pytest.mark.asyncio
async def test_unhandled_exception_returns_generic_500_without_leaking_detail(
    client: AsyncClient,
) -> None:
    async with client:
        response = await client.get("/unexpected")

    assert response.status_code == 500
    assert response.json() == {"detail": "Internal server error"}
    assert "db connection string" not in response.text


def test_ws_close_code_defaults_to_1011_unless_overridden() -> None:
    assert DomainError.ws_close_code == 1011
    assert ConflictError.ws_close_code == 1011
    assert NotFoundError.ws_close_code == 1011
    assert Unauthenticated.ws_close_code == 1011
