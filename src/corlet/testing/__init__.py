"""Testing helpers for corlet applications and components.

::

    from corlet.testing import running, EventRecorder, assert_no_leaks

    async def test_orders_use_the_database():
        fake_db = FakeDatabase()
        async with running(OrdersComponent, services={Database: fake_db}) as c:
            orders = c.services.instance(OrderService)
            await orders.place(...)
            assert fake_db.inserted

``running`` starts only the components you list; their service dependencies
are satisfied by the mock objects in ``services``.  On exit it stops the
container and asserts that nothing leaked (services, extensions,
subscriptions, tasks owned by components).
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator, Callable, Mapping
from typing import Any

from ..core import (
    ComponentState,
    Container,
    ExtensionPoint,
    ServiceRegistration,
)
from ..core._util import EXTERNAL_OWNER
from ..core.events import EventBus, Subscription, topic_matches, topic_of
from ..core.registry import check_contract

__all__ = [
    "EventRecorder",
    "assert_extension_contract",
    "assert_no_leaks",
    "leaks",
    "running",
    "wait_for_state",
]


class EventRecorder:
    """Records events published on a bus (all of them by default)."""

    def __init__(self, bus: EventBus | Container, pattern: str = "**") -> None:
        self._bus = bus.events if isinstance(bus, Container) else bus
        self.pattern = pattern
        self.events: list[Any] = []
        self._waiters: list[tuple[Callable[[Any], bool], asyncio.Future[Any]]] = []
        self._sub: Subscription = self._bus.subscribe(pattern, self._record, owner="<recorder>")

    def _record(self, event: Any) -> None:
        self.events.append(event)
        for pred, fut in list(self._waiters):
            if not fut.done() and pred(event):
                fut.set_result(event)

    @property
    def topics(self) -> list[str]:
        """Topics of the recorded events, in order."""
        return [topic_of(e) for e in self.events]

    def of_type(self, cls: type) -> list[Any]:
        """Recorded events that are instances of ``cls``."""
        return [e for e in self.events if isinstance(e, cls)]

    def matching(self, pattern: str) -> list[Any]:
        """Recorded events whose topic matches ``pattern``."""
        return [e for e in self.events if topic_matches(pattern, topic_of(e))]

    async def wait_for(
        self, what: str | type | Callable[[Any], bool], *, timeout: float = 2.0
    ) -> Any:
        """Wait for (and return) the first matching event, past or future."""
        pred = _predicate(what)
        for e in self.events:
            if pred(e):
                return e
        fut: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
        entry = (pred, fut)
        self._waiters.append(entry)
        try:
            return await asyncio.wait_for(fut, timeout)
        finally:
            self._waiters.remove(entry)

    def clear(self) -> None:
        """Forget the recorded events."""
        self.events.clear()

    def close(self) -> None:
        """Stop recording."""
        self._sub.cancel()

    def __enter__(self) -> EventRecorder:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


def _predicate(what: str | type | Callable[[Any], bool]) -> Callable[[Any], bool]:
    if isinstance(what, str):
        return lambda e: topic_matches(what, topic_of(e))
    if isinstance(what, type):
        return lambda e: isinstance(e, what)
    return what


async def wait_for_state(
    container: Container, cid: str, state: ComponentState, *, timeout: float = 2.0
) -> None:
    """Poll until ``cid`` reaches ``state`` (useful after crashes/restarts)."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while container.state_of(cid) is not state:
        if loop.time() > deadline:
            raise AssertionError(
                f"{cid!r} is {container.state_of(cid).value}, expected {state.value} "
                f"(reason: {container.inspect().component(cid).reason})"
            )
        await asyncio.sleep(0.005)


def leaks(container: Container, component_id: str | None = None) -> dict[str, list[str]]:
    """Resources still owned by non-running components (or by ``component_id``)."""
    if component_id is not None:
        owners = {component_id}
    else:
        owners = {cid for cid in container.component_ids if not container.state_of(cid).running}
    found: dict[str, list[str]] = {}

    def add(owner: str, what: str) -> None:
        if owner in owners:
            found.setdefault(owner, []).append(what)

    for reg in container.services.registrations():
        add(reg.owner, f"service {reg.key}")
    for ext in container.extensions.all_extensions():
        add(ext.owner, f"extension {ext.name} -> {ext.point_id}")
    for sub in container.events.subscriptions():
        add(sub.owner, f"subscription {sub.pattern or sub.type_key}")
    for key, watches in container.services._watches.items():
        for w in watches:
            add(w.owner, f"service watch {key}")
    for pid, ewatches in container.extensions._watches.items():
        for ew in ewatches:
            add(ew.owner, f"extension watch {pid}")
    for cid in owners:
        if cid not in container or container.state_of(cid).running:
            continue
        ctx = container._records[cid].last_ctx
        if ctx is not None:
            live = ctx.owns().get("Task", 0)
            if live:
                add(cid, f"{live} task(s)")
    return found


def assert_no_leaks(container: Container, component_id: str | None = None) -> None:
    """Raise ``AssertionError`` if `leaks` finds any resources."""
    found = leaks(container, component_id)
    if found:
        lines = "\n".join(f"  {o}: {', '.join(items)}" for o, items in sorted(found.items()))
        raise AssertionError(f"resources leaked after stop:\n{lines}")


def assert_extension_contract(point: ExtensionPoint[Any], impl: Any) -> None:
    """Assert that ``impl`` would be accepted as a contribution to ``point``."""
    if point.contract is not None and not check_contract(point.contract, impl):
        raise AssertionError(f"{impl!r} does not satisfy the contract of {point.id!r}")
    if point.method is not None and not callable(getattr(impl, point.method, None)):
        raise AssertionError(f"{impl!r} has no callable {point.method!r} required by {point.id!r}")


@contextlib.asynccontextmanager
async def running(
    *components: Any,
    services: Mapping[Any, Any] | None = None,
    config: Mapping[str, Any] | None = None,
    check_leaks: bool = True,
    **container_kwargs: Any,
) -> AsyncIterator[Container]:
    """Start a container with ``components`` and mock ``services``; stop on exit."""
    container_kwargs.setdefault("name", "test")
    container = Container(*components, config=config, **container_kwargs)
    regs: list[ServiceRegistration] = []
    for contract, instance in (services or {}).items():
        regs.append(container.register_service(contract, instance))
    await container.start()
    try:
        yield container
    finally:
        await container.stop()
        for r in regs:
            r.unregister()
        if check_leaks:
            assert_no_leaks(container)
            stray = [r for r in container.services.registrations() if r.owner != EXTERNAL_OWNER]
            if stray:
                raise AssertionError(f"services still registered after stop: {stray}")
