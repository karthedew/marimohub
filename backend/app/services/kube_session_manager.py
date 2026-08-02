import asyncio
import base64
from collections import OrderedDict
from collections.abc import Awaitable, Mapping
import contextlib
from datetime import datetime
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
from app.models import Deployment, Notebook
from app.services import runtime_contract as contract
from app.services.runtime_credentials import generate_runtime_credential
from app.services.session_manager import (
    NotebookStartupError,
    RuntimeMode,
    RuntimeRef,
    SessionCapacityError,
    SessionInfo,
    SessionMode,
    SessionNotFoundError,
    SessionPhase,
    SessionStartError,
    SessionTarget,
)

_POLL_INTERVAL_SECONDS = 0.5
_DELETE_POLL_INTERVAL_SECONDS = 0.5
_ACTIVITY_THROTTLE_MAX_ENTRIES = 10_000
_MESSAGE_MAX_LENGTH = 512
_STARTUP_FAILURE_DETAIL = "The notebook could not start. Check that it is a valid marimo notebook."

logger = logging.getLogger(__name__)

# Must stay below the CRD's minimum `spec.idleTimeoutSeconds` (30s) so a
# throttled activity annotation never lags enough to trip a premature
# idle-sleep. Currently 15 < 30; keep this invariant if either value changes.
MARK_ACTIVE_COALESCE_SECONDS = 15.0


class _Condition(TypedDict, total=False):
    type: str
    status: str
    reason: str
    message: str


class _CRSpec(TypedDict, total=False):
    notebookId: str
    workspaceId: str
    creatorId: str | None
    mode: str
    image: str
    baseUrl: str
    deploymentRevision: int


class _CRStatus(TypedDict, total=False):
    phase: str
    serviceName: str
    podName: str
    lastActivity: str
    message: str
    conditions: list[_Condition]
    observedGeneration: int


class _CustomResource(TypedDict, total=False):
    apiVersion: str
    kind: str
    metadata: dict[str, Any]
    spec: _CRSpec
    status: _CRStatus


def _labels(notebook: Notebook, mode: str) -> dict[str, str]:
    return {
        contract.LABEL_NOTEBOOK: str(notebook.id),
        contract.LABEL_WORKSPACE: str(notebook.workspace_id),
        contract.LABEL_MODE: mode,
    }


def _condition(cr: _CustomResource, condition_type: str) -> _Condition | None:
    for condition in cr.get("status", {}).get("conditions") or []:
        if condition.get("type") == condition_type:
            return condition
    return None


def _is_terminating(cr: _CustomResource) -> bool:
    return cr.get("metadata", {}).get("deletionTimestamp") is not None


def _observed_current_generation(cr: _CustomResource) -> bool:
    """Whether `status.observedGeneration` has caught up with `metadata.generation`.

    Absence of either field (a CR body that predates the controller writing
    them, or a test double that never populates them) is treated as "trust
    it" rather than a mismatch: only a *reported* generation lag should ever
    withhold a Ready Runtime from routing.
    """
    status = cr.get("status", {})
    metadata = cr.get("metadata", {})
    if "observedGeneration" not in status or "generation" not in metadata:
        return True
    return status["observedGeneration"] == metadata["generation"]


def _phase_of(status: _CRStatus) -> SessionPhase:
    phase = status.get("phase")
    if phase == "Ready":
        return SessionPhase.READY
    if phase == "Sleeping":
        return SessionPhase.SLEEPING
    if phase in (None, "Pending", "Starting"):
        return SessionPhase.STARTING
    return SessionPhase.FAILED


def _bounded(message: str | None) -> str | None:
    if not message:
        return None
    return message[:_MESSAGE_MAX_LENGTH]


