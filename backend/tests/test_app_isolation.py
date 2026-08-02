"""Structural proof that the public and internal apps stay genuinely separate.

`app.main` and `app.internal_main` are two distinct FastAPI apps (not one
app with a second router bolted on), so the split holds even before anything
runs them as separate Deployments: a public client can never reach
`/api/internal/*`, and the internal app never exposes anything but health
and Runtime-authenticated routes.
"""

from fastapi.routing import APIRoute
from httpx import ASGITransport, AsyncClient
import pytest
from starlette.routing import BaseRoute

from app.internal_main import app as internal_app
from app.main import app as public_app

_INTERNAL_PATHS = (
    "/api/internal/runtimes/00000000-0000-0000-0000-000000000000/source",
    "/api/internal/notebooks/00000000-0000-0000-0000-000000000000/data",
)

# Route name prefixes that would indicate a public/auth/workspace/notebook
# management surface leaking into the internal app.
_FORBIDDEN_INTERNAL_PATH_PREFIXES = (
    "/api/auth",
    "/api/workspaces",
    "/api/notebooks",
    "/api/sessions",
    "/api/deployments",
    "/api/proxy",
)


@pytest.mark.asyncio
@pytest.mark.parametrize("path", _INTERNAL_PATHS)
async def test_public_app_returns_404_for_internal_paths(path: str) -> None:
    async with AsyncClient(
        transport=ASGITransport(app=public_app), base_url="http://test"
    ) as client:
        response = await client.get(path)

    assert response.status_code == 404


def _route_paths(app_routes: list[BaseRoute]) -> set[str]:
    return {route.path for route in app_routes if isinstance(route, APIRoute)}


def test_internal_app_exposes_only_health_and_internal_routes() -> None:
    paths = _route_paths(list(internal_app.routes))

    assert all(path == "/api/health" or path.startswith("/api/internal/") for path in paths), paths


def test_internal_app_has_no_public_management_routes() -> None:
    paths = _route_paths(list(internal_app.routes))

    for path in paths:
        assert not path.startswith(_FORBIDDEN_INTERNAL_PATH_PREFIXES), path


def test_public_app_never_mounts_the_internal_router() -> None:
    paths = _route_paths(list(public_app.routes))

    assert not any(path.startswith("/api/internal/") for path in paths)
