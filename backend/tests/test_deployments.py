from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
import http.server
import socket
import socketserver
import threading
from types import SimpleNamespace
from uuid import UUID, uuid4

from httpx import AsyncClient
import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from fastapi.testclient import TestClient

from app.api import deployments as deployments_module
from app.db.database import get_db
from app.main import app
from app.models import Deployment, DeploymentStatus, Notebook, User
from app.services.deployment_lifecycle import IdleDeploymentReaper, mark_running_deployments_sleeping
from app.services.process_manager import SessionInfo, SessionStartError, SessionTarget, get_process_manager
from test_notebooks import create_notebook, register_and_login


class FakeDeploymentProcessManager:
    def __init__(self, upstream_base_url: str = "http://127.0.0.1:1") -> None:
        self.upstream_base_url = upstream_base_url
        self.sessions: dict[UUID, SessionInfo] = {}
        self.targets: dict[UUID, SessionTarget] = {}
        self.spawn_error: Exception | None = None
        self.spawned: list[tuple[UUID, UUID, str]] = []
        self.stopped: list[UUID] = []
        self.touches: list[UUID] = []

    async def spawn_deployment(self, notebook: Notebook, deployment_id: UUID, slug: str) -> SessionInfo:
        if self.spawn_error is not None:
            raise self.spawn_error
        session = SessionInfo(
            id=deployment_id,
            notebook_id=notebook.id,
            mode="deploy",
            port=9000,
            pid=123,
            last_active=datetime.now(UTC),
            creator_id=None,
        )
        self.sessions[deployment_id] = session
        self.targets[deployment_id] = SessionTarget(
            http_base_url=f"{self.upstream_base_url}/api/deployments/{slug}",
            ws_base_url=f"ws://127.0.0.1:9000/api/deployments/{slug}",
            access_token="secret",
        )
        self.spawned.append((notebook.id, deployment_id, slug))
        return session

    def target(self, session_id: UUID) -> SessionTarget | None:
        return self.targets.get(session_id)

    def get(self, session_id: UUID) -> SessionInfo | None:
        return self.sessions.get(session_id)

    def touch(self, session_id: UUID) -> None:
        self.touches.append(session_id)

    async def stop(self, session_id: UUID) -> None:
        self.stopped.append(session_id)
        self.sessions.pop(session_id, None)
        self.targets.pop(session_id, None)


class _ThreadingServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@pytest.fixture
def upstream_http_server() -> Iterator[SimpleNamespace]:
    requests: list[dict[str, object]] = []

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            requests.append({"path": self.path, "header": self.headers.get("X-Molab-Test")})
            self.send_response(200)
            self.send_header("X-Upstream", "ok")
            self.end_headers()
            self.wfile.write(b"deployment-response")

        def log_message(self, format: str, *args: object) -> None:
            pass

    port = _free_port()
    server = _ThreadingServer(("127.0.0.1", port), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield SimpleNamespace(port=port, requests=requests, base_url=f"http://127.0.0.1:{port}")
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


@pytest.fixture
def fake_deployment_manager(upstream_http_server: SimpleNamespace) -> Iterator[FakeDeploymentProcessManager]:
    manager = FakeDeploymentProcessManager(upstream_http_server.base_url)
    app.dependency_overrides[get_process_manager] = lambda: manager
    try:
        yield manager
    finally:
        app.dependency_overrides.pop(get_process_manager, None)


@pytest.mark.asyncio
async def test_deploy_creation_enforces_owner_and_unique_slug(api_client: AsyncClient) -> None:
    _, owner_headers = await register_and_login(api_client, "deploy-owner")
    _, other_headers = await register_and_login(api_client, "deploy-other")
    owner_notebook = await create_notebook(api_client, owner_headers, "CPU Dashboard", "x = 1")
    other_notebook = await create_notebook(api_client, other_headers, "Memory Dashboard", "x = 2")

    anonymous = await api_client.post(f"/api/notebooks/{owner_notebook['id']}/deploy", json={"slug": "cpu"})
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
    assert created.json() == {"slug": "cpu", "status": "sleeping", "url": "http://test/api/deployments/cpu"}
    assert duplicate.status_code == 409


@pytest.mark.asyncio
async def test_deploy_creation_allows_omitted_body(api_client: AsyncClient) -> None:
    _, owner_headers = await register_and_login(api_client, "deploy-empty-body")
    notebook = await create_notebook(api_client, owner_headers, "Empty Body Deploy", "x = 1")

    response = await api_client.post(f"/api/notebooks/{notebook['id']}/deploy", headers=owner_headers)

    assert response.status_code == 200
    assert response.json() == {
        "slug": "empty-body-deploy",
        "status": "sleeping",
        "url": "http://test/api/deployments/empty-body-deploy",
    }


@pytest.mark.asyncio
async def test_deployment_table_allows_only_one_row_per_notebook(db_session: AsyncSession) -> None:
    user = User(id=uuid4(), username="single-deploy-owner", email="single-deploy@example.com", password_hash="hash")
    notebook = Notebook(id=uuid4(), user_id=user.id, title="Single Deploy", source="x = 1")
    db_session.add_all(
        [
            user,
            notebook,
            Deployment(notebook_id=notebook.id, slug="single-deploy"),
            Deployment(notebook_id=notebook.id, slug="single-deploy-2"),
        ]
    )

    with pytest.raises(IntegrityError):
        await db_session.commit()


@pytest.mark.asyncio
async def test_generated_deployment_slug_is_bounded(api_client: AsyncClient) -> None:
    _, owner_headers = await register_and_login(api_client, "deploy-long-slug")
    notebook = await create_notebook(api_client, owner_headers, "A" * 300, "x = 1")

    response = await api_client.post(f"/api/notebooks/{notebook['id']}/deploy", headers=owner_headers)

    assert response.status_code == 200
    slug = response.json()["slug"]
    assert len(slug) == 255
    assert slug == "a" * 255


@pytest.mark.asyncio
async def test_first_deployment_request_wakes_and_proxies_original_request(
    api_client: AsyncClient,
    fake_deployment_manager: FakeDeploymentProcessManager,
    upstream_http_server: SimpleNamespace,
) -> None:
    _, owner_headers = await register_and_login(api_client, "wake-owner")
    notebook = await create_notebook(api_client, owner_headers, "Wake Notebook", "x = 1")
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
            "path": "/api/deployments/wake-notebook/nested/path?metric=cpu&value=94.2&access_token=secret",
            "header": "header-value",
        }
    ]


