import asyncio
from collections.abc import AsyncGenerator, Awaitable, Callable, Generator
import contextlib
from datetime import UTC, datetime
import logging
import os
import socket
from types import SimpleNamespace
from typing import cast
from uuid import UUID, uuid4

from fastapi import FastAPI, Request, WebSocket
from fastapi.testclient import TestClient
import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.responses import Response
from starlette.websockets import WebSocketDisconnect
import uvicorn
from websockets.asyncio.client import ClientConnection, connect as connect_websocket
from websockets.asyncio.server import ServerConnection, serve as serve_websocket
from websockets.exceptions import ConnectionClosed

from app.api.deployments import DeploymentResolver
from app.api.proxy import SessionResolver, edit_save_callback
from app.core.config import get_settings
from app.core.errors import DomainError
from app.models import Deployment, DeploymentDesiredState, Notebook, Workspace
from app.services import marimo_proxy
from app.services.marimo_proxy import (
    ActivityLease,
    GatewayError,
    GatewayRoute,
    Resolver,
    UpstreamNotFound,
    UpstreamNotReady,
    UpstreamUnreachable,
)
from app.services.notebook_storage import NotebookStorageService
from app.services.session_manager import (
    RuntimeMode,
    SessionInfo,
    SessionManager,
    SessionPhase,
    SessionTarget,
)

_RUNTIME_IMAGE = "registry.example/marimo-runtime@sha256:" + "0" * 64

# A fake upstream access token, routed through a constant so the value is never a
# string literal at the sensitive call site below.
_UPSTREAM_AUTH = "t"
_TARGET = SessionTarget(
    http_base_url="http://upstream", ws_base_url="ws://upstream", access_token=_UPSTREAM_AUTH
)


class _FakeManager:
    """Implements the `SessionManager` protocol; only `mark_active` is exercised by most tests."""

    def __init__(
        self,
        *,
        target: SessionTarget | None = None,
        info: SessionInfo | None = None,
    ) -> None:
        self.target_value = target
        self.info_value = info
        self.mark_active_calls: list[UUID] = []
        self.spawn_calls: list[tuple[UUID, UUID, str]] = []

    async def spawn(
        self, notebook: Notebook, mode: str, creator_id: UUID | None = None
    ) -> SessionInfo:
        raise NotImplementedError

    async def spawn_deployment(self, notebook: Notebook, deployment: Deployment) -> SessionInfo:
        raise NotImplementedError

    async def get(self, session_id: UUID) -> SessionInfo | None:
        return self.info_value

    async def target(self, session_id: UUID) -> SessionTarget | None:
        return self.target_value

    async def mark_active(self, session_id: UUID) -> None:
        self.mark_active_calls.append(session_id)

    async def stop(self, session_id: UUID) -> None:
        raise NotImplementedError

    async def shutdown(self) -> None:
        return None


class _WakingManager(_FakeManager):
    """A manager whose `target()` only resolves after `spawn_deployment` has run once."""

    def __init__(self, *, target_after_wake: SessionTarget | None) -> None:
        super().__init__()
        self.target_after_wake = target_after_wake
        self.woken = False

    async def target(self, session_id: UUID) -> SessionTarget | None:
        return self.target_after_wake if self.woken else None

    async def spawn_deployment(self, notebook: Notebook, deployment: Deployment) -> SessionInfo:
        self.spawn_calls.append((notebook.id, deployment.id, deployment.slug))
        self.woken = True
        return SessionInfo(
            id=deployment.id,
            notebook_id=notebook.id,
            mode="deploy",
            phase=SessionPhase.READY,
            last_active=datetime.now(UTC),
            creator_id=None,
            deployment_revision=deployment.revision,
        )


class _StaticResolver:
    """A resolver that always returns a fixed route, recording each `resolve()` call."""

    def __init__(self, route: GatewayRoute) -> None:
        self.route = route
        self.calls = 0

    async def resolve(self) -> GatewayRoute:
        self.calls += 1
        return self.route


