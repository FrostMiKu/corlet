"""pytest plugin (auto-registered through the ``pytest11`` entry point).

Fixtures:

``corlet_container``
    a factory ``await corlet_container(*components, services=..., config=...)``
    returning a started container; every container it created is stopped and
    leak-checked at teardown.  Requires ``pytest-asyncio``.
``event_recorder``
    a factory ``event_recorder(container_or_bus, pattern="**")``.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable, Iterator, Mapping
from typing import Any

import pytest

from ..core import Container
from . import EventRecorder, assert_no_leaks


@pytest.fixture
def event_recorder() -> Iterator[Callable[..., EventRecorder]]:
    created: list[EventRecorder] = []

    def make(bus: Any, pattern: str = "**") -> EventRecorder:
        rec = EventRecorder(bus, pattern)
        created.append(rec)
        return rec

    yield make
    for rec in created:
        rec.close()


try:
    import pytest_asyncio
except ImportError:  # pragma: no cover
    pytest_asyncio = None  # type: ignore[assignment]

if pytest_asyncio is not None:

    @pytest_asyncio.fixture
    async def corlet_container() -> AsyncIterator[Callable[..., Any]]:
        created: list[Container] = []

        async def make(
            *components: Any,
            services: Mapping[Any, Any] | None = None,
            config: Mapping[str, Any] | None = None,
            start: bool = True,
            **kwargs: Any,
        ) -> Container:
            kwargs.setdefault("name", "test")
            c = Container(*components, config=config, **kwargs)
            for contract, instance in (services or {}).items():
                c.register_service(contract, instance)
            created.append(c)
            if start:
                await c.start()
            return c

        yield make
        for c in created:
            await c.stop()
            assert_no_leaks(c)
