import asyncio
from collections.abc import AsyncIterator
from typing import Annotated
from urllib.parse import quote, urlencode
from uuid import UUID

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request, WebSocket, WebSocketDisconnect, status
from starlette.background import BackgroundTask
from starlette.responses import StreamingResponse
import websockets
from websockets.exceptions import ConnectionClosed

from app.services.process_manager import ProcessManager, get_process_manager

router = APIRouter(prefix="/api/proxy", tags=["proxy"])

_HOP_BY_HOP_HEADERS = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailer",
    "transfer-encoding",
    "upgrade",
}


def _filtered_header_pairs(headers: object, *, strip_content_length: bool = False) -> list[tuple[bytes, bytes]]:
    items = getattr(headers, "raw", headers)
    blocked_headers = _HOP_BY_HOP_HEADERS | {"host"}
    if strip_content_length:
        blocked_headers.add("content-length")
    return [
        (key, value)
        for key, value in items
        if key.decode("latin-1").lower() not in blocked_headers
    ]


def _filtered_headers(headers: object, *, strip_content_length: bool = False) -> list[tuple[str, str]]:
    return [
        (key.decode("latin-1"), value.decode("latin-1"))
        for key, value in _filtered_header_pairs(headers, strip_content_length=strip_content_length)
    ]


def _target_url(base_url: str, path: str, query_string: bytes, access_token: str | None) -> str:
    url = f"{base_url}/{quote(path, safe='/')}"
    query = query_string.decode("latin-1") if query_string else ""
    if access_token is not None:
        token_query = urlencode({"access_token": access_token})
        query = f"{query}&{token_query}" if query else token_query
    if query:
        return f"{url}?{query}"
    return url


async def _request_body(request: Request) -> AsyncIterator[bytes]:
    async for chunk in request.stream():
        yield chunk


@router.api_route("/{session_id}/{path:path}", methods=["GET", "POST"], name="proxy_http")
async def proxy_http(
    session_id: UUID,
    path: str,
    request: Request,
    manager: Annotated[ProcessManager, Depends(get_process_manager)],
) -> StreamingResponse:
    target = manager.target(session_id)
    if target is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Session not found")

    manager.touch(session_id)
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
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail="Notebook session is unreachable") from exc

    response = StreamingResponse(
        upstream_response.aiter_raw(),
        status_code=upstream_response.status_code,
        background=BackgroundTask(_close_upstream, client, upstream_response),
    )
    response.raw_headers = _filtered_header_pairs(upstream_response.headers, strip_content_length=True)
    return response


async def _close_upstream(client: httpx.AsyncClient, response: httpx.Response) -> None:
    await response.aclose()
    await client.aclose()


async def _relay_websocket(websocket: WebSocket, target_url: str, manager: ProcessManager, session_id: UUID) -> None:
    await websocket.accept()
    async with websockets.connect(target_url) as upstream:
        async def client_to_upstream() -> None:
            while True:
                message = await websocket.receive()
                if message["type"] == "websocket.disconnect":
                    await upstream.close()
                    return
                manager.touch(session_id)
                if "text" in message:
                    await upstream.send(message["text"])
                elif "bytes" in message:
                    await upstream.send(message["bytes"])

        async def upstream_to_client() -> None:
            async for message in upstream:
                manager.touch(session_id)
                if isinstance(message, str):
                    await websocket.send_text(message)
                else:
                    await websocket.send_bytes(message)

        tasks = [asyncio.create_task(client_to_upstream()), asyncio.create_task(upstream_to_client())]
        done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in pending:
            task.cancel()
        await asyncio.gather(*done, *pending, return_exceptions=True)


@router.websocket("/{session_id}/ws")
async def proxy_ws(
    session_id: UUID,
    websocket: WebSocket,
    manager: Annotated[ProcessManager, Depends(get_process_manager)],
) -> None:
    target = manager.target(session_id)
    if target is None:
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
        return

    target_url = _target_url(target.ws_base_url, "ws", websocket.scope.get("query_string", b""), target.access_token)
    try:
        await _relay_websocket(websocket, target_url, manager, session_id)
    except (ConnectionClosed, WebSocketDisconnect, OSError):
        return
