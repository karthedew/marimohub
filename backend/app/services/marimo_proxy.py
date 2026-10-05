import asyncio
from collections.abc import Awaitable, Callable
import contextlib
from dataclasses import dataclass
from typing import Protocol, cast
from urllib.parse import quote
from uuid import UUID

from fastapi import Request, WebSocket, WebSocketDisconnect, status
import httpx
from starlette.background import BackgroundTask
from starlette.responses import Response, StreamingResponse
from starlette.websockets import WebSocketState
import websockets
from websockets.asyncio.client import ClientConnection
from websockets.exceptions import ConnectionClosed
from websockets.frames import EXTERNAL_CLOSE_CODES, CloseCode

from app.core.config import get_settings
from app.core.errors import DomainError
from app.services.session_manager import SessionManager, SessionTarget

# The single method allow-list both routers register, and the set whose request
# body is streamed upstream (every body-capable method, i.e. everything but GET/HEAD).
PROXY_METHODS = ["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"]
_BODY_METHODS = {"POST", "PUT", "PATCH", "DELETE", "OPTIONS"}

# httpx's default is a flat 5s for everything, which turned ordinary slow
# responses into 502s. A browser's first load of a notebook fans out ~190
# module requests that one marimo event loop answers in turn, so the tail
# waited more than 5s, and exports or file downloads legitimately take longer.
# Once connected, a response may take as long as marimo needs. Connecting is
# bounded at 10 s, room for a couple of dropped-SYN retransmits (1 s, 3 s,
# 7 s) during a burst of new connections. A Runtime that is really gone still
# fails fast: a Service with no endpoints rejects the connection at once.
_UPSTREAM_TIMEOUT = httpx.Timeout(connect=10.0, read=300.0, write=60.0, pool=5.0)

# marimo sends a cell's whole output as one WebSocket message, so a large
# chart, table or HTML output easily outgrows the websockets library's
# default 1 MiB limit, and a message over the limit ends the connection
# (1009) with the output lost. The cap stays finite so that one message
# cannot take unbounded memory.
UPSTREAM_WS_MAX_MESSAGE_BYTES = 64 * 2**20

# The close codes a close frame may carry: the defined ones websockets will
# send, plus 3000-4999, which RFC 6455 leaves to libraries and applications
# (marimo closes an unauthorized client with 3000).
_SENDABLE_CLOSE_CODES = frozenset(EXTERNAL_CLOSE_CODES) | frozenset(range(3000, 5000))

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


class ActivityLease:
    """Keeps signaling activity for a Runtime across the full life of a proxied connection.

    A single request/response edge or WebSocket frame already calls
    `mark_active` once at that edge, but a long-lived stream (a large
    download, an open WebSocket with quiet stretches) needs more than edge
    signals: the operator's idle clock only ever sees what this gateway
    tells it. This periodically re-signals for as long as it stays open,
    independent of whether any traffic crosses it in that window.
    """

    def __init__(self, manager: SessionManager, session_id: UUID, interval_seconds: float) -> None:
        """Configure the runtime to signal for and how often, without starting yet."""
        self._manager = manager
        self._session_id = session_id
        self._interval_seconds = interval_seconds
        self._task: asyncio.Task[None] | None = None

    def start(self) -> None:
        """Begin the periodic signal; call exactly once per connection."""
        self._task = asyncio.create_task(self._pulse())

    async def _pulse(self) -> None:
        while True:
            await asyncio.sleep(self._interval_seconds)
            await self._manager.mark_active(self._session_id)

    async def stop(self) -> None:
        """Cancel the periodic signal; safe to call more than once."""
        if self._task is None:
            return
        task, self._task = self._task, None
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


class _RawHeaders(Protocol):
    """Structural type for header containers exposing raw byte pairs."""

    @property
    def raw(self) -> list[tuple[bytes, bytes]]: ...