def test_deployment_websocket_wakes_and_relays_to_upstream(
    fake_deployment_manager: FakeDeploymentProcessManager,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user = User(id=uuid4(), username="deploy-ws-owner", email="deploy-ws@example.com", password_hash="hash")
    notebook = Notebook(id=uuid4(), user_id=user.id, title="WebSocket Deploy", source="x = 1")
    deployment = Deployment(
        id=uuid4(),
        notebook_id=notebook.id,
        slug="ws-deploy",
        status=DeploymentStatus.SLEEPING,
    )
    relayed: list[str] = []
    woke: list[UUID] = []
    fake_deployment_manager.targets[deployment.id] = SessionTarget(
        http_base_url="http://127.0.0.1:9000/api/deployments/ws-deploy",
        ws_base_url="ws://127.0.0.1:9000/api/deployments/ws-deploy",
        access_token="secret",
    )

    async def fake_load_deployment(db, slug):
        assert slug == "ws-deploy"
        return deployment, notebook

    async def fake_wake_deployment(db, manager, loaded_deployment, loaded_notebook):
        woke.append(loaded_deployment.id)

    async def fake_relay(websocket, target_url, manager, deployment_id):
        relayed.append(target_url)
        manager.touch(deployment_id)
        await websocket.accept()
        await websocket.close()

    class FakeDb:
        def add(self, item):
            return None

        async def commit(self):
            return None

    monkeypatch.setattr(deployments_module, "_load_deployment", fake_load_deployment)
    monkeypatch.setattr(deployments_module, "_wake_deployment", fake_wake_deployment)
    monkeypatch.setattr(deployments_module, "_relay_websocket", fake_relay)
    app.dependency_overrides[get_db] = FakeDb
    client = TestClient(app)
    try:
        with client.websocket_connect("/api/deployments/ws-deploy/ws?client=browser"):
            pass
    finally:
        app.dependency_overrides.pop(get_db, None)

    assert woke == [deployment.id]
    assert relayed == ["ws://127.0.0.1:9000/api/deployments/ws-deploy/ws?client=browser&access_token=secret"]
    assert fake_deployment_manager.touches == [deployment.id]


@pytest.mark.asyncio
async def test_deployment_wake_failure_returns_503(
    api_client: AsyncClient,
    fake_deployment_manager: FakeDeploymentProcessManager,
) -> None:
    _, owner_headers = await register_and_login(api_client, "wake-fail-owner")
    notebook = await create_notebook(api_client, owner_headers, "Wake Fail", "x = 1")
    await api_client.post(
        f"/api/notebooks/{notebook['id']}/deploy",
        headers=owner_headers,
        json={"slug": "wake-fail"},
    )
    fake_deployment_manager.spawn_error = SessionStartError("boom")

    response = await api_client.get("/api/deployments/wake-fail")

    assert response.status_code == 503
    assert response.json()["detail"] == "Deployment failed to wake"


@pytest.mark.asyncio
async def test_delete_deployment_requires_owner_stops_runtime_and_marks_stopped(
    api_client: AsyncClient,
    db_session: AsyncSession,
    fake_deployment_manager: FakeDeploymentProcessManager,
) -> None:
    _, owner_headers = await register_and_login(api_client, "delete-owner")
    _, other_headers = await register_and_login(api_client, "delete-other")
    notebook = await create_notebook(api_client, owner_headers, "Delete Deploy", "x = 1")
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
    assert deployment.status == DeploymentStatus.STOPPED


@pytest.mark.asyncio
async def test_idle_reaper_sleeps_idle_running_deployments(
    db_session: AsyncSession,
    test_database_url: str,
) -> None:
    user = User(id=uuid4(), username="idle-owner", email="idle@example.com", password_hash="hash")
    notebook = Notebook(id=uuid4(), user_id=user.id, title="Idle", source="x = 1")
    deployment = Deployment(
        id=uuid4(),
        notebook_id=notebook.id,
        slug="idle",
        status=DeploymentStatus.RUNNING,
        port=9000,
        last_active=datetime.now(UTC) - timedelta(minutes=2),
    )
    db_session.add_all([user, notebook, deployment])
    await db_session.commit()
    manager = FakeDeploymentProcessManager()
    manager.sessions[deployment.id] = SessionInfo(
        id=deployment.id,
        notebook_id=notebook.id,
        mode="deploy",
        port=9000,
        pid=123,
        last_active=datetime.now(UTC) - timedelta(minutes=2),
        creator_id=None,
    )
    manager.targets[deployment.id] = SessionTarget(
        http_base_url="http://127.0.0.1:9000/api/deployments/idle",
        ws_base_url="ws://127.0.0.1:9000/api/deployments/idle",
        access_token="secret",
    )
    engine = create_async_engine(test_database_url, pool_pre_ping=True)
    sessionmaker = async_sessionmaker(engine, expire_on_commit=False)
    reaper = IdleDeploymentReaper(
        sessionmaker=sessionmaker,
        manager=manager,
        settings=SimpleNamespace(IDLE_TIMEOUT_MINUTES=1),
        interval_seconds=0.01,
    )

    await reaper.reap_once()
    await engine.dispose()
    await db_session.refresh(deployment)

    assert manager.stopped == [deployment.id]
    assert deployment.status == DeploymentStatus.SLEEPING
    assert deployment.port is None


@pytest.mark.asyncio
async def test_idle_reaper_keeps_recently_active_live_deployment(
    db_session: AsyncSession,
    test_database_url: str,
) -> None:
    user = User(id=uuid4(), username="active-owner", email="active@example.com", password_hash="hash")
    notebook = Notebook(id=uuid4(), user_id=user.id, title="Active", source="x = 1")
    deployment = Deployment(
        id=uuid4(),
        notebook_id=notebook.id,
        slug="active",
        status=DeploymentStatus.RUNNING,
        port=9000,
        last_active=datetime.now(UTC) - timedelta(minutes=2),
    )
    db_session.add_all([user, notebook, deployment])
    await db_session.commit()
    manager = FakeDeploymentProcessManager()
    manager.sessions[deployment.id] = SessionInfo(
        id=deployment.id,
        notebook_id=notebook.id,
        mode="deploy",
        port=9000,
        pid=123,
        last_active=datetime.now(UTC),
        creator_id=None,
    )
    manager.targets[deployment.id] = SessionTarget(
        http_base_url="http://127.0.0.1:9000/api/deployments/active",
        ws_base_url="ws://127.0.0.1:9000/api/deployments/active",
        access_token="secret",
    )
    engine = create_async_engine(test_database_url, pool_pre_ping=True)
    sessionmaker = async_sessionmaker(engine, expire_on_commit=False)
    reaper = IdleDeploymentReaper(
        sessionmaker=sessionmaker,
        manager=manager,
        settings=SimpleNamespace(IDLE_TIMEOUT_MINUTES=1),
        interval_seconds=0.01,
    )

    await reaper.reap_once()
    await engine.dispose()
    await db_session.refresh(deployment)

    assert manager.stopped == []
    assert deployment.status == DeploymentStatus.RUNNING
    assert deployment.port == 9000
    assert deployment.last_active == manager.sessions[deployment.id].last_active


@pytest.mark.asyncio
async def test_startup_marks_running_deployments_sleeping(db_session: AsyncSession, test_database_url: str) -> None:
    user = User(id=uuid4(), username="startup-owner", email="startup@example.com", password_hash="hash")
    notebook = Notebook(id=uuid4(), user_id=user.id, title="Startup", source="x = 1")
    deployment = Deployment(
        id=uuid4(),
        notebook_id=notebook.id,
        slug="startup",
        status=DeploymentStatus.RUNNING,
        port=9000,
        last_active=datetime.now(UTC),
    )
    db_session.add_all([user, notebook, deployment])
    await db_session.commit()
    engine = create_async_engine(test_database_url, pool_pre_ping=True)
    sessionmaker = async_sessionmaker(engine, expire_on_commit=False)

    await mark_running_deployments_sleeping(sessionmaker)
    await engine.dispose()
    await db_session.refresh(deployment)

    assert deployment.status == DeploymentStatus.SLEEPING
    assert deployment.port is None