def _cr_to_info(cr: _CustomResource) -> SessionInfo:
    spec = cr.get("spec", {})
    status = cr.get("status", {})
    metadata = cr.get("metadata", {})
    creator_id = spec.get("creatorId")
    last_activity = status.get("lastActivity")
    phase = _phase_of(status)
    failure_reason: str | None = None
    message: str | None = None
    if phase is SessionPhase.FAILED:
        ready = _condition(cr, contract.CONDITION_READY)
        failure_reason = ready.get("reason") if ready else None
        message = _bounded(status.get("message"))
    return SessionInfo(
        id=UUID(cast("str", metadata["name"])),
        notebook_id=UUID(spec["notebookId"]),
        mode=cast("RuntimeMode", spec["mode"]),
        phase=phase,
        last_active=datetime.fromisoformat(last_activity) if last_activity else None,
        creator_id=UUID(creator_id) if creator_id else None,
        failure_reason=failure_reason,
        message=message,
        deployment_revision=spec.get("deploymentRevision"),
    )


def _decode_secret_key(secret: client.V1Secret, key: str) -> str | None:
    data = secret.data or {}
    raw = data.get(key)
    return base64.b64decode(raw).decode("utf-8") if raw is not None else None


def _serving_name(cr: _CustomResource) -> str | None:
    """Return the CR's Service name if it is currently `Ready` and routable.

    A `Ready` phase alone is not enough: a terminating CR (one already marked
    for deletion) and a `Ready` status that has not yet caught up with the
    current spec generation (mid resources replacement) must never be routed
    either, since both would hand a caller a Pod that is about to disappear
    or does not match the spec that was actually validated.
    """
    status = cr.get("status", {})
    if status.get("phase") != "Ready":
        return None
    if _is_terminating(cr) or not _observed_current_generation(cr):
        return None
    return status.get("serviceName") or None


class _ActivityThrottle:
    """Bounds per-runtime activity-coalescing state with LRU eviction.

    A long-lived replica proxies traffic for many runtimes over its process
    lifetime; without a cap this would grow one entry per runtime ID ever
    seen, long after each runtime stopped existing.
    """

    def __init__(self, max_entries: int = _ACTIVITY_THROTTLE_MAX_ENTRIES) -> None:
        self._last_marked: OrderedDict[UUID, float] = OrderedDict()
        self._max_entries = max_entries

    def ready(self, session_id: UUID, now: float, window_seconds: float) -> bool:
        """Return whether `session_id` is outside its coalescing window."""
        last = self._last_marked.get(session_id)
        if last is None:
            return True
        self._last_marked.move_to_end(session_id)
        return now - last >= window_seconds

    def record(self, session_id: UUID, now: float) -> None:
        self._last_marked[session_id] = now
        self._last_marked.move_to_end(session_id)
        while len(self._last_marked) > self._max_entries:
            self._last_marked.popitem(last=False)

    def discard(self, session_id: UUID) -> None:
        self._last_marked.pop(session_id, None)


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
        self,
        group: str,
        version: str,
        namespace: str,
        plural: str,
        name: str,
        body: object | None = None,
    ) -> Awaitable[dict[str, Any]]: ...

    def list_namespaced_custom_object(
        self,
        group: str,
        version: str,
        namespace: str,
        plural: str,
        label_selector: str = "",
    ) -> Awaitable[dict[str, Any]]: ...


class _CoreV1Client(Protocol):
    """The slice of `CoreV1Api` the manager calls; narrow enough for a test fake."""

    def create_namespaced_secret(
        self, namespace: str, body: client.V1Secret
    ) -> Awaitable[client.V1Secret]: ...

    def read_namespaced_secret(self, name: str, namespace: str) -> Awaitable[client.V1Secret]: ...