# The browser's own credentials never reach a Runtime. That covers every
# cookie it sends: MarimoHub's login cookie, other apps' cookies on a shared
# host such as localhost, and marimo cookies signed by an earlier process of
# the same Runtime, whose random signing secret died with it (every
# Deployment wake starts a new process). It also covers its Authorization
# header, which may hold MarimoHub's own bearer JWT. Instead, the gateway
# authenticates every upstream request itself with the Runtime's token, as a
# bearer header that marimo checks on every request. That way nothing about
# a Runtime's auth depends on browser state, and the token never appears in
# a URL.
_REQUEST_HEADERS_NOT_FORWARDED = _HOP_BY_HOP_HEADERS | {"host", "cookie", "authorization"}

# marimo answers a bearer-authenticated request with its own session cookie.
# The browser could only ever send that cookie back to be dropped above, so
# it is dropped here too rather than piling up in the jar, one per Runtime.
# Content-Length is recomputed for the re-streamed body.
_RESPONSE_HEADERS_NOT_FORWARDED = _HOP_BY_HOP_HEADERS | {"set-cookie", "content-length"}


def _forwardable_header_pairs(
    headers: _RawHeaders, blocked: frozenset[str] | set[str]
) -> list[tuple[bytes, bytes]]:
    return [
        (key, value) for key, value in headers.raw if key.decode("latin-1").lower() not in blocked
    ]


def _upstream_request_headers(headers: _RawHeaders, access_token: str) -> list[tuple[str, str]]:
    """Return the browser's headers minus its credentials, plus the Runtime's own bearer token."""
    forwarded = [
        (key.decode("latin-1"), value.decode("latin-1"))
        for key, value in _forwardable_header_pairs(headers, _REQUEST_HEADERS_NOT_FORWARDED)
    ]
    return [*forwarded, ("Authorization", f"Bearer {access_token}")]


def _client_response_header_pairs(headers: _RawHeaders) -> list[tuple[bytes, bytes]]:
    return _forwardable_header_pairs(headers, _RESPONSE_HEADERS_NOT_FORWARDED)


def _build_target_url(base_url: str, path: str, query_string: bytes) -> str:
    """Build the upstream URL for `path`, keeping the browser's query string unchanged."""
    url = f"{base_url}/{quote(path, safe='/')}"
    if query_string:
        return f"{url}?{query_string.decode('latin-1')}"
    return url


ResponseBodyCallback = Callable[[httpx.Response, bytes], Awaitable[None]]


async def _request_body(request: Request) -> bytes | None:
    if request.method not in _BODY_METHODS:
        return None
    return await request.body()


async def _close_upstream_and_release_lease(
    client: httpx.AsyncClient, response: httpx.Response, lease: ActivityLease
) -> None:
    await response.aclose()
    await client.aclose()
    await lease.stop()


async def _send_with_connect_retry(
    client: httpx.AsyncClient,
    request: httpx.Request,
    *,
    stream: bool,
    follow_redirects: bool,
) -> httpx.Response:
    """Send `request`, retrying once if the connection itself could not be made.

    A failed connect means no byte of the request reached the Runtime, so
    resending is safe for every method, body included (the body is buffered
    bytes). A notebook page load fans out ~190 requests, each on a new
    connection. Under a burst of 50 users opening notebooks, an occasional
    connect stalled past its timeout and the user got a 502, where one fresh
    attempt gets through.
    """
    try:
        return await client.send(request, stream=stream, follow_redirects=follow_redirects)
    except (httpx.ConnectError, httpx.ConnectTimeout):
        return await client.send(request, stream=stream, follow_redirects=follow_redirects)


