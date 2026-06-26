from datetime import UTC, datetime
import re
from typing import Annotated
from uuid import UUID

import httpx
from fastapi import APIRouter, Body, Depends, HTTPException, Request, Response, WebSocket, WebSocketDisconnect, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.background import BackgroundTask
from starlette.responses import StreamingResponse
from websockets.exceptions import ConnectionClosed

from app.api.deps import get_current_user, get_current_user_optional
from app.api.proxy import _filtered_header_pairs, _filtered_headers, _request_body, _relay_websocket, _target_url
from app.db.database import get_db
from app.models import Deployment, DeploymentStatus, Notebook, User
from app.schemas import DeploymentCreate, DeploymentOut
from app.services.process_manager import (
    PortAllocationError,
    ProcessManager,
    SessionCapacityError,
    SessionNotFoundError,
    SessionStartError,
    get_process_manager,
)

router = APIRouter(tags=["deployments"])
DEPLOYMENT_SLUG_MAX_LENGTH = 255


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
    while suffix < 10000:
        existing = await db.scalar(select(Deployment.id).where(Deployment.slug == candidate))
        if existing is None or existing == current_id:
            return candidate
        candidate = _slug_candidate(base, suffix)
        suffix += 1
    raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Unable to generate a deployment slug")


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
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Deployment failed to wake") from exc
    deployment.status = DeploymentStatus.RUNNING
    deployment.port = session.port


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
    deployment, notebook = row
    if deployment.status == DeploymentStatus.STOPPED:
        raise _not_found()
    return deployment, notebook


async def _close_upstream(client: httpx.AsyncClient, response: httpx.Response) -> None:
    await response.aclose()
    await client.aclose()


async def _proxy_deployment_request(
    request: Request,
    manager: ProcessManager,
    deployment: Deployment,
    path: str,
) -> StreamingResponse:
    target = manager.target(deployment.id)
    if target is None:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Deployment is unavailable")

    manager.touch(deployment.id)
    client = httpx.AsyncClient(follow_redirects=False)
    upstream_request = client.build_request(
        request.method,
        _target_url(
            target.http_base_url,
            path,
            request.scope.get("query_string", b""),
            None if request.headers.get("cookie") else target.access_token,
        ),
        headers=_filtered_headers(request.headers),
        content=_request_body(request) if request.method == "POST" else None,
    )
    try:
        upstream_response = await client.send(upstream_request, stream=True)
    except httpx.HTTPError as exc:
        await client.aclose()
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail="Deployment is unreachable") from exc

    response = StreamingResponse(
        upstream_response.aiter_raw(),
        status_code=upstream_response.status_code,
        background=BackgroundTask(_close_upstream, client, upstream_response),
    )
    response.raw_headers = _filtered_header_pairs(upstream_response.headers, strip_content_length=True)
    return response


@router.post("/api/notebooks/{notebook_id}/deploy", response_model=DeploymentOut)
async def deploy_notebook(
    notebook_id: UUID,
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
    manager: Annotated[ProcessManager, Depends(get_process_manager)],
    payload: Annotated[DeploymentCreate | None, Body()] = None,
) -> DeploymentOut:
    payload = payload or DeploymentCreate()
    notebook = await db.scalar(select(Notebook).where(Notebook.id == notebook_id))
    if notebook is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Notebook not found")
    if notebook.user_id != current_user.id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Notebook owner required")

    deployment = await db.scalar(select(Deployment).where(Deployment.notebook_id == notebook.id))
    slug = payload.slug or await _unique_slug(db, notebook.title, deployment.id if deployment is not None else None)
    owner_of_slug = await db.scalar(select(Deployment.id).where(Deployment.slug == slug))
    if owner_of_slug is not None and (deployment is None or owner_of_slug != deployment.id):
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Deployment slug is already in use")

    if deployment is None:
        deployment = Deployment(notebook_id=notebook.id, slug=slug)
    else:
        if manager.target(deployment.id) is not None:
            try:
                await manager.stop(deployment.id)
            except SessionNotFoundError:
                pass
        deployment.slug = slug
    deployment.status = DeploymentStatus.SLEEPING
    deployment.port = None
    deployment.last_active = None
    db.add(deployment)
    try:
        await db.commit()
    except IntegrityError as exc:
        await db.rollback()
        existing = await db.scalar(select(Deployment).where(Deployment.notebook_id == notebook.id))
        if existing is not None:
            return DeploymentOut(slug=existing.slug, status=existing.status, url=_deployment_url(request, existing.slug))
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Deployment slug is already in use") from exc
    await db.refresh(deployment)

    return DeploymentOut(slug=deployment.slug, status=deployment.status, url=_deployment_url(request, deployment.slug))


@router.api_route("/api/deployments/{slug}", methods=["GET", "POST"], name="deployment_http_root")
@router.api_route("/api/deployments/{slug}/{path:path}", methods=["GET", "POST"], name="deployment_http")
async def deployment_http(
    slug: str,
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db)],
    _current_user: Annotated[User | None, Depends(get_current_user_optional)],
    manager: Annotated[ProcessManager, Depends(get_process_manager)],
    path: str = "",
) -> StreamingResponse:
    deployment, notebook = await _load_deployment(db, slug)

    await _wake_deployment(db, manager, deployment, notebook)
    deployment.last_active = datetime.now(UTC)
    db.add(deployment)
    await db.commit()
    return await _proxy_deployment_request(request, manager, deployment, path)


@router.websocket("/api/deployments/{slug}/ws")
async def deployment_ws(
    slug: str,
    websocket: WebSocket,
    db: Annotated[AsyncSession, Depends(get_db)],
    manager: Annotated[ProcessManager, Depends(get_process_manager)],
) -> None:
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
        target_url = _target_url(target.ws_base_url, "ws", websocket.scope.get("query_string", b""), target.access_token)
        await _relay_websocket(websocket, target_url, manager, deployment.id)
    except HTTPException:
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
    except (ConnectionClosed, WebSocketDisconnect, OSError):
        return


@router.delete("/api/deployments/{slug}", status_code=status.HTTP_204_NO_CONTENT, response_class=Response)
async def delete_deployment(
    slug: str,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
    manager: Annotated[ProcessManager, Depends(get_process_manager)],
) -> Response:
    deployment, notebook = await _load_deployment(db, slug)
    if notebook.user_id != current_user.id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Notebook owner required")

    if manager.target(deployment.id) is not None:
        try:
            await manager.stop(deployment.id)
        except SessionNotFoundError:
            pass
    deployment.status = DeploymentStatus.STOPPED
    deployment.port = None
    deployment.last_active = None
    db.add(deployment)
    await db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)
