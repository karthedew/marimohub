import asyncio
import base64
from collections.abc import Awaitable, Mapping
from datetime import UTC, datetime
from http import HTTPStatus
import logging
import secrets
from typing import Any, Protocol, TypedDict, cast
from uuid import UUID, uuid4

from kubernetes_asyncio import client, config as kube_config
from kubernetes_asyncio.client.api.core_v1_api import CoreV1Api
from kubernetes_asyncio.client.api.custom_objects_api import CustomObjectsApi
from kubernetes_asyncio.client.exceptions import ApiException
from kubernetes_asyncio.config.config_exception import ConfigException

from app.core.config import Settings
from app.core.security import create_session_token
from app.models import Notebook
from app.services.session_manager import (
    NotebookStartupError,
    RuntimeMode,
    SessionCapacityError,
    SessionInfo,
    SessionMode,
    SessionNotFoundError,
    SessionPhase,
    SessionStartError,
    SessionTarget,
)

_GROUP = "marimohub.io"
_VERSION = "v1alpha1"
_PLURAL = "marimosessions"
_API_VERSION = f"{_GROUP}/{_VERSION}"
_KIND = "MarimoSession"
_WAKE_ANNOTATION = f"{_GROUP}/wake"
_LAST_ACTIVITY_ANNOTATION = f"{_GROUP}/last-activity"

_QUOTA_MESSAGE_PREFIX = "QuotaExceeded:"
_POLL_INTERVAL_SECONDS = 0.5

logger = logging.getLogger(__name__)

# Must stay below the CRD's minimum `spec.idleTimeoutSeconds` (30s) so a
# throttled activity annotation never lags enough to trip a premature
# idle-sleep. Currently 15 < 30; keep this invariant if either value changes.
MARK_ACTIVE_COALESCE_SECONDS = 15.0


class _CRSpec(TypedDict, total=False):
    notebookId: str
    workspaceId: str
    creatorId: str | None
    mode: str
    image: str
    baseUrl: str


class _CRStatus(TypedDict, total=False):
    phase: str
    serviceName: str
    podName: str
    lastActivity: str
    message: str


class _CustomResource(TypedDict, total=False):
    apiVersion: str
    kind: str
    metadata: dict[str, Any]
    spec: _CRSpec
    status: _CRStatus


def _secret_name(session_id: UUID) -> str:
    return f"msess-{session_id}-env"


def _rfc3339_now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _labels(notebook: Notebook, mode: str) -> dict[str, str]:
    return {
        f"{_GROUP}/notebook": str(notebook.id),
        f"{_GROUP}/workspace": str(notebook.workspace_id),
        f"{_GROUP}/mode": mode,
    }


def _phase_of(status: _CRStatus) -> SessionPhase:
    phase = status.get("phase")
    if phase == "Ready":
        return SessionPhase.READY
    if phase == "Sleeping":
        return SessionPhase.SLEEPING
    if phase in (None, "Pending", "Starting"):
        return SessionPhase.STARTING
    return SessionPhase.FAILED


def _cr_to_info(cr: _CustomResource) -> SessionInfo:
    spec = cr.get("spec", {})
    status = cr.get("status", {})
    metadata = cr.get("metadata", {})
    creator_id = spec.get("creatorId")
    last_activity = status.get("lastActivity")
    return SessionInfo(
        id=UUID(cast("str", metadata["name"])),
        notebook_id=UUID(spec["notebookId"]),
        mode=cast("RuntimeMode", spec["mode"]),
        phase=_phase_of(status),
        last_active=datetime.fromisoformat(last_activity) if last_activity else None,
        creator_id=UUID(creator_id) if creator_id else None,
    )


def _decode_secret_key(secret: client.V1Secret, key: str) -> str | None:
    data = secret.data or {}
    raw = data.get(key)
    return base64.b64decode(raw).decode("utf-8") if raw is not None else None


def _serving_name(cr: _CustomResource) -> str | None:
    """Return the CR's Service name if it is currently `Ready` and serving."""
    status = cr.get("status", {})
    if status.get("phase") != "Ready":
        return None
    return status.get("serviceName") or None


