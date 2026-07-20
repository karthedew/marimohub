import contextlib
from dataclasses import dataclass
import re
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Body, Depends, HTTPException, Request, Response, WebSocket, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import NotebookRead, NotebookWrite, get_current_user_optional
from app.db.database import get_db
from app.models import Deployment, DeploymentStatus, Notebook, User
from app.schemas import DeploymentCreate, DeploymentOut
from app.services import marimo_proxy
from app.services.access import Action, authorize_notebook
from app.services.marimo_proxy import GatewayRoute, UpstreamNotFound, UpstreamNotReady
from app.services.session_manager import (
    SessionManager,
    SessionNotFoundError,
    SessionPhase,
    get_session_manager,
)
from app.services.slug import unique_slug

router = APIRouter(tags=["deployments"])
DEPLOYMENT_SLUG_MAX_LENGTH = 255


def _deployment_url(request: Request, slug: str) -> str:
    return str(request.base_url.replace(path=f"api/deployments/{slug}", query=""))


def _slug_base(title: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")
    return (slug or "notebook")[:DEPLOYMENT_SLUG_MAX_LENGTH].rstrip("-") or "notebook"


async def _unique_slug(db: AsyncSession, title: str, current_id: UUID | None = None) -> str:
    exclude = Deployment.id != current_id if current_id is not None else None
    return await unique_slug(
        db,
        _slug_base(title),
        Deployment.slug,
        max_length=DEPLOYMENT_SLUG_MAX_LENGTH,
        exclude=exclude,
    )


async def _stop_if_running(manager: SessionManager, deployment_id: UUID) -> None:
    with contextlib.suppress(SessionNotFoundError):
        await manager.stop(deployment_id)


async def resolved_status(manager: SessionManager, deployment: Deployment) -> DeploymentStatus:
    """Project the durable row + live runtime onto the coarse client status.

    ``STOPPED`` is authoritative in the row (owner intent, no runtime represents it) and
    short-circuits without consulting the backend. Otherwise the status is read through to
    the runtime on every call, since ``RUNNING`` is never persisted: a failed, timed-out, or
    capacity-rejected wake never mutates the row, so it can never read back as ``RUNNING``.
    """
    if deployment.status is DeploymentStatus.STOPPED:
        return DeploymentStatus.STOPPED
    info = await manager.get(deployment.id)
    if info is not None and info.phase is SessionPhase.READY:
        return DeploymentStatus.RUNNING
    return DeploymentStatus.SLEEPING


async def _load_deployment_by_slug(db: AsyncSession, slug: str) -> tuple[Deployment, Notebook]:
    row = (
        await db.execute(
            select(Deployment, Notebook)
            .join(Notebook, Deployment.notebook_id == Notebook.id)
            .where(Deployment.slug == slug)
        )
    ).one_or_none()
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Deployment not found")
    return row._tuple()


async def _deployment_out_after_conflict(
    db: AsyncSession, request: Request, notebook_id: UUID, exc: IntegrityError
) -> DeploymentOut:
    await db.rollback()
    existing = await db.scalar(select(Deployment).where(Deployment.notebook_id == notebook_id))
    if existing is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="Deployment slug is already in use"
        ) from exc
    return DeploymentOut(
        slug=existing.slug,
        status=existing.status,
        url=_deployment_url(request, existing.slug),
    )


async def _load_active_deployment(db: AsyncSession, slug: str) -> tuple[Deployment, Notebook]:
    row = (
        await db.execute(
            select(Deployment, Notebook)
            .join(Notebook, Deployment.notebook_id == Notebook.id)
            .where(Deployment.slug == slug)
        )
    ).one_or_none()
    if row is None:
        raise UpstreamNotFound("Deployment not found")
    deployment, notebook = row._tuple()
    if deployment.status == DeploymentStatus.STOPPED:
        raise UpstreamNotFound("Deployment not found")
    return deployment, notebook


@dataclass(slots=True)
class DeploymentResolver:
    """Resolve a deployment slug to its target, waking a sleeping deployment idempotently.

    A cheap `target()` probe is tried first so an already-awake deployment costs one read;
    `spawn_deployment` is only reached on the sleeping path, and it is itself idempotent by
    CR name, so concurrent first-hits on a sleeping deployment share one wake.
    """

    db: AsyncSession
    manager: SessionManager
    slug: str

    async def resolve(self) -> GatewayRoute:
        """Return the deployment's routable target, waking it first if it's asleep."""
        deployment, notebook = await _load_active_deployment(self.db, self.slug)
        target = await self.manager.target(deployment.id)
        if target is None:
            await self.manager.spawn_deployment(notebook, deployment.id, deployment.slug)
            target = await self.manager.target(deployment.id)
            if target is None:
                raise UpstreamNotReady("Deployment is unavailable")
        return GatewayRoute(deployment.id, target)


