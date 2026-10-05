import asyncio
import base64
from collections.abc import Generator
import copy
import inspect
import time
from typing import Any, Literal
from uuid import UUID, uuid4

from kubernetes_asyncio import client
from kubernetes_asyncio.client.exceptions import ApiException
from kubernetes_asyncio.config.config_exception import ConfigException
import pytest
from starlette.concurrency import run_in_threadpool

from app.core.config import Settings, get_settings
from app.core.errors import Unauthenticated
from app.services import runtime_contract as contract
import app.services.runtime_credentials as runtime_credentials_module
from app.services.runtime_credentials import (
    RuntimeCredentialVerifier,
    VerificationUnavailableError,
    generate_runtime_credential,
    get_runtime_credential_verifier,
    parse_runtime_token,
)

_NAMESPACE = "marimohub-sessions"

ClusterResource = Literal["cr", "secret"]


def _injected_failure(failures: dict[ClusterResource, int], resource: ClusterResource) -> None:
    if resource in failures:
        raise ApiException(status=failures[resource], reason="Unavailable")


class _FakeRuntimeReadClient:
    """Only implements `get_namespaced_custom_object`, matching `_RuntimeReadClient`."""

    def __init__(
        self, objects: dict[str, dict[str, Any]], failures: dict[ClusterResource, int]
    ) -> None:
        self.objects = objects
        self.failures = failures

    async def get_namespaced_custom_object(
        self, group: str, version: str, namespace: str, plural: str, name: str
    ) -> dict[str, Any]:
        _injected_failure(self.failures, "cr")
        if name not in self.objects:
            raise ApiException(status=404, reason="NotFound")
        return copy.deepcopy(self.objects[name])


class _FakeSecretReadClient:
    """Only implements `read_namespaced_secret`, matching `_SecretReadClient`."""

    def __init__(
        self, secrets: dict[str, client.V1Secret], failures: dict[ClusterResource, int]
    ) -> None:
        self.secrets = secrets
        self.failures = failures

    async def read_namespaced_secret(self, name: str, namespace: str) -> client.V1Secret:
        _injected_failure(self.failures, "secret")
        if name not in self.secrets:
            raise ApiException(status=404, reason="NotFound")
        return self.secrets[name]


def _encode(value: str) -> str:
    return base64.b64encode(value.encode("utf-8")).decode("ascii")


