import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Protocol, cast
from urllib.parse import quote, urlencode
from uuid import UUID

from fastapi import Request, WebSocket, WebSocketDisconnect, status
import httpx
from starlette.background import BackgroundTask
from starlette.responses import Response, StreamingResponse
import websockets
from websockets.asyncio.client import ClientConnection
from websockets.exceptions import ConnectionClosed

from app.core.errors import DomainError
from app.services.session_manager import SessionManager, SessionTarget

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


@dataclass(frozen=True, slots=True)
class GatewayRoute:
    """A resolved, routable session: the mark_active + forward/relay key and its upstream."""

    session_id: UUID
    target: SessionTarget


class Resolver(Protocol):
    """Map one entry-point key to a routable session, waking it if the backend can sleep.

    Raises a `GatewayError` (or a `SessionManagerError` from a wake attempt) when no
    routable upstream can be produced.
    """

    async def resolve(self) -> GatewayRoute:
        """Return a routable session, waking it first if the backend supports sleep."""
        ...


class GatewayError(DomainError):
    """Base for every gateway resolution/transport failure."""


class UpstreamNotFound(GatewayError):  # noqa: N818 -- named for the resolution outcome, not "Error" noise
    """The entry-point key does not resolve to a routable session (unknown id/slug, stopped)."""

    # `ws_close_code` must be assigned before `status`: once `status` is bound as a class
    # attribute it shadows the `fastapi.status` module for the rest of this class body.
    ws_close_code = status.WS_1008_POLICY_VIOLATION
    status = status.HTTP_404_NOT_FOUND


class UpstreamNotReady(GatewayError):  # noqa: N818 -- named for the resolution outcome, not "Error" noise
    """The upstream exists but is still not serving after a wake attempt."""

    status = status.HTTP_503_SERVICE_UNAVAILABLE


class UpstreamUnreachable(GatewayError):  # noqa: N818 -- named for the resolution outcome, not "Error" noise
    """A transport error occurred reaching a resolved upstream target."""

    status = status.HTTP_502_BAD_GATEWAY


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


def _build_target_url(
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


async def _forward_http(
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
        _build_target_url(
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
        raise UpstreamUnreachable("Notebook process is unreachable") from exc

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
    websocket: WebSocket, upstream: ClientConnection, manager: SessionManager, session_id: UUID
) -> None:
    while True:
        message = await websocket.receive()
        if message["type"] == "websocket.disconnect":
            await upstream.close()
            return
        await manager.mark_active(session_id)
        if "text" in message:
            await upstream.send(cast("str", message["text"]))
        elif "bytes" in message:
            await upstream.send(cast("bytes", message["bytes"]))


async def _upstream_to_client(
    websocket: WebSocket, upstream: ClientConnection, manager: SessionManager, session_id: UUID
) -> None:
    async for message in upstream:
        await manager.mark_active(session_id)
        if isinstance(message, str):
            await websocket.send_text(message)
        else:
            await websocket.send_bytes(message)


async def _relay_websocket(
    websocket: WebSocket, target_url: str, manager: SessionManager, session_id: UUID
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


async def proxy_http(
    request: Request,
    manager: SessionManager,
    resolver: Resolver,
    path: str,
    *,
    response_body_callback: ResponseBodyCallback | None = None,
    follow_redirects: bool = False,
) -> Response:
    """Resolve-or-wake, mark activity, and forward the HTTP request to the upstream session."""
    route = await resolver.resolve()
    await manager.mark_active(route.session_id)
    return await _forward_http(
        request,
        route.target,
        path,
        response_body_callback=response_body_callback,
        follow_redirects=follow_redirects,
    )


async def proxy_websocket(
    websocket: WebSocket, manager: SessionManager, resolver: Resolver
) -> None:
    """Resolve-or-wake, mark connect activity, and relay the WebSocket to the upstream session."""
    try:
        route = await resolver.resolve()
    except DomainError as exc:  # GatewayError or a SessionManagerError from a wake
        await websocket.close(code=exc.ws_close_code)
        return
    await manager.mark_active(route.session_id)  # connect edge; _relay_websocket marks per frame
    target_url = _build_target_url(
        route.target.ws_base_url,
        "ws",
        cast("bytes", websocket.scope.get("query_string", b"")),
        route.target.access_token,
    )
    try:
        await _relay_websocket(websocket, target_url, manager, route.session_id)
    except (ConnectionClosed, WebSocketDisconnect, OSError):
        return
