from dataclasses import dataclass
import logging
import re
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Body, Depends, HTTPException, Request, Response, WebSocket, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import NotebookRead, NotebookWrite, get_current_user_optional
from app.core.errors import NotFoundError
from app.db.database import get_db
from app.models import Deployment, DeploymentDesiredState, Notebook, User
from app.schemas import DeploymentCreate, DeploymentOut
from app.schemas.deployment import DeploymentStatus
from app.services import deployment_lifecycle, marimo_proxy
from app.services.access import Action, authorize_notebook
from app.services.marimo_proxy import GatewayRoute, UpstreamNotFound, UpstreamNotReady
from app.services.notebook_storage import NotebookStorageService, get_notebook_storage
from app.services.session_manager import (
    SessionInfo,
    SessionManager,
    SessionManagerError,
    SessionPhase,
    get_session_manager,
)
from app.services.slug import unique_slug

logger = logging.getLogger(__name__)

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


async def _live_info(manager: SessionManager, deployment: Deployment) -> SessionInfo | None:
    """The Runtime's info, or `None` while stopped or not currently reachable."""
    if deployment.desired_state is DeploymentDesiredState.STOPPED:
        return None
    return await manager.get(deployment.id)


_LIVE_PHASE_STATUS = {
    SessionPhase.READY: DeploymentStatus.RUNNING,
    SessionPhase.FAILED: DeploymentStatus.FAILED,
}


def _status_from(deployment: Deployment, info: SessionInfo | None) -> DeploymentStatus:
    if deployment.desired_state is DeploymentDesiredState.STOPPED:
        return DeploymentStatus.STOPPED
    if info is None:
        return DeploymentStatus.SLEEPING
    return _LIVE_PHASE_STATUS.get(info.phase, DeploymentStatus.SLEEPING)


async def resolved_status(manager: SessionManager, deployment: Deployment) -> DeploymentStatus:
    """Project the durable row + live runtime onto the coarse client status.

    ``STOPPED`` is authoritative in the row (owner intent, no runtime represents it) and
    short-circuits without consulting the backend. Otherwise the status is read through to
    the runtime on every call, since ``RUNNING`` is never persisted: a failed, timed-out, or
    capacity-rejected wake never mutates the row, so it can never read back as ``RUNNING``.
    """
    return _status_from(deployment, await _live_info(manager, deployment))


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


def _conflict_status(deployment: Deployment) -> DeploymentStatus:
    if deployment.desired_state is DeploymentDesiredState.STOPPED:
        return DeploymentStatus.STOPPED
    return DeploymentStatus.SLEEPING


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
        status=_conflict_status(existing),
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
    if deployment.desired_state is DeploymentDesiredState.STOPPED:
        raise UpstreamNotFound("Deployment not found")
    return deployment, notebook


@dataclass(slots=True)
class DeploymentResolver:
    """Resolve a deployment slug to its target, waking a sleeping deployment idempotently.

    A cheap revision-aware probe is tried first so an already-awake deployment costs one
    read; `ensure_running` is only reached when nothing routable is running yet, and it is
    itself idempotent (it serializes through the Workspace/Deployment lock), so concurrent
    first-hits on a sleeping deployment share one wake.

    Every failure this resolves into is a generic `UpstreamNotFound`/`UpstreamNotReady`:
    quota, a deterministic startup failure, and a wake timeout are all real distinctions the
    *authorized* Notebook-management surface exposes (`get_notebook_deployment`), but an
    anonymous Deployment visitor never sees a Condition Reason or a manager's raw message --
    only "it's unavailable."
    """

    db: AsyncSession
    manager: SessionManager
    slug: str

    async def resolve(self) -> GatewayRoute:
        """Return the deployment's routable target, waking it first if it's asleep."""
        deployment, notebook = await _load_active_deployment(self.db, self.slug)
        info = await deployment_lifecycle.resolve_target_info(self.manager, deployment)
        if info is None or info.phase is not SessionPhase.READY:
            try:
                info = await deployment_lifecycle.ensure_running(
                    self.db, self.manager, notebook, deployment.id
                )
            except NotFoundError as exc:
                raise UpstreamNotFound("Deployment not found") from exc
            except SessionManagerError as exc:
                logger.warning("deployment %s failed to wake: %s", deployment.id, exc)
                raise UpstreamNotReady("Deployment is unavailable") from exc
            if (
                info.deployment_revision is not None
                and info.deployment_revision != deployment.revision
            ):
                raise UpstreamNotReady("Deployment is unavailable")
        target = await self.manager.target(deployment.id)
        if target is None:
            raise UpstreamNotReady("Deployment is unavailable")
        return GatewayRoute(deployment.id, target)


@router.post(
    "/api/notebooks/{notebook_id}/deploy",
    response_model=DeploymentOut,
    response_model_exclude_none=True,
)
async def deploy_notebook(
    ctx: NotebookWrite,
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db)],
    manager: Annotated[SessionManager, Depends(get_session_manager)],
    storage: Annotated[NotebookStorageService, Depends(get_notebook_storage)],
    payload: Annotated[DeploymentCreate | None, Body()] = None,
) -> DeploymentOut:
    """Deploy or redeploy a notebook the caller can edit.

    Commits a new immutable source/image snapshot and a bumped revision; an
    ordinary Notebook source edit never touches an existing deployment's
    snapshot, only this endpoint does.
    """
    payload = payload or DeploymentCreate()
    notebook = ctx.notebook

    existing_id = await db.scalar(
        select(Deployment.id).where(Deployment.notebook_id == notebook.id)
    )
    slug = payload.slug or await _unique_slug(db, notebook.title, existing_id)
    owner_of_slug = await db.scalar(select(Deployment.id).where(Deployment.slug == slug))
    if owner_of_slug is not None and owner_of_slug != existing_id:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="Deployment slug is already in use"
        )

    try:
        deployment = await deployment_lifecycle.deploy(db, manager, notebook, storage, slug=slug)
    except IntegrityError as exc:
        return await _deployment_out_after_conflict(db, request, notebook.id, exc)

    return DeploymentOut(
        slug=deployment.slug,
        status=DeploymentStatus.SLEEPING,
        url=_deployment_url(request, deployment.slug),
    )


@router.get(
    "/api/notebooks/{notebook_id}/deployment",
    response_model=DeploymentOut,
    response_model_exclude_none=True,
)
async def get_notebook_deployment(
    ctx: NotebookRead,
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db)],
    manager: Annotated[SessionManager, Depends(get_session_manager)],
) -> DeploymentOut:
    """Return a notebook's deployment with its status read through the runtime.

    An authorized caller (this endpoint requires read access to the
    Notebook) additionally gets a stable, sanitized failure Reason/message
    while the deployment is `failed`.
    """
    deployment = await db.scalar(
        select(Deployment).where(Deployment.notebook_id == ctx.notebook.id)
    )
    if deployment is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Deployment not found")

    info = await _live_info(manager, deployment)
    deployment_status = _status_from(deployment, info)
    failed = deployment_status is DeploymentStatus.FAILED and info is not None

    return DeploymentOut(
        slug=deployment.slug,
        status=deployment_status,
        url=_deployment_url(request, deployment.slug),
        failure_reason=info.failure_reason if failed else None,
        message=info.message if failed else None,
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

    await deployment_lifecycle.stop(db, manager, notebook, deployment.id)
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