class _FailingResolver:
    def __init__(self, error: DomainError) -> None:
        self.error = error

    async def resolve(self) -> GatewayRoute:
        raise self.error


class _FakeStorage(NotebookStorageService):
    def __init__(self, *, fail: bool = False) -> None:
        super().__init__()
        self.fail = fail
        self.put_calls: list[tuple[Notebook, str | None]] = []

    async def get(self, notebook: Notebook) -> str | None:
        return notebook.source

    async def put(self, notebook: Notebook, source: str | None) -> None:
        if self.fail:
            raise RuntimeError("store unavailable")
        self.put_calls.append((notebook, source))

    async def delete(self, notebook_id: UUID) -> None:
        return None


class _FakeDb:
    def __init__(self, notebook: Notebook | None) -> None:
        self.notebook = notebook
        self.committed = False
        self.rolled_back = False

    async def get(self, model: type, pk: UUID) -> Notebook | None:
        assert model is Notebook
        return self.notebook

    async def commit(self) -> None:
        self.committed = True

    async def rollback(self) -> None:
        self.rolled_back = True


def _build_ws_app(manager: SessionManager, resolver: Resolver) -> FastAPI:
    app = FastAPI()

    @app.websocket("/ws")
    async def _ws(websocket: WebSocket) -> None:
        await marimo_proxy.proxy_websocket(websocket, manager, resolver)

    return app


# ── GatewayError taxonomy ────────────────────────────────────────────────────


def test_gateway_error_hierarchy_status_and_ws_close_code() -> None:
    assert issubclass(GatewayError, DomainError)
    assert UpstreamNotFound.status == 404
    assert UpstreamNotFound.ws_close_code == 1008
    assert UpstreamNotReady.status == 503
    assert UpstreamNotReady.ws_close_code == 1011
    assert UpstreamUnreachable.status == 502
    assert UpstreamUnreachable.ws_close_code == 1011


# ── SessionResolver ───────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_session_resolver_raises_upstream_not_found_when_target_missing() -> None:
    manager = _FakeManager(target=None)
    resolver = SessionResolver(cast("SessionManager", manager), uuid4())

    with pytest.raises(UpstreamNotFound):
        await resolver.resolve()


@pytest.mark.asyncio
async def test_session_resolver_returns_route_when_target_present() -> None:
    manager = _FakeManager(target=_TARGET)
    session_id = uuid4()
    resolver = SessionResolver(cast("SessionManager", manager), session_id)

    route = await resolver.resolve()

    assert route == GatewayRoute(session_id, _TARGET)


# ── DeploymentResolver — resolve-or-wake ordering ────────────────────────────


async def _active_deployment(db_session: AsyncSession, *, slug: str) -> tuple[Notebook, Deployment]:
    """Commit a real, active Deployment row (workspace/notebook included) for resolver tests.

    `DeploymentResolver.resolve()` now locks the owning Workspace/Deployment
    through real Postgres row locks when it needs to wake, so these tests
    need real committed rows, not bare in-memory objects.
    """
    workspace = Workspace(slug=f"resolver-{slug}", name="Resolver")
    db_session.add(workspace)
    await db_session.flush()
    notebook = Notebook(workspace_id=workspace.id, title="T", source="x = 1")
    db_session.add(notebook)
    await db_session.flush()
    deployment = Deployment(
        notebook_id=notebook.id,
        slug=slug,
        desired_state=DeploymentDesiredState.ACTIVE,
        source_snapshot="x = 1",
        source_sha256="deadbeef",
        runtime_image=_RUNTIME_IMAGE,
        revision=1,
    )
    db_session.add(deployment)
    await db_session.commit()
    return notebook, deployment


