"""Runtime credential minting and live verification.

A Runtime authenticates to the internal API with one opaque bearer value,
minted once when its owned Secret is created and never rotated -- the
Secret is immutable, so there is no in-place refresh to perform and no
reason for a wake to mint a new one (see `KubeSessionManager._create_secret`
and `_wake`). Revocation is a property of live cluster state, not of the
token itself: deleting the CR, deleting the Secret, or replacing either with
a different UID makes every subsequent `verify()` call fail, with no
separate revocation list to maintain.

`RuntimeCredentialVerifier` performs a live named `get`/`read` against the CR
and Secret the presented token names, on every call, with no cache in front.
This is deliberately the internal API's *entire* Kubernetes surface: nothing
in this module lists, creates, patches, or deletes anything, so a future
RBAC lockdown for the internal ServiceAccount has no broader code path here
that would ever need wider permissions. A cache is a separate, later
decision that must preserve bounded revocation (see ADR 0001), not something
this module anticipates.
"""

import asyncio
import base64
from collections.abc import Awaitable, Mapping
from dataclasses import dataclass
import secrets
from typing import Any, Protocol, cast
from uuid import UUID

from kubernetes_asyncio import client, config as kube_config
from kubernetes_asyncio.client.api.core_v1_api import CoreV1Api
from kubernetes_asyncio.client.api.custom_objects_api import CustomObjectsApi
from kubernetes_asyncio.client.exceptions import ApiException
from kubernetes_asyncio.config.config_exception import ConfigException

from app.core.config import Settings, get_settings
from app.core.errors import Unauthenticated
from app.services import runtime_contract as contract

_TOKEN_PREFIX = "mh_rt_v1"  # noqa: S105 -- a token format tag, not a credential value
_RANDOM_BYTES = 32
_INVALID_CREDENTIAL = "Invalid runtime credential"


def generate_runtime_credential(runtime_id: UUID) -> str:
    """Mint a new opaque bearer credential for `runtime_id`'s owned Secret.

    Called exactly once, when the Secret is created. The random component
    comes from a CSPRNG, never a counter or timestamp, and the credential is
    never derived from anything else an attacker could reconstruct.
    """
    return f"{_TOKEN_PREFIX}.{runtime_id}.{secrets.token_urlsafe(_RANDOM_BYTES)}"


def parse_runtime_token(token: str) -> UUID | None:
    """Return the Runtime id embedded in `token`, or `None` if it isn't this shape.

    A parse failure is not a 5xx: `RuntimeCredentialVerifier.verify` turns it
    into the same 401 an expired, foreign, or tampered credential gets,
    never revealing which check failed.
    """
    prefix, _, remainder = token.partition(".")
    if prefix != _TOKEN_PREFIX:
        return None
    id_part, _, random_part = remainder.partition(".")
    if not random_part:
        return None
    try:
        return UUID(id_part)
    except ValueError:
        return None


@dataclass(frozen=True, slots=True)
class RuntimePrincipal:
    """The identity a Runtime proves about itself by presenting its own credential."""

    runtime_id: UUID
    notebook_id: UUID
    workspace_id: UUID
    mode: str
    deployment_revision: int | None

    @classmethod
    def from_cr(cls, cr: Mapping[str, Any]) -> "RuntimePrincipal":
        """Build a principal from a live CR body; a malformed shape raises."""
        spec = cr["spec"]
        return cls(
            runtime_id=UUID(cast("str", cr["metadata"]["name"])),
            notebook_id=UUID(spec["notebookId"]),
            workspace_id=UUID(spec["workspaceId"]),
            mode=spec["mode"],
            deployment_revision=spec.get("deploymentRevision"),
        )


class _RuntimeReadClient(Protocol):
    """The only CR operation Runtime-credential verification ever performs."""

    def get_namespaced_custom_object(
        self, group: str, version: str, namespace: str, plural: str, name: str
    ) -> Awaitable[dict[str, Any]]: ...


class _SecretReadClient(Protocol):
    """The only Secret operation Runtime-credential verification ever performs."""

    def read_namespaced_secret(self, name: str, namespace: str) -> Awaitable[client.V1Secret]: ...


def _decode_secret_key(secret: client.V1Secret, key: str) -> str | None:
    raw = (secret.data or {}).get(key)
    return base64.b64decode(raw).decode("utf-8") if raw is not None else None


def _owned_by_cr(cr: Mapping[str, Any], secret: client.V1Secret) -> bool:
    metadata = cr.get("metadata", {})
    for ref in secret.metadata.owner_references or []:
        if (
            ref.controller
            and ref.api_version == contract.API_VERSION
            and ref.kind == contract.KIND
            and ref.name == metadata.get("name")
            and ref.uid == metadata.get("uid")
        ):
            return True
    return False


