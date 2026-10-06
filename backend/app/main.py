"""The public application: routed, user- and anonymous-visitor-facing.

`/api/internal/*` is deliberately not mounted here. It lives in
`app.internal_main`, a separate ASGI app -- see that module's docstring for
why the two are kept apart rather than one app gated by a dependency.
"""

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from urllib.parse import urlsplit

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from starlette.types import ASGIApp

from app.api.auth import router as auth_router
from app.api.data import router as data_router
from app.api.deployments import router as deployments_router
from app.api.notebooks import router as notebooks_router
from app.api.proxy import router as proxy_router
from app.api.sessions import router as sessions_router
from app.api.workspaces import router as workspaces_router
from app.core.config import Settings, get_settings
from app.core.errors import register_error_handlers
from app.services.session_manager import shutdown_session_manager

# The Vite dev server, always allowed for local development.
DEV_SPA_ORIGIN = "http://localhost:5173"


def cors_allow_origins(settings: Settings) -> list[str]:
    """Return the browser origins allowed to call this API with credentials.

    The dev SPA's origin always; plus the origin of ``PUBLIC_APP_URL`` when it
    is explicitly set (an SPA served from another origin, e.g. the e2e suite's
    frontend on E2E_FRONTEND_PORT). Unset, the SPA shares the API's origin and
    needs no CORS.
    """
    origins = [DEV_SPA_ORIGIN]
    if settings.PUBLIC_APP_URL is not None:
        parts = urlsplit(str(settings.PUBLIC_APP_URL))
        origin = f"{parts.scheme}://{parts.netloc}"
        if origin not in origins:
            origins.append(origin)
    return origins


class _SettingsCORSMiddleware(CORSMiddleware):
    """CORS whose allow-list comes from `Settings`.

    Starlette instantiates middleware when the app first starts serving, not
    at import, so importing this module still needs no environment.
    """

    def __init__(self, app: ASGIApp) -> None:
        """Wrap ``app`` with the allow-list of the process settings."""
        super().__init__(
            app,
            allow_origins=cors_allow_origins(get_settings()),
            allow_credentials=True,
            allow_methods=["*"],
            allow_headers=["*"],
        )


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    """Tear down background services on shutdown."""
    try:
        yield
    finally:
        await shutdown_session_manager()


app = FastAPI(title="MarimoHub API", lifespan=lifespan)
register_error_handlers(app)

app.add_middleware(_SettingsCORSMiddleware)

app.include_router(auth_router)
app.include_router(data_router)
app.include_router(deployments_router)
app.include_router(notebooks_router)
app.include_router(sessions_router)
app.include_router(workspaces_router)
app.include_router(proxy_router)


@app.get("/api/health")
async def health() -> dict[str, str]:
    """Return a simple liveness payload."""
    return {"status": "ok"}