class FakeRuntimeCluster:
    """An in-memory stand-in for the live CR + Secret state a Runtime credential is checked against.

    Used both by this module's direct `RuntimeCredentialVerifier` tests and
    by the internal-API endpoint tests, which need the same ability to mint
    a valid credential and then mutate cluster state (delete the CR, delete
    the Secret, replace the CR with a new UID) to prove revocation.
    """

    def __init__(self) -> None:
        self.objects: dict[str, dict[str, Any]] = {}
        self.secrets: dict[str, client.V1Secret] = {}
        self.read_failures: dict[ClusterResource, int] = {}

    def verifier(self) -> RuntimeCredentialVerifier:
        return RuntimeCredentialVerifier(
            namespace=_NAMESPACE,
            clients=(
                _FakeRuntimeReadClient(self.objects, self.read_failures),
                _FakeSecretReadClient(self.secrets, self.read_failures),
            ),
        )

    def fail_reads(self, resource: ClusterResource, status: int) -> None:
        """Fail every read of `resource` with `status`, as a throttled or sick API server would."""
        self.read_failures[resource] = status

    def mint(
        self,
        *,
        notebook_id: UUID | None = None,
        workspace_id: UUID | None = None,
        mode: str = "edit",
        deployment_revision: int | None = None,
        runtime_id: UUID | None = None,
    ) -> tuple[UUID, str]:
        """Register a live CR + valid owned Secret and return `(runtime_id, credential)`."""
        runtime_id = runtime_id or uuid4()
        uid = f"uid-{runtime_id}"
        credential = generate_runtime_credential(runtime_id)
        spec: dict[str, Any] = {
            "notebookId": str(notebook_id or uuid4()),
            "workspaceId": str(workspace_id or uuid4()),
            "mode": mode,
        }
        if deployment_revision is not None:
            spec["deploymentRevision"] = deployment_revision
        self.objects[str(runtime_id)] = {
            "apiVersion": contract.API_VERSION,
            "kind": contract.KIND,
            "metadata": {"name": str(runtime_id), "uid": uid},
            "spec": spec,
        }
        self.secrets[contract.secret_name(str(runtime_id))] = client.V1Secret(
            metadata=client.V1ObjectMeta(
                name=contract.secret_name(str(runtime_id)),
                owner_references=[
                    client.V1OwnerReference(
                        api_version=contract.API_VERSION,
                        kind=contract.KIND,
                        name=str(runtime_id),
                        uid=uid,
                        controller=True,
                    )
                ],
            ),
            data={
                contract.SECRET_KEY_MARIMO_TOKEN: _encode("marimo-token"),
                contract.SECRET_KEY_RUNTIME_CREDENTIAL: _encode(credential),
            },
            type=contract.SECRET_TYPE,
            immutable=True,
        )
        return runtime_id, credential

    def delete_cr(self, runtime_id: UUID) -> None:
        self.objects.pop(str(runtime_id), None)

    def mark_terminating(self, runtime_id: UUID) -> None:
        self.objects[str(runtime_id)]["metadata"]["deletionTimestamp"] = "2026-01-01T00:00:00Z"

    def delete_secret(self, runtime_id: UUID) -> None:
        self.secrets.pop(contract.secret_name(str(runtime_id)), None)

    def recreate_cr_with_new_uid(self, runtime_id: UUID) -> None:
        """Simulate the CR being deleted and recreated under the same name.

        The Secret (still owner-referencing the *old* UID) is left in place,
        standing in for GC not having caught up yet.
        """
        self.objects[str(runtime_id)]["metadata"]["uid"] = f"uid-{runtime_id}-recreated"

    def corrupt_secret(self, runtime_id: UUID, **fields: object) -> None:
        secret = self.secrets[contract.secret_name(str(runtime_id))]
        for field, value in fields.items():
            setattr(secret, field, value)


def test_generate_runtime_credential_has_expected_shape() -> None:
    runtime_id = uuid4()

    token = generate_runtime_credential(runtime_id)

    prefix, id_part, random_part = token.split(".")
    assert prefix == "mh_rt_v1"
    assert UUID(id_part) == runtime_id
    assert len(random_part) >= 32


def test_generate_runtime_credential_is_not_deterministic() -> None:
    runtime_id = uuid4()

    assert generate_runtime_credential(runtime_id) != generate_runtime_credential(runtime_id)


def test_parse_runtime_token_roundtrips_the_runtime_id() -> None:
    runtime_id = uuid4()

    token = generate_runtime_credential(runtime_id)

    assert parse_runtime_token(token) == runtime_id


@pytest.mark.parametrize(
    "token",
    [
        "",
        "not-a-credential",
        "mh_rt_v1",
        f"mh_rt_v1.{uuid4()}",
        f"mh_rt_v1.{uuid4()}.",
        f"wrong_prefix.{uuid4()}.abcdefghijklmnopqrstuvwxyz012345",
        "mh_rt_v1.not-a-uuid.abcdefghijklmnopqrstuvwxyz012345",
    ],
)
def test_parse_runtime_token_rejects_malformed_input(token: str) -> None:
    assert parse_runtime_token(token) is None


@pytest.mark.asyncio
async def test_verify_accepts_a_valid_live_bound_credential() -> None:
    cluster = FakeRuntimeCluster()
    notebook_id, workspace_id = uuid4(), uuid4()
    runtime_id, credential = cluster.mint(
        notebook_id=notebook_id, workspace_id=workspace_id, mode="run"
    )

    principal = await cluster.verifier().verify(credential)

    assert principal.runtime_id == runtime_id
    assert principal.notebook_id == notebook_id
    assert principal.workspace_id == workspace_id
    assert principal.mode == "run"


@pytest.mark.asyncio
async def test_verify_rejects_malformed_token_without_any_cluster_read() -> None:
    cluster = FakeRuntimeCluster()

    with pytest.raises(Unauthenticated):
        await cluster.verifier().verify("garbage")


