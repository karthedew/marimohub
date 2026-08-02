from dataclasses import dataclass, field
from typing import Any

import pytest

from app.commands.drain_sessions import _Clock, drain_sessions
from app.services import runtime_contract as contract

_NAMESPACE = "marimohub-sessions"


@dataclass
class _ListResult:
    items: list[object]


class FakeCustomObjectsApi:
    """A namespace of MarimoSession CR bodies, deletable by name."""

    def __init__(self, names: list[str]) -> None:
        self.objects: dict[str, dict[str, Any]] = {
            name: {"metadata": {"name": name}} for name in names
        }
        self.deleted: list[str] = []

    async def list_namespaced_custom_object(
        self, group: str, version: str, namespace: str, plural: str
    ) -> dict[str, Any]:
        return {"items": list(self.objects.values())}

    async def delete_namespaced_custom_object(
        self,
        group: str,
        version: str,
        namespace: str,
        plural: str,
        name: str,
        body: object | None = None,
    ) -> dict[str, Any]:
        self.deleted.append(name)
        return self.objects.pop(name)


class FakeCoreV1Api:
    """Simulates owner-reference GC removing every child after `gc_after_polls` checks.

    `polls` counts calls made through any of the three list methods below
    (a real drain checks all three every iteration), so `gc_after_polls=2`
    means children are still present on the first check and gone by the
    second -- standing in for GC completing some real but bounded time after
    the owning CR's foreground delete finishes.
    """

    def __init__(self, *, has_children: bool, gc_after_polls: int = 0) -> None:
        self.has_children = has_children
        self.gc_after_polls = gc_after_polls
        self.polls = 0

    def _remaining(self) -> list[object]:
        if not self.has_children:
            return []
        self.polls += 1
        if self.gc_after_polls and self.polls > self.gc_after_polls:
            return []
        return [object()]

    async def list_namespaced_pod(self, namespace: str, label_selector: str) -> _ListResult:
        return _ListResult(self._remaining())

    async def list_namespaced_service(self, namespace: str, label_selector: str) -> _ListResult:
        return _ListResult([])

    async def list_namespaced_secret(self, namespace: str, label_selector: str) -> _ListResult:
        return _ListResult([])


@dataclass
class _FakeClock:
    """A monotonic clock `drain_sessions` advances only when it calls `sleep`."""

    time: float = 0.0
    sleeps: list[float] = field(default_factory=list)

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.time += seconds

    def now(self) -> float:
        return self.time


@pytest.mark.asyncio
async def test_drain_deletes_every_session_and_reports_success_once_children_clear() -> None:
    custom = FakeCustomObjectsApi(["a", "b"])
    core = FakeCoreV1Api(has_children=False)
    clock = _FakeClock()

    ok = await drain_sessions(
        custom,
        core,
        _NAMESPACE,
        timeout_seconds=10,
        poll_interval_seconds=1,
        clock=_Clock(sleep=clock.sleep, now=clock.now),
    )

    assert ok is True
    assert set(custom.deleted) == {"a", "b"}
    assert custom.objects == {}


@pytest.mark.asyncio
async def test_drain_waits_for_children_before_reporting_success() -> None:
    custom = FakeCustomObjectsApi(["a"])
    core = FakeCoreV1Api(has_children=True, gc_after_polls=2)
    clock = _FakeClock()

    ok = await drain_sessions(
        custom,
        core,
        _NAMESPACE,
        timeout_seconds=10,
        poll_interval_seconds=1,
        clock=_Clock(sleep=clock.sleep, now=clock.now),
    )

    assert ok is True
    assert len(clock.sleeps) >= 1


@pytest.mark.asyncio
async def test_drain_times_out_when_children_never_clear() -> None:
    custom = FakeCustomObjectsApi(["a"])
    core = FakeCoreV1Api(has_children=True)
    clock = _FakeClock()

    ok = await drain_sessions(
        custom,
        core,
        _NAMESPACE,
        timeout_seconds=3,
        poll_interval_seconds=1,
        clock=_Clock(sleep=clock.sleep, now=clock.now),
    )

    assert ok is False
    assert clock.time >= 3


@pytest.mark.asyncio
async def test_drain_with_no_sessions_and_no_children_returns_immediately() -> None:
    custom = FakeCustomObjectsApi([])
    core = FakeCoreV1Api(has_children=False)
    clock = _FakeClock()

    ok = await drain_sessions(
        custom,
        core,
        _NAMESPACE,
        timeout_seconds=10,
        poll_interval_seconds=1,
        clock=_Clock(sleep=clock.sleep, now=clock.now),
    )

    assert ok is True
    assert clock.sleeps == []


def test_child_label_selector_is_a_bare_existence_query() -> None:
    # No "=value": every Runtime child carries this key regardless of which
    # Runtime it belongs to, so an existence-only selector is what actually
    # catches "any child of any Runtime remains".
    assert "=" not in contract.LABEL_SESSION
