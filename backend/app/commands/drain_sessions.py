"""Pre-delete uninstall drain: foreground-deletes every MarimoSession CR.

The chart's `templates/uninstall-drain/job.yaml` runs this as a Helm
`pre-delete` hook before Helm removes the operator or NetworkPolicies, so no
untrusted notebook Runtime Pod, Service, or credential Secret ever outlives
the release that owns them. Unlike the migration Job and the maintenance
CronJobs, this command never touches the database: it only ever talks to
the Kubernetes API, and its own RBAC (see the chart's uninstall-drain Role)
grants nothing beyond MarimoSession get/list/watch/delete plus
Pod/Service/Secret get/list/watch.

Run via `python -m app.commands.drain_sessions`. Configuration is read
directly from the environment (not `app.core.config.Settings`, which
requires `DATABASE_URL`/`SECRET_KEY` this command has no use for):

- `SESSION_NAMESPACE` (required): the sessions namespace to drain.
- `DRAIN_TIMEOUT_SECONDS` (default 120): bounds how long to wait for every
  MarimoSession and its owned Pods/Services/Secrets to fully disappear
  before aborting.
- `DRAIN_POLL_INTERVAL_SECONDS` (default 2): how often to re-check.

Exits 0 once every MarimoSession CR, and every Pod/Service/Secret it owned,
is gone. Exits 1 -- which Helm's hook-failure detection treats as blocking
the uninstall -- if any remain once the timeout elapses.
"""

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
import logging
import os
import sys
from typing import Any, Protocol, cast

from kubernetes_asyncio import client, config as kube_config
from kubernetes_asyncio.client.api.core_v1_api import CoreV1Api
from kubernetes_asyncio.client.api.custom_objects_api import CustomObjectsApi
from kubernetes_asyncio.client.exceptions import ApiException
from kubernetes_asyncio.config.config_exception import ConfigException

from app.services import runtime_contract as contract

logger = logging.getLogger("app.commands.drain_sessions")

_DEFAULT_TIMEOUT_SECONDS = 120.0
_DEFAULT_POLL_INTERVAL_SECONDS = 2.0
_HTTP_NOT_FOUND = 404


