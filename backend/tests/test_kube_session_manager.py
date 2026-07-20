import base64
from collections.abc import Generator, Mapping
import copy
import pathlib
from typing import Any, cast
from uuid import UUID, uuid4

from kubernetes_asyncio import client
from kubernetes_asyncio.client.exceptions import ApiException
import pytest
import yaml

from app.core.config import get_settings
from app.models import Notebook
from app.services import kube_session_manager as kube_module
from app.services.kube_session_manager import KubeSessionManager
from app.services.session_manager import (
    NotebookStartupError,
    SessionCapacityError,
    SessionNotFoundError,
    SessionPhase,
    SessionStartError,
)

_NAMESPACE = "marimohub-sessions"
_DNS_SUFFIX = "svc"
_PORT = 8080


@pytest.fixture(autouse=True)
def _settings_env(monkeypatch: pytest.MonkeyPatch) -> Generator[None, None, None]:
    # create_session_token() reads SECRET_KEY/SESSION_TOKEN_TTL_SECONDS from
    # settings; these tests are unit tests of the manager, not the API, and
    # must not depend on another test module having primed the environment.
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://molab:molab@localhost:5432/molab_test")
    monkeypatch.setenv("SECRET_KEY", "test-secret-with-at-least-32-bytes")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


class FakeCustomObjectsApi:
    """Fake `CustomObjectsApi` backed by an in-memory dict of CR bodies.

    Patching the wake annotation flips the stored phase to `Ready`, standing
    in for the controller's reconcile loop so a poll immediately after a wake
    observes a resolved session without real cluster timing.

    An optional shared `events` list, handed to both this fake and a
    `FakeCoreV1Api`, lets a test observe the *global* call order across the
    two clients (each fake's own `patch_calls` only orders calls against
    itself, which can't tell a secret-refresh-then-annotate timeline from an
    annotate-then-refresh one).
    """

    def __init__(self, events: list[str] | None = None) -> None:
        self.objects: dict[str, dict[str, Any]] = {}
        self.create_calls: list[dict[str, Any]] = []
        self.patch_calls: list[tuple[str, dict[str, Any]]] = []
        self.delete_calls: list[str] = []
        self.on_create: Any = None
        self.events: list[str] = events if events is not None else []

    async def create_namespaced_custom_object(
        self, group: str, version: str, namespace: str, plural: str, body: Mapping[str, Any]
    ) -> dict[str, Any]:
        name = body["metadata"]["name"]
        if name in self.objects:
            raise ApiException(status=409, reason="AlreadyExists")
        stored: dict[str, Any] = copy.deepcopy(dict(body))
        stored["metadata"]["uid"] = f"uid-{name}"
        stored.setdefault("status", {})
        if self.on_create is not None:
            self.on_create(stored)
        self.objects[name] = stored
        self.create_calls.append(copy.deepcopy(stored))
        return copy.deepcopy(stored)

    async def get_namespaced_custom_object(
        self, group: str, version: str, namespace: str, plural: str, name: str
    ) -> dict[str, Any]:
        if name not in self.objects:
            raise ApiException(status=404, reason="NotFound")
        return copy.deepcopy(self.objects[name])

    async def patch_namespaced_custom_object(
        self,
        group: str,
        version: str,
        namespace: str,
        plural: str,
        name: str,
        body: Mapping[str, Any],
    ) -> dict[str, Any]:
        if name not in self.objects:
            raise ApiException(status=404, reason="NotFound")
        self.patch_calls.append((name, copy.deepcopy(dict(body))))
        cr = self.objects[name]
        annotations = body.get("metadata", {}).get("annotations", {})
        cr.setdefault("metadata", {}).setdefault("annotations", {}).update(annotations)
        if kube_module._WAKE_ANNOTATION in annotations:
            cr["status"]["phase"] = "Ready"
            self.events.append(f"wake_annotation:{name}")
        return copy.deepcopy(cr)

    async def delete_namespaced_custom_object(
        self, group: str, version: str, namespace: str, plural: str, name: str
    ) -> dict[str, Any]:
        if name not in self.objects:
            raise ApiException(status=404, reason="NotFound")
        self.delete_calls.append(name)
        return self.objects.pop(name)


