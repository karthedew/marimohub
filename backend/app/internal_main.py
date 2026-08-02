"""The internal application: reachable only from the Runtime namespace.

This is a separate ASGI app from `app.main`, not a second router mounted
onto it, because the two are meant to run as distinct Deployments behind
distinct ServiceAccounts (see ADR 0001 and the Runtime Contract's Resource
Ownership table): the public app never needs Kubernetes credentials at all,
and this app never needs a database session for anything but the two
Runtime-authenticated routes it serves. Keeping them as separate importable
`app`s -- `uvicorn app.main:app` vs. `uvicorn app.internal_main:app` -- is
what makes that split real instead of aspirational; the chart that actually
runs them as two Deployments is later work, but nothing about this module
assumes it will always be imported alongside the public app.

In production this app is served over OpenShift service-serving-certificate
TLS with no Route -- reachable only from Pods inside the cluster, never from
a public listener. Nothing here hardcodes a plaintext-only assumption: TLS
termination is an uvicorn startup concern (`--ssl-keyfile`/`--ssl-certfile`),
not something this module's request handling needs to know about.
"""

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.api.internal import router as internal_router
from app.core.errors import register_error_handlers


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    """No background services to tear down: every route here is a live cluster/DB read."""
    yield


app = FastAPI(title="MarimoHub Internal API", lifespan=lifespan)
register_error_handlers(app)

app.include_router(internal_router)


@app.get("/api/health")
async def health() -> dict[str, str]:
    """Return a simple liveness payload."""
    return {"status": "ok"}
