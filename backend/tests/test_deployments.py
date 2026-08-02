from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
import http.server
import socket
import socketserver
import threading
from typing import cast, override
from uuid import UUID, uuid4

from fastapi import WebSocket
from fastapi.testclient import TestClient
from httpx import AsyncClient
import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.api import deployments as deployments_module
from app.api.deployments import resolved_status
from app.db.database import get_db
from app.main import app
from app.models import Deployment, DeploymentDesiredState, Notebook, User, Workspace
from app.schemas.deployment import SLUG_MESSAGE, DeploymentStatus
from app.services import deployment_lifecycle, marimo_proxy
from app.services.session_manager import (
    NotebookStartupError,
    RuntimeRef,
    SessionCapacityError,
    SessionInfo,
    SessionManager,
    SessionMode,
    SessionNotFoundError,
    SessionPhase,
    SessionStartError,
    SessionTarget,
    get_session_manager,
)
from test_notebooks import create_notebook, publish_notebook, register_and_login

# A fake upstream access token, routed through a constant so the value is never a
# string literal at the sensitive call site below.
_UPSTREAM_AUTH = "secret"
_RUNTIME_IMAGE = "registry.example/marimo-runtime@sha256:" + "0" * 64


@dataclass
class _UpstreamServer:
    port: int
    requests: list[dict[str, object]]
    base_url: str


class FakeDeploymentSessionManager:
    def __init__(self, upstream_base_url: str = "http://127.0.0.1:1") -> None:
        super().__init__()
        self.upstream_base_url = upstream_base_url
        self.sessions: dict[UUID, SessionInfo] = {}
        self.targets: dict[UUID, SessionTarget] = {}
        self.spawn_error: Exception | None = None
        self.spawned: list[tuple[UUID, UUID, str]] = []
        self.stopped: list[UUID] = []
        self.touches: list[UUID] = []
        self.stopped_workspaces: list[UUID] = []

    async def spawn_deployment(self, notebook: Notebook, deployment: Deployment) -> SessionInfo:
        if self.spawn_error is not None:
            raise self.spawn_error
        session = SessionInfo(
            id=deployment.id,
            notebook_id=notebook.id,
            mode="deploy",
            phase=SessionPhase.READY,
            last_active=datetime.now(UTC),
            creator_id=None,
            deployment_revision=deployment.revision,
        )
        self.sessions[deployment.id] = session
        self.targets[deployment.id] = SessionTarget(
            http_base_url=f"{self.upstream_base_url}/api/deployments/{deployment.slug}",
            ws_base_url=f"ws://127.0.0.1:9000/api/deployments/{deployment.slug}",
            access_token=_UPSTREAM_AUTH,
        )
        self.spawned.append((notebook.id, deployment.id, deployment.slug))
        return session

    async def spawn(
        self, notebook: Notebook, mode: SessionMode, creator_id: UUID | None = None
    ) -> SessionInfo:
        raise NotImplementedError

    async def target(self, session_id: UUID) -> SessionTarget | None:
        return self.targets.get(session_id)

    async def get(self, session_id: UUID) -> SessionInfo | None:
        return self.sessions.get(session_id)

    async def mark_active(self, session_id: UUID) -> None:
        self.touches.append(session_id)

    async def stop(self, session_id: UUID) -> None:
        if session_id not in self.sessions and session_id not in self.targets:
            raise SessionNotFoundError("Runtime not found")
        self.stopped.append(session_id)
        self.sessions.pop(session_id, None)
        self.targets.pop(session_id, None)

    async def stop_workspace_sessions(self, workspace_id: UUID) -> None:
        self.stopped_workspaces.append(workspace_id)

    async def reconcilable_runtimes(self) -> list[RuntimeRef]:
        return []

    async def shutdown(self) -> None:
        return None


class _ThreadingServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return cast("int", sock.getsockname()[1])