class _CustomObjectsClient(Protocol):
    """The slice of `CustomObjectsApi` the manager calls; narrow enough for a test fake.

    Declared with plain (non-``async``) methods returning ``Awaitable``, not
    ``Coroutine``: the real client wraps its calls in a way that is awaitable
    but not a native coroutine object, which an ``async def`` protocol method
    would reject structurally.
    """

    def create_namespaced_custom_object(
        self, group: str, version: str, namespace: str, plural: str, body: Mapping[str, Any]
    ) -> Awaitable[dict[str, Any]]: ...

    def get_namespaced_custom_object(
        self, group: str, version: str, namespace: str, plural: str, name: str
    ) -> Awaitable[dict[str, Any]]: ...

    def patch_namespaced_custom_object(
        self,
        group: str,
        version: str,
        namespace: str,
        plural: str,
        name: str,
        body: Mapping[str, Any],
    ) -> Awaitable[dict[str, Any]]: ...

    def delete_namespaced_custom_object(
        self, group: str, version: str, namespace: str, plural: str, name: str
    ) -> Awaitable[dict[str, Any]]: ...


class _CoreV1Client(Protocol):
    """The slice of `CoreV1Api` the manager calls; narrow enough for a test fake."""

    def create_namespaced_secret(
        self, namespace: str, body: client.V1Secret
    ) -> Awaitable[client.V1Secret]: ...

    def read_namespaced_secret(self, name: str, namespace: str) -> Awaitable[client.V1Secret]: ...

    def patch_namespaced_secret(
        self, name: str, namespace: str, body: Mapping[str, Any]
    ) -> Awaitable[client.V1Secret]: ...