class FakeCoreV1Api:
    """Fake `CoreV1Api` that mirrors the server's stringData -> base64 data move.

    See `FakeCustomObjectsApi`'s docstring for the shared `events` list.
    """

    def __init__(self, events: list[str] | None = None) -> None:
        self.secrets: dict[str, client.V1Secret] = {}
        self.create_calls: list[client.V1Secret] = []
        self.patch_calls: list[tuple[str, dict[str, Any]]] = []
        self.events: list[str] = events if events is not None else []

    @staticmethod
    def _encode(string_data: dict[str, str]) -> dict[str, str]:
        return {
            key: base64.b64encode(value.encode("utf-8")).decode("ascii")
            for key, value in string_data.items()
        }

    async def create_namespaced_secret(
        self, namespace: str, body: client.V1Secret
    ) -> client.V1Secret:
        stored = client.V1Secret(
            metadata=body.metadata, data=self._encode(body.string_data or {}), type=body.type
        )
        self.secrets[body.metadata.name] = stored
        self.create_calls.append(stored)
        return stored

    async def read_namespaced_secret(self, name: str, namespace: str) -> client.V1Secret:
        if name not in self.secrets:
            raise ApiException(status=404, reason="NotFound")
        return self.secrets[name]

    async def patch_namespaced_secret(
        self, name: str, namespace: str, body: Mapping[str, Any]
    ) -> client.V1Secret:
        if name not in self.secrets:
            raise ApiException(status=404, reason="NotFound")
        self.patch_calls.append((name, dict(body)))
        secret = self.secrets[name]
        data = dict(secret.data or {})
        data.update(self._encode(body.get("stringData", {})))
        secret.data = data
        if "SESSION_TOKEN" in body.get("stringData", {}):
            self.events.append(f"secret_refresh:{name}")
        return secret


def _notebook(*, notebook_id: UUID | None = None, workspace_id: UUID | None = None) -> Notebook:
    return Notebook(
        id=notebook_id or uuid4(),
        workspace_id=workspace_id or uuid4(),
        title="Kube-backed notebook",
    )


def _manager(
    custom: FakeCustomObjectsApi, core: FakeCoreV1Api, *, ready_timeout_seconds: float = 5.0
) -> KubeSessionManager:
    return KubeSessionManager(
        namespace=_NAMESPACE,
        service_dns_suffix=_DNS_SUFFIX,
        service_port=_PORT,
        runtime_image=None,
        ready_timeout_seconds=ready_timeout_seconds,
        clients=(custom, core),
    )


def _mark_ready(cr: dict[str, Any], *, service_name: str = "msess-svc") -> None:
    cr["status"] = {"phase": "Ready", "serviceName": service_name}


@pytest.mark.asyncio
async def test_spawn_uses_uuid4_cr_name_and_label_set() -> None:
    custom, core = FakeCustomObjectsApi(), FakeCoreV1Api()
    custom.on_create = _mark_ready
    notebook = _notebook()
    manager = _manager(custom, core)

    session = await manager.spawn(notebook, "edit")

    assert str(session.id) in custom.objects
    assert session.mode == "edit"
    assert session.phase == SessionPhase.READY
    labels = custom.create_calls[0]["metadata"]["labels"]
    assert labels == {
        "marimohub.io/notebook": str(notebook.id),
        "marimohub.io/workspace": str(notebook.workspace_id),
        "marimohub.io/mode": "edit",
    }
    assert custom.create_calls[0]["spec"]["baseUrl"] == f"/api/proxy/{session.id}"


@pytest.mark.asyncio
async def test_spawn_deployment_uses_deployment_id_as_cr_name() -> None:
    custom, core = FakeCustomObjectsApi(), FakeCoreV1Api()
    custom.on_create = _mark_ready
    notebook = _notebook()
    deployment_id = uuid4()
    manager = _manager(custom, core)

    session = await manager.spawn_deployment(notebook, deployment_id, "plasma-dashboard")

    assert session.id == deployment_id
    assert str(deployment_id) in custom.objects
    assert custom.create_calls[0]["spec"]["baseUrl"] == "/api/deployments/plasma-dashboard"
    assert custom.create_calls[0]["spec"]["mode"] == "deploy"


@pytest.mark.asyncio
async def test_spawn_creates_secret_with_owner_ref_and_both_tokens() -> None:
    custom, core = FakeCustomObjectsApi(), FakeCoreV1Api()
    custom.on_create = _mark_ready
    notebook = _notebook()
    manager = _manager(custom, core)

    session = await manager.spawn(notebook, "run")

    secret = core.secrets[f"msess-{session.id}-env"]
    assert secret.metadata.owner_references[0].name == str(session.id)
    assert secret.metadata.owner_references[0].uid == f"uid-{session.id}"
    assert secret.metadata.owner_references[0].kind == "MarimoSession"
    assert set(secret.data or {}) == {"MARIMO_TOKEN", "SESSION_TOKEN"}