@pytest.mark.asyncio
async def test_deployment_resolver_fast_path_skips_spawn_when_already_routable(
    db_session: AsyncSession,
) -> None:
    notebook, deployment = await _active_deployment(db_session, slug="fast-path")
    info = SessionInfo(
        id=deployment.id,
        notebook_id=notebook.id,
        mode="deploy",
        phase=SessionPhase.READY,
        last_active=datetime.now(UTC),
        creator_id=None,
        deployment_revision=deployment.revision,
    )
    manager = _FakeManager(target=_TARGET, info=info)
    resolver = DeploymentResolver(db_session, cast("SessionManager", manager), "fast-path")

    route = await resolver.resolve()

    assert route == GatewayRoute(deployment.id, _TARGET)
    assert manager.spawn_calls == []


@pytest.mark.asyncio
async def test_deployment_resolver_wakes_exactly_once_when_sleeping(
    db_session: AsyncSession,
) -> None:
    notebook, deployment = await _active_deployment(db_session, slug="wake-once")
    manager = _WakingManager(target_after_wake=_TARGET)
    resolver = DeploymentResolver(db_session, cast("SessionManager", manager), "wake-once")

    route = await resolver.resolve()

    assert route == GatewayRoute(deployment.id, _TARGET)
    assert manager.spawn_calls == [(notebook.id, deployment.id, "wake-once")]


@pytest.mark.asyncio
async def test_deployment_resolver_raises_upstream_not_ready_when_still_unroutable_after_wake(
    db_session: AsyncSession,
) -> None:
    notebook, deployment = await _active_deployment(db_session, slug="never-ready")
    manager = _WakingManager(target_after_wake=None)
    resolver = DeploymentResolver(db_session, cast("SessionManager", manager), "never-ready")

    with pytest.raises(UpstreamNotReady):
        await resolver.resolve()

    assert manager.spawn_calls == [(notebook.id, deployment.id, "never-ready")]


# ── proxy_http: resolve → mark_active → forward ordering ────────────────────


class _EventManager(_FakeManager):
    """Records each `mark_active` call into a shared, ordering-sensitive event log."""

    def __init__(self, events: list[str]) -> None:
        super().__init__()
        self.events = events

    async def mark_active(self, session_id: UUID) -> None:
        self.events.append("mark_active")


@pytest.mark.asyncio
async def test_proxy_http_resolves_marks_active_then_forwards_once_per_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    session_id = uuid4()
    manager = _EventManager(events)

    class _RecordingResolver:
        async def resolve(self) -> GatewayRoute:
            events.append("resolve")
            return GatewayRoute(session_id, _TARGET)

    async def fake_forward(
        request: object,
        target: SessionTarget,
        path: str,
        *,
        lease: object = None,
        response_body_callback: object = None,
        follow_redirects: bool = False,
    ) -> Response:
        events.append("forward")
        return Response(status_code=200)

    monkeypatch.setattr(marimo_proxy, "_forward_http", fake_forward)

    await marimo_proxy.proxy_http(
        cast("Request", object()), cast("SessionManager", manager), _RecordingResolver(), "p"
    )
    await marimo_proxy.proxy_http(
        cast("Request", object()), cast("SessionManager", manager), _RecordingResolver(), "p"
    )

    assert events == ["resolve", "mark_active", "forward"] * 2


@pytest.mark.asyncio
async def test_proxy_http_propagates_gateway_error_from_resolver() -> None:
    manager = _FakeManager()
    resolver = _FailingResolver(UpstreamNotFound("gone"))

    with pytest.raises(UpstreamNotFound):
        await marimo_proxy.proxy_http(
            cast("Request", object()), cast("SessionManager", manager), resolver, "p"
        )


# ── _forward_http: the gateway authenticates upstream, the browser never does ─

_SESSION_ID = UUID("6f1c1d0e-5a4b-4c3d-8e2f-0123456789ab")
_RUNTIME_ORIGIN = "http://msess-6f1c1d0e.marimohub-sessions.svc:8080"
# The browser's own credentials: MarimoHub's login cookie and bearer JWT, a
# cookie from another app on the same host, and a marimo cookie signed by an
# earlier process of this same Runtime (which a woken Deployment can no
# longer verify). None of them may reach the Runtime.
_BROWSER_COOKIES = (
    "__Host-marimohub_oidc=signed-login-state; theme=dark; "
    f"session_8080_api_proxy_{_SESSION_ID}=stale-signature"
)
_BROWSER_BEARER = "Bearer marimohub-app-jwt"