@pytest.fixture
def redirecting_upstream_http_server() -> Iterator[_UpstreamServer]:
    requests: list[dict[str, object]] = []

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            requests.append({"path": self.path, "cookie": self.headers.get("Cookie")})
            if self.headers.get("Cookie") != "marimo-session=ready":
                self.send_response(303)
                self.send_header("Location", "/api/deployments/redirect-root/")
                self.send_header(
                    "Set-Cookie", "marimo-session=ready; Path=/api/deployments/redirect-root"
                )
                self.end_headers()
                return
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(b"deployment-html")

        @override
        def log_message(self, format: str, *args: object) -> None:
            pass

    port = _free_port()
    server = _ThreadingServer(("127.0.0.1", port), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield _UpstreamServer(port=port, requests=requests, base_url=f"http://127.0.0.1:{port}")
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


@pytest.fixture
def upstream_http_server() -> Iterator[_UpstreamServer]:
    requests: list[dict[str, object]] = []

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            requests.append({"path": self.path, "header": self.headers.get("X-Molab-Test")})
            self.send_response(200)
            self.send_header("X-Upstream", "ok")
            self.end_headers()
            self.wfile.write(b"deployment-response")

        def _record_body_request(self, method: str) -> None:
            length = int(self.headers.get("Content-Length", "0"))
            body = self.rfile.read(length)
            requests.append(
                {
                    "method": method,
                    "path": self.path,
                    "header": self.headers.get("X-Molab-Test"),
                    "body": body,
                }
            )
            self.send_response(200)
            self.send_header("X-Upstream", "ok")
            self.end_headers()
            self.wfile.write(b"deployment-response")

        def do_PUT(self) -> None:
            self._record_body_request("PUT")

        def do_PATCH(self) -> None:
            self._record_body_request("PATCH")

        def do_DELETE(self) -> None:
            self._record_body_request("DELETE")

        @override
        def log_message(self, format: str, *args: object) -> None:
            pass

    port = _free_port()
    server = _ThreadingServer(("127.0.0.1", port), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield _UpstreamServer(port=port, requests=requests, base_url=f"http://127.0.0.1:{port}")
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


@pytest.fixture
def fake_deployment_manager(
    upstream_http_server: _UpstreamServer,
) -> Iterator[FakeDeploymentSessionManager]:
    manager = FakeDeploymentSessionManager(upstream_http_server.base_url)
    app.dependency_overrides[get_session_manager] = lambda: manager
    try:
        yield manager
    finally:
        app.dependency_overrides.pop(get_session_manager, None)


class _ProjectionManager:
    """A `get`-only `SessionManager` double for `resolved_status` projection tests."""

    def __init__(self, info: SessionInfo | None) -> None:
        self.info = info
        self.get_calls: list[UUID] = []

    async def get(self, session_id: UUID) -> SessionInfo | None:
        self.get_calls.append(session_id)
        return self.info


def _live_info(deployment: Deployment, phase: SessionPhase) -> SessionInfo:
    return SessionInfo(
        id=deployment.id,
        notebook_id=deployment.notebook_id,
        mode="deploy",
        phase=phase,
        last_active=datetime.now(UTC),
        creator_id=None,
    )


def _active_deployment(slug: str) -> Deployment:
    return Deployment(
        id=uuid4(),
        notebook_id=uuid4(),
        slug=slug,
        desired_state=DeploymentDesiredState.ACTIVE,
        source_snapshot="x = 1",
        source_sha256="deadbeef",
        runtime_image=_RUNTIME_IMAGE,
        revision=1,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("phase", "expected"),
    [
        (SessionPhase.READY, DeploymentStatus.RUNNING),
        (SessionPhase.STARTING, DeploymentStatus.SLEEPING),
        (SessionPhase.SLEEPING, DeploymentStatus.SLEEPING),
        (SessionPhase.FAILED, DeploymentStatus.FAILED),
    ],
)
async def test_resolved_status_reads_through_live_phase(
    phase: SessionPhase, expected: DeploymentStatus
) -> None:
    deployment = _active_deployment("phase-slug")
    manager = _ProjectionManager(_live_info(deployment, phase))

    result = await resolved_status(cast("SessionManager", manager), deployment)

    assert result == expected
    assert manager.get_calls == [deployment.id]


@pytest.mark.asyncio
async def test_resolved_status_treats_unknown_runtime_as_sleeping() -> None:
    deployment = _active_deployment("unknown-slug")
    manager = _ProjectionManager(None)

    result = await resolved_status(cast("SessionManager", manager), deployment)

    assert result == DeploymentStatus.SLEEPING
    assert manager.get_calls == [deployment.id]


@pytest.mark.asyncio
async def test_resolved_status_stopped_short_circuits_without_manager_call() -> None:
    deployment = Deployment(
        id=uuid4(),
        notebook_id=uuid4(),
        slug="stopped-slug",
        desired_state=DeploymentDesiredState.STOPPED,
    )
    manager = _ProjectionManager(_live_info(deployment, SessionPhase.READY))

    result = await resolved_status(cast("SessionManager", manager), deployment)

    assert result == DeploymentStatus.STOPPED
    assert manager.get_calls == []


@pytest.mark.asyncio
async def test_deploy_creation_enforces_write_access_and_unique_slug(
    api_client: AsyncClient, db_session: AsyncSession
) -> None:
    _, owner_headers, owner_ws = await register_and_login(api_client, db_session, "deploy-owner")
    _, other_headers, other_ws = await register_and_login(api_client, db_session, "deploy-other")
    owner_notebook = await create_notebook(
        api_client, owner_headers, owner_ws, "CPU Dashboard", "x = 1"
    )
    other_notebook = await create_notebook(
        api_client, other_headers, other_ws, "Memory Dashboard", "x = 2"
    )
    # Published so a non-member's deploy attempt exercises the write-role check (401/403)
    # rather than the private-notebook existence-hiding check (404).
    await publish_notebook(api_client, owner_headers, str(owner_notebook["id"]), "public")

    anonymous = await api_client.post(
        f"/api/notebooks/{owner_notebook['id']}/deploy", json={"slug": "cpu"}
    )
    other = await api_client.post(
        f"/api/notebooks/{owner_notebook['id']}/deploy",
        headers=other_headers,
        json={"slug": "cpu"},
    )
    created = await api_client.post(
        f"/api/notebooks/{owner_notebook['id']}/deploy",
        headers=owner_headers,
        json={"slug": "cpu"},
    )
    duplicate = await api_client.post(
        f"/api/notebooks/{other_notebook['id']}/deploy",
        headers=other_headers,
        json={"slug": "cpu"},
    )

    assert anonymous.status_code == 401
    assert other.status_code == 403
    assert created.status_code == 200
    assert created.json() == {
        "slug": "cpu",
        "status": "sleeping",
        "url": "http://test/api/deployments/cpu",
    }
    assert duplicate.status_code == 409


@pytest.mark.asyncio
async def test_deploy_creation_allows_omitted_body(
    api_client: AsyncClient, db_session: AsyncSession
) -> None:
    _, owner_headers, owner_ws = await register_and_login(
        api_client, db_session, "deploy-empty-body"
    )
    notebook = await create_notebook(
        api_client, owner_headers, owner_ws, "Empty Body Deploy", "x = 1"
    )

    response = await api_client.post(
        f"/api/notebooks/{notebook['id']}/deploy", headers=owner_headers
    )

    assert response.status_code == 200
    assert response.json() == {
        "slug": "empty-body-deploy",
        "status": "sleeping",
        "url": "http://test/api/deployments/empty-body-deploy",
    }


@pytest.mark.asyncio
async def test_deploy_rejects_invalid_slug_with_friendly_message(
    api_client: AsyncClient, db_session: AsyncSession
) -> None:
    _, owner_headers, owner_ws = await register_and_login(api_client, db_session, "deploy-bad-slug")
    notebook = await create_notebook(api_client, owner_headers, owner_ws, "Bad Slug", "x = 1")

    response = await api_client.post(
        f"/api/notebooks/{notebook['id']}/deploy",
        headers=owner_headers,
        json={"slug": "Invalid Slug!"},
    )

    assert response.status_code == 422
    detail = cast("list[dict[str, object]]", response.json()["detail"])
    assert SLUG_MESSAGE in [error["msg"] for error in detail]
    assert "[a-z0-9]" not in response.text


@pytest.mark.asyncio
async def test_deployment_table_allows_only_one_row_per_notebook(db_session: AsyncSession) -> None:
    user = User(id=uuid4(), username="single-deploy-owner", email="single-deploy@example.com")
    workspace = Workspace(id=uuid4(), slug="single-deploy-ws", name="single-deploy-ws")
    notebook = Notebook(
        id=uuid4(),
        workspace_id=workspace.id,
        created_by=user.id,
        title="Single Deploy",
        source="x = 1",
    )
    db_session.add_all(
        [
            user,
            workspace,
            notebook,
            Deployment(notebook_id=notebook.id, slug="single-deploy"),
            Deployment(notebook_id=notebook.id, slug="single-deploy-2"),
        ]
    )

    with pytest.raises(IntegrityError):
        await db_session.commit()


@pytest.mark.asyncio
async def test_generated_deployment_slug_is_bounded(
    api_client: AsyncClient, db_session: AsyncSession
) -> None:
    _, owner_headers, owner_ws = await register_and_login(
        api_client, db_session, "deploy-long-slug"
    )
    notebook = await create_notebook(api_client, owner_headers, owner_ws, "A" * 300, "x = 1")

    response = await api_client.post(
        f"/api/notebooks/{notebook['id']}/deploy", headers=owner_headers
    )

    assert response.status_code == 200
    slug = cast("str", response.json()["slug"])
    assert len(slug) == 255
    assert slug == "a" * 255


@pytest.mark.asyncio
async def test_deploy_commits_source_snapshot_digest_and_revision_one(
    api_client: AsyncClient, db_session: AsyncSession
) -> None:
    _, owner_headers, owner_ws = await register_and_login(api_client, db_session, "deploy-snapshot")
    notebook = await create_notebook(
        api_client, owner_headers, owner_ws, "Snapshot Notebook", "x = 1"
    )

    response = await api_client.post(
        f"/api/notebooks/{notebook['id']}/deploy",
        headers=owner_headers,
        json={"slug": "snapshot-deploy"},
    )

    assert response.status_code == 200
    deployment = await db_session.scalar(
        select(Deployment).where(Deployment.slug == "snapshot-deploy")
    )
    assert deployment is not None
    assert deployment.source_snapshot == "x = 1"
    assert deployment.source_sha256
    assert deployment.runtime_image
    assert deployment.revision == 1
    assert deployment.desired_state is DeploymentDesiredState.ACTIVE


@pytest.mark.asyncio
async def test_notebook_source_edit_does_not_alter_an_existing_deployment_snapshot(
    api_client: AsyncClient, db_session: AsyncSession
) -> None:
    _, owner_headers, owner_ws = await register_and_login(
        api_client, db_session, "deploy-immutable"
    )
    notebook = await create_notebook(
        api_client, owner_headers, owner_ws, "Immutable Notebook", "x = 1"
    )
    await api_client.post(
        f"/api/notebooks/{notebook['id']}/deploy",
        headers=owner_headers,
        json={"slug": "immutable-deploy"},
    )
    before = await db_session.scalar(
        select(Deployment).where(Deployment.slug == "immutable-deploy")
    )
    assert before is not None
    snapshot_before, sha_before, revision_before = (
        before.source_snapshot,
        before.source_sha256,
        before.revision,
    )

    edited = await api_client.put(
        f"/api/notebooks/{notebook['id']}",
        headers=owner_headers,
        json={"source": "x = 999  # edited after deploy"},
    )
    assert edited.status_code == 200

    await db_session.refresh(before)
    assert before.source_snapshot == snapshot_before
    assert before.source_sha256 == sha_before
    assert before.revision == revision_before


@pytest.mark.asyncio
async def test_redeploy_bumps_revision_and_replaces_the_snapshot(
    api_client: AsyncClient,
    db_session: AsyncSession,
    fake_deployment_manager: FakeDeploymentSessionManager,
) -> None:
    _, owner_headers, owner_ws = await register_and_login(api_client, db_session, "deploy-redeploy")
    notebook = await create_notebook(
        api_client, owner_headers, owner_ws, "Redeploy Notebook", "x = 1"
    )
    await api_client.post(
        f"/api/notebooks/{notebook['id']}/deploy",
        headers=owner_headers,
        json={"slug": "redeploy-target"},
    )
    # Visit it once so the manager has a live Runtime under the old
    # revision/snapshot, proving redeploy actually tears that one down.
    await api_client.get("/api/deployments/redeploy-target")
    deployment_id = fake_deployment_manager.spawned[0][1]
    assert deployment_id in fake_deployment_manager.sessions

    await api_client.put(
        f"/api/notebooks/{notebook['id']}",
        headers=owner_headers,
        json={"source": "x = 2  # new snapshot"},
    )
    redeployed = await api_client.post(
        f"/api/notebooks/{notebook['id']}/deploy",
        headers=owner_headers,
        json={"slug": "redeploy-target"},
    )

    assert redeployed.status_code == 200
    deployment = await db_session.scalar(
        select(Deployment).where(Deployment.slug == "redeploy-target")
    )
    assert deployment is not None
    assert deployment.id == deployment_id
    assert deployment.revision == 2
    assert deployment.source_snapshot == "x = 2  # new snapshot"
    # The old Runtime (still serving revision 1) must be torn down, not left
    # stale and reachable while a new one is created lazily on next visit.
    assert deployment_id in fake_deployment_manager.stopped
    assert deployment_id not in fake_deployment_manager.sessions


@pytest.mark.asyncio
async def test_authorized_caller_sees_sanitized_failure_reason_and_message(
    api_client: AsyncClient,
    db_session: AsyncSession,
    fake_deployment_manager: FakeDeploymentSessionManager,
) -> None:
    _, owner_headers, owner_ws = await register_and_login(api_client, db_session, "deploy-failed")
    notebook = await create_notebook(
        api_client, owner_headers, owner_ws, "Failed Notebook", "x = 1"
    )
    await api_client.post(
        f"/api/notebooks/{notebook['id']}/deploy",
        headers=owner_headers,
        json={"slug": "failed-deploy"},
    )
    deployment = await db_session.scalar(
        select(Deployment).where(Deployment.slug == "failed-deploy")
    )
    assert deployment is not None
    fake_deployment_manager.sessions[deployment.id] = SessionInfo(
        id=deployment.id,
        notebook_id=UUID(str(notebook["id"])),
        mode="deploy",
        phase=SessionPhase.FAILED,
        last_active=datetime.now(UTC),
        creator_id=None,
        failure_reason="RuntimeExited",
        message="container exited 1",
    )

    response = await api_client.get(
        f"/api/notebooks/{notebook['id']}/deployment", headers=owner_headers
    )

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "failed"
    assert body["failure_reason"] == "RuntimeExited"
    assert body["message"] == "container exited 1"


@pytest.mark.asyncio
async def test_first_deployment_request_wakes_and_proxies_original_request(
    api_client: AsyncClient,
    db_session: AsyncSession,
    fake_deployment_manager: FakeDeploymentSessionManager,
    upstream_http_server: _UpstreamServer,
) -> None:
    _, owner_headers, owner_ws = await register_and_login(api_client, db_session, "wake-owner")
    notebook = await create_notebook(api_client, owner_headers, owner_ws, "Wake Notebook", "x = 1")
    deploy = await api_client.post(
        f"/api/notebooks/{notebook['id']}/deploy",
        headers=owner_headers,
        json={"slug": "wake-notebook"},
    )

    response = await api_client.get(
        "/api/deployments/wake-notebook/nested/path?metric=cpu&value=94.2",
        headers={"X-Molab-Test": "header-value"},
    )

    assert deploy.status_code == 200
    assert response.status_code == 200
    assert response.headers["X-Upstream"] == "ok"
    assert response.content == b"deployment-response"
    assert fake_deployment_manager.spawned[0][0] == UUID(str(notebook["id"]))
    assert fake_deployment_manager.spawned[0][2] == "wake-notebook"
    assert fake_deployment_manager.touches == [fake_deployment_manager.spawned[0][1]]
    assert upstream_http_server.requests == [
        {
            "path": (
                "/api/deployments/wake-notebook/nested/path"
                "?metric=cpu&value=94.2&access_token=secret"
            ),
            "header": "header-value",
        }
    ]


@pytest.mark.asyncio
async def test_deployment_root_follows_marimo_auth_redirect(
    api_client: AsyncClient,
    db_session: AsyncSession,
    redirecting_upstream_http_server: _UpstreamServer,
) -> None:
    manager = FakeDeploymentSessionManager(redirecting_upstream_http_server.base_url)
    app.dependency_overrides[get_session_manager] = lambda: manager
    try:
        _, owner_headers, owner_ws = await register_and_login(
            api_client, db_session, "redirect-root-owner"
        )
        notebook = await create_notebook(
            api_client, owner_headers, owner_ws, "Redirect Root", "x = 1"
        )
        await api_client.post(
            f"/api/notebooks/{notebook['id']}/deploy",
            headers=owner_headers,
            json={"slug": "redirect-root"},
        )

        response = await api_client.get("/api/deployments/redirect-root")
    finally:
        app.dependency_overrides.pop(get_session_manager, None)

    assert response.status_code == 200
    assert response.content == b"deployment-html"
    assert redirecting_upstream_http_server.requests == [
        {"path": "/api/deployments/redirect-root/?access_token=secret", "cookie": None},
        {"path": "/api/deployments/redirect-root/", "cookie": "marimo-session=ready"},
    ]


@pytest.mark.parametrize("method", ["PUT", "PATCH", "DELETE"])
@pytest.mark.asyncio
async def test_deployment_proxy_forwards_body_for_non_post_methods(
    api_client: AsyncClient,
    db_session: AsyncSession,
    fake_deployment_manager: FakeDeploymentSessionManager,
    upstream_http_server: _UpstreamServer,
    method: str,
) -> None:
    _, owner_headers, owner_ws = await register_and_login(
        api_client, db_session, f"body-{method.lower()}"
    )
    notebook = await create_notebook(
        api_client, owner_headers, owner_ws, "Body Method Deploy", "x = 1"
    )
    await api_client.post(
        f"/api/notebooks/{notebook['id']}/deploy",
        headers=owner_headers,
        json={"slug": "body-method"},
    )

    response = await api_client.request(
        method,
        "/api/deployments/body-method/api/packages/install?name=polars",
        headers={"X-Molab-Test": "header-value"},
        content=b"payload",
    )

    assert response.status_code == 200
    assert response.content == b"deployment-response"
    assert upstream_http_server.requests[0] == {
        "method": method,
        "path": "/api/deployments/body-method/api/packages/install?name=polars&access_token=secret",
        "header": "header-value",
        "body": b"payload",
    }
    assert fake_deployment_manager.touches == [fake_deployment_manager.spawned[0][1]]


def test_deployment_websocket_wakes_and_relays_to_upstream(
    fake_deployment_manager: FakeDeploymentSessionManager,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    notebook = Notebook(id=uuid4(), workspace_id=uuid4(), title="WebSocket Deploy", source="x = 1")
    deployment = Deployment(
        id=uuid4(),
        notebook_id=notebook.id,
        slug="ws-deploy",
        desired_state=DeploymentDesiredState.ACTIVE,
        revision=1,
        source_snapshot="x = 1",
        source_sha256="deadbeef",
        runtime_image=_RUNTIME_IMAGE,
    )
    relayed: list[str] = []

    async def fake_load_active_deployment(db: object, slug: str) -> tuple[Deployment, Notebook]:
        assert slug == "ws-deploy"
        return deployment, notebook

    async def fake_relay(
        websocket: WebSocket,
        target_url: str,
        manager: FakeDeploymentSessionManager,
        deployment_id: UUID,
    ) -> None:
        relayed.append(target_url)
        await manager.mark_active(deployment_id)
        await websocket.accept()
        await websocket.close()

    class FakeDb:
        def add(self, item: object) -> None:
            return None

        async def commit(self) -> None:
            return None

    # This test exercises the WebSocket relay plumbing, not the
    # Workspace/Deployment locking `ensure_running` performs (that is
    # covered directly, against a real database, in test_gateway.py's
    # `DeploymentResolver` tests) -- so the locking layer is stubbed out
    # rather than handed a real `AsyncSession` a `TestClient` WebSocket's own
    # event loop cannot safely share with this test's.
    async def fake_resolve_target_info(manager: object, dep: Deployment) -> SessionInfo | None:
        return None

    async def fake_ensure_running(
        db: object, manager: FakeDeploymentSessionManager, nb: Notebook, deployment_id: UUID
    ) -> SessionInfo:
        return await manager.spawn_deployment(notebook, deployment)

    monkeypatch.setattr(deployments_module, "_load_active_deployment", fake_load_active_deployment)
    monkeypatch.setattr(deployment_lifecycle, "resolve_target_info", fake_resolve_target_info)
    monkeypatch.setattr(deployment_lifecycle, "ensure_running", fake_ensure_running)
    monkeypatch.setattr(marimo_proxy, "_relay_websocket", fake_relay)
    app.dependency_overrides[get_db] = FakeDb
    client = TestClient(app)
    try:
        with client.websocket_connect("/api/deployments/ws-deploy/ws?client=browser"):
            pass
    finally:
        app.dependency_overrides.pop(get_db, None)

    # No target is registered for the deployment yet, so the resolver's fast-path
    # probe misses and it wakes via the manager's idempotent spawn_deployment.
    assert fake_deployment_manager.spawned == [(notebook.id, deployment.id, "ws-deploy")]
    assert relayed == [
        "ws://127.0.0.1:9000/api/deployments/ws-deploy/ws?client=browser&access_token=secret"
    ]
    # One mark_active at connect (the gateway's connect edge) plus one from the frame
    # the fake relay simulates.
    assert fake_deployment_manager.touches == [deployment.id, deployment.id]


@pytest.mark.asyncio
async def test_anonymous_deployment_wake_timeout_gets_generic_unavailable_response(
    api_client: AsyncClient,
    db_session: AsyncSession,
    fake_deployment_manager: FakeDeploymentSessionManager,
) -> None:
    """Anonymous Deployment traffic never sees a manager's raw message, only a generic one."""
    _, owner_headers, owner_ws = await register_and_login(api_client, db_session, "wake-fail-owner")
    notebook = await create_notebook(api_client, owner_headers, owner_ws, "Wake Fail", "x = 1")
    await api_client.post(
        f"/api/notebooks/{notebook['id']}/deploy",
        headers=owner_headers,
        json={"slug": "wake-fail"},
    )
    fake_deployment_manager.spawn_error = SessionStartError(
        "did not become ready", detail="Deployment did not start in time."
    )

    response = await api_client.get("/api/deployments/wake-fail")

    assert response.status_code == 503
    assert response.json()["detail"] == "Deployment is unavailable"


@pytest.mark.asyncio
async def test_anonymous_deployment_startup_failure_gets_generic_unavailable_response(
    api_client: AsyncClient,
    db_session: AsyncSession,
    fake_deployment_manager: FakeDeploymentSessionManager,
) -> None:
    _, owner_headers, owner_ws = await register_and_login(
        api_client, db_session, "wake-startup-owner"
    )
    notebook = await create_notebook(api_client, owner_headers, owner_ws, "Wake Startup", "x = 1")
    await api_client.post(
        f"/api/notebooks/{notebook['id']}/deploy",
        headers=owner_headers,
        json={"slug": "wake-startup"},
    )
    fake_deployment_manager.spawn_error = NotebookStartupError(
        "process exited",
        detail="The notebook could not start. Check that it is a valid marimo notebook.",
    )

    response = await api_client.get("/api/deployments/wake-startup")

    # A deterministic startup failure is never retried, but an anonymous
    # visitor is told no more than "unavailable" -- the actionable detail is
    # reserved for the authorized surface (`get_notebook_deployment`).
    assert response.status_code == 503
    assert response.json()["detail"] == "Deployment is unavailable"


@pytest.mark.asyncio
async def test_anonymous_deployment_capacity_failure_gets_generic_unavailable_response(
    api_client: AsyncClient,
    db_session: AsyncSession,
    fake_deployment_manager: FakeDeploymentSessionManager,
) -> None:
    _, owner_headers, owner_ws = await register_and_login(
        api_client, db_session, "wake-capacity-owner"
    )
    notebook = await create_notebook(api_client, owner_headers, owner_ws, "Wake Capacity", "x = 1")
    await api_client.post(
        f"/api/notebooks/{notebook['id']}/deploy",
        headers=owner_headers,
        json={"slug": "wake-capacity"},
    )
    fake_deployment_manager.spawn_error = SessionCapacityError(
        "Maximum concurrent sessions reached"
    )

    response = await api_client.get("/api/deployments/wake-capacity")

    # A quota rejection is a real, stable classification, but an anonymous
    # visitor gets no more distinction than "unavailable" -- not even a 429
    # vs. 503 split, since that alone would leak which category applied.
    assert response.status_code == 503
    assert response.json()["detail"] == "Deployment is unavailable"


@pytest.mark.asyncio
async def test_failed_wake_never_persists_running(
    api_client: AsyncClient,
    db_session: AsyncSession,
    fake_deployment_manager: FakeDeploymentSessionManager,
) -> None:
    _, owner_headers, owner_ws = await register_and_login(
        api_client, db_session, "wake-never-running-owner"
    )
    notebook = await create_notebook(
        api_client, owner_headers, owner_ws, "Wake Never Running", "x = 1"
    )
    await api_client.post(
        f"/api/notebooks/{notebook['id']}/deploy",
        headers=owner_headers,
        json={"slug": "wake-never-running"},
    )
    fake_deployment_manager.spawn_error = SessionStartError(
        "did not become ready", detail="Deployment did not start in time."
    )

    response = await api_client.get("/api/deployments/wake-never-running")
    assert response.status_code == 503

    deployment = await db_session.scalar(
        select(Deployment).where(Deployment.slug == "wake-never-running")
    )
    assert deployment is not None
    assert deployment.desired_state == DeploymentDesiredState.ACTIVE

    read_model = await api_client.get(
        f"/api/notebooks/{notebook['id']}/deployment", headers=owner_headers
    )
    assert read_model.status_code == 200
    assert read_model.json()["status"] == "sleeping"


@pytest.mark.asyncio
async def test_get_notebook_deployment_reports_running_after_wake(
    api_client: AsyncClient,
    db_session: AsyncSession,
    fake_deployment_manager: FakeDeploymentSessionManager,
) -> None:
    _, owner_headers, owner_ws = await register_and_login(
        api_client, db_session, "read-model-running-owner"
    )
    notebook = await create_notebook(
        api_client, owner_headers, owner_ws, "Read Model Running", "x = 1"
    )
    await api_client.post(
        f"/api/notebooks/{notebook['id']}/deploy",
        headers=owner_headers,
        json={"slug": "read-model-running"},
    )
    await api_client.get("/api/deployments/read-model-running")

    response = await api_client.get(
        f"/api/notebooks/{notebook['id']}/deployment", headers=owner_headers
    )

    assert response.status_code == 200
    assert response.json()["status"] == "running"


@pytest.mark.asyncio
async def test_get_notebook_deployment_reports_stopped_without_waking(
    api_client: AsyncClient,
    db_session: AsyncSession,
    fake_deployment_manager: FakeDeploymentSessionManager,
) -> None:
    _, owner_headers, owner_ws = await register_and_login(
        api_client, db_session, "read-model-stopped-owner"
    )
    notebook = await create_notebook(
        api_client, owner_headers, owner_ws, "Read Model Stopped", "x = 1"
    )
    await api_client.post(
        f"/api/notebooks/{notebook['id']}/deploy",
        headers=owner_headers,
        json={"slug": "read-model-stopped"},
    )
    await api_client.delete("/api/deployments/read-model-stopped", headers=owner_headers)

    response = await api_client.get(
        f"/api/notebooks/{notebook['id']}/deployment", headers=owner_headers
    )

    assert response.status_code == 200
    assert response.json()["status"] == "stopped"
    assert fake_deployment_manager.spawned == []


@pytest.mark.asyncio
async def test_get_notebook_deployment_requires_view_access(
    api_client: AsyncClient,
    db_session: AsyncSession,
    fake_deployment_manager: FakeDeploymentSessionManager,
) -> None:
    _, owner_headers, owner_ws = await register_and_login(
        api_client, db_session, "read-model-visibility-owner"
    )
    _, other_headers, _ = await register_and_login(
        api_client, db_session, "read-model-visibility-other"
    )
    notebook = await create_notebook(
        api_client, owner_headers, owner_ws, "Read Model Visibility", "x = 1"
    )
    await api_client.post(
        f"/api/notebooks/{notebook['id']}/deploy",
        headers=owner_headers,
        json={"slug": "read-model-visibility"},
    )

    anonymous = await api_client.get(f"/api/notebooks/{notebook['id']}/deployment")
    other = await api_client.get(
        f"/api/notebooks/{notebook['id']}/deployment", headers=other_headers
    )
    owner = await api_client.get(
        f"/api/notebooks/{notebook['id']}/deployment", headers=owner_headers
    )

    assert anonymous.status_code == 404
    assert other.status_code == 404
    assert owner.status_code == 200


@pytest.mark.asyncio
async def test_get_notebook_deployment_404_when_none_exists(
    api_client: AsyncClient, db_session: AsyncSession
) -> None:
    _, owner_headers, owner_ws = await register_and_login(api_client, db_session, "no-deploy-owner")
    notebook = await create_notebook(api_client, owner_headers, owner_ws, "No Deploy", "x = 1")

    response = await api_client.get(
        f"/api/notebooks/{notebook['id']}/deployment", headers=owner_headers
    )

    assert response.status_code == 404


@pytest.mark.asyncio
async def test_delete_deployment_requires_write_access_stops_runtime_and_marks_stopped(
    api_client: AsyncClient,
    db_session: AsyncSession,
    fake_deployment_manager: FakeDeploymentSessionManager,
) -> None:
    _, owner_headers, owner_ws = await register_and_login(api_client, db_session, "delete-owner")
    _, other_headers, _ = await register_and_login(api_client, db_session, "delete-other")
    notebook = await create_notebook(api_client, owner_headers, owner_ws, "Delete Deploy", "x = 1")
    # Published so a non-member's delete attempt exercises the write-role check (401/403)
    # rather than the private-notebook existence-hiding check (404).
    await publish_notebook(api_client, owner_headers, str(notebook["id"]), "public")
    await api_client.post(
        f"/api/notebooks/{notebook['id']}/deploy",
        headers=owner_headers,
        json={"slug": "delete-deploy"},
    )
    await api_client.get("/api/deployments/delete-deploy")
    deployment_id = fake_deployment_manager.spawned[0][1]

    anonymous = await api_client.delete("/api/deployments/delete-deploy")
    other = await api_client.delete("/api/deployments/delete-deploy", headers=other_headers)
    owner = await api_client.delete("/api/deployments/delete-deploy", headers=owner_headers)
    deployment = await db_session.get(Deployment, deployment_id)

    assert anonymous.status_code == 401
    assert other.status_code == 403
    assert owner.status_code == 204
    assert fake_deployment_manager.stopped == [deployment_id]
    assert deployment is not None
    await db_session.refresh(deployment)
    assert deployment.desired_state == DeploymentDesiredState.STOPPED


@pytest.mark.asyncio
async def test_delete_already_stopped_deployment_is_idempotent(
    api_client: AsyncClient,
    db_session: AsyncSession,
    fake_deployment_manager: FakeDeploymentSessionManager,
) -> None:
    _, owner_headers, owner_ws = await register_and_login(
        api_client, db_session, "double-delete-owner"
    )
    notebook = await create_notebook(api_client, owner_headers, owner_ws, "Double Delete", "x = 1")
    await api_client.post(
        f"/api/notebooks/{notebook['id']}/deploy",
        headers=owner_headers,
        json={"slug": "double-delete"},
    )

    first = await api_client.delete("/api/deployments/double-delete", headers=owner_headers)
    second = await api_client.delete("/api/deployments/double-delete", headers=owner_headers)

    assert first.status_code == 204
    assert second.status_code == 204
