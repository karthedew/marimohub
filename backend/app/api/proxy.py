from dataclasses import dataclass
from http import HTTPStatus
import logging
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Request, WebSocket
import httpx
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.responses import Response

from app.db.database import get_db
from app.models import Notebook
from app.services import marimo_proxy
from app.services.marimo_proxy import GatewayRoute, ResponseBodyCallback, UpstreamNotFound
from app.services.notebook_storage import NotebookStorageService, get_notebook_storage
from app.services.session_manager import SessionManager, get_session_manager

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/proxy", tags=["proxy"])


@dataclass(slots=True)
class SessionResolver:
    """Resolve a proxy session id to its target.

    Edit/run sessions never sleep, so a missing target is terminal — there is no wake branch.
    """

    manager: SessionManager
    session_id: UUID

    async def resolve(self) -> GatewayRoute:
        """Return the session's routable target, or raise `UpstreamNotFound`."""
        target = await self.manager.target(self.session_id)
        if target is None:
            raise UpstreamNotFound("Session not found")
        return GatewayRoute(self.session_id, target)


def edit_save_callback(
    manager: SessionManager,
    storage: NotebookStorageService,
    db: AsyncSession,
    session_id: UUID,
) -> ResponseBodyCallback:
    """Persist a proxied `POST api/kernel/save` through the storage seam.

    Backend-neutral: the gateway sees the same save body for subprocess and pod upstreams.
    """

    async def persist(response: httpx.Response, body: bytes) -> None:
        if response.status_code >= HTTPStatus.BAD_REQUEST:
            return  # marimo rejected the save — nothing durable to mirror
        info = await manager.get(session_id)
        if info is None or info.mode != "edit":
            return  # only edit sessions own source (run/deploy never save)
        notebook = await db.get(Notebook, info.notebook_id)
        if notebook is None:
            return
        try:
            await storage.put(notebook, body.decode("utf-8"))
            await db.commit()
        except Exception:
            await db.rollback()
            logger.exception("edit-save persist failed for session %s", session_id)
            # marimo's ~1s autosave retries on the next save; never fault the editor stream
            # for a transient persistence blip.

    return persist


@router.api_route(
    "/{session_id}/{path:path}", methods=marimo_proxy.PROXY_METHODS, name="proxy_http"
)
async def proxy_http(
    session_id: UUID,
    path: str,
    request: Request,
    manager: Annotated[SessionManager, Depends(get_session_manager)],
    storage: Annotated[NotebookStorageService, Depends(get_notebook_storage)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> Response:
    """Proxy an HTTP request to a session, persisting edit-mode saves."""
    callback = (
        edit_save_callback(manager, storage, db, session_id)
        if request.method == "POST" and path == "api/kernel/save"
        else None
    )
    return await marimo_proxy.proxy_http(
        request,
        manager,
        SessionResolver(manager, session_id),
        path,
        response_body_callback=callback,
    )


@router.websocket("/{session_id}/ws")
async def proxy_ws(
    session_id: UUID,
    websocket: WebSocket,
    manager: Annotated[SessionManager, Depends(get_session_manager)],
) -> None:
    """Proxy a WebSocket connection to a session's notebook process."""
    await marimo_proxy.proxy_websocket(websocket, manager, SessionResolver(manager, session_id))