@pytest.mark.asyncio
async def test_verify_rejects_when_cr_is_missing() -> None:
    cluster = FakeRuntimeCluster()
    _, credential = cluster.mint()
    runtime_id = parse_runtime_token(credential)
    assert runtime_id is not None
    cluster.delete_cr(runtime_id)

    with pytest.raises(Unauthenticated):
        await cluster.verifier().verify(credential)


@pytest.mark.asyncio
async def test_verify_rejects_a_terminating_cr_even_if_the_secret_still_exists() -> None:
    cluster = FakeRuntimeCluster()
    runtime_id, credential = cluster.mint()
    cluster.mark_terminating(runtime_id)

    with pytest.raises(Unauthenticated):
        await cluster.verifier().verify(credential)


@pytest.mark.asyncio
async def test_verify_rejects_when_secret_is_missing() -> None:
    cluster = FakeRuntimeCluster()
    runtime_id, credential = cluster.mint()
    cluster.delete_secret(runtime_id)

    with pytest.raises(Unauthenticated):
        await cluster.verifier().verify(credential)


@pytest.mark.asyncio
async def test_verify_rejects_a_recreated_cr_with_a_different_uid() -> None:
    """A same-name CR recreated with a new UID can never authenticate with the old token.

    The Secret's owner reference still names the *old* UID, so even though
    both the CR and the Secret exist by name, the ownership check fails --
    exactly the case where GC has not caught up removing the stale Secret.
    """
    cluster = FakeRuntimeCluster()
    runtime_id, old_credential = cluster.mint()
    cluster.recreate_cr_with_new_uid(runtime_id)

    with pytest.raises(Unauthenticated):
        await cluster.verifier().verify(old_credential)


@pytest.mark.asyncio
async def test_verify_rejects_a_non_immutable_secret() -> None:
    cluster = FakeRuntimeCluster()
    runtime_id, credential = cluster.mint()
    cluster.corrupt_secret(runtime_id, immutable=False)

    with pytest.raises(Unauthenticated):
        await cluster.verifier().verify(credential)


@pytest.mark.asyncio
async def test_verify_rejects_a_secret_with_the_wrong_type() -> None:
    cluster = FakeRuntimeCluster()
    runtime_id, credential = cluster.mint()
    cluster.corrupt_secret(runtime_id, type="kubernetes.io/tls")

    with pytest.raises(Unauthenticated):
        await cluster.verifier().verify(credential)


@pytest.mark.asyncio
async def test_verify_rejects_a_secret_missing_the_marimo_token_key() -> None:
    cluster = FakeRuntimeCluster()
    runtime_id, credential = cluster.mint()
    secret = cluster.secrets[contract.secret_name(str(runtime_id))]
    data = dict(secret.data or {})
    del data[contract.SECRET_KEY_MARIMO_TOKEN]
    cluster.corrupt_secret(runtime_id, data=data)

    with pytest.raises(Unauthenticated):
        await cluster.verifier().verify(credential)


@pytest.mark.asyncio
async def test_verify_rejects_a_credential_that_does_not_match_the_secret() -> None:
    cluster = FakeRuntimeCluster()
    runtime_id, _ = cluster.mint()
    forged = f"mh_rt_v1.{runtime_id}.{'a' * 43}"

    with pytest.raises(Unauthenticated):
        await cluster.verifier().verify(forged)


@pytest.mark.asyncio
async def test_verify_uses_constant_time_comparison(monkeypatch: pytest.MonkeyPatch) -> None:
    """`verify` must reject via `secrets.compare_digest`, never Python's `==`.

    Patching `compare_digest` to a spy that still does the real comparison
    proves it's actually on the call path, not merely imported and unused.
    """
    calls: list[tuple[str, str]] = []
    real_compare_digest = runtime_credentials_module.secrets.compare_digest

    def _spy(a: str, b: str) -> bool:
        calls.append((a, b))
        return real_compare_digest(a, b)

    monkeypatch.setattr(runtime_credentials_module.secrets, "compare_digest", _spy)
    cluster = FakeRuntimeCluster()
    _, credential = cluster.mint()

    await cluster.verifier().verify(credential)

    assert calls


@pytest.mark.asyncio
async def test_verify_deployment_credential_carries_its_revision() -> None:
    cluster = FakeRuntimeCluster()
    runtime_id, credential = cluster.mint(mode="deploy", deployment_revision=3)

    principal = await cluster.verifier().verify(credential)

    assert principal.runtime_id == runtime_id
    assert principal.mode == "deploy"
    assert principal.deployment_revision == 3


