import asyncio
import base64
from collections.abc import Callable, Generator, Mapping
import copy
import pathlib
from typing import Any, cast
from uuid import UUID, uuid4

from kubernetes_asyncio import client
from kubernetes_asyncio.client.exceptions import ApiException
from kubernetes_asyncio.config.config_exception import ConfigException
import pytest
import yaml

from app.core.config import get_settings
from app.models import Deployment, Notebook
from app.services import runtime_contract as contract
import app.services.kube_session_manager as kube_module
from app.services.kube_session_manager import KubeSessionManager, _ActivityThrottle
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
_RUNTIME_IMAGE = "registry.example/marimo-runtime@sha256:" + "0" * 64


@pytest.fixture(autouse=True)
def _settings_env(monkeypatch: pytest.MonkeyPatch) -> Generator[None, None, None]:
    # These tests are unit tests of the manager, not the API, and must not
    # depend on another test module having primed the environment.
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://molab:molab@localhost:5432/molab_test")
    monkeypatch.setenv("SECRET_KEY", "test-secret-with-at-least-32-bytes")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


class FakeCustomObjectsApi:
    """Fake `CustomObjectsApi` backed by an in-memory dict of CR bodies.

    Patching the wake-request annotation flips the stored phase to `Ready`,
    standing in for the controller's reconcile loop so a poll immediately
    after a wake observes a resolved runtime without real cluster timing.

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
        self.delete_calls: list[tuple[str, object | None]] = []
        self.on_create: Any = None
        self.events: list[str] = events if events is not None else []
        # False leaves a woken CR as it was, as before the controller acts on it.
        self.wake_makes_ready = True

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
        *,
        _content_type: str | None = None,
    ) -> dict[str, Any]:
        # Like the real API server: an object body is only valid as a merge
        # patch. Without an explicit content type, kubernetes_asyncio sends
        # JSON Patch, and the server rejects the request.
        if _content_type != "application/merge-patch+json":
            raise ApiException(status=400, reason="BadRequest")
        if name not in self.objects:
            raise ApiException(status=404, reason="NotFound")
        self.patch_calls.append((name, copy.deepcopy(dict(body))))
        cr = self.objects[name]
        annotations = body.get("metadata", {}).get("annotations", {})
        cr.setdefault("metadata", {}).setdefault("annotations", {}).update(annotations)
        if contract.ANNOTATION_WAKE_REQUEST in annotations:
            if self.wake_makes_ready:
                cr["status"] = {
                    "phase": "Ready",
                    "serviceName": cr["status"].get("serviceName", "msess-svc"),
                }
            self.events.append(f"wake_annotation:{name}")
        return copy.deepcopy(cr)

    async def delete_namespaced_custom_object(
        self,
        group: str,
        version: str,
        namespace: str,
        plural: str,
        name: str,
        body: object | None = None,
    ) -> dict[str, Any]:
        if name not in self.objects:
            raise ApiException(status=404, reason="NotFound")
        self.delete_calls.append((name, body))
        return self.objects.pop(name)

    async def list_namespaced_custom_object(
        self, group: str, version: str, namespace: str, plural: str, label_selector: str = ""
    ) -> dict[str, Any]:
        items = list(self.objects.values())
        for clause in filter(None, label_selector.split(",")):
            if "!=" in clause:
                key, _, value = clause.partition("!=")
                items = [
                    item
                    for item in items
                    if item.get("metadata", {}).get("labels", {}).get(key) != value
                ]
            elif "=" in clause:
                key, _, value = clause.partition("=")
                items = [
                    item
                    for item in items
                    if item.get("metadata", {}).get("labels", {}).get(key) == value
                ]
        return {"items": copy.deepcopy(items)}


class FakeCoreV1Api:
    """Fake `CoreV1Api` that mirrors the server's stringData -> base64 data move.

    Deliberately has no `patch_namespaced_secret`: the real manager never
    calls it either, since the credential Secret is created once, immutable,
    and never patched again (see `KubeSessionManager._wake`).
    """

    def __init__(self) -> None:
        self.secrets: dict[str, client.V1Secret] = {}
        self.create_calls: list[client.V1Secret] = []
        # Runs just before a create is processed, so a test can land another
        # replica's same-name Secret between this replica's read and create.
        self.before_create: Callable[[client.V1Secret], object] | None = None

    @staticmethod
    def _encode(string_data: dict[str, str]) -> dict[str, str]:
        return {
            key: base64.b64encode(value.encode("utf-8")).decode("ascii")
            for key, value in string_data.items()
        }

    async def create_namespaced_secret(
        self, namespace: str, body: client.V1Secret
    ) -> client.V1Secret:
        if self.before_create is not None:
            self.before_create(body)
        if body.metadata.name in self.secrets:
            raise ApiException(status=409, reason="AlreadyExists")
        stored = client.V1Secret(
            metadata=body.metadata,
            data=self._encode(body.string_data or {}),
            type=body.type,
            immutable=body.immutable,
        )
        self.secrets[body.metadata.name] = stored
        self.create_calls.append(stored)
        return stored

    async def read_namespaced_secret(self, name: str, namespace: str) -> client.V1Secret:
        if name not in self.secrets:
            raise ApiException(status=404, reason="NotFound")
        return self.secrets[name]


def _notebook(*, notebook_id: UUID | None = None, workspace_id: UUID | None = None) -> Notebook:
    return Notebook(
        id=notebook_id or uuid4(),
        workspace_id=workspace_id or uuid4(),
        title="Kube-backed notebook",
    )


def _deployment(
    notebook: Notebook,
    *,
    deployment_id: UUID | None = None,
    slug: str = "plasma-dashboard",
    revision: int = 1,
) -> Deployment:
    return Deployment(
        id=deployment_id or uuid4(),
        notebook_id=notebook.id,
        slug=slug,
        revision=revision,
        runtime_image=_RUNTIME_IMAGE,
        source_snapshot="x = 1",
        source_sha256="deadbeef",
    )


def _manager(
    custom: FakeCustomObjectsApi, core: FakeCoreV1Api, *, ready_timeout_seconds: float = 5.0
) -> KubeSessionManager:
    return KubeSessionManager(
        namespace=_NAMESPACE,
        service_dns_suffix=_DNS_SUFFIX,
        service_port=_PORT,
        runtime_image=_RUNTIME_IMAGE,
        ready_timeout_seconds=ready_timeout_seconds,
        delete_timeout_seconds=5.0,
        clients=(custom, core),
    )


def _mark_ready(cr: dict[str, Any], *, service_name: str = "msess-svc") -> None:
    cr["status"] = {
        "phase": "Ready",
        "serviceName": service_name,
        "conditions": [{"type": "Ready", "status": "True"}],
    }


def _mark_quota_exceeded(cr: dict[str, Any]) -> None:
    cr["status"] = {
        "phase": "Pending",
        "conditions": [
            {
                "type": "CapacityAvailable",
                "status": "False",
                "reason": "QuotaExceeded",
                "message": "pods quota exhausted",
            }
        ],
    }


def _mark_failed(cr: dict[str, Any], *, reason: str = "RuntimeExited") -> None:
    cr["status"] = {
        "phase": "Failed",
        "conditions": [
            {"type": "Ready", "status": "False", "reason": reason, "message": "container exited 1"}
        ],
    }


def _owner_reference(cr_name: str, uid: str, *, controller: bool = True) -> client.V1OwnerReference:
    return client.V1OwnerReference(
        api_version=contract.API_VERSION,
        kind=contract.KIND,
        name=cr_name,
        uid=uid,
        controller=controller,
    )


def _existing_secret(
    secret_name: str, owner_references: list[client.V1OwnerReference] | None
) -> client.V1Secret:
    """A complete Runtime Secret already in the namespace, owned as the test says."""
    return client.V1Secret(
        metadata=client.V1ObjectMeta(name=secret_name, owner_references=owner_references),
        data={
            "MARIMO_TOKEN": base64.b64encode(b"token").decode(),
            "RUNTIME_CREDENTIAL": base64.b64encode(b"mh_rt_v1.x.abc").decode(),
        },
        type="Opaque",
        immutable=True,
    )


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
        contract.LABEL_NOTEBOOK: str(notebook.id),
        contract.LABEL_WORKSPACE: str(notebook.workspace_id),
        contract.LABEL_MODE: "edit",
    }
    assert custom.create_calls[0]["spec"]["baseUrl"] == f"/api/proxy/{session.id}"


@pytest.mark.asyncio
async def test_spawn_deployment_creates_cr_bound_to_revision_and_image() -> None:
    custom, core = FakeCustomObjectsApi(), FakeCoreV1Api()
    custom.on_create = _mark_ready
    notebook = _notebook()
    deployment = _deployment(notebook, revision=7)
    manager = _manager(custom, core)

    session = await manager.spawn_deployment(notebook, deployment)

    assert session.id == deployment.id
    assert str(deployment.id) in custom.objects
    spec = custom.create_calls[0]["spec"]
    assert spec["baseUrl"] == f"/api/deployments/{deployment.slug}"
    assert spec["mode"] == "deploy"
    assert spec["deploymentRevision"] == 7
    assert spec["image"] == _RUNTIME_IMAGE


@pytest.mark.asyncio
async def test_spawn_creates_immutable_secret_with_owner_ref_and_both_keys() -> None:
    custom, core = FakeCustomObjectsApi(), FakeCoreV1Api()
    custom.on_create = _mark_ready
    notebook = _notebook()
    manager = _manager(custom, core)

    session = await manager.spawn(notebook, "run")

    secret = core.secrets[f"msess-{session.id}-env"]
    assert secret.metadata.owner_references[0].name == str(session.id)
    assert secret.metadata.owner_references[0].uid == f"uid-{session.id}"
    assert secret.metadata.owner_references[0].kind == "MarimoSession"
    assert set(secret.data or {}) == {"MARIMO_TOKEN", "RUNTIME_CREDENTIAL"}
    assert secret.immutable is True


@pytest.mark.asyncio
async def test_spawned_runtime_credential_is_bound_to_the_runtime_id() -> None:
    custom, core = FakeCustomObjectsApi(), FakeCoreV1Api()
    custom.on_create = _mark_ready
    manager = _manager(custom, core)

    session = await manager.spawn(_notebook(), "run")

    secret = core.secrets[f"msess-{session.id}-env"]
    credential = base64.b64decode(secret.data["RUNTIME_CREDENTIAL"]).decode()
    assert credential.startswith(f"mh_rt_v1.{session.id}.")


@pytest.mark.asyncio
async def test_spawn_deployment_wake_only_annotates_and_never_touches_the_secret() -> None:
    custom, core = FakeCustomObjectsApi(), FakeCoreV1Api()
    notebook = _notebook()
    deployment = _deployment(notebook)
    name = str(deployment.id)

    custom.objects[name] = {
        "apiVersion": contract.API_VERSION,
        "kind": contract.KIND,
        "metadata": {"name": name, "uid": f"uid-{name}"},
        "spec": {
            "notebookId": str(notebook.id),
            "workspaceId": str(notebook.workspace_id),
            "creatorId": None,
            "mode": "deploy",
            "baseUrl": f"/api/deployments/{deployment.slug}",
            "deploymentRevision": deployment.revision,
        },
        "status": {"phase": "Sleeping"},
    }
    existing_secret = client.V1Secret(
        metadata=client.V1ObjectMeta(name=f"msess-{name}-env"),
        data={
            "MARIMO_TOKEN": base64.b64encode(b"token").decode(),
            "RUNTIME_CREDENTIAL": base64.b64encode(f"mh_rt_v1.{name}.abc".encode()).decode(),
        },
        type="Opaque",
        immutable=True,
    )
    core.secrets[f"msess-{name}-env"] = existing_secret
    manager = _manager(custom, core)

    session = await manager.spawn_deployment(notebook, deployment)

    assert session.id == deployment.id
    assert session.phase == SessionPhase.READY
    # A wake is nothing but the annotation: RUNTIME_CREDENTIAL never expires
    # and the Secret is immutable, so there is nothing left to refresh, and
    # `create_namespaced_secret` (the only write path) is never called again
    # once the Secret already exists.
    assert core.secrets[f"msess-{name}-env"] is existing_secret
    assert core.create_calls == []
    wake_patch_name, wake_patch_body = custom.patch_calls[0]
    assert wake_patch_name == name
    assert contract.ANNOTATION_WAKE_REQUEST in wake_patch_body["metadata"]["annotations"]


@pytest.mark.asyncio
async def test_wake_annotation_is_a_random_token_not_a_timestamp() -> None:
    custom, core = FakeCustomObjectsApi(), FakeCoreV1Api()
    notebook = _notebook()
    deployment = _deployment(notebook)
    name = str(deployment.id)
    custom.objects[name] = {
        "metadata": {"name": name, "uid": f"uid-{name}"},
        "spec": {
            "notebookId": str(notebook.id),
            "workspaceId": str(notebook.workspace_id),
            "creatorId": None,
            "mode": "deploy",
            "baseUrl": f"/api/deployments/{deployment.slug}",
            "deploymentRevision": deployment.revision,
        },
        "status": {"phase": "Sleeping"},
    }
    core.secrets[f"msess-{name}-env"] = client.V1Secret(
        metadata=client.V1ObjectMeta(name=f"msess-{name}-env"),
        data={"MARIMO_TOKEN": base64.b64encode(b"t").decode()},
        type="Opaque",
        immutable=True,
    )
    manager = _manager(custom, core)

    await manager.spawn_deployment(notebook, deployment)

    _, body = custom.patch_calls[0]
    token = body["metadata"]["annotations"][contract.ANNOTATION_WAKE_REQUEST]
    # An RFC3339 timestamp always contains ':' (and typically 'T'); a
    # `secrets.token_urlsafe` value never does. This is the cheapest
    # reliable way to prove the annotation carries an opaque token, not a
    # clock reading the caller could ever construct or predict.
    assert ":" not in token


@pytest.mark.asyncio
async def test_spawn_deployment_already_ready_returns_existing_info_without_waking() -> None:
    custom, core = FakeCustomObjectsApi(), FakeCoreV1Api()
    notebook = _notebook()
    deployment = _deployment(notebook)
    name = str(deployment.id)
    custom.objects[name] = {
        "metadata": {"name": name},
        "spec": {
            "notebookId": str(notebook.id),
            "workspaceId": str(notebook.workspace_id),
            "creatorId": None,
            "mode": "deploy",
            "baseUrl": f"/api/deployments/{deployment.slug}",
            "deploymentRevision": deployment.revision,
        },
        "status": {"phase": "Ready", "serviceName": "msess-svc"},
    }
    manager = _manager(custom, core)

    session = await manager.spawn_deployment(notebook, deployment)

    assert session.id == deployment.id
    assert session.phase == SessionPhase.READY
    assert core.create_calls == []
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
    assert (
        target.http_base_url
        == f"http://msess-abc.{_NAMESPACE}.{_DNS_SUFFIX}:{_PORT}/api/proxy/{session.id}"
    )
    assert (
        target.ws_base_url
        == f"ws://msess-abc.{_NAMESPACE}.{_DNS_SUFFIX}:{_PORT}/api/proxy/{session.id}"
    )
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
async def test_target_is_none_while_cr_is_terminating() -> None:
    custom, core = FakeCustomObjectsApi(), FakeCoreV1Api()
    notebook = _notebook()
    name = str(uuid4())
    custom.objects[name] = {
        "metadata": {"name": name, "deletionTimestamp": "2026-01-01T00:00:00Z"},
        "spec": {
            "notebookId": str(notebook.id),
            "workspaceId": str(notebook.workspace_id),
            "creatorId": None,
            "mode": "run",
            "baseUrl": f"/api/proxy/{name}",
        },
        "status": {"phase": "Ready", "serviceName": "msess-svc"},
    }
    manager = _manager(custom, core)

    assert await manager.target(UUID(name)) is None


@pytest.mark.asyncio
async def test_target_is_none_when_observed_generation_lags() -> None:
    custom, core = FakeCustomObjectsApi(), FakeCoreV1Api()
    notebook = _notebook()
    name = str(uuid4())
    custom.objects[name] = {
        "metadata": {"name": name, "generation": 2},
        "spec": {
            "notebookId": str(notebook.id),
            "workspaceId": str(notebook.workspace_id),
            "creatorId": None,
            "mode": "run",
            "baseUrl": f"/api/proxy/{name}",
        },
        "status": {"phase": "Ready", "serviceName": "msess-svc", "observedGeneration": 1},
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
async def test_mark_active_writes_a_random_token_not_a_timestamp() -> None:
    custom, core = FakeCustomObjectsApi(), FakeCoreV1Api()
    custom.on_create = _mark_ready
    manager = _manager(custom, core)
    session = await manager.spawn(_notebook(), "run")
    custom.patch_calls.clear()

    await manager.mark_active(session.id)

    _, body = custom.patch_calls[0]
    token = body["metadata"]["annotations"][contract.ANNOTATION_ACTIVITY]
    assert ":" not in token


@pytest.mark.asyncio
async def test_mark_active_does_not_advance_throttle_on_failed_patch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    custom, core = FakeCustomObjectsApi(), FakeCoreV1Api()
    custom.on_create = _mark_ready
    manager = _manager(custom, core)
    session = await manager.spawn(_notebook(), "run")
    custom.patch_calls.clear()

    async def _boom(*args: object, **kwargs: object) -> None:
        raise RuntimeError("cluster unavailable")

    monkeypatch.setattr(custom, "patch_namespaced_custom_object", _boom)
    await manager.mark_active(session.id)  # swallowed; throttle must not advance

    monkeypatch.undo()
    await manager.mark_active(session.id)  # should still attempt, not be suppressed

    assert len(custom.patch_calls) == 1


def test_activity_throttle_evicts_least_recently_used_beyond_max_entries() -> None:
    throttle = _ActivityThrottle(max_entries=2)
    a, b, c = uuid4(), uuid4(), uuid4()

    throttle.record(a, 0.0)
    throttle.record(b, 1.0)
    throttle.record(c, 2.0)  # evicts `a`, the least recently used

    assert throttle.ready(a, 100.0, window_seconds=1000.0) is True
    assert throttle.ready(b, 100.0, window_seconds=1000.0) is False
    assert throttle.ready(c, 100.0, window_seconds=1000.0) is False


@pytest.mark.asyncio
async def test_spawn_raises_capacity_error_on_quota_condition() -> None:
    custom, core = FakeCustomObjectsApi(), FakeCoreV1Api()
    custom.on_create = _mark_quota_exceeded
    manager = _manager(custom, core)

    with pytest.raises(SessionCapacityError):
        await manager.spawn(_notebook(), "run")


def _quota_blocked_deploy_cr(
    notebook: Notebook, deployment: Deployment, *, phase: str = "Starting"
) -> dict[str, Any]:
    """A deploy CR whose last start a ResourceQuota rejected, as the operator leaves it."""
    name = str(deployment.id)
    return {
        "apiVersion": contract.API_VERSION,
        "kind": contract.KIND,
        "metadata": {
            "name": name,
            "uid": f"uid-{name}",
            "annotations": {contract.ANNOTATION_WAKE_REQUEST: "earlier-visit"},
        },
        "spec": {
            "notebookId": str(notebook.id),
            "workspaceId": str(notebook.workspace_id),
            "creatorId": None,
            "mode": "deploy",
            "baseUrl": f"/api/deployments/{deployment.slug}",
            "deploymentRevision": deployment.revision,
        },
        "status": {
            "phase": phase,
            "observedWakeRequest": "earlier-visit",
            "conditions": [
                {
                    "type": "CapacityAvailable",
                    "status": "False",
                    "reason": "QuotaExceeded",
                    "message": "pods quota exhausted",
                }
            ],
        },
    }


def _wake_requests(custom: FakeCustomObjectsApi) -> list[str]:
    return [
        body["metadata"]["annotations"][contract.ANNOTATION_WAKE_REQUEST]
        for _, body in custom.patch_calls
        if contract.ANNOTATION_WAKE_REQUEST in body.get("metadata", {}).get("annotations", {})
    ]


def _quota_blocked_deployment() -> tuple[FakeCustomObjectsApi, Notebook, Deployment]:
    custom = FakeCustomObjectsApi()
    custom.wake_makes_ready = False  # the operator has not retried yet
    notebook = _notebook()
    deployment = _deployment(notebook)
    custom.objects[str(deployment.id)] = _quota_blocked_deploy_cr(notebook, deployment)
    return custom, notebook, deployment


@pytest.mark.asyncio
async def test_visit_to_a_quota_blocked_deployment_requests_a_retry() -> None:
    """The operator retries a quota-rejected start only on a fresh wake request."""
    custom, notebook, deployment = _quota_blocked_deployment()
    manager = _manager(custom, FakeCoreV1Api())

    with pytest.raises(SessionCapacityError, match="pods quota exhausted"):
        await manager.spawn_deployment(notebook, deployment)

    (wake,) = _wake_requests(custom)
    assert wake != "earlier-visit"


@pytest.mark.asyncio
async def test_burst_of_visitors_to_a_quota_blocked_deployment_requests_one_retry() -> None:
    custom, notebook, deployment = _quota_blocked_deployment()
    real_patch = custom.patch_namespaced_custom_object

    async def _patch_in_flight(
        group: str,
        version: str,
        namespace: str,
        plural: str,
        name: str,
        body: Mapping[str, Any],
        *,
        _content_type: str | None = None,
    ) -> dict[str, Any]:
        await asyncio.sleep(0.01)  # other visitors arrive while the PATCH is in flight
        return await real_patch(
            group, version, namespace, plural, name, body, _content_type=_content_type
        )

    custom.patch_namespaced_custom_object = _patch_in_flight  # ty: ignore[invalid-assignment]
    manager = _manager(custom, FakeCoreV1Api())

    outcomes = await asyncio.gather(
        *(manager.spawn_deployment(notebook, deployment) for _ in range(5)),
        return_exceptions=True,
    )

    assert all(isinstance(outcome, SessionCapacityError) for outcome in outcomes)
    assert len(_wake_requests(custom)) == 1


@pytest.mark.asyncio
async def test_quota_blocked_deployment_is_retried_again_once_the_window_passes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(kube_module, "QUOTA_RETRY_COALESCE_SECONDS", 0.05)
    custom, notebook, deployment = _quota_blocked_deployment()
    manager = _manager(custom, FakeCoreV1Api())

    for _ in range(2):
        with pytest.raises(SessionCapacityError):
            await manager.spawn_deployment(notebook, deployment)
    assert len(_wake_requests(custom)) == 1

    await asyncio.sleep(0.06)
    with pytest.raises(SessionCapacityError):
        await manager.spawn_deployment(notebook, deployment)

    assert len(_wake_requests(custom)) == 2


@pytest.mark.asyncio
async def test_failed_deployment_is_never_woken_even_with_a_quota_condition() -> None:
    custom = FakeCustomObjectsApi()
    notebook = _notebook()
    deployment = _deployment(notebook)
    cr = _quota_blocked_deploy_cr(notebook, deployment, phase="Failed")
    custom.objects[str(deployment.id)] = cr
    manager = _manager(custom, FakeCoreV1Api())

    with pytest.raises(SessionCapacityError):
        await manager.spawn_deployment(notebook, deployment)

    assert _wake_requests(custom) == []


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
    custom.on_create = _mark_failed
    manager = _manager(custom, core)

    with pytest.raises(NotebookStartupError):
        await manager.spawn(_notebook(), "run")


@pytest.mark.asyncio
async def test_get_reports_sanitized_failure_reason_and_message() -> None:
    custom, core = FakeCustomObjectsApi(), FakeCoreV1Api()
    custom.on_create = lambda cr: _mark_failed(cr, reason="OOMKilled")
    manager = _manager(custom, core)
    notebook = _notebook()

    with pytest.raises(NotebookStartupError):
        await manager.spawn(notebook, "run")

    # `spawn`'s own failure compensates (deletes) the CR; this test re-creates
    # one directly to inspect `get()`'s projection in isolation.
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
        "status": {
            "phase": "Failed",
            "message": "killed",
            "conditions": [{"type": "Ready", "status": "False", "reason": "OOMKilled"}],
        },
    }

    info = await manager.get(UUID(name))

    assert info is not None
    assert info.phase == SessionPhase.FAILED
    assert info.failure_reason == "OOMKilled"
    assert info.message == "killed"


@pytest.mark.asyncio
async def test_spawn_failure_compensates_by_deleting_cr_and_secret() -> None:
    custom, core = FakeCustomObjectsApi(), FakeCoreV1Api()
    custom.on_create = _mark_failed
    manager = _manager(custom, core)

    with pytest.raises(NotebookStartupError):
        await manager.spawn(_notebook(), "run")

    assert custom.objects == {}
    assert custom.delete_calls
    deleted_name, delete_body = custom.delete_calls[0]
    assert isinstance(delete_body, client.V1DeleteOptions)
    assert delete_body.propagation_policy == "Foreground"
    assert deleted_name is not None


@pytest.mark.asyncio
async def test_spawn_compensation_preserves_original_exception_on_cancellation() -> None:
    custom, core = FakeCustomObjectsApi(), FakeCoreV1Api()
    manager = _manager(custom, core, ready_timeout_seconds=5.0)

    async def _raise_cancel(session_id: UUID) -> None:
        raise asyncio.CancelledError

    manager._poll_ready = _raise_cancel  # ty: ignore[invalid-assignment]

    with pytest.raises(asyncio.CancelledError):
        await manager.spawn(_notebook(), "run")

    assert custom.objects == {}  # compensating delete still ran despite the cancellation


@pytest.mark.asyncio
async def test_deploy_create_failure_does_not_delete_the_cr() -> None:
    """Deploy Runtimes are retained on failure; only edit/run creation compensates."""
    custom, core = FakeCustomObjectsApi(), FakeCoreV1Api()
    custom.on_create = _mark_failed
    notebook = _notebook()
    deployment = _deployment(notebook)
    manager = _manager(custom, core)

    with pytest.raises(NotebookStartupError):
        await manager.spawn_deployment(notebook, deployment)

    assert str(deployment.id) in custom.objects
    assert custom.delete_calls == []


@pytest.mark.asyncio
async def test_ensure_deploy_running_waits_for_terminating_cr_before_recreating() -> None:
    custom, core = FakeCustomObjectsApi(), FakeCoreV1Api()
    notebook = _notebook()
    deployment = _deployment(notebook)
    name = str(deployment.id)
    custom.objects[name] = {
        "metadata": {"name": name, "deletionTimestamp": "2026-01-01T00:00:00Z"},
        "spec": {"notebookId": str(notebook.id), "workspaceId": str(notebook.workspace_id)},
        "status": {"phase": "Ready"},
    }
    manager = _manager(custom, core)
    custom.on_create = _mark_ready
    real_get = custom.get_namespaced_custom_object
    calls = 0

    async def _gc_finishes_on_first_wait_poll(
        group: str, version: str, namespace: str, plural: str, name_arg: str
    ) -> dict[str, Any]:
        nonlocal calls
        if name_arg != name:
            return await real_get(group, version, namespace, plural, name_arg)
        calls += 1
        if calls == 1:
            # The CR's own initial fetch: still terminating, seen normally.
            return await real_get(group, version, namespace, plural, name_arg)
        if calls == 2:
            # The controller's GC finishes by the time `_wait_gone` first polls.
            custom.objects.pop(name, None)
            raise ApiException(status=404, reason="NotFound")
        # Every later read (the recreated CR's own polling) behaves normally.
        return await real_get(group, version, namespace, plural, name_arg)

    custom.get_namespaced_custom_object = _gc_finishes_on_first_wait_poll  # ty: ignore[invalid-assignment]

    session = await manager.spawn_deployment(notebook, deployment)

    assert session.phase == SessionPhase.READY


@pytest.mark.asyncio
async def test_missing_owned_secret_is_repaired_on_wake() -> None:
    custom, core = FakeCustomObjectsApi(), FakeCoreV1Api()
    notebook = _notebook()
    deployment = _deployment(notebook)
    name = str(deployment.id)
    custom.objects[name] = {
        "metadata": {"name": name, "uid": f"uid-{name}"},
        "spec": {
            "notebookId": str(notebook.id),
            "workspaceId": str(notebook.workspace_id),
            "creatorId": None,
            "mode": "deploy",
            "baseUrl": f"/api/deployments/{deployment.slug}",
            "deploymentRevision": deployment.revision,
        },
        "status": {"phase": "Sleeping"},
    }
    # No secret registered at all -- simulates it having been deleted out-of-band.
    manager = _manager(custom, core)

    async def _mark_after_wake(*args: object, **kwargs: object) -> dict[str, Any]:
        cr = custom.objects[name]
        cr["status"] = {"phase": "Ready", "serviceName": "msess-svc"}
        return copy.deepcopy(cr)

    custom.patch_namespaced_custom_object = _mark_after_wake  # ty: ignore[invalid-assignment]

    session = await manager.spawn_deployment(notebook, deployment)

    assert session.phase == SessionPhase.READY
    assert f"msess-{name}-env" in core.secrets


@pytest.mark.asyncio
async def test_secret_create_conflict_is_success_when_this_cr_controls_the_secret() -> None:
    """Two cold visitors start one Deployment, and both find no Secret.

    Both create one; the slower create conflicts with the Secret the faster
    one just minted for this very CR. That visitor must go on to the ready
    Runtime rather than fail.
    """
    custom, core = FakeCustomObjectsApi(), FakeCoreV1Api()
    custom.on_create = _mark_ready
    notebook = _notebook()
    deployment = _deployment(notebook)
    name = str(deployment.id)
    secret_name = f"msess-{name}-env"
    winner = _existing_secret(secret_name, [_owner_reference(name, f"uid-{name}")])
    core.before_create = lambda body: core.secrets.setdefault(body.metadata.name, winner)
    manager = _manager(custom, core)

    session = await manager.spawn_deployment(notebook, deployment)

    assert session.phase == SessionPhase.READY
    assert core.secrets[secret_name] is winner
    assert core.create_calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "owner_references",
    [
        pytest.param(
            lambda cr_name: [_owner_reference(cr_name, "uid-of-an-earlier-cr")],
            id="controlled-by-an-earlier-cr-of-the-same-name",
        ),
        pytest.param(
            lambda cr_name: [_owner_reference(cr_name, f"uid-{cr_name}", controller=False)],
            id="owned-but-not-controlled-by-this-cr",
        ),
        pytest.param(lambda cr_name: None, id="no-owner"),
    ],
)
async def test_secret_create_conflict_fails_clearly_when_this_cr_does_not_control_it(
    owner_references: Callable[[str], list[client.V1OwnerReference] | None],
) -> None:
    custom, core = FakeCustomObjectsApi(), FakeCoreV1Api()
    custom.on_create = _mark_ready
    notebook = _notebook()
    deployment = _deployment(notebook)
    name = str(deployment.id)
    secret_name = f"msess-{name}-env"
    foreign = _existing_secret(secret_name, owner_references(name))
    core.before_create = lambda body: core.secrets.setdefault(body.metadata.name, foreign)
    manager = _manager(custom, core)

    with pytest.raises(SessionStartError, match=f"{secret_name} .*not controlled by"):
        await manager.spawn_deployment(notebook, deployment)

    assert core.secrets[secret_name] is foreign  # neither replaced nor adopted
    assert core.create_calls == []


@pytest.mark.asyncio
async def test_secret_create_conflict_fails_clearly_when_the_secret_then_vanishes() -> None:
    custom, core = FakeCustomObjectsApi(), FakeCoreV1Api()
    custom.on_create = _mark_ready
    notebook = _notebook()
    deployment = _deployment(notebook)

    def _conflict_with_a_secret_that_is_then_gone(body: client.V1Secret) -> None:
        raise ApiException(status=409, reason="AlreadyExists")

    core.before_create = _conflict_with_a_secret_that_is_then_gone
    manager = _manager(custom, core)

    with pytest.raises(SessionStartError, match="disappeared"):
        await manager.spawn_deployment(notebook, deployment)


@pytest.mark.asyncio
async def test_spawn_compensates_when_a_secret_it_does_not_control_is_in_the_way() -> None:
    custom, core = FakeCustomObjectsApi(), FakeCoreV1Api()
    custom.on_create = _mark_ready
    core.before_create = lambda body: core.secrets.setdefault(
        body.metadata.name, _existing_secret(body.metadata.name, None)
    )
    manager = _manager(custom, core)

    with pytest.raises(SessionStartError, match="not controlled by"):
        await manager.spawn(_notebook(), "run")

    assert custom.objects == {}  # the edit/run CR it created was deleted again


@pytest.mark.asyncio
async def test_stop_deletes_cr_with_foreground_propagation_and_waits_for_not_found() -> None:
    custom, core = FakeCustomObjectsApi(), FakeCoreV1Api()
    custom.on_create = _mark_ready
    manager = _manager(custom, core)
    session = await manager.spawn(_notebook(), "run")

    await manager.stop(session.id)

    assert str(session.id) not in custom.objects
    name, body = custom.delete_calls[0]
    assert name == str(session.id)
    assert isinstance(body, client.V1DeleteOptions)
    assert body.propagation_policy == "Foreground"
    with pytest.raises(SessionNotFoundError):
        await manager.stop(session.id)


@pytest.mark.asyncio
async def test_stop_workspace_sessions_deletes_only_non_deploy_runtimes_in_workspace() -> None:
    custom, core = FakeCustomObjectsApi(), FakeCoreV1Api()
    workspace_id = uuid4()
    other_workspace_id = uuid4()
    notebook_in = _notebook(workspace_id=workspace_id)
    notebook_out = _notebook(workspace_id=other_workspace_id)
    manager = _manager(custom, core)

    custom.on_create = _mark_ready
    edit_session = await manager.spawn(notebook_in, "edit")
    run_session = await manager.spawn(notebook_in, "run")
    other_ws_session = await manager.spawn(notebook_out, "run")
    deployment = _deployment(notebook_in)
    deploy_session = await manager.spawn_deployment(notebook_in, deployment)

    await manager.stop_workspace_sessions(workspace_id)

    assert str(edit_session.id) not in custom.objects
    assert str(run_session.id) not in custom.objects
    assert str(other_ws_session.id) in custom.objects
    assert str(deploy_session.id) in custom.objects  # deploy runtimes are out of scope here


@pytest.mark.asyncio
async def test_reconcilable_runtimes_lists_every_cr_in_the_namespace() -> None:
    custom, core = FakeCustomObjectsApi(), FakeCoreV1Api()
    custom.on_create = _mark_ready
    manager = _manager(custom, core)
    notebook = _notebook()
    edit_session = await manager.spawn(notebook, "edit")
    deployment = _deployment(notebook, revision=3)
    deploy_session = await manager.spawn_deployment(notebook, deployment)

    refs = await manager.reconcilable_runtimes()

    by_id = {ref.id: ref for ref in refs}
    assert by_id[edit_session.id].mode == "edit"
    assert by_id[edit_session.id].workspace_id == notebook.workspace_id
    assert by_id[deploy_session.id].mode == "deploy"
    assert by_id[deploy_session.id].deployment_revision == 3


@pytest.mark.asyncio
async def test_shutdown_leaves_injected_clients_alone() -> None:
    custom, core = FakeCustomObjectsApi(), FakeCoreV1Api()
    custom.on_create = _mark_ready
    manager = _manager(custom, core)

    await manager.shutdown()

    # Still served by the injected pair: nothing was closed or swapped out.
    session = await manager.spawn(_notebook(), "run")
    assert str(session.id) in custom.objects


def _not_in_a_cluster() -> None:
    raise ConfigException("Service host/port is not set.")


async def _no_kubeconfig_to_load() -> None:
    return None


@pytest.mark.asyncio
async def test_shutdown_closes_the_api_client_the_manager_built(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(kube_module.kube_config, "load_incluster_config", _not_in_a_cluster)
    monkeypatch.setattr(kube_module.kube_config, "load_kube_config", _no_kubeconfig_to_load)
    manager = KubeSessionManager(
        namespace=_NAMESPACE,
        service_dns_suffix=_DNS_SUFFIX,
        service_port=_PORT,
        runtime_image=_RUNTIME_IMAGE,
        ready_timeout_seconds=5.0,
    )
    await manager._clients()
    api_client = manager._api_client
    assert api_client is not None
    http_session = api_client.rest_client.pool_manager
    assert not http_session.closed

    await manager.shutdown()
    await manager.shutdown()  # nothing left to close; must not fail

    assert http_session.closed
    assert manager._api_client is None


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

    deploy_notebook = _notebook()
    await manager.spawn(_notebook(), "edit")
    await manager.spawn_deployment(deploy_notebook, _deployment(deploy_notebook))

    assert custom.create_calls, "expected at least one CR create"
    for created in custom.create_calls:
        spec = created["spec"]
        missing = [field for field in required if spec.get(field) is None]
        assert not missing, f"CR spec missing required field(s) {missing}: {spec}"
