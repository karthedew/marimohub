from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.auth import router as auth_router
from app.api.data import router as data_router
from app.api.deployments import router as deployments_router
from app.api.internal import router as internal_router
from app.api.notebooks import router as notebooks_router
from app.api.proxy import router as proxy_router
from app.api.sessions import router as sessions_router
from app.api.workspaces import router as workspaces_router
from app.core.errors import register_error_handlers
from app.services.session_manager import shutdown_session_manager


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    """Tear down background services on shutdown."""
    try:
        yield
    finally:
        await shutdown_session_manager()


app = FastAPI(title="MarimoHub API", lifespan=lifespan)
register_error_handlers(app)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(auth_router)
app.include_router(data_router)
app.include_router(deployments_router)
app.include_router(internal_router)
app.include_router(notebooks_router)
app.include_router(sessions_router)
app.include_router(workspaces_router)
app.include_router(proxy_router)


@app.get("/api/health")
async def health() -> dict[str, str]:
    """Return a simple liveness payload."""
    return {"status": "ok"}
