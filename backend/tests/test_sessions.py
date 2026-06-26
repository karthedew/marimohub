from collections.abc import Iterator
from datetime import UTC, datetime
import http.server
import shutil
import socket
import socketserver
import threading
from types import SimpleNamespace
from uuid import UUID, uuid4

from httpx import AsyncClient
import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.services.process_manager import ProcessManager, SessionCapacityError, SessionInfo, SessionTarget, get_process_manager
from app.api import proxy as proxy_module
from test_notebooks import create_notebook, publish_notebook, register_and_login


class FakeProcessManager:
    def __init__(self) -> None:
        self.sessions: dict[UUID, SessionInfo] = {}
        self.spawn_error: Exception | None = None
        self.spawned: list[tuple[UUID, str, UUID | None]] = []
        self.stopped: list[UUID] = []
        self.target_response: SessionTarget | None = None
        self.touches: list[UUID] = []

    async def spawn(self, notebook: object, mode: str, creator_id: UUID | None = None) -> SessionInfo:
        if self.spawn_error is not None:
            raise self.spawn_error
        notebook_id = notebook.id
        session = SessionInfo(
            id=uuid4(),
            notebook_id=notebook_id,
            mode=mode,
            port=9000,
            pid=123,
            last_active=datetime.now(UTC),
            creator_id=creator_id,
        )
        self.sessions[session.id] = session
        self.spawned.append((notebook_id, mode, creator_id))
        return session

    def get(self, session_id: UUID) -> SessionInfo | None:
        return self.sessions.get(session_id)

    async def stop(self, session_id: UUID) -> None:
        self.stopped.append(session_id)
        self.sessions.pop(session_id, None)

    def target(self, session_id: UUID) -> SessionTarget | None:
        if session_id not in self.sessions:
            return None
        return self.target_response

    def touch(self, session_id: UUID) -> None:
        self.touches.append(session_id)


@pytest.fixture
def fake_process_manager() -> Iterator[FakeProcessManager]:
    manager = FakeProcessManager()
    app.dependency_overrides[get_process_manager] = lambda: manager
    try:
        yield manager
    finally:
        app.dependency_overrides.pop(get_process_manager, None)


@pytest.mark.asyncio
async def test_create_session_authorizes_edit_and_run_modes(
    api_client: AsyncClient,
    fake_process_manager: FakeProcessManager,
) -> None:
    owner, owner_headers = await register_and_login(api_client, "owner")
    _, other_headers = await register_and_login(api_client, "other")
    draft = await create_notebook(api_client, owner_headers, "Draft", "x = 1")
    public = await create_notebook(api_client, owner_headers, "Public", "x = 2")
    await publish_notebook(api_client, owner_headers, str(public["id"]), "public")

    anonymous_edit = await api_client.post("/api/sessions", json={"notebook_id": draft["id"], "mode": "edit"})
    other_edit_public = await api_client.post(
        "/api/sessions",
        headers=other_headers,
        json={"notebook_id": public["id"], "mode": "edit"},
    )
    anonymous_run_draft = await api_client.post("/api/sessions", json={"notebook_id": draft["id"], "mode": "run"})
    owner_edit = await api_client.post(
        "/api/sessions",
        headers=owner_headers,
        json={"notebook_id": draft["id"], "mode": "edit"},
    )
    anonymous_run_public = await api_client.post("/api/sessions", json={"notebook_id": public["id"], "mode": "run"})

    assert anonymous_edit.status_code == 401
    assert other_edit_public.status_code == 403
    assert anonymous_run_draft.status_code == 404
    assert owner_edit.status_code == 201
    assert owner_edit.json()["mode"] == "edit"
    assert owner_edit.json()["proxy_url"].startswith("http://test/api/proxy/")
    assert anonymous_run_public.status_code == 201
    assert fake_process_manager.spawned == [
        (UUID(str(draft["id"])), "edit", UUID(str(owner["id"]))),
        (UUID(str(public["id"])), "run", None),
    ]


@pytest.mark.asyncio
async def test_anonymous_run_allows_visible_unlisted_notebooks(
    api_client: AsyncClient,
    fake_process_manager: FakeProcessManager,
) -> None:
    _, owner_headers = await register_and_login(api_client, "owner")
    notebook = await create_notebook(api_client, owner_headers, "Unlisted", "x = 1")
    await publish_notebook(api_client, owner_headers, str(notebook["id"]), "unlisted")

    response = await api_client.post("/api/sessions", json={"notebook_id": notebook["id"], "mode": "run"})

    assert response.status_code == 201
    assert response.json()["notebook_id"] == notebook["id"]
    assert fake_process_manager.spawned[0][2] is None


@pytest.mark.asyncio
async def test_create_session_maps_capacity_errors_to_503(
    api_client: AsyncClient,
    fake_process_manager: FakeProcessManager,
) -> None:
    _, owner_headers = await register_and_login(api_client, "owner")
    notebook = await create_notebook(api_client, owner_headers, "Notebook", "x = 1")
    fake_process_manager.spawn_error = SessionCapacityError("Maximum concurrent sessions reached")

    response = await api_client.post(
        "/api/sessions",
        headers=owner_headers,
        json={"notebook_id": notebook["id"], "mode": "edit"},
    )

    assert response.status_code == 503