class KubeSessionManager:
    """Implements `SessionManager` against `MarimoSession` custom resources.

    Holds no session dict — every lookup is a labelled get against the
    cluster, which is what lets any API replica answer `get`/`target`
    statelessly, the crash-safety a CRD-backed session buys over an
    in-process registry.
    """

    def __init__(
        self,
        *,
        namespace: str,
        service_dns_suffix: str,
        service_port: int,
        runtime_image: str | None,
        ready_timeout_seconds: float,
        clients: tuple[_CustomObjectsClient, _CoreV1Client] | None = None,
    ) -> None:
        """Configure cluster coordinates; an injected `clients` pair bypasses config loading."""
        self._namespace = namespace
        self._service_dns_suffix = service_dns_suffix
        self._service_port = service_port
        self._runtime_image = runtime_image
        self._ready_timeout_seconds = ready_timeout_seconds
        self._custom_objects_api, self._core_v1_api = clients or (None, None)
        self._client_lock = asyncio.Lock()
        self._last_marked_active: dict[UUID, float] = {}

    @classmethod
    def from_settings(cls, settings: Settings) -> "KubeSessionManager":
        """Build a kube session manager from application settings."""
        return cls(
            namespace=settings.SESSION_NAMESPACE,
            service_dns_suffix=settings.SESSION_SERVICE_DNS_SUFFIX,
            service_port=settings.SESSION_SERVICE_PORT,
            runtime_image=settings.SESSION_RUNTIME_IMAGE,
            ready_timeout_seconds=settings.SESSION_READY_TIMEOUT_SECONDS,
        )

    async def _clients(self) -> tuple[_CustomObjectsClient, _CoreV1Client]:
        if self._custom_objects_api is not None and self._core_v1_api is not None:
            return self._custom_objects_api, self._core_v1_api
        async with self._client_lock:
            if self._custom_objects_api is None or self._core_v1_api is None:
                try:
                    kube_config.load_incluster_config()
                except ConfigException:
                    await kube_config.load_kube_config()
                api_client = client.ApiClient()
                self._custom_objects_api = CustomObjectsApi(api_client)
                self._core_v1_api = CoreV1Api(api_client)
        return self._custom_objects_api, self._core_v1_api

    async def spawn(
        self, notebook: Notebook, mode: SessionMode, creator_id: UUID | None = None
    ) -> SessionInfo:
        """Create a CR + Secret for a new edit/run session and wait for it to be ready."""
        if mode not in ("edit", "run"):
            raise ValueError("mode must be 'edit' or 'run'")
        session_id = uuid4()
        return await self._create_and_wait(
            notebook,
            mode,
            session_id,
            creator_id=creator_id,
            base_url=f"/api/proxy/{session_id}",
        )

    async def spawn_deployment(
        self, notebook: Notebook, deployment_id: UUID, slug: str
    ) -> SessionInfo:
        """Create, wake, or return the deploy session for a deployment, idempotently.

        CR name = `deployment_id`, so a repeated call races into `create`'s
        `AlreadyExists` rather than a second Pod — that conflict is the
        idempotency barrier, replacing the subprocess backend's lock dance.
        """
        base_url = f"/api/deployments/{slug}"
        try:
            return await self._create_and_wait(
                notebook, "deploy", deployment_id, creator_id=None, base_url=base_url
            )
        except ApiException as exc:
            if exc.status != HTTPStatus.CONFLICT:
                raise

        cr = await self._get_cr(deployment_id)
        if cr is None:
            raise SessionStartError("Deployment session vanished during create race")
        phase = cr.get("status", {}).get("phase")
        if phase == "Sleeping":
            await self._wake(deployment_id, notebook.id)
        elif phase == "Ready":
            return _cr_to_info(cr)
        return await self._poll_ready(deployment_id)

    async def _create_and_wait(
        self,
        notebook: Notebook,
        mode: RuntimeMode,
        session_id: UUID,
        *,
        creator_id: UUID | None,
        base_url: str,
    ) -> SessionInfo:
        custom, _ = await self._clients()
        name = str(session_id)
        spec: _CRSpec = {
            "notebookId": str(notebook.id),
            "workspaceId": str(notebook.workspace_id),
            "creatorId": str(creator_id) if creator_id is not None else None,
            "mode": mode,
            "baseUrl": base_url,
        }
        if self._runtime_image is not None:
            spec["image"] = self._runtime_image
        body: _CustomResource = {
            "apiVersion": _API_VERSION,
            "kind": _KIND,
            "metadata": {"name": name, "labels": _labels(notebook, mode)},
            "spec": spec,
        }
        created = cast(
            "_CustomResource",
            await custom.create_namespaced_custom_object(
                _GROUP, _VERSION, self._namespace, _PLURAL, body
            ),
        )
        owner_uid = cast("str", created["metadata"]["uid"])
        await self._create_secret(session_id, notebook.id, owner_uid=owner_uid, owner_name=name)
        return await self._poll_ready(session_id)

    async def _create_secret(
        self, session_id: UUID, notebook_id: UUID, *, owner_uid: str, owner_name: str
    ) -> None:
        _, core = await self._clients()
        secret = client.V1Secret(
            metadata=client.V1ObjectMeta(
                name=_secret_name(session_id),
                owner_references=[
                    client.V1OwnerReference(
                        api_version=_API_VERSION,
                        kind=_KIND,
                        name=owner_name,
                        uid=owner_uid,
                    )
                ],
            ),
            string_data={
                "MARIMO_TOKEN": secrets.token_urlsafe(32),
                "SESSION_TOKEN": create_session_token(session_id, notebook_id),
            },
            type="Opaque",
        )
        await core.create_namespaced_secret(self._namespace, secret)

    async def _wake(self, deployment_id: UUID, notebook_id: UUID) -> None:
        custom, core = await self._clients()
        token = create_session_token(deployment_id, notebook_id)
        await core.patch_namespaced_secret(
            _secret_name(deployment_id), self._namespace, {"stringData": {"SESSION_TOKEN": token}}
        )
        await custom.patch_namespaced_custom_object(
            _GROUP,
            _VERSION,
            self._namespace,
            _PLURAL,
            str(deployment_id),
            {"metadata": {"annotations": {_WAKE_ANNOTATION: _rfc3339_now()}}},
        )

    async def _poll_ready(self, session_id: UUID) -> SessionInfo:
        deadline = asyncio.get_running_loop().time() + self._ready_timeout_seconds
        while True:
            cr = await self._get_cr(session_id)
            if cr is None:
                raise SessionStartError("Session disappeared while starting")
            status = cr.get("status", {})
            phase = status.get("phase")
            message = status.get("message", "")
            if phase == "Ready":
                return _cr_to_info(cr)
            if message.startswith(_QUOTA_MESSAGE_PREFIX):
                raise SessionCapacityError(message)
            if phase == "Failed":
                raise NotebookStartupError(f"Session {session_id} failed to start", detail=message)
            if asyncio.get_running_loop().time() >= deadline:
                raise SessionStartError("Session did not become ready in time")
            await asyncio.sleep(_POLL_INTERVAL_SECONDS)

    async def _get_cr(self, session_id: UUID) -> _CustomResource | None:
        custom, _ = await self._clients()
        try:
            return cast(
                "_CustomResource",
                await custom.get_namespaced_custom_object(
                    _GROUP, _VERSION, self._namespace, _PLURAL, str(session_id)
                ),
            )
        except ApiException as exc:
            if exc.status == HTTPStatus.NOT_FOUND:
                return None
            raise

    async def get(self, session_id: UUID) -> SessionInfo | None:
        """Return a snapshot of the session's CR, or `None` if it is unknown."""
        cr = await self._get_cr(session_id)
        return _cr_to_info(cr) if cr is not None else None

    async def target(self, session_id: UUID) -> SessionTarget | None:
        """Return proxy URLs and token for the session, or `None` if not serving."""
        cr = await self._get_cr(session_id)
        if cr is None:
            return None
        service_name = _serving_name(cr)
        if service_name is None:
            return None
        token = await self._read_marimo_token(session_id)
        if token is None:
            return None
        base_url = cr.get("spec", {}).get("baseUrl", "")
        host = f"{service_name}.{self._namespace}.{self._service_dns_suffix}"
        return SessionTarget(
            http_base_url=f"http://{host}:{self._service_port}{base_url}",
            ws_base_url=f"ws://{host}:{self._service_port}{base_url}",
            access_token=token,
        )

    async def _read_marimo_token(self, session_id: UUID) -> str | None:
        _, core = await self._clients()
        try:
            secret = await core.read_namespaced_secret(_secret_name(session_id), self._namespace)
        except ApiException as exc:
            if exc.status == HTTPStatus.NOT_FOUND:
                return None
            raise
        return _decode_secret_key(secret, "MARIMO_TOKEN")

    async def mark_active(self, session_id: UUID) -> None:
        """Coalesce activity into at most one annotation PATCH per session per window.

        Cheap enough to call on every proxied HTTP request and WS frame: a
        call inside the window returns with zero I/O, and any PATCH failure
        (including a 404 for a since-deleted session) is swallowed so this
        can never fault a live proxy stream.
        """
        now = asyncio.get_running_loop().time()
        last = self._last_marked_active.get(session_id)
        if last is not None and now - last < MARK_ACTIVE_COALESCE_SECONDS:
            return
        self._last_marked_active[session_id] = now
        try:
            custom, _ = await self._clients()
            await custom.patch_namespaced_custom_object(
                _GROUP,
                _VERSION,
                self._namespace,
                _PLURAL,
                str(session_id),
                {"metadata": {"annotations": {_LAST_ACTIVITY_ANNOTATION: _rfc3339_now()}}},
            )
        except Exception:  # noqa: BLE001 -- best-effort activity signal, never fault the caller
            logger.debug("mark_active PATCH failed for session %s", session_id, exc_info=True)

    async def stop(self, session_id: UUID) -> None:
        """Delete the session's CR; ownerRef GC cascades Pod/Service/Secret."""
        custom, _ = await self._clients()
        try:
            await custom.delete_namespaced_custom_object(
                _GROUP, _VERSION, self._namespace, _PLURAL, str(session_id)
            )
        except ApiException as exc:
            if exc.status == HTTPStatus.NOT_FOUND:
                raise SessionNotFoundError("Session not found") from exc
            raise
        self._last_marked_active.pop(session_id, None)

    async def shutdown(self) -> None:
        """No-op: session state lives in etcd and survives an API restart."""