@pytest.mark.asyncio
async def test_spawn_deployment_wake_refreshes_token_then_annotates_then_polls_ready() -> None:
    # A single shared event log (rather than each fake's own `patch_calls`) is
    # what actually proves *global* ordering: two independent per-client
    # lists can't distinguish "secret refreshed, then annotated" from
    # "annotated, then secret refreshed" when both happen to be index 0 in
    # their own list.
    events: list[str] = []
    custom, core = FakeCustomObjectsApi(events), FakeCoreV1Api(events)
    notebook = _notebook()
    deployment_id = uuid4()
    name = str(deployment_id)

    custom.objects[name] = {
        "apiVersion": "marimohub.io/v1alpha1",
        "kind": "MarimoSession",
        "metadata": {"name": name, "uid": f"uid-{name}"},
        "spec": {
            "notebookId": str(notebook.id),
            "workspaceId": str(notebook.workspace_id),
            "creatorId": None,
            "mode": "deploy",
            "baseUrl": "/api/deployments/plasma-dashboard",
        },
        "status": {"phase": "Sleeping"},
    }
    core.secrets[f"msess-{name}-env"] = client.V1Secret(
        metadata=client.V1ObjectMeta(name=f"msess-{name}-env"),
        data={
            "MARIMO_TOKEN": base64.b64encode(b"old-token").decode(),
            "SESSION_TOKEN": base64.b64encode(b"stale").decode(),
        },
        type="Opaque",
    )
    manager = _manager(custom, core)

    session = await manager.spawn_deployment(notebook, deployment_id, "plasma-dashboard")

    assert session.id == deployment_id
    assert session.phase == SessionPhase.READY
    # The shared event log orders across *both* clients, so this actually
    # pins the secret-refresh-before-wake-annotation timeline the wake
    # contract requires (a restarted pod must never boot with a stale token).
    assert events == [f"secret_refresh:msess-{name}-env", f"wake_annotation:{name}"]
    assert core.patch_calls[0][0] == f"msess-{name}-env"
    assert "SESSION_TOKEN" in core.patch_calls[0][1]["stringData"]
    wake_patch_name, wake_patch_body = custom.patch_calls[0]
    assert wake_patch_name == name
    assert kube_module._WAKE_ANNOTATION in wake_patch_body["metadata"]["annotations"]


@pytest.mark.asyncio
async def test_spawn_deployment_already_ready_returns_existing_info_without_waking() -> None:
    custom, core = FakeCustomObjectsApi(), FakeCoreV1Api()
    notebook = _notebook()
    deployment_id = uuid4()
    name = str(deployment_id)
    custom.objects[name] = {
        "metadata": {"name": name},
        "spec": {
            "notebookId": str(notebook.id),
            "workspaceId": str(notebook.workspace_id),
            "creatorId": None,
            "mode": "deploy",
            "baseUrl": "/api/deployments/plasma-dashboard",
        },
        "status": {"phase": "Ready", "serviceName": "msess-svc"},
    }
    manager = _manager(custom, core)

    session = await manager.spawn_deployment(notebook, deployment_id, "plasma-dashboard")

    assert session.id == deployment_id
    assert session.phase == SessionPhase.READY
    assert core.patch_calls == []
    assert custom.patch_calls == []


@pytest.mark.asyncio
async def test_target_built_from_service_name_namespace_suffix_port_and_secret_token() -> None:
    custom, core = FakeCustomObjectsApi(), FakeCoreV1Api()
    custom.on_create = lambda cr: _mark_ready(cr, service_name="msess-abc")
    notebook = _notebook()
    manager = _manager(custom, core)
    session = await manager.spawn(notebook, "run")

    target = await manager.target(session.id)

    assert target is not None
    assert target.http_base_url == f"http://msess-abc.{_NAMESPACE}.{_DNS_SUFFIX}:{_PORT}/api/proxy/{session.id}"
    assert target.ws_base_url == f"ws://msess-abc.{_NAMESPACE}.{_DNS_SUFFIX}:{_PORT}/api/proxy/{session.id}"
    stored_secret = core.secrets[f"msess-{session.id}-env"]
    assert target.access_token == base64.b64decode(stored_secret.data["MARIMO_TOKEN"]).decode()


@pytest.mark.asyncio
async def test_target_is_none_when_not_ready() -> None:
    custom, core = FakeCustomObjectsApi(), FakeCoreV1Api()
    notebook = _notebook()
    name = str(uuid4())
    custom.objects[name] = {
        "metadata": {"name": name},
        "spec": {
            "notebookId": str(notebook.id),
            "workspaceId": str(notebook.workspace_id),
            "creatorId": None,
            "mode": "run",
            "baseUrl": f"/api/proxy/{name}",
        },
        "status": {"phase": "Starting"},
    }
    manager = _manager(custom, core)

    assert await manager.target(UUID(name)) is None


