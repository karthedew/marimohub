from datetime import UTC, datetime
import logging
from typing import cast
from uuid import UUID, uuid4

from fastapi import FastAPI, Request, WebSocket
from fastapi.testclient import TestClient
import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.responses import Response
from starlette.websockets import WebSocketDisconnect

from app.api import deployments as deployments_module
from app.api.deployments import DeploymentResolver
from app.api.proxy import SessionResolver, edit_save_callback
from app.core.errors import DomainError
from app.models import Deployment, Notebook
from app.services import marimo_proxy
from app.services.marimo_proxy import (
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

    async def spawn_deployment(
        self, notebook: Notebook, deployment_id: UUID, slug: str
    ) -> SessionInfo:
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

    async def spawn_deployment(
        self, notebook: Notebook, deployment_id: UUID, slug: str
    ) -> SessionInfo:
        self.spawn_calls.append((notebook.id, deployment_id, slug))
        self.woken = True
        return SessionInfo(
            id=deployment_id,
            notebook_id=notebook.id,
            mode="deploy",
            phase=SessionPhase.READY,
            last_active=datetime.now(UTC),
            creator_id=None,
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


@pytest.mark.asyncio
async def test_deployment_resolver_fast_path_skips_spawn_when_already_routable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    deployment = Deployment(id=uuid4(), notebook_id=uuid4(), slug="s")
    notebook = Notebook(id=deployment.notebook_id, workspace_id=uuid4(), title="T", source="x = 1")

    async def fake_load(db: object, slug: str) -> tuple[Deployment, Notebook]:
        return deployment, notebook

    monkeypatch.setattr(deployments_module, "_load_active_deployment", fake_load)
    manager = _FakeManager(target=_TARGET)
    resolver = DeploymentResolver(cast("AsyncSession", None), cast("SessionManager", manager), "s")

    route = await resolver.resolve()

    assert route == GatewayRoute(deployment.id, _TARGET)
    assert manager.spawn_calls == []


@pytest.mark.asyncio
async def test_deployment_resolver_wakes_exactly_once_when_sleeping(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    deployment = Deployment(id=uuid4(), notebook_id=uuid4(), slug="s")
    notebook = Notebook(id=deployment.notebook_id, workspace_id=uuid4(), title="T", source="x = 1")

    async def fake_load(db: object, slug: str) -> tuple[Deployment, Notebook]:
        return deployment, notebook

    monkeypatch.setattr(deployments_module, "_load_active_deployment", fake_load)
    manager = _WakingManager(target_after_wake=_TARGET)
    resolver = DeploymentResolver(cast("AsyncSession", None), cast("SessionManager", manager), "s")

    route = await resolver.resolve()

    assert route == GatewayRoute(deployment.id, _TARGET)
    assert manager.spawn_calls == [(notebook.id, deployment.id, "s")]


@pytest.mark.asyncio
async def test_deployment_resolver_raises_upstream_not_ready_when_still_unroutable_after_wake(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    deployment = Deployment(id=uuid4(), notebook_id=uuid4(), slug="s")
    notebook = Notebook(id=deployment.notebook_id, workspace_id=uuid4(), title="T", source="x = 1")

    async def fake_load(db: object, slug: str) -> tuple[Deployment, Notebook]:
        return deployment, notebook

    monkeypatch.setattr(deployments_module, "_load_active_deployment", fake_load)
    manager = _WakingManager(target_after_wake=None)
    resolver = DeploymentResolver(cast("AsyncSession", None), cast("SessionManager", manager), "s")

    with pytest.raises(UpstreamNotReady):
        await resolver.resolve()

    assert manager.spawn_calls == [(notebook.id, deployment.id, "s")]


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

    def fake_connect(url: str) -> FakeUpstream:
        connected_urls.append(url)
        return FakeUpstream()

    monkeypatch.setattr(marimo_proxy.websockets, "connect", fake_connect)
    app = _build_ws_app(cast("SessionManager", manager), resolver)

    with TestClient(app) as client, client.websocket_connect("/ws") as websocket:
        websocket.send_text("hello")
        assert websocket.receive_text() == "upstream:hello"

    assert connected_urls == ["ws://upstream/ws?access_token=t"]
    assert resolver.calls == 1
    # One connect-edge mark_active plus one for the client frame and one for the
    # upstream echo frame the fake relay produces.
    assert manager.mark_active_calls == [session_id, session_id, session_id]


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