class _CustomObjectsClient(Protocol):
    """The slice of `CustomObjectsApi` this command calls; narrow enough for a test fake.

    Mirrors `app.services.kube_session_manager._CustomObjectsClient`.
    """

    def list_namespaced_custom_object(
        self, group: str, version: str, namespace: str, plural: str
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


class _ListResult(Protocol):
    items: list[object]


class _CoreV1Client(Protocol):
    """The slice of `CoreV1Api` this command calls; narrow enough for a test fake."""

    def list_namespaced_pod(
        self, namespace: str, label_selector: str
    ) -> Awaitable[_ListResult]: ...

    def list_namespaced_service(
        self, namespace: str, label_selector: str
    ) -> Awaitable[_ListResult]: ...

    def list_namespaced_secret(
        self, namespace: str, label_selector: str
    ) -> Awaitable[_ListResult]: ...


async def _load_clients() -> tuple[CustomObjectsApi, CoreV1Api]:
    try:
        kube_config.load_incluster_config()
    except ConfigException:
        await kube_config.load_kube_config()
    api_client = client.ApiClient()
    return CustomObjectsApi(api_client), CoreV1Api(api_client)


async def _list_session_names(custom: _CustomObjectsClient, namespace: str) -> list[str]:
    listing = await custom.list_namespaced_custom_object(
        contract.GROUP, contract.VERSION, namespace, contract.PLURAL
    )
    return [item["metadata"]["name"] for item in listing.get("items", [])]


async def _delete_all_sessions(
    custom: _CustomObjectsClient, namespace: str, names: list[str]
) -> None:
    for name in names:
        try:
            await custom.delete_namespaced_custom_object(
                contract.GROUP,
                contract.VERSION,
                namespace,
                contract.PLURAL,
                name,
                body=client.V1DeleteOptions(propagation_policy="Foreground"),
            )
        except ApiException as exc:
            if exc.status != _HTTP_NOT_FOUND:
                raise


async def _children_remain(core: _CoreV1Client, namespace: str) -> bool:
    """Whether any Pod/Service/Secret still carries the child-of-a-Runtime label.

    Checked independently of the CR listing itself: owner-reference GC runs
    asynchronously to a foreground CR delete completing, so a drain that
    only watched the CR list could return before every child is actually
    gone.
    """
    # `contract.LABEL_SESSION` alone (no `=value`) is a label-selector
    # existence query: every Runtime child carries this key regardless of
    # which Runtime it belongs to.
    pods = await core.list_namespaced_pod(namespace, label_selector=contract.LABEL_SESSION)
    if pods.items:
        return True
    services = await core.list_namespaced_service(namespace, label_selector=contract.LABEL_SESSION)
    if services.items:
        return True
    secrets = await core.list_namespaced_secret(namespace, label_selector=contract.LABEL_SESSION)
    return bool(secrets.items)


@dataclass
class _Clock:
    """The two time primitives `drain_sessions` needs, injectable for tests.

    Bundled into one parameter rather than passed as two (`sleep`/`now`)
    separately so the function itself stays under the project's positional/
    keyword argument-count lint threshold.
    """

    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep
    now: Callable[[], float] = field(default_factory=lambda: _loop_time)


def _loop_time() -> float:
    return asyncio.get_running_loop().time()


async def drain_sessions(
    custom: _CustomObjectsClient,
    core: _CoreV1Client,
    namespace: str,
    *,
    timeout_seconds: float,
    poll_interval_seconds: float,
    clock: _Clock | None = None,
) -> bool:
    """Delete every MarimoSession in `namespace` and wait for it and its children to be gone.

    Returns `True` once nothing remains, `False` if `timeout_seconds`
    elapses first. `clock` is injectable so a test can drive this without a
    real clock or real waiting.
    """
    clock = clock if clock is not None else _Clock()
    names = await _list_session_names(custom, namespace)
    logger.info("draining %d MarimoSession(s) in %s", len(names), namespace)
    await _delete_all_sessions(custom, namespace, names)

    deadline = clock.now() + timeout_seconds
    while True:
        remaining = await _list_session_names(custom, namespace)
        if not remaining and not await _children_remain(core, namespace):
            logger.info("drain complete: no MarimoSession or owned child remains in %s", namespace)
            return True
        if clock.now() >= deadline:
            logger.error(
                "drain timed out after %ss: %d MarimoSession(s) still present in %s",
                timeout_seconds,
                len(remaining),
                namespace,
            )
            return False
        await clock.sleep(poll_interval_seconds)


async def _run() -> bool:
    namespace = os.environ.get("SESSION_NAMESPACE")
    if not namespace:
        logger.error("SESSION_NAMESPACE is required")
        return False
    timeout_seconds = float(os.environ.get("DRAIN_TIMEOUT_SECONDS", _DEFAULT_TIMEOUT_SECONDS))
    poll_interval_seconds = float(
        os.environ.get("DRAIN_POLL_INTERVAL_SECONDS", _DEFAULT_POLL_INTERVAL_SECONDS)
    )
    custom, core = await _load_clients()
    # The generated clients accept a wider parameter set (e.g.
    # `grace_period_seconds`) than the narrow Protocols above actually call
    # through, so the structural match is asserted here rather than
    # satisfied automatically -- the same pattern
    # `kube_session_manager.KubeSessionManager._clients` uses for the same
    # reason.
    return await drain_sessions(
        cast("_CustomObjectsClient", custom),
        cast("_CoreV1Client", core),
        namespace,
        timeout_seconds=timeout_seconds,
        poll_interval_seconds=poll_interval_seconds,
    )


def main() -> None:
    """Run the drain once; exit non-zero on timeout so Helm blocks the uninstall."""
    logging.basicConfig(level=logging.INFO)
    if not asyncio.run(_run()):
        sys.exit(1)


if __name__ == "__main__":
    main()
