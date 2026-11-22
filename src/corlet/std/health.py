"""Health checks.

Components contribute checks to `HEALTH_CHECKS`; the
`HealthService` aggregates them together with component states::

    async def db_reachable() -> CheckResult:
        ok = await pool.ping()
        return CheckResult.up() if ok else CheckResult.down("ping failed")

    @component("acme.db", extensions=[contribution(HEALTH_CHECKS, HealthCheck("db", db_reachable))])

A check function may return a `CheckResult`, a ``bool`` or ``None``
(= up); raising or timing out counts as down.
"""

from __future__ import annotations

import asyncio
import inspect
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Protocol, runtime_checkable

from ..core import (
    Component,
    ComponentContext,
    ComponentState,
    ExtensionPoint,
    Strategy,
    component,
)

__all__ = [
    "HEALTH_CHECKS",
    "CheckResult",
    "HealthCheck",
    "HealthComponent",
    "HealthReport",
    "HealthService",
    "Status",
]


class Status(StrEnum):
    """Health status."""

    UP = "up"
    DEGRADED = "degraded"
    DOWN = "down"


@dataclass(frozen=True)
class CheckResult:
    """Result of one health check."""

    status: Status
    detail: str | None = None
    data: Mapping[str, Any] = field(default_factory=dict)

    @classmethod
    def up(cls, detail: str | None = None, **data: Any) -> CheckResult:
        """A passing result."""
        return cls(Status.UP, detail, data)

    @classmethod
    def degraded(cls, detail: str | None = None, **data: Any) -> CheckResult:
        """A degraded result."""
        return cls(Status.DEGRADED, detail, data)

    @classmethod
    def down(cls, detail: str | None = None, **data: Any) -> CheckResult:
        """A failing result."""
        return cls(Status.DOWN, detail, data)


CheckFunc = Callable[[], Awaitable[Any] | Any]


@dataclass(frozen=True)
class HealthCheck:
    """A health check; contribute it to `HEALTH_CHECKS`.

    Attributes:
        name: Name shown in the report.
        check: Sync or async callable returning `CheckResult`, ``bool`` or ``None``.
        timeout: Seconds before the check counts as down.
        critical: A failing non-critical check degrades instead of downing.
    """

    name: str
    check: CheckFunc
    timeout: float = 5.0
    critical: bool = True


HEALTH_CHECKS: ExtensionPoint[HealthCheck] = ExtensionPoint(
    "corlet.health.checks",
    contract=HealthCheck,
    strategy=Strategy.COLLECT,
    description="Health checks aggregated by corlet.health",
)


@dataclass(frozen=True)
class CheckReport:
    """Outcome of one check within a `HealthReport`."""

    name: str
    owner: str
    status: Status
    detail: str | None
    duration_ms: float
    critical: bool


@dataclass(frozen=True)
class HealthReport:
    """Aggregated health: overall status, checks and component states."""

    status: Status
    checks: tuple[CheckReport, ...]
    components: Mapping[str, str]
    generated_at: float

    def to_dict(self) -> dict[str, Any]:
        """JSON-serializable representation."""
        return {
            "status": self.status.value,
            "generated_at": self.generated_at,
            "components": dict(self.components),
            "checks": [
                {
                    "name": c.name,
                    "owner": c.owner,
                    "status": c.status.value,
                    "detail": c.detail,
                    "duration_ms": round(c.duration_ms, 2),
                    "critical": c.critical,
                }
                for c in self.checks
            ],
        }


@runtime_checkable
class HealthService(Protocol):
    """Health aggregation service registered by ``corlet.health``."""

    async def report(self) -> HealthReport:
        """Run all contributed checks concurrently and aggregate them.

        The status is DOWN if a critical check is down, DEGRADED if a
        non-critical check failed or a component is FAILED, otherwise UP.
        """
        ...


def _coerce(result: Any) -> CheckResult:
    if isinstance(result, CheckResult):
        return result
    if result is None or result is True:
        return CheckResult.up()
    if result is False:
        return CheckResult.down()
    return CheckResult.up(str(result))


class _Health:
    def __init__(self, ctx: ComponentContext) -> None:
        self._ctx = ctx

    async def _run(self, owner: str, hc: HealthCheck) -> CheckReport:
        t0 = time.perf_counter()
        try:
            r = hc.check()
            if inspect.isawaitable(r):
                r = await asyncio.wait_for(r, hc.timeout)
            res = _coerce(r)
        except TimeoutError:
            res = CheckResult.down(f"timed out after {hc.timeout}s")
        except Exception as exc:
            res = CheckResult.down(f"{type(exc).__name__}: {exc}")
        status = res.status
        if status is Status.DOWN and not hc.critical:
            status = Status.DEGRADED
        return CheckReport(
            hc.name, owner, status, res.detail, (time.perf_counter() - t0) * 1000, hc.critical
        )

    async def report(self) -> HealthReport:
        exts = self._ctx.extensions.extensions(HEALTH_CHECKS)
        checks = await asyncio.gather(*(self._run(e.owner, e.impl) for e in exts))
        container = self._ctx.container
        states = {cid: container.state_of(cid).value for cid in container.component_ids}
        status = Status.UP
        if any(c.status is Status.DEGRADED for c in checks) or any(
            s == ComponentState.FAILED.value for s in states.values()
        ):
            status = Status.DEGRADED
        if any(c.status is Status.DOWN for c in checks):
            status = Status.DOWN
        return HealthReport(status, tuple(checks), states, time.time())


@component(
    "corlet.health",
    version="0.1.0",
    description="Aggregated health report from contributed checks and component states",
    provides=[HealthService],
    defines=[HEALTH_CHECKS],
    privileged=True,
)
class HealthComponent(Component):
    """``corlet.health``: registers `HealthService` and defines `HEALTH_CHECKS`."""

    async def start(self, ctx: ComponentContext) -> None:
        ctx.services.register(HealthService, _Health(ctx))