def _require_valid_owned_secret(cr: Mapping[str, Any], secret: client.V1Secret) -> None:
    """Mirror the operator's own precondition for trusting this Secret's contents.

    `credentials.go`'s `validateOwnedSecret` is authoritative: type Opaque,
    `immutable: true`, an owner reference naming this exact live CR UID, and
    both keys non-empty. Any daylight between that check and this one would
    let the backend trust a Secret the controller itself would reject as a
    credential source.
    """
    if secret.type != contract.SECRET_TYPE:
        raise Unauthenticated(_INVALID_CREDENTIAL)
    if not secret.immutable:
        raise Unauthenticated(_INVALID_CREDENTIAL)
    if not _owned_by_cr(cr, secret):
        raise Unauthenticated(_INVALID_CREDENTIAL)
    data = secret.data or {}
    for key in (contract.SECRET_KEY_MARIMO_TOKEN, contract.SECRET_KEY_RUNTIME_CREDENTIAL):
        if not data.get(key):
            raise Unauthenticated(_INVALID_CREDENTIAL)


class RuntimeCredentialVerifier:
    """Validates a presented RUNTIME_CREDENTIAL against live cluster state.

    Holds no session dict and no cache -- every `verify()` call is a named
    `get`/`read` against the cluster, which is what makes CR deletion,
    Secret deletion, and Secret-UID replacement (a recreated same-name CR)
    all revoke a credential on the very next request rather than only after
    some cache entry expires.
    """

    def __init__(
        self,
        *,
        namespace: str,
        clients: tuple[_RuntimeReadClient, _SecretReadClient] | None = None,
    ) -> None:
        """Configure the target namespace; an injected `clients` pair bypasses config loading."""
        self._namespace = namespace
        self._custom_objects_api, self._core_v1_api = clients or (None, None)
        self._client_lock = asyncio.Lock()

    @classmethod
    def from_settings(cls, settings: Settings) -> "RuntimeCredentialVerifier":
        """Build a verifier targeting the configured Runtime namespace."""
        return cls(namespace=settings.SESSION_NAMESPACE)

    async def _clients(self) -> tuple[_RuntimeReadClient, _SecretReadClient]:
        if self._custom_objects_api is not None and self._core_v1_api is not None:
            return (
                cast("_RuntimeReadClient", self._custom_objects_api),
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
        # `CustomObjectsApi` accepts a wider parameter set than the narrow,
        # single-method `_RuntimeReadClient` protocol, so the structural
        # match is asserted here. `CoreV1Api` needs no such cast: its
        # narrower `_SecretReadClient` counterpart already matches
        # structurally.
        return (
            cast("_RuntimeReadClient", self._custom_objects_api),
            self._core_v1_api,
        )

    async def verify(self, token: str) -> RuntimePrincipal:
        """Validate `token` and return the Runtime identity it proves.

        Raises `Unauthenticated` for every failure mode -- a malformed
        token, a missing or terminating CR, a missing/invalid/foreign
        Secret, or a credential mismatch -- so a caller can never
        distinguish which precondition failed from the response alone.
        """
        runtime_id = parse_runtime_token(token)
        if runtime_id is None:
            raise Unauthenticated(_INVALID_CREDENTIAL)
        custom, core = await self._clients()
        cr = await self._get_live_cr(custom, runtime_id)
        secret = await self._get_secret(core, runtime_id)
        _require_valid_owned_secret(cr, secret)
        expected = _decode_secret_key(secret, contract.SECRET_KEY_RUNTIME_CREDENTIAL)
        if expected is None or not secrets.compare_digest(token, expected):
            raise Unauthenticated(_INVALID_CREDENTIAL)
        try:
            return RuntimePrincipal.from_cr(cr)
        except (KeyError, ValueError) as exc:
            raise Unauthenticated(_INVALID_CREDENTIAL) from exc

    async def _get_live_cr(self, custom: _RuntimeReadClient, runtime_id: UUID) -> Mapping[str, Any]:
        try:
            cr = await custom.get_namespaced_custom_object(
                contract.GROUP, contract.VERSION, self._namespace, contract.PLURAL, str(runtime_id)
            )
        except ApiException as exc:
            raise Unauthenticated(_INVALID_CREDENTIAL) from exc
        if cr.get("metadata", {}).get("deletionTimestamp") is not None:
            # A terminating CR revokes immediately: garbage collection may
            # not have removed the Secret yet, but routing or trusting a
            # Runtime already marked for deletion would defeat the point of
            # tying the credential's lifetime to the CR's.
            raise Unauthenticated(_INVALID_CREDENTIAL)
        return cr

    async def _get_secret(self, core: _SecretReadClient, runtime_id: UUID) -> client.V1Secret:
        try:
            return await core.read_namespaced_secret(
                contract.secret_name(str(runtime_id)), self._namespace
            )
        except ApiException as exc:
            raise Unauthenticated(_INVALID_CREDENTIAL) from exc


_verifier_state: dict[str, RuntimeCredentialVerifier | None] = {"instance": None}


def get_runtime_credential_verifier() -> RuntimeCredentialVerifier:
    """Return the process-wide Runtime credential verifier, creating it on first use."""
    verifier = _verifier_state["instance"]
    if verifier is None:
        verifier = RuntimeCredentialVerifier.from_settings(get_settings())
        _verifier_state["instance"] = verifier
    return verifier