def _browser_request(*, query_string: bytes = b"") -> Request:
    headers = [
        (b"cookie", _BROWSER_COOKIES.encode("latin-1")),
        (b"authorization", _BROWSER_BEARER.encode("latin-1")),
        (b"x-molab-test", b"kept"),
    ]
    return Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/",
            "headers": headers,
            "query_string": query_string,
        }
    )


async def _forward_and_record(
    monkeypatch: pytest.MonkeyPatch, request: Request, target: SessionTarget
) -> tuple[httpx.Request, Response]:
    """Forward `request` to a recording stand-in for the Runtime; return what it got and sent."""
    seen: list[httpx.Request] = []
    real_async_client = httpx.AsyncClient

    def _recording_client(*, follow_redirects: bool, timeout: httpx.Timeout) -> httpx.AsyncClient:
        def _runtime(upstream_request: httpx.Request) -> httpx.Response:
            seen.append(upstream_request)
            # What marimo answers a bearer-authenticated request with.
            return httpx.Response(
                200,
                headers=[
                    ("set-cookie", f"session_8080_api_proxy_{_SESSION_ID}=fresh; Path=/"),
                    ("x-upstream", "ok"),
                ],
            )

        return real_async_client(
            transport=httpx.MockTransport(_runtime),
            follow_redirects=follow_redirects,
            timeout=timeout,
        )

    async def _ignore_body(response: httpx.Response, body: bytes) -> None:
        return None

    monkeypatch.setattr(marimo_proxy.httpx, "AsyncClient", _recording_client)
    lease = ActivityLease(cast("SessionManager", _FakeManager()), _SESSION_ID, interval_seconds=60)

    response = await marimo_proxy._forward_http(
        request, target, "", lease=lease, response_body_callback=_ignore_body
    )

    (upstream_request,) = seen
    return upstream_request, response


_PROXY_TARGET = SessionTarget(
    http_base_url=f"{_RUNTIME_ORIGIN}/api/proxy/{_SESSION_ID}",
    ws_base_url=f"ws://msess-6f1c1d0e.marimohub-sessions.svc:8080/api/proxy/{_SESSION_ID}",
    access_token=_UPSTREAM_AUTH,
)