def test_runtime_credential_verifier_module_never_lists_creates_patches_or_deletes() -> None:
    """Structural guard for the internal API's Kubernetes RBAC shape.

    Everything the internal app can reach through `RuntimeCredentialVerifier`
    must be expressible as a `get`-only role; this fails the moment the
    module gains a call to any broader verb, so a future RBAC lockdown never
    has to guess whether tightening to `get` would break something.
    """
    source = inspect.getsource(runtime_credentials_module)
    for forbidden in (
        "list_namespaced",
        "create_namespaced",
        "patch_namespaced",
        "delete_namespaced",
        "replace_namespaced",
    ):
        assert forbidden not in source, (
            f"unexpected {forbidden!r} call in {runtime_credentials_module.__name__}"
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("resource", ["cr", "secret"])
@pytest.mark.parametrize("api_status", [401, 403, 429, 500, 503])
async def test_verify_reports_unavailable_when_a_read_fails_other_than_not_found(
    resource: ClusterResource, api_status: int
) -> None:
    """A failed cluster read says nothing about the credential, so it must not be a 401.

    The fetcher gives up on a 401 for good, which fails the Runtime; a 503
    is retried by curl itself.
    """
    cluster = FakeRuntimeCluster()
    _, credential = cluster.mint()
    cluster.fail_reads(resource, api_status)

    with pytest.raises(VerificationUnavailableError) as exc_info:
        await cluster.verifier().verify(credential)

    assert exc_info.value.status == 503
    assert credential not in exc_info.value.detail


@pytest.mark.asyncio
async def test_close_leaves_injected_clients_alone() -> None:
    cluster = FakeRuntimeCluster()
    runtime_id, credential = cluster.mint()
    verifier = cluster.verifier()

    await verifier.close()

    assert (await verifier.verify(credential)).runtime_id == runtime_id


def _not_in_a_cluster() -> None:
    raise ConfigException("Service host/port is not set.")


async def _no_kubeconfig_to_load() -> None:
    return None


@pytest.mark.asyncio
async def test_close_closes_the_api_client_the_verifier_built(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    kube_config = runtime_credentials_module.kube_config
    monkeypatch.setattr(kube_config, "load_incluster_config", _not_in_a_cluster)
    monkeypatch.setattr(kube_config, "load_kube_config", _no_kubeconfig_to_load)
    verifier = RuntimeCredentialVerifier(namespace=_NAMESPACE)
    await verifier._clients()
    api_client = verifier._api_client
    assert api_client is not None
    http_session = api_client.rest_client.pool_manager
    assert not http_session.closed

    await verifier.close()
    await verifier.close()  # nothing left to close; must not fail

    assert http_session.closed
    assert verifier._api_client is None


@pytest.fixture
def _fresh_process_verifier(monkeypatch: pytest.MonkeyPatch) -> Generator[None, None, None]:
    """No process-wide verifier yet; the previous one (if any) is restored afterward."""
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://molab:molab@localhost:5432/molab_test")
    monkeypatch.setenv("SECRET_KEY", "test-secret-with-at-least-32-bytes")
    monkeypatch.setitem(runtime_credentials_module._verifier_state, "instance", None)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.mark.asyncio
@pytest.mark.usefixtures("_fresh_process_verifier")
async def test_concurrent_first_calls_create_exactly_one_verifier(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """FastAPI resolves this sync dependency in its threadpool, as `run_in_threadpool` does."""
    real_from_settings = RuntimeCredentialVerifier.from_settings
    created: list[RuntimeCredentialVerifier] = []

    def _slow_from_settings(settings: Settings) -> RuntimeCredentialVerifier:
        time.sleep(0.05)  # widens the window between the unlocked check and the store
        verifier = real_from_settings(settings)
        created.append(verifier)
        return verifier

    monkeypatch.setattr(RuntimeCredentialVerifier, "from_settings", _slow_from_settings)

    verifiers = await asyncio.gather(
        *(run_in_threadpool(get_runtime_credential_verifier) for _ in range(50))
    )

    assert len(created) == 1
    assert all(verifier is created[0] for verifier in verifiers)