@router.post("/api/notebooks/{notebook_id}/deploy", response_model=DeploymentOut)
async def deploy_notebook(
    ctx: NotebookWrite,
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db)],
    manager: Annotated[SessionManager, Depends(get_session_manager)],
    payload: Annotated[DeploymentCreate | None, Body()] = None,
) -> DeploymentOut:
    """Create or reset a deployment for a notebook the caller can edit."""
    payload = payload or DeploymentCreate()
    notebook = ctx.notebook

    deployment = await db.scalar(select(Deployment).where(Deployment.notebook_id == notebook.id))
    slug = payload.slug or await _unique_slug(
        db, notebook.title, deployment.id if deployment is not None else None
    )
    owner_of_slug = await db.scalar(select(Deployment.id).where(Deployment.slug == slug))
    if owner_of_slug is not None and (deployment is None or owner_of_slug != deployment.id):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="Deployment slug is already in use"
        )

    if deployment is None:
        deployment = Deployment(notebook_id=notebook.id, slug=slug)
    else:
        await _stop_if_running(manager, deployment.id)
        deployment.slug = slug
    deployment.status = DeploymentStatus.SLEEPING
    db.add(deployment)
    try:
        await db.commit()
    except IntegrityError as exc:
        return await _deployment_out_after_conflict(db, request, notebook.id, exc)
    await db.refresh(deployment)

    return DeploymentOut(
        slug=deployment.slug,
        status=deployment.status,
        url=_deployment_url(request, deployment.slug),
    )


@router.get("/api/notebooks/{notebook_id}/deployment", response_model=DeploymentOut)
async def get_notebook_deployment(
    ctx: NotebookRead,
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db)],
    manager: Annotated[SessionManager, Depends(get_session_manager)],
) -> DeploymentOut:
    """Return a notebook's deployment with its status read through the runtime."""
    deployment = await db.scalar(
        select(Deployment).where(Deployment.notebook_id == ctx.notebook.id)
    )
    if deployment is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Deployment not found")

    return DeploymentOut(
        slug=deployment.slug,
        status=await resolved_status(manager, deployment),
        url=_deployment_url(request, deployment.slug),
    )


# Registered before the proxy catch-all so management DELETE on the bare slug
# is not shadowed by the proxied DELETE method.
@router.delete(
    "/api/deployments/{slug}", status_code=status.HTTP_204_NO_CONTENT, response_class=Response
)
async def delete_deployment(
    slug: str,
    db: Annotated[AsyncSession, Depends(get_db)],
    actor: Annotated[User | None, Depends(get_current_user_optional)],
    manager: Annotated[SessionManager, Depends(get_session_manager)],
) -> Response:
    """Stop a deployment and mark it stopped, idempotently."""
    deployment, notebook = await _load_deployment_by_slug(db, slug)
    await authorize_notebook(db, notebook, actor, Action.WRITE)

    await _stop_if_running(manager, deployment.id)
    deployment.status = DeploymentStatus.STOPPED
    db.add(deployment)
    await db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.websocket("/api/deployments/{slug}/ws")
async def deployment_ws(
    slug: str,
    websocket: WebSocket,
    db: Annotated[AsyncSession, Depends(get_db)],
    manager: Annotated[SessionManager, Depends(get_session_manager)],
) -> None:
    """Wake the deployment if needed and relay the WebSocket to it."""
    await marimo_proxy.proxy_websocket(websocket, manager, DeploymentResolver(db, manager, slug))


@router.api_route(
    "/api/deployments/{slug}", methods=marimo_proxy.PROXY_METHODS, name="deployment_http_root"
)
@router.api_route(
    "/api/deployments/{slug}/{path:path}",
    methods=marimo_proxy.PROXY_METHODS,
    name="deployment_http",
)
async def deployment_http(
    slug: str,
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db)],
    manager: Annotated[SessionManager, Depends(get_session_manager)],
    path: str = "",
) -> Response:
    """Wake the deployment if needed and proxy the HTTP request to it."""
    return await marimo_proxy.proxy_http(
        request,
        manager,
        DeploymentResolver(db, manager, slug),
        path,
        follow_redirects=request.method == "GET" and path == "",
    )