class KubeSessionManager:
    """Implements `SessionManager` against `MarimoSession` custom resources.

    Holds no session dict — every lookup is a labelled get against the
    cluster, which is what lets any API replica answer `get`/`target`
    statelessly, the crash-safety a CRD-backed session buys over an
    in-process registry.
    """

    def __init__(  # noqa: PLR0913 -- one cohesive set of cluster coordinates, no natural subgroup
        self,
        *,
        namespace: str,
        service_dns_suffix: str,
        service_port: int,
        runtime_image: str | None,
        ready_timeout_seconds: float,
        delete_timeout_seconds: float = 30.0,
        clients: tuple[_CustomObjectsClient, _CoreV1Client] | None = None,
    ) -> None:
        """Configure cluster coordinates; an injected `clients` pair bypasses config loading."""
        self._namespace = namespace
        self._service_dns_suffix = service_dns_suffix
        self._service_port = service_port
        self._runtime_image = runtime_image
        self._ready_timeout_seconds = ready_timeout_seconds
        self._delete_timeout_seconds = delete_timeout_seconds
        self._custom_objects_api, self._core_v1_api = clients or (None, None)
        self._client_lock = asyncio.Lock()
        self._activity_throttle = _ActivityThrottle()

    @classmethod
    def from_settings(cls, settings: Settings) -> "KubeSessionManager":
        """Build a kube session manager from application settings."""
        return cls(
            namespace=settings.SESSION_NAMESPACE,
            service_dns_suffix=settings.SESSION_SERVICE_DNS_SUFFIX,
            service_port=settings.SESSION_SERVICE_PORT,
            runtime_image=settings.SESSION_RUNTIME_IMAGE,
            ready_timeout_seconds=settings.SESSION_READY_TIMEOUT_SECONDS,
            delete_timeout_seconds=settings.SESSION_DELETE_TIMEOUT_SECONDS,
        )

    async def _clients(self) -> tuple[_CustomObjectsClient, _CoreV1Client]:
        if self._custom_objects_api is not None and self._core_v1_api is not None:
            return (
                cast("_CustomObjectsClient", self._custom_objects_api),
                self._core_v1_api,
            )
        async with self._client_lock:
            if self._custom_objects_api is None or self._core_v1_api is None:
                try:
                    kube_config.load_incluster_config()
                except ConfigException:
                    await kube_config.load_kube_config()
                api_client = client.ApiClient()
                self._custom_objects_api = CustomObjectsApi(api_client)
                self._core_v1_api = CoreV1Api(api_client)
        # The generated `CustomObjectsApi` client accepts a wider parameter
        # set (e.g. `grace_period_seconds`) than the narrow
        # `_CustomObjectsClient` protocol this manager actually calls
        # through, so the structural match is asserted here rather than
        # satisfied automatically. `CoreV1Api` needs no such cast: its
        # narrower `_CoreV1Client` counterpart already matches structurally.
        return (
            cast("_CustomObjectsClient", self._custom_objects_api),
            self._core_v1_api,
        )

    async def spawn(
        self, notebook: Notebook, mode: SessionMode, creator_id: UUID | None = None
    ) -> SessionInfo:
        """Create a CR + Secret for a new edit/run session and wait for it to be ready.

        Creation, Secret creation, and readiness polling are one compensated
        operation: any failure along the way (Secret-create failure, quota,
        deterministic startup failure, timeout, or the caller's own
        cancellation) deletes the CR it just created and waits for it to be
        gone, rather than leaving an orphaned, credential-less, or
        never-to-be-routed CR behind for something else to notice later.
        """
        if mode not in ("edit", "run"):
            raise ValueError("mode must be 'edit' or 'run'")
        session_id = uuid4()
        return await self._create_and_wait_compensating(
            notebook,
            mode,
            session_id,
            creator_id=creator_id,
            base_url=f"/api/proxy/{session_id}",
        )

    async def _create_and_wait_compensating(
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
            "image": self._runtime_image or "",
            "baseUrl": base_url,
        }
        body: _CustomResource = {
            "apiVersion": contract.API_VERSION,
            "kind": contract.KIND,
            "metadata": {"name": name, "labels": _labels(notebook, mode)},
            "spec": spec,
        }
        created = cast(
            "_CustomResource",
            await custom.create_namespaced_custom_object(
                contract.GROUP, contract.VERSION, self._namespace, contract.PLURAL, body
            ),
        )
        owner_uid = cast("str", created["metadata"]["uid"])
        try:
            await self._create_secret(session_id, owner_uid=owner_uid, owner_name=name)
            return await self._poll_ready(session_id)
        except BaseException:
            # Shielded so the caller's own cancellation (one of the failure
            # modes this compensates) cannot also cut off the cleanup it triggered.
            await asyncio.shield(self._compensate(session_id))
            raise

    async def _compensate(self, session_id: UUID) -> None:
        """Best-effort delete-and-wait for a failed edit/run create.

        Never raises: a cleanup failure must not mask the original error that
        triggered it. `stop` already waits for the CR to be gone, bounded by
        `_delete_timeout_seconds`.
        """
        try:
            await self.stop(session_id)
        except SessionNotFoundError:
            pass
        except Exception:
            logger.exception("compensating cleanup failed for runtime %s", session_id)

    async def spawn_deployment(self, notebook: Notebook, deployment: Deployment) -> SessionInfo:
        """Ensure a deploy Runtime is running and wait for it, idempotently.

        Deploy Runtimes are never compensated on failure: the CR is retained
        so a Deployment stays diagnosable until an authorized redeploy or
        stop, per the Deployment-Runtime retention rule.
        """
        await self._ensure_deploy_running(notebook, deployment)
        return await self._poll_ready(deployment.id)

    async def _ensure_deploy_running(self, notebook: Notebook, deployment: Deployment) -> None:
        name = str(deployment.id)
        cr = await self._get_cr(deployment.id)
        if cr is not None and _is_terminating(cr):
            # A caller must never reuse or route a terminating CR; wait for
            # its actual removal (foreground GC of Pod/Service/Secret) before
            # creating its replacement under the same name.
            await self._wait_gone(deployment.id)
            cr = None
        if cr is None:
            await self._create_deploy_cr(notebook, deployment)
            return
        phase = cr.get("status", {}).get("phase")
        if phase == "Ready":
            return
        if phase == "Sleeping":
            # A missing owned Secret (e.g. deleted out-of-band while asleep)
            # is repairable; an existing invalid/foreign Secret is not, and
            # is left for an authorized redeploy or administrator to resolve.
            await self._ensure_secret(deployment.id, cr=cr, owner_name=name)
            await self._wake(deployment.id)
        # Pending/Starting/Failed: an attempt is already in flight or
        # terminal; `_poll_ready` classifies it without a new mutation here.

    async def _create_deploy_cr(self, notebook: Notebook, deployment: Deployment) -> None:
        custom, _ = await self._clients()
        name = str(deployment.id)
        spec: _CRSpec = {
            "notebookId": str(notebook.id),
            "workspaceId": str(notebook.workspace_id),
            "creatorId": None,
            "mode": "deploy",
            "image": deployment.runtime_image or "",
            "baseUrl": f"/api/deployments/{deployment.slug}",
            "deploymentRevision": deployment.revision,
        }
        body: _CustomResource = {
            "apiVersion": contract.API_VERSION,
            "kind": contract.KIND,
            "metadata": {"name": name, "labels": _labels(notebook, "deploy")},
            "spec": spec,
        }
        try:
            created = cast(
                "_CustomResource",
                await custom.create_namespaced_custom_object(
                    contract.GROUP, contract.VERSION, self._namespace, contract.PLURAL, body
                ),
            )
        except ApiException as exc:
            if exc.status != HTTPStatus.CONFLICT:
                raise
            existing = await self._get_cr(deployment.id)
            if existing is None:
                raise SessionStartError(
                    f"Deployment runtime {deployment.id} vanished during create race"
                ) from exc
            created = existing
        await self._ensure_secret(deployment.id, cr=created, owner_name=name)

    async def _ensure_secret(
        self, session_id: UUID, *, cr: _CustomResource, owner_name: str
    ) -> None:
        _, core = await self._clients()
        try:
            await core.read_namespaced_secret(
                contract.secret_name(str(session_id)), self._namespace
            )
        except ApiException as exc:
            if exc.status != HTTPStatus.NOT_FOUND:
                raise
            await self._create_secret(
                session_id, owner_uid=cr["metadata"]["uid"], owner_name=owner_name
            )

    async def _create_secret(self, session_id: UUID, *, owner_uid: str, owner_name: str) -> None:
        _, core = await self._clients()
        secret = client.V1Secret(
            metadata=client.V1ObjectMeta(
                name=contract.secret_name(str(session_id)),
                owner_references=[
                    client.V1OwnerReference(
                        api_version=contract.API_VERSION,
                        kind=contract.KIND,
                        name=owner_name,
                        uid=owner_uid,
                        controller=True,
                    )
                ],
            ),
            string_data={
                contract.SECRET_KEY_MARIMO_TOKEN: secrets.token_urlsafe(32),
                contract.SECRET_KEY_RUNTIME_CREDENTIAL: generate_runtime_credential(session_id),
            },
            type=contract.SECRET_TYPE,
            # Minted once, here, and never patched afterward (see `_wake`
            # below): marking the Secret immutable lets the operator treat
            # "immutable: true" as proof its contents can never have been
            # rotated out from under a running Pod, rather than having to
            # detect mutation after the fact.
            immutable=True,
        )
        await core.create_namespaced_secret(self._namespace, secret)

    async def _wake(self, deployment_id: UUID) -> None:
        # No secret refresh: RUNTIME_CREDENTIAL never expires and the Secret
        # is immutable, so a wake has nothing to rotate -- the annotation
        # alone is the signal the operator acts on.
        custom, _ = await self._clients()
        await custom.patch_namespaced_custom_object(
            contract.GROUP,
            contract.VERSION,
            self._namespace,
            contract.PLURAL,
            str(deployment_id),
            {
                "metadata": {
                    "annotations": {contract.ANNOTATION_WAKE_REQUEST: secrets.token_urlsafe(24)}
                }
            },
        )

    async def _poll_ready(self, session_id: UUID) -> SessionInfo:
        deadline = asyncio.get_running_loop().time() + self._ready_timeout_seconds
        while True:
            cr = await self._get_cr(session_id)
            if cr is None:
                raise SessionStartError(f"Runtime {session_id} disappeared while starting")
            capacity = _condition(cr, contract.CONDITION_CAPACITY_AVAILABLE)
            if (
                capacity is not None
                and capacity.get("status") == "False"
                and capacity.get("reason") == contract.REASON_QUOTA_EXCEEDED
            ):
                raise SessionCapacityError(
                    _bounded(capacity.get("message")) or "No capacity available"
                )
            status = cr.get("status", {})
            if status.get("phase") == "Ready" and _observed_current_generation(cr):
                return _cr_to_info(cr)
            if status.get("phase") == "Failed":
                ready = _condition(cr, contract.CONDITION_READY)
                reason = ready.get("reason") if ready else "Unknown"
                raise NotebookStartupError(
                    f"Runtime {session_id} failed to start ({reason})",
                    detail=_STARTUP_FAILURE_DETAIL,
                )
            if asyncio.get_running_loop().time() >= deadline:
                raise SessionStartError(f"Runtime {session_id} did not become ready in time")
            await asyncio.sleep(_POLL_INTERVAL_SECONDS)

    async def _get_cr(self, session_id: UUID) -> _CustomResource | None:
        custom, _ = await self._clients()
        try:
            return cast(
                "_CustomResource",
                await custom.get_namespaced_custom_object(
                    contract.GROUP,
                    contract.VERSION,
                    self._namespace,
                    contract.PLURAL,
                    str(session_id),
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
            secret = await core.read_namespaced_secret(
                contract.secret_name(str(session_id)), self._namespace
            )
        except ApiException as exc:
            if exc.status == HTTPStatus.NOT_FOUND:
                return None
            raise
        return _decode_secret_key(secret, contract.SECRET_KEY_MARIMO_TOKEN)

    async def mark_active(self, session_id: UUID) -> None:
        """Coalesce activity into at most one annotation PATCH per session per window.

        Cheap enough to call on every proxied HTTP request and WS frame: a
        call inside the window returns with zero I/O. The local throttle only
        advances after a *successful* PATCH, so a failed write (including a
        404 for a since-deleted session, swallowed here so this can never
        fault a live proxy stream) never falsely suppresses the next attempt.
        """
        now = asyncio.get_running_loop().time()
        if not self._activity_throttle.ready(session_id, now, MARK_ACTIVE_COALESCE_SECONDS):
            return
        try:
            custom, _ = await self._clients()
            await custom.patch_namespaced_custom_object(
                contract.GROUP,
                contract.VERSION,
                self._namespace,
                contract.PLURAL,
                str(session_id),
                {
                    "metadata": {
                        "annotations": {contract.ANNOTATION_ACTIVITY: secrets.token_urlsafe(16)}
                    }
                },
            )
        except Exception:  # noqa: BLE001 -- best-effort activity signal, never fault the caller
            logger.debug("mark_active PATCH failed for session %s", session_id, exc_info=True)
            return
        self._activity_throttle.record(session_id, now)

    async def stop(self, session_id: UUID) -> None:
        """Foreground-delete the session's CR and wait until it is actually gone.

        Waiting outside any database lock (the caller's job, not this
        method's) is what lets a subsequent create reuse the same CR name
        safely: Kubernetes garbage-collects owner-referenced Pod/Service/
        Secret children before a foreground-deleted parent's own removal
        completes, so `NotFound` here means the whole resource graph is gone.
        """
        custom, _ = await self._clients()
        try:
            await custom.delete_namespaced_custom_object(
                contract.GROUP,
                contract.VERSION,
                self._namespace,
                contract.PLURAL,
                str(session_id),
                body=client.V1DeleteOptions(propagation_policy="Foreground"),
            )
        except ApiException as exc:
            if exc.status == HTTPStatus.NOT_FOUND:
                raise SessionNotFoundError("Runtime not found") from exc
            raise
        await self._wait_gone(session_id)
        self._activity_throttle.discard(session_id)

    async def _wait_gone(self, session_id: UUID) -> None:
        deadline = asyncio.get_running_loop().time() + self._delete_timeout_seconds
        while await self._get_cr(session_id) is not None:
            if asyncio.get_running_loop().time() >= deadline:
                raise SessionStartError(f"Runtime {session_id} did not finish deleting in time")
            await asyncio.sleep(_DELETE_POLL_INTERVAL_SECONDS)

    async def stop_workspace_sessions(self, workspace_id: UUID) -> None:
        """Best-effort delete-and-wait for every edit/run Runtime in `workspace_id`.

        Deployment Runtimes are excluded (`marimohub.io/mode!=deploy`):
        callers stop those individually, by Deployment id, through `stop`.
        """
        custom, _ = await self._clients()
        listing = await custom.list_namespaced_custom_object(
            contract.GROUP,
            contract.VERSION,
            self._namespace,
            contract.PLURAL,
            label_selector=f"{contract.LABEL_WORKSPACE}={workspace_id},{contract.LABEL_MODE}!=deploy",
        )
        names = [item["metadata"]["name"] for item in listing.get("items", [])]
        for name in names:
            with contextlib.suppress(SessionNotFoundError):
                await self.stop(UUID(name))

    async def reconcilable_runtimes(self) -> list[RuntimeRef]:
        """List every Runtime CR in the namespace for the maintenance sweep.

        A malformed or foreign object (missing the fields this contract
        requires) is skipped rather than raising: the sweep's job is
        reconciling MarimoHub's own Runtimes, not validating every object a
        cluster operator happens to have created in this namespace.
        """
        custom, _ = await self._clients()
        listing = await custom.list_namespaced_custom_object(
            contract.GROUP, contract.VERSION, self._namespace, contract.PLURAL
        )
        refs: list[RuntimeRef] = []
        for item in listing.get("items", []):
            spec = item.get("spec", {})
            try:
                refs.append(
                    RuntimeRef(
                        id=UUID(cast("str", item["metadata"]["name"])),
                        mode=cast("RuntimeMode", spec["mode"]),
                        workspace_id=UUID(spec["workspaceId"]),
                        notebook_id=UUID(spec["notebookId"]),
                        deployment_revision=spec.get("deploymentRevision"),
                    )
                )
            except (KeyError, ValueError):
                continue
        return refs

    async def shutdown(self) -> None:
        """No-op: session state lives in etcd and survives an API restart."""