@pytest.mark.asyncio
async def test_delete_session_requires_creator(
    api_client: AsyncClient,
    fake_process_manager: FakeProcessManager,
) -> None:
    owner, owner_headers = await register_and_login(api_client, "owner")
    _, other_headers = await register_and_login(api_client, "other")
    session_id = uuid4()
    fake_process_manager.sessions[session_id] = SessionInfo(
        id=session_id,
        notebook_id=uuid4(),
        mode="run",
        port=9000,
        pid=123,
        last_active=datetime.now(UTC),
        creator_id=UUID(str(owner["id"])),
    )

    anonymous_delete = await api_client.delete(f"/api/sessions/{session_id}")
    other_delete = await api_client.delete(f"/api/sessions/{session_id}", headers=other_headers)
    owner_delete = await api_client.delete(f"/api/sessions/{session_id}", headers=owner_headers)

    assert anonymous_delete.status_code == 401
    assert other_delete.status_code == 403
    assert owner_delete.status_code == 204
    assert fake_process_manager.stopped == [session_id]


@pytest.mark.asyncio
async def test_delete_anonymous_session_by_session_id(
    api_client: AsyncClient,
    fake_process_manager: FakeProcessManager,
) -> None:
    session_id = uuid4()
    fake_process_manager.sessions[session_id] = SessionInfo(
        id=session_id,
        notebook_id=uuid4(),
        mode="run",
        port=9000,
        pid=123,
        last_active=datetime.now(UTC),
        creator_id=None,
    )

    response = await api_client.delete(f"/api/sessions/{session_id}")

    assert response.status_code == 204
    assert fake_process_manager.stopped == [session_id]


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
            requests.append({"method": "GET", "path": self.path, "header": self.headers.get("X-Molab-Test")})
            if self.path.startswith("/api/proxy/"):
                base_path = self.path.split("?", 1)[0].rstrip("/")
                self.send_response(303)
                self.send_header("Location", f"{base_path}/")
                self.send_header("Set-Cookie", f"marimo_auth=ok; Path={base_path}; HttpOnly")
                self.send_header("Set-Cookie", f"marimo_theme=dark; Path={base_path}")
                self.end_headers()
                return
            self.send_response(200)
            self.send_header("X-Upstream", "ok")
            self.end_headers()
            self.wfile.write(b"get-response")

        def do_POST(self) -> None:
            length = int(self.headers.get("Content-Length", "0"))
            body = self.rfile.read(length)
            requests.append(
                {"method": "POST", "path": self.path, "header": self.headers.get("X-Molab-Test"), "body": body}
            )
            self.send_response(201)
            self.send_header("X-Upstream", "ok")
            self.end_headers()
            self.wfile.write(body.upper())

        def log_message(self, format: str, *args: object) -> None:
            pass

    port = _free_port()
    server = _ThreadingServer(("127.0.0.1", port), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield SimpleNamespace(port=port, requests=requests)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


@pytest.mark.asyncio
async def test_http_proxy_preserves_query_headers_body_and_touches_session(
    api_client: AsyncClient,
    fake_process_manager: FakeProcessManager,
    upstream_http_server: SimpleNamespace,
) -> None:
    session_id = uuid4()
    fake_process_manager.sessions[session_id] = SessionInfo(
        id=session_id,
        notebook_id=uuid4(),
        mode="run",
        port=upstream_http_server.port,
        pid=123,
        last_active=datetime.now(UTC),
        creator_id=None,
    )
    fake_process_manager.target_response = SessionTarget(
        http_base_url=f"http://127.0.0.1:{upstream_http_server.port}",
        ws_base_url=f"ws://127.0.0.1:{upstream_http_server.port}",
        access_token="secret token",
    )

    get_response = await api_client.get(
        f"/api/proxy/{session_id}/nested/path?metric=cpu&value=94.2",
        headers={"X-Molab-Test": "header-value"},
    )
    post_response = await api_client.post(
        f"/api/proxy/{session_id}/submit?source=grafana",
        headers={"X-Molab-Test": "post-header"},
        content=b"payload",
    )

    assert get_response.status_code == 200
    assert get_response.headers["X-Upstream"] == "ok"
    assert get_response.content == b"get-response"
    assert post_response.status_code == 201
    assert post_response.content == b"PAYLOAD"
    assert upstream_http_server.requests[0] == {
        "method": "GET",
        "path": "/nested/path?metric=cpu&value=94.2&access_token=secret+token",
        "header": "header-value",
    }
    assert upstream_http_server.requests[1] == {
        "method": "POST",
        "path": "/submit?source=grafana&access_token=secret+token",
        "header": "post-header",
        "body": b"payload",
    }
    assert fake_process_manager.touches == [session_id, session_id]


@pytest.mark.asyncio
async def test_http_proxy_preserves_marimo_auth_redirect_and_cookie_headers(
    api_client: AsyncClient,
    fake_process_manager: FakeProcessManager,
    upstream_http_server: SimpleNamespace,
) -> None:
    session_id = uuid4()
    proxy_base = f"/api/proxy/{session_id}"
    fake_process_manager.sessions[session_id] = SessionInfo(
        id=session_id,
        notebook_id=uuid4(),
        mode="run",
        port=upstream_http_server.port,
        pid=123,
        last_active=datetime.now(UTC),
        creator_id=None,
    )
    fake_process_manager.target_response = SessionTarget(
        http_base_url=f"http://127.0.0.1:{upstream_http_server.port}{proxy_base}",
        ws_base_url=f"ws://127.0.0.1:{upstream_http_server.port}{proxy_base}",
        access_token="secret",
    )

    response = await api_client.get(f"{proxy_base}/", follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["Location"] == f"{proxy_base}/"
    assert response.headers.get_list("Set-Cookie") == [
        f"marimo_auth=ok; Path={proxy_base}; HttpOnly",
        f"marimo_theme=dark; Path={proxy_base}",
    ]
    assert upstream_http_server.requests[-1] == {
        "method": "GET",
        "path": f"{proxy_base}/?access_token=secret",
        "header": None,
    }
    assert fake_process_manager.touches == [session_id]


@pytest.mark.integration
@pytest.mark.asyncio
async def test_run_session_proxy_url_loads_real_marimo_session(api_client: AsyncClient) -> None:
    if shutil.which("marimo") is None:
        pytest.skip("marimo executable is not available")

    port = _free_port()
    manager = ProcessManager(port_range=f"{port}-{port}", max_concurrent_sessions=1)
    app.dependency_overrides[get_process_manager] = lambda: manager
    try:
        _, owner_headers = await register_and_login(api_client, "browser")
        notebook = await create_notebook(
            api_client,
            owner_headers,
            "Browser Load",
            "import marimo\n\n"
            "__generated_with = '0.23.10'\n"
            "app = marimo.App()\n\n"
            "@app.cell\n"
            "def _():\n"
            "    'browser-equivalent proxy load'\n"
            "    return\n\n"
            "if __name__ == '__main__':\n"
            "    app.run()\n",
        )
        await publish_notebook(api_client, owner_headers, str(notebook["id"]), "public")

        session_response = await api_client.post("/api/sessions", json={"notebook_id": notebook["id"], "mode": "run"})
        assert session_response.status_code == 201

        load_response = await api_client.get(session_response.json()["proxy_url"], follow_redirects=True)

        assert load_response.status_code == 200
        assert b"marimo" in load_response.content.lower()
        assert load_response.url.path.startswith(f"/api/proxy/{session_response.json()['id']}")
    finally:
        app.dependency_overrides.pop(get_process_manager, None)
        await manager.shutdown()


def test_websocket_proxy_relays_frames_and_touches_session(
    fake_process_manager: FakeProcessManager,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session_id = uuid4()
    sent_messages: list[str | bytes] = []
    connected_urls: list[str] = []

    fake_process_manager.sessions[session_id] = SessionInfo(
        id=session_id,
        notebook_id=uuid4(),
        mode="run",
        port=9000,
        pid=123,
        last_active=datetime.now(UTC),
        creator_id=None,
    )
    fake_process_manager.target_response = SessionTarget(
        http_base_url="http://127.0.0.1:9000",
        ws_base_url="ws://127.0.0.1:9000",
        access_token="secret",
    )

    class FakeUpstream:
        def __init__(self) -> None:
            self._messages: list[str | bytes] = []

        async def __aenter__(self) -> "FakeUpstream":
            return self

        async def __aexit__(self, exc_type: object, exc: object, traceback: object) -> None:
            return None

        async def send(self, message: str | bytes) -> None:
            sent_messages.append(message)
            self._messages.append(f"upstream:{message}" if isinstance(message, str) else b"upstream:" + message)

        async def close(self) -> None:
            return None

        def __aiter__(self) -> "FakeUpstream":
            return self

        async def __anext__(self) -> str | bytes:
            while not self._messages:
                await proxy_module.asyncio.sleep(0)
            return self._messages.pop(0)

    def fake_connect(url: str) -> FakeUpstream:
        connected_urls.append(url)
        return FakeUpstream()

    monkeypatch.setattr(proxy_module.websockets, "connect", fake_connect)

    with TestClient(app) as client:
        with client.websocket_connect(f"/api/proxy/{session_id}/ws?client=browser") as websocket:
            websocket.send_text("hello")
            assert websocket.receive_text() == "upstream:hello"

    assert connected_urls == ["ws://127.0.0.1:9000/ws?client=browser&access_token=secret"]
    assert sent_messages == ["hello"]
    assert fake_process_manager.touches == [session_id, session_id]