async def _forward_http(
    request: Request,
    target: SessionTarget,
    path: str,
    *,
    lease: ActivityLease,
    response_body_callback: ResponseBodyCallback | None = None,
    follow_redirects: bool = False,
) -> Response:
    """Proxy an HTTP request to the notebook process and return its response.

    `lease` must stay alive for as long as the response body is still being
    read: a buffered response stops it before returning, while a streamed
    one hands it to the same background task that closes the upstream
    connection, so it keeps signaling for the whole download.
    """
    client = httpx.AsyncClient(follow_redirects=follow_redirects, timeout=_UPSTREAM_TIMEOUT)
    body = await _request_body(request)
    query_string = cast("bytes", request.scope.get("query_string", b""))
    upstream_request = client.build_request(
        request.method,
        _build_target_url(target.http_base_url, path, query_string),
        headers=_upstream_request_headers(request.headers, target.access_token),
        content=body,
    )
    try:
        upstream_response = await _send_with_connect_retry(
            client,
            upstream_request,
            stream=response_body_callback is None,
            follow_redirects=follow_redirects,
        )
    except httpx.HTTPError as exc:
        await client.aclose()
        await lease.stop()
        raise UpstreamUnreachable("Notebook process is unreachable") from exc

    if response_body_callback is not None:
        response_body = upstream_response.content
        await response_body_callback(upstream_response, response_body)
        response = Response(content=response_body, status_code=upstream_response.status_code)
        response.raw_headers = _client_response_header_pairs(upstream_response.headers)
        await upstream_response.aclose()
        await client.aclose()
        await lease.stop()
        return response

    response = StreamingResponse(
        upstream_response.aiter_raw(),
        status_code=upstream_response.status_code,
        background=BackgroundTask(
            _close_upstream_and_release_lease, client, upstream_response, lease
        ),
    )
    response.raw_headers = _client_response_header_pairs(upstream_response.headers)
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


def _client_close_frame(upstream: ClientConnection) -> tuple[int, str]:
    """Return the close code and reason that end the browser's socket as the Runtime ended its own.

    marimo's frontend acts on both (a reason such as MARIMO_ALREADY_CONNECTED
    tells it another tab holds the session, rather than to reconnect). Codes
    no close frame may carry are mapped: 1005 (closed without a code) to a
    normal close, and 1006 (the connection ended without a close frame) or
    any other to 1014, a gateway that lost its upstream.
    """
    code = upstream.close_code
    if code is not None and code in _SENDABLE_CLOSE_CODES:
        return code, upstream.close_reason or ""
    if code == CloseCode.NO_STATUS_RCVD:
        return CloseCode.NORMAL_CLOSURE, ""
    return CloseCode.BAD_GATEWAY, ""


async def _relay_websocket(
    websocket: WebSocket,
    target_url: str,
    access_token: str,
    manager: SessionManager,
    session_id: UUID,
) -> None:
    """Bridge the client WebSocket to the upstream notebook WebSocket bidirectionally.

    When the Runtime ends the connection, the browser's socket is closed with
    the same code and reason.
    """
    await websocket.accept()
    async with websockets.connect(
        target_url,
        additional_headers={"Authorization": f"Bearer {access_token}"},
        max_size=UPSTREAM_WS_MAX_MESSAGE_BYTES,
    ) as upstream:
        tasks = [
            asyncio.create_task(_client_to_upstream(websocket, upstream, manager, session_id)),
            asyncio.create_task(_upstream_to_client(websocket, upstream, manager, session_id)),
        ]
        _done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in pending:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
    # Both directions are done and the upstream is closed. Unless the browser
    # left first (or could no longer be written to), it is the Runtime that
    # ended the connection.
    if (
        websocket.client_state is WebSocketState.CONNECTED
        and websocket.application_state is WebSocketState.CONNECTED
    ):
        code, reason = _client_close_frame(upstream)
        await websocket.close(code=code, reason=reason)


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
    lease = ActivityLease(
        manager, route.session_id, get_settings().ACTIVITY_SIGNAL_INTERVAL_SECONDS
    )
    lease.start()
    try:
        return await _forward_http(
            request,
            route.target,
            path,
            lease=lease,
            response_body_callback=response_body_callback,
            follow_redirects=follow_redirects,
        )
    except BaseException:
        # `_forward_http` already stops the lease on every path it itself
        # returns or raises from; this only catches something unexpected
        # above it. `ActivityLease.stop` is idempotent either way.
        await lease.stop()
        raise


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
    lease = ActivityLease(
        manager, route.session_id, get_settings().ACTIVITY_SIGNAL_INTERVAL_SECONDS
    )
    lease.start()
    target_url = _build_target_url(
        route.target.ws_base_url, "ws", cast("bytes", websocket.scope.get("query_string", b""))
    )
    try:
        await _relay_websocket(
            websocket, target_url, route.target.access_token, manager, route.session_id
        )
    except (ConnectionClosed, WebSocketDisconnect, OSError):
        return
    finally:
        await lease.stop()
