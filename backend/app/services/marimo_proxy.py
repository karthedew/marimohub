import asyncio
from collections.abc import Awaitable, Callable
from typing import Protocol, cast
from urllib.parse import quote, urlencode
from uuid import UUID

from fastapi import HTTPException, Request, WebSocket, status
import httpx
from starlette.background import BackgroundTask
from starlette.responses import Response, StreamingResponse
import websockets
from websockets.asyncio.client import ClientConnection

from app.services.process_manager import ProcessManager, SessionTarget

# The single method allow-list both routers register, and the set whose request
# body is streamed upstream (every body-capable method, i.e. everything but GET/HEAD).
PROXY_METHODS = ["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"]
_BODY_METHODS = {"POST", "PUT", "PATCH", "DELETE", "OPTIONS"}

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


class _RawHeaders(Protocol):
    """Structural type for header containers exposing raw byte pairs."""

    @property
    def raw(self) -> list[tuple[bytes, bytes]]: ...


def _filtered_header_pairs(
    headers: _RawHeaders, *, strip_content_length: bool = False
) -> list[tuple[bytes, bytes]]:
    blocked_headers = _HOP_BY_HOP_HEADERS | {"host"}
    if strip_content_length:
        blocked_headers.add("content-length")
    return [
        (key, value)
        for key, value in headers.raw
        if key.decode("latin-1").lower() not in blocked_headers
    ]


def _filtered_headers(
    headers: _RawHeaders, *, strip_content_length: bool = False
) -> list[tuple[str, str]]:
    return [
        (key.decode("latin-1"), value.decode("latin-1"))
        for key, value in _filtered_header_pairs(headers, strip_content_length=strip_content_length)
    ]


def build_target_url(
    base_url: str, path: str, query_string: bytes, access_token: str | None
) -> str:
    """Build the upstream URL, appending the access token to the query if given."""
    url = f"{base_url}/{quote(path, safe='/')}"
    query = query_string.decode("latin-1") if query_string else ""
    if access_token is not None:
        token_query = urlencode({"access_token": access_token})
        query = f"{query}&{token_query}" if query else token_query
    if query:
        return f"{url}?{query}"
    return url


ResponseBodyCallback = Callable[[httpx.Response, bytes], Awaitable[None]]


async def _request_body(request: Request) -> bytes | None:
    if request.method not in _BODY_METHODS:
        return None
    return await request.body()


async def _close_upstream(client: httpx.AsyncClient, response: httpx.Response) -> None:
    await response.aclose()
    await client.aclose()


async def forward_http(
    request: Request,
    target: SessionTarget,
    path: str,
    *,
    response_body_callback: ResponseBodyCallback | None = None,
    follow_redirects: bool = False,
) -> Response:
    """Proxy an HTTP request to the notebook process and return its response."""
    client = httpx.AsyncClient(follow_redirects=follow_redirects)
    body = await _request_body(request)
    query_string = cast("bytes", request.scope.get("query_string", b""))
    upstream_request = client.build_request(
        request.method,
        build_target_url(
            target.http_base_url,
            path,
            query_string,
            None if request.headers.get("cookie") else target.access_token,
        ),
        headers=_filtered_headers(request.headers),
        content=body,
    )
    try:
        upstream_response = await client.send(
            upstream_request,
            stream=response_body_callback is None,
            follow_redirects=follow_redirects,
        )
    except httpx.HTTPError as exc:
        await client.aclose()
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY, detail="Notebook process is unreachable"
        ) from exc

    if response_body_callback is not None:
        response_body = upstream_response.content
        await response_body_callback(upstream_response, response_body)
        response = Response(content=response_body, status_code=upstream_response.status_code)
        response.raw_headers = _filtered_header_pairs(
            upstream_response.headers, strip_content_length=True
        )
        await upstream_response.aclose()
        await client.aclose()
        return response

    response = StreamingResponse(
        upstream_response.aiter_raw(),
        status_code=upstream_response.status_code,
        background=BackgroundTask(_close_upstream, client, upstream_response),
    )
    response.raw_headers = _filtered_header_pairs(
        upstream_response.headers, strip_content_length=True
    )
    return response


async def _client_to_upstream(
    websocket: WebSocket, upstream: ClientConnection, manager: ProcessManager, session_id: UUID
) -> None:
    while True:
        message = await websocket.receive()
        if message["type"] == "websocket.disconnect":
            await upstream.close()
            return
        manager.touch(session_id)
        if "text" in message:
            await upstream.send(cast("str", message["text"]))
        elif "bytes" in message:
            await upstream.send(cast("bytes", message["bytes"]))


async def _upstream_to_client(
    websocket: WebSocket, upstream: ClientConnection, manager: ProcessManager, session_id: UUID
) -> None:
    async for message in upstream:
        manager.touch(session_id)
        if isinstance(message, str):
            await websocket.send_text(message)
        else:
            await websocket.send_bytes(message)


async def relay_websocket(
    websocket: WebSocket, target_url: str, manager: ProcessManager, session_id: UUID
) -> None:
    """Bridge the client WebSocket to the upstream notebook WebSocket bidirectionally."""
    await websocket.accept()
    async with websockets.connect(target_url) as upstream:
        tasks = [
            asyncio.create_task(_client_to_upstream(websocket, upstream, manager, session_id)),
            asyncio.create_task(_upstream_to_client(websocket, upstream, manager, session_id)),
        ]
        _done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in pending:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