@pytest.mark.asyncio
async def test_forward_http_authenticates_with_the_runtime_token_and_never_the_browsers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Upstream gets the Runtime's bearer token and none of the browser's credentials.

    That is what keeps a returning visitor working after a Deployment wakes
    (marimo's earlier cookie can no longer be verified), keeps unrelated
    cookies on a shared host from mattering, and keeps MarimoHub's own login
    cookie and JWT away from notebook code.
    """
    upstream, _ = await _forward_and_record(
        monkeypatch, _browser_request(query_string=b"metric=cpu"), _PROXY_TARGET
    )

    assert upstream.headers.get_list("authorization") == [f"Bearer {_UPSTREAM_AUTH}"]
    assert "cookie" not in upstream.headers
    assert upstream.headers["x-molab-test"] == "kept"
    assert upstream.url.path == f"/api/proxy/{_SESSION_ID}/"
    assert dict(upstream.url.params) == {"metric": "cpu"}


@pytest.mark.asyncio
async def test_forward_http_drops_marimos_session_cookie_from_the_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, response = await _forward_and_record(monkeypatch, _browser_request(), _PROXY_TARGET)

    header_names = {key.decode("latin-1").lower() for key, _ in response.raw_headers}
    assert "set-cookie" not in header_names
    assert "x-upstream" in header_names


# ── proxy_websocket: close-code mapping + connect/frame mark_active ─────────


@pytest.mark.parametrize(
    ("error", "expected_code"),
    [
        (UpstreamNotFound("missing"), 1008),
        (UpstreamNotReady("not ready"), 1011),
        (UpstreamUnreachable("unreachable"), 1011),
    ],
)
def test_proxy_websocket_closes_with_mapped_code_on_resolve_failure(
    error: DomainError, expected_code: int
) -> None:
    manager = _FakeManager()
    app = _build_ws_app(cast("SessionManager", manager), _FailingResolver(error))
    client = TestClient(app)

    with pytest.raises(WebSocketDisconnect) as exc_info, client.websocket_connect("/ws"):
        pass

    assert exc_info.value.code == expected_code
    assert manager.mark_active_calls == []


def test_proxy_websocket_marks_active_at_connect_and_per_frame(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session_id = uuid4()
    manager = _FakeManager()
    resolver = _StaticResolver(GatewayRoute(session_id, _TARGET))
    connected_urls: list[str] = []
    connected_headers: list[dict[str, str]] = []

    class FakeUpstream:
        def __init__(self) -> None:
            self._messages: list[str | bytes] = []

        async def __aenter__(self) -> "FakeUpstream":
            return self

        async def __aexit__(self, exc_type: object, exc: object, tb: object) -> None:
            return None

        async def send(self, message: str | bytes) -> None:
            self._messages.append(f"upstream:{message}" if isinstance(message, str) else message)

        async def close(self) -> None:
            return None

        def __aiter__(self) -> "FakeUpstream":
            return self

        async def __anext__(self) -> str | bytes:
            while not self._messages:
                await marimo_proxy.asyncio.sleep(0)
            return self._messages.pop(0)

    def fake_connect(
        url: str, *, additional_headers: dict[str, str], max_size: int | None
    ) -> FakeUpstream:
        connected_urls.append(url)
        connected_headers.append(additional_headers)
        connected_max_sizes.append(max_size)
        return FakeUpstream()

    connected_max_sizes: list[int | None] = []
    monkeypatch.setattr(marimo_proxy.websockets, "connect", fake_connect)
    app = _build_ws_app(cast("SessionManager", manager), resolver)

    with TestClient(app) as client, client.websocket_connect("/ws") as websocket:
        websocket.send_text("hello")
        assert websocket.receive_text() == "upstream:hello"

    assert connected_urls == ["ws://upstream/ws"]
    assert connected_headers == [{"Authorization": f"Bearer {_UPSTREAM_AUTH}"}]
    assert connected_max_sizes == [64 * 2**20]
    assert resolver.calls == 1
    # One connect-edge mark_active plus one for the client frame and one for the
    # upstream echo frame the fake relay produces.
    assert manager.mark_active_calls == [session_id, session_id, session_id]


# ── the WebSocket relay end to end: a real Runtime-side server, a real gateway ─


@pytest.fixture
def _gateway_settings(monkeypatch: pytest.MonkeyPatch) -> Generator[None, None, None]:
    """Settings the gateway can load whichever tests ran before (it reads the lease interval)."""
    for name, default in (
        ("DATABASE_URL", "postgresql+asyncpg://molab:molab@localhost:5432/molab_test"),
        ("SECRET_KEY", "test-secret-with-at-least-32-bytes"),
    ):
        monkeypatch.setenv(name, os.environ.get(name, default))
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@contextlib.asynccontextmanager
async def _relay_in_front_of(
    runtime: Callable[[ServerConnection], Awaitable[None]],
) -> AsyncGenerator[str, None]:
    """Serve `runtime` as a Runtime's WebSocket behind the real relay; yield the browser's URL.

    The relay runs in a real Uvicorn server, as in production, so frames and
    close codes cross real sockets in both hops.
    """
    async with serve_websocket(runtime, "127.0.0.1", 0) as upstream:
        port = upstream.sockets[0].getsockname()[1]
        target = SessionTarget(
            http_base_url=f"http://127.0.0.1:{port}",
            ws_base_url=f"ws://127.0.0.1:{port}",
            access_token=_UPSTREAM_AUTH,
        )
        app = _build_ws_app(
            cast("SessionManager", _FakeManager()), _StaticResolver(GatewayRoute(uuid4(), target))
        )
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            gateway = uvicorn.Server(uvicorn.Config(app, lifespan="off", log_level="warning"))
            serving = asyncio.create_task(gateway.serve(sockets=[listener]))
            try:
                for _ in range(500):  # up to 5 s
                    if gateway.started:
                        break
                    await asyncio.sleep(0.01)
                yield f"ws://127.0.0.1:{listener.getsockname()[1]}/ws"
            finally:
                gateway.should_exit = True
                await serving


# A cell output the size of a large chart: more than the websockets library's
# default 1 MiB message limit.
_BIG_OUTPUT = "x" * (3 * 2**19)


@pytest.mark.asyncio
@pytest.mark.usefixtures("_gateway_settings")
async def test_websocket_relay_delivers_a_message_over_one_mib() -> None:
    async def runtime(connection: ServerConnection) -> None:
        await connection.send(_BIG_OUTPUT)
        await connection.wait_closed()

    async with (
        _relay_in_front_of(runtime) as url,
        connect_websocket(url, max_size=None) as browser,
    ):
        message = await asyncio.wait_for(browser.recv(), timeout=10)

    assert message == _BIG_OUTPUT


@pytest.mark.asyncio
@pytest.mark.usefixtures("_gateway_settings")
async def test_websocket_relay_closes_the_browser_with_the_runtimes_code_and_reason() -> None:
    async def runtime(connection: ServerConnection) -> None:
        # What marimo answers a second tab opening the same session with.
        await connection.close(code=1003, reason="MARIMO_ALREADY_CONNECTED")

    async with _relay_in_front_of(runtime) as url, connect_websocket(url) as browser:
        with pytest.raises(ConnectionClosed):
            await asyncio.wait_for(browser.recv(), timeout=10)

    assert browser.close_code == 1003
    assert browser.close_reason == "MARIMO_ALREADY_CONNECTED"


@pytest.mark.asyncio
@pytest.mark.usefixtures("_gateway_settings")
async def test_websocket_relay_reports_a_runtime_lost_mid_connection_as_bad_gateway() -> None:
    async def runtime(connection: ServerConnection) -> None:
        connection.transport.abort()  # the Runtime's process dies mid-connection

    async with _relay_in_front_of(runtime) as url, connect_websocket(url) as browser:
        with pytest.raises(ConnectionClosed):
            await asyncio.wait_for(browser.recv(), timeout=10)

    assert browser.close_code == 1014


@pytest.mark.parametrize(
    ("runtime_code", "runtime_reason", "browser_close"),
    [
        (1000, "", (1000, "")),
        (1001, "going away", (1001, "going away")),
        (1003, "MARIMO_ALREADY_CONNECTED", (1003, "MARIMO_ALREADY_CONNECTED")),
        (3000, "MARIMO_UNAUTHORIZED", (3000, "MARIMO_UNAUTHORIZED")),
        (1005, "", (1000, "")),  # a close frame without a code
        (1006, "", (1014, "")),  # no close frame at all
    ],
)
def test_client_close_frame_mirrors_the_runtime_or_maps_codes_no_frame_may_carry(
    runtime_code: int, runtime_reason: str, browser_close: tuple[int, str]
) -> None:
    upstream = SimpleNamespace(close_code=runtime_code, close_reason=runtime_reason)

    assert marimo_proxy._client_close_frame(cast("ClientConnection", upstream)) == browser_close


# ── edit_save_callback: guard conditions + swallow-on-failure ───────────────


def _session_info(mode: str, notebook_id: UUID) -> SessionInfo:
    return SessionInfo(
        id=uuid4(),
        notebook_id=notebook_id,
        mode=cast("RuntimeMode", mode),
        phase=SessionPhase.READY,
        last_active=datetime.now(UTC),
        creator_id=None,
    )


@pytest.mark.asyncio
async def test_edit_save_callback_persists_on_success_for_edit_mode() -> None:
    notebook = Notebook(id=uuid4(), workspace_id=uuid4(), title="T", source="old")
    info = _session_info("edit", notebook.id)
    manager = _FakeManager(info=info)
    storage = _FakeStorage()
    db = _FakeDb(notebook)
    callback = edit_save_callback(
        cast("SessionManager", manager), storage, cast("AsyncSession", db), info.id
    )

    await callback(httpx.Response(200), b"x = 2")

    assert storage.put_calls == [(notebook, "x = 2")]
    assert db.committed is True


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["run", "deploy"])
async def test_edit_save_callback_skips_non_edit_sessions(mode: str) -> None:
    notebook = Notebook(id=uuid4(), workspace_id=uuid4(), title="T", source="old")
    info = _session_info(mode, notebook.id)
    manager = _FakeManager(info=info)
    storage = _FakeStorage()
    db = _FakeDb(notebook)
    callback = edit_save_callback(
        cast("SessionManager", manager), storage, cast("AsyncSession", db), info.id
    )

    await callback(httpx.Response(200), b"x = 2")

    assert storage.put_calls == []
    assert db.committed is False


@pytest.mark.asyncio
async def test_edit_save_callback_skips_error_responses() -> None:
    notebook = Notebook(id=uuid4(), workspace_id=uuid4(), title="T", source="old")
    info = _session_info("edit", notebook.id)
    manager = _FakeManager(info=info)
    storage = _FakeStorage()
    db = _FakeDb(notebook)
    callback = edit_save_callback(
        cast("SessionManager", manager), storage, cast("AsyncSession", db), info.id
    )

    await callback(httpx.Response(400), b"x = 2")

    assert storage.put_calls == []
    assert db.committed is False


@pytest.mark.asyncio
async def test_edit_save_callback_skips_unknown_session() -> None:
    manager = _FakeManager(info=None)
    storage = _FakeStorage()
    db = _FakeDb(None)
    callback = edit_save_callback(
        cast("SessionManager", manager), storage, cast("AsyncSession", db), uuid4()
    )

    await callback(httpx.Response(200), b"x = 2")

    assert storage.put_calls == []
    assert db.committed is False


@pytest.mark.asyncio
async def test_edit_save_callback_swallows_persist_failure(
    caplog: pytest.LogCaptureFixture,
) -> None:
    notebook = Notebook(id=uuid4(), workspace_id=uuid4(), title="T", source="old")
    info = _session_info("edit", notebook.id)
    manager = _FakeManager(info=info)
    storage = _FakeStorage(fail=True)
    db = _FakeDb(notebook)
    callback = edit_save_callback(
        cast("SessionManager", manager), storage, cast("AsyncSession", db), info.id
    )

    with caplog.at_level(logging.ERROR):
        await callback(httpx.Response(200), b"x = 2")  # must not raise

    assert db.rolled_back is True
    assert db.committed is False
    assert any("edit-save persist failed" in message for message in caplog.messages)


# ── ActivityLease: keeps signaling for the connection's full duration ──────


@pytest.mark.asyncio
async def test_activity_lease_signals_periodically_until_stopped() -> None:
    manager = _FakeManager()
    session_id = uuid4()
    lease = ActivityLease(cast("SessionManager", manager), session_id, interval_seconds=0.01)

    lease.start()
    await asyncio.sleep(0.05)
    await lease.stop()
    fired_while_running = len(manager.mark_active_calls)

    await asyncio.sleep(0.05)  # after stop, no further signals should arrive

    assert fired_while_running > 0
    assert len(manager.mark_active_calls) == fired_while_running
    assert all(call == session_id for call in manager.mark_active_calls)


@pytest.mark.asyncio
async def test_activity_lease_stop_is_idempotent() -> None:
    manager = _FakeManager()
    lease = ActivityLease(cast("SessionManager", manager), uuid4(), interval_seconds=1.0)

    lease.start()
    await lease.stop()
    await lease.stop()  # must not raise or hang


@pytest.mark.asyncio
async def test_streamed_response_release_helper_closes_upstream_and_stops_lease() -> None:
    """The background task Starlette runs after draining a streamed response must release the lease.

    This is what actually keeps `ActivityLease` alive for a streamed
    response's *full* duration: `_forward_http` hands the lease to this same
    callback, so it is only stopped once the client has finished reading the
    body, not the instant the (still-streaming) response object is returned.
    """
    closed: list[str] = []

    class _FakeUpstreamResponse:
        async def aclose(self) -> None:
            closed.append("response")

    class _FakeHTTPXClient:
        async def aclose(self) -> None:
            closed.append("client")

    manager = _FakeManager()
    lease = ActivityLease(cast("SessionManager", manager), uuid4(), interval_seconds=1.0)
    lease.start()

    await marimo_proxy._close_upstream_and_release_lease(
        cast("httpx.AsyncClient", _FakeHTTPXClient()),
        cast("httpx.Response", _FakeUpstreamResponse()),
        lease,
    )

    assert closed == ["response", "client"]
    assert lease._task is None  # stopped, not merely requested to stop


# ── _forward_http: one retry when the connection itself fails ───────────────


async def _forward_with_flaky_connect(
    monkeypatch: pytest.MonkeyPatch, failures: list[Exception]
) -> tuple[int, Response | Exception]:
    """Forward a POST to a Runtime whose first connects raise `failures` in order."""
    attempts = 0
    real_async_client = httpx.AsyncClient

    def _flaky_client(*, follow_redirects: bool, timeout: httpx.Timeout) -> httpx.AsyncClient:
        def _runtime(upstream_request: httpx.Request) -> httpx.Response:
            nonlocal attempts
            attempts += 1
            if failures:
                raise failures.pop(0)
            assert upstream_request.content == b"payload"
            return httpx.Response(200, content=b"ok")

        return real_async_client(
            transport=httpx.MockTransport(_runtime),
            follow_redirects=follow_redirects,
            timeout=timeout,
        )

    async def _ignore_body(response: httpx.Response, body: bytes) -> None:
        return None

    monkeypatch.setattr(marimo_proxy.httpx, "AsyncClient", _flaky_client)
    request = Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/",
            "headers": [],
            "query_string": b"",
        },
        receive=_body_receiver(b"payload"),
    )
    lease = ActivityLease(cast("SessionManager", _FakeManager()), _SESSION_ID, interval_seconds=60)
    try:
        result: Response | Exception = await marimo_proxy._forward_http(
            request,
            _PROXY_TARGET,
            "api/kernel/instantiate",
            lease=lease,
            response_body_callback=_ignore_body,
        )
    except UpstreamUnreachable as exc:
        result = exc
    return attempts, result


def _body_receiver(body: bytes):  # noqa: ANN202 -- an ASGI receive callable
    async def receive() -> dict[str, object]:
        return {"type": "http.request", "body": body, "more_body": False}

    return receive


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure",
    [httpx.ConnectTimeout("connect timed out"), httpx.ConnectError("connection refused")],
    ids=["connect-timeout", "connect-error"],
)
async def test_forward_http_retries_once_when_the_connect_fails(
    monkeypatch: pytest.MonkeyPatch, failure: Exception
) -> None:
    """A failed connect sent nothing, so one resend (body included) is safe and usually succeeds."""
    attempts, result = await _forward_with_flaky_connect(monkeypatch, [failure])

    assert attempts == 2
    assert isinstance(result, Response)
    assert result.status_code == 200


@pytest.mark.asyncio
async def test_forward_http_gives_up_after_a_second_failed_connect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    attempts, result = await _forward_with_flaky_connect(
        monkeypatch, [httpx.ConnectTimeout("first"), httpx.ConnectTimeout("second")]
    )

    assert attempts == 2
    assert isinstance(result, UpstreamUnreachable)


@pytest.mark.asyncio
async def test_forward_http_never_retries_once_the_request_may_have_been_sent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A read timeout means the Runtime may have acted on the request; resending could repeat it."""
    attempts, result = await _forward_with_flaky_connect(
        monkeypatch, [httpx.ReadTimeout("no response")]
    )

    assert attempts == 1
    assert isinstance(result, UpstreamUnreachable)
