import contextlib
from datetime import UTC, datetime
import re
from typing import Annotated, cast
from uuid import UUID

from fastapi import (
    APIRouter,
    Body,
    Depends,
    HTTPException,
    Request,
    Response,
    WebSocket,
    WebSocketDisconnect,
    status,
)
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from websockets.exceptions import ConnectionClosed

from app.api.deps import get_current_user, get_current_user_optional
from app.db.database import get_db
from app.models import Deployment, DeploymentStatus, Notebook, User
from app.schemas import DeploymentCreate, DeploymentOut
from app.services import marimo_proxy
from app.services.process_manager import (
    NotebookStartupError,
    PortAllocationError,
    ProcessManager,
    SessionCapacityError,
    SessionNotFoundError,
    SessionStartError,
    get_process_manager,
)

router = APIRouter(tags=["deployments"])
DEPLOYMENT_SLUG_MAX_LENGTH = 255
MAX_SLUG_SUFFIX = 10000


def _not_found() -> HTTPException:
    return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Deployment not found")


def _deployment_url(request: Request, slug: str) -> str:
    return str(request.base_url.replace(path=f"api/deployments/{slug}", query=""))


def _slug_base(title: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")
    return (slug or "notebook")[:DEPLOYMENT_SLUG_MAX_LENGTH].rstrip("-") or "notebook"


def _slug_candidate(base: str, suffix: int | None) -> str:
    if suffix is None:
        return base
    suffix_text = f"-{suffix}"
    return f"{base[: DEPLOYMENT_SLUG_MAX_LENGTH - len(suffix_text)].rstrip('-')}{suffix_text}"


async def _unique_slug(db: AsyncSession, title: str, current_id: UUID | None = None) -> str:
    base = _slug_base(title)
    candidate = _slug_candidate(base, None)
    suffix = 2
    while suffix < MAX_SLUG_SUFFIX:
        existing = await db.scalar(select(Deployment.id).where(Deployment.slug == candidate))
        if existing is None or existing == current_id:
            return candidate
        candidate = _slug_candidate(base, suffix)
        suffix += 1
    raise HTTPException(
        status_code=status.HTTP_400_BAD_REQUEST, detail="Unable to generate a deployment slug"
    )


async def _wake_deployment(
    db: AsyncSession,
    manager: ProcessManager,
    deployment: Deployment,
    notebook: Notebook,
) -> None:
    if deployment.status == DeploymentStatus.RUNNING and manager.target(deployment.id) is not None:
        return

    try:
        session = await manager.spawn_deployment(notebook, deployment.id, deployment.slug)
    except (SessionCapacityError, PortAllocationError, SessionStartError) as exc:
        deployment.status = DeploymentStatus.SLEEPING
        deployment.port = None
        deployment.last_active = None
        db.add(deployment)
        await db.commit()
        # A process that exits during startup is a deterministic failure, so
        # surface it as a non-retryable 502 rather than a "still warming" 503.
        if isinstance(exc, NotebookStartupError):
            raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=exc.detail) from exc
        detail = (
            exc.detail
            if isinstance(exc, SessionStartError)
            else "Deployment is at capacity. Try again shortly."
        )
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=detail) from exc
    deployment.status = DeploymentStatus.RUNNING
    deployment.port = session.port


async def _load_owned_notebook(db: AsyncSession, notebook_id: UUID, current_user: User) -> Notebook:
    notebook = await db.scalar(select(Notebook).where(Notebook.id == notebook_id))
    if notebook is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Notebook not found")
    if notebook.user_id != current_user.id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Notebook owner required")
    return notebook


async def _stop_if_running(manager: ProcessManager, deployment_id: UUID) -> None:
    if manager.target(deployment_id) is not None:
        with contextlib.suppress(SessionNotFoundError):
            await manager.stop(deployment_id)


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


async def _load_deployment(db: AsyncSession, slug: str) -> tuple[Deployment, Notebook]:
    row = (
        await db.execute(
            select(Deployment, Notebook)
            .join(Notebook, Deployment.notebook_id == Notebook.id)
            .where(Deployment.slug == slug)
        )
    ).one_or_none()
    if row is None:
        raise _not_found()
    deployment, notebook = row.tuple()
    if deployment.status == DeploymentStatus.STOPPED:
        raise _not_found()
    return deployment, notebook


@router.post("/api/notebooks/{notebook_id}/deploy", response_model=DeploymentOut)
async def deploy_notebook(
    notebook_id: UUID,
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
    manager: Annotated[ProcessManager, Depends(get_process_manager)],
    payload: Annotated[DeploymentCreate | None, Body()] = None,
) -> DeploymentOut:
    """Create or reset a deployment for a notebook owned by the caller."""
    payload = payload or DeploymentCreate()
    notebook = await _load_owned_notebook(db, notebook_id, current_user)

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
    deployment.port = None
    deployment.last_active = None
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


# Registered before the proxy catch-all so management DELETE on the bare slug
# is not shadowed by the proxied DELETE method.
@router.delete(
    "/api/deployments/{slug}", status_code=status.HTTP_204_NO_CONTENT, response_class=Response
)
async def delete_deployment(
    slug: str,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
    manager: Annotated[ProcessManager, Depends(get_process_manager)],
) -> Response:
    """Stop a deployment and mark it stopped (owner only)."""
    deployment, notebook = await _load_deployment(db, slug)
    if notebook.user_id != current_user.id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Notebook owner required")

    await _stop_if_running(manager, deployment.id)
    deployment.status = DeploymentStatus.STOPPED
    deployment.port = None
    deployment.last_active = None
    db.add(deployment)
    await db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.websocket("/api/deployments/{slug}/ws")
async def deployment_ws(
    slug: str,
    websocket: WebSocket,
    db: Annotated[AsyncSession, Depends(get_db)],
    manager: Annotated[ProcessManager, Depends(get_process_manager)],
) -> None:
    """Wake the deployment if needed and relay the WebSocket to it."""
    try:
        deployment, notebook = await _load_deployment(db, slug)
        await _wake_deployment(db, manager, deployment, notebook)
        deployment.last_active = datetime.now(UTC)
        db.add(deployment)
        await db.commit()
        target = manager.target(deployment.id)
        if target is None:
            await websocket.close(code=status.WS_1011_INTERNAL_ERROR)
            return
        query_string = cast("bytes", websocket.scope.get("query_string", b""))
        target_url = marimo_proxy.build_target_url(
            target.ws_base_url, "ws", query_string, target.access_token
        )
        await marimo_proxy.relay_websocket(websocket, target_url, manager, deployment.id)
    except HTTPException:
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
    except (ConnectionClosed, WebSocketDisconnect, OSError):
        return


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
    _current_user: Annotated[User | None, Depends(get_current_user_optional)],
    manager: Annotated[ProcessManager, Depends(get_process_manager)],
    path: str = "",
) -> Response:
    """Wake the deployment if needed and proxy the HTTP request to it."""
    deployment, notebook = await _load_deployment(db, slug)

    await _wake_deployment(db, manager, deployment, notebook)
    deployment.last_active = datetime.now(UTC)
    db.add(deployment)
    await db.commit()

    target = manager.target(deployment.id)
    if target is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Deployment is unavailable"
        )
    manager.touch(deployment.id)
    return await marimo_proxy.forward_http(
        request,
        target,
        path,
        follow_redirects=request.method == "GET" and path == "",
    )