@pytest.mark.asyncio
async def test_target_is_none_when_ready_but_no_service_name() -> None:
    custom, core = FakeCustomObjectsApi(), FakeCoreV1Api()
    notebook = _notebook()
    name = str(uuid4())
    custom.objects[name] = {
        "metadata": {"name": name},
        "spec": {
            "notebookId": str(notebook.id),
            "workspaceId": str(notebook.workspace_id),
            "creatorId": None,
            "mode": "run",
            "baseUrl": f"/api/proxy/{name}",
        },
        "status": {"phase": "Ready"},
    }
    manager = _manager(custom, core)

    assert await manager.target(UUID(name)) is None


@pytest.mark.asyncio
async def test_mark_active_coalesces_two_calls_into_one_patch() -> None:
    custom, core = FakeCustomObjectsApi(), FakeCoreV1Api()
    custom.on_create = _mark_ready
    manager = _manager(custom, core)
    session = await manager.spawn(_notebook(), "run")
    custom.patch_calls.clear()

    await manager.mark_active(session.id)
    await manager.mark_active(session.id)

    assert len(custom.patch_calls) == 1


@pytest.mark.asyncio
async def test_mark_active_never_raises_for_unknown_session() -> None:
    custom, core = FakeCustomObjectsApi(), FakeCoreV1Api()
    manager = _manager(custom, core)

    await manager.mark_active(uuid4())  # would 404 inside; must not propagate


@pytest.mark.asyncio
async def test_spawn_raises_capacity_error_on_quota_exceeded_sentinel() -> None:
    custom, core = FakeCustomObjectsApi(), FakeCoreV1Api()

    def _quota_exceeded(cr: dict[str, Any]) -> None:
        cr["status"] = {"phase": "Pending", "message": "QuotaExceeded: pods quota exhausted"}

    custom.on_create = _quota_exceeded
    manager = _manager(custom, core)

    with pytest.raises(SessionCapacityError):
        await manager.spawn(_notebook(), "run")


@pytest.mark.asyncio
async def test_spawn_raises_session_start_error_on_poll_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(kube_module, "_POLL_INTERVAL_SECONDS", 0.01)
    custom, core = FakeCustomObjectsApi(), FakeCoreV1Api()
    manager = _manager(custom, core, ready_timeout_seconds=0.03)

    with pytest.raises(SessionStartError):
        await manager.spawn(_notebook(), "run")


@pytest.mark.asyncio
async def test_spawn_raises_notebook_startup_error_on_failed_phase() -> None:
    custom, core = FakeCustomObjectsApi(), FakeCoreV1Api()

    def _failed(cr: dict[str, Any]) -> None:
        cr["status"] = {"phase": "Failed", "message": "container exited 1"}

    custom.on_create = _failed
    manager = _manager(custom, core)

    with pytest.raises(NotebookStartupError):
        await manager.spawn(_notebook(), "run")


@pytest.mark.asyncio
async def test_stop_deletes_cr_and_unknown_session_raises_not_found() -> None:
    custom, core = FakeCustomObjectsApi(), FakeCoreV1Api()
    custom.on_create = _mark_ready
    manager = _manager(custom, core)
    session = await manager.spawn(_notebook(), "run")

    await manager.stop(session.id)

    assert str(session.id) not in custom.objects
    with pytest.raises(SessionNotFoundError):
        await manager.stop(session.id)


@pytest.mark.asyncio
async def test_shutdown_is_a_no_op() -> None:
    manager = _manager(FakeCustomObjectsApi(), FakeCoreV1Api())

    await manager.shutdown()


# Risk register #4: kube backend correctness is unprovable without a real
# cluster, so this parses the checked-in operator contract
# (deploy/crd/marimosession.yaml) directly rather than hardcoding a
# duplicate required-field list, and fails the moment the manager's CR
# bodies drift from it.
_CRD_PATH = pathlib.Path(__file__).resolve().parents[2] / "deploy" / "crd" / "marimosession.yaml"


def _crd_required_spec_fields() -> list[str]:
    crd = yaml.safe_load(_CRD_PATH.read_text())
    schema = crd["spec"]["versions"][0]["schema"]["openAPIV3Schema"]
    required = schema["properties"]["spec"]["required"]
    return cast("list[str]", required)


@pytest.mark.asyncio
async def test_spawned_cr_bodies_carry_every_required_crd_spec_field() -> None:
    custom, core = FakeCustomObjectsApi(), FakeCoreV1Api()
    custom.on_create = _mark_ready
    manager = _manager(custom, core)
    required = _crd_required_spec_fields()
    assert required  # sanity: the CRD actually declares required fields

    await manager.spawn(_notebook(), "edit")
    await manager.spawn_deployment(_notebook(), uuid4(), "plasma-dashboard")

    assert custom.create_calls, "expected at least one CR create"
    for created in custom.create_calls:
        spec = created["spec"]
        missing = [field for field in required if spec.get(field) is None]
        assert not missing, f"CR spec missing required field(s) {missing}: {spec}"
