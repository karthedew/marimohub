from http import HTTPStatus
from typing import Annotated, cast
from uuid import UUID

from fastapi import (
    APIRouter,
    Depends,
    HTTPException,
    Request,
    WebSocket,
    WebSocketDisconnect,
    status,
)
import httpx
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.responses import Response
from websockets.exceptions import ConnectionClosed

from app.db.database import get_db
from app.models import Notebook
from app.services import marimo_proxy
from app.services.notebook_storage import PostgresNotebookStorage
from app.services.process_manager import ProcessManager, get_process_manager

router = APIRouter(prefix="/api/proxy", tags=["proxy"])


@router.api_route(
    "/{session_id}/{path:path}", methods=marimo_proxy.PROXY_METHODS, name="proxy_http"
)
async def proxy_http(
    session_id: UUID,
    path: str,
    request: Request,
    manager: Annotated[ProcessManager, Depends(get_process_manager)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> Response:
    """Proxy an HTTP request to a session, persisting edit-mode saves."""
    target = manager.target(session_id)
    if target is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Session not found")

    manager.touch(session_id)

    async def persist_marimo_save(response: httpx.Response, body: bytes) -> None:
        session = manager.get(session_id)
        if (
            response.status_code >= HTTPStatus.BAD_REQUEST
            or session is None
            or session.mode != "edit"
        ):
            return
        notebook = await db.get(Notebook, session.notebook_id)
        if notebook is None:
            return
        source = body.decode("utf-8")
        await PostgresNotebookStorage(db).put(notebook, source)
        await db.commit()

    callback = (
        persist_marimo_save if request.method == "POST" and path == "api/kernel/save" else None
    )
    return await marimo_proxy.forward_http(request, target, path, response_body_callback=callback)


@router.websocket("/{session_id}/ws")
async def proxy_ws(
    session_id: UUID,
    websocket: WebSocket,
    manager: Annotated[ProcessManager, Depends(get_process_manager)],
) -> None:
    """Proxy a WebSocket connection to a session's notebook process."""
    target = manager.target(session_id)
    if target is None:
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
        return

    query_string = cast("bytes", websocket.scope.get("query_string", b""))
    target_url = marimo_proxy.build_target_url(
        target.ws_base_url, "ws", query_string, target.access_token
    )
    try:
        await marimo_proxy.relay_websocket(websocket, target_url, manager, session_id)
    except (ConnectionClosed, WebSocketDisconnect, OSError):
        return
