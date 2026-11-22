"""Periodic and delayed jobs.

Two ways to schedule work:

* imperatively through the `Scheduler` service::

      @component("acme.cache", requires_services=[Scheduler])
      class Cache(Component):
          async def start(self, ctx, scheduler: Scheduler):
              job = scheduler.every(60, self.evict, name="evict")
              ctx.defer(job.cancel)       # optional: done automatically on stop

* declaratively, by contributing a `Job` to `JOBS`::

      @component("acme.report", extensions=[contribution(JOBS, Job("daily", 86400, send))])

Jobs run in the context of the component that owns them (log records carry
its id), never overlap with themselves, and are cancelled automatically when
their owner stops or the contribution is withdrawn.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import random
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from ..core import (
    Component,
    ComponentContext,
    ComponentState,
    ComponentStateChanged,
    ExtensionPoint,
    Strategy,
    component,
)
from ..core._util import EXTERNAL_OWNER, create_task, current_component
from ..core.extensions import Extension

__all__ = ["JOBS", "Job", "JobHandle", "Scheduler", "SchedulerComponent"]

log = logging.getLogger("corlet.scheduler")
JobFunc = Callable[[], Any] | Callable[[], Awaitable[Any]]


@dataclass(frozen=True)
class Job:
    """Declarative job description (contribute it to `JOBS`)."""

    name: str
    interval: float
    func: JobFunc
    initial_delay: float | None = None
    """Delay before the first run (default: ``interval``)."""
    jitter: float = 0.0
    """Random extra delay in seconds, per run."""
    run_on_start: bool = False


JOBS: ExtensionPoint[Job] = ExtensionPoint(
    "corlet.scheduler.jobs",
    contract=Job,
    strategy=Strategy.BROADCAST,
    description="Periodic jobs run by the corlet.scheduler component",
)


@dataclass(eq=False)
class JobHandle:
    """A scheduled job, returned by `Scheduler.every` and `Scheduler.after`."""

    name: str
    owner: str
    interval: float | None
    runs: int = 0
    failures: int = 0
    last_run: float | None = None
    last_error: BaseException | None = field(default=None, repr=False)
    _task: asyncio.Task[None] | None = field(default=None, repr=False)
    _scheduler: _SchedulerImpl | None = field(default=None, repr=False)

    @property
    def active(self) -> bool:
        """Whether the job is still scheduled."""
        return self._task is not None and not self._task.done()

    def cancel(self) -> None:
        """Stop the job (idempotent)."""
        if self._task is not None:
            self._task.cancel()
        if self._scheduler is not None:
            self._scheduler._jobs.discard(self)

    close = cancel


@runtime_checkable
class Scheduler(Protocol):
    """Scheduling service registered by ``corlet.scheduler``."""

    def every(
        self,
        interval: float,
        func: JobFunc,
        *,
        name: str | None = None,
        initial_delay: float | None = None,
        jitter: float = 0.0,
        run_now: bool = False,
    ) -> JobHandle:
        """Run ``func`` every ``interval`` seconds.

        Runs never overlap; the job belongs to the calling component and is
        cancelled when that component stops.

        Args:
            interval: Seconds between the starts of two runs.
            func: Sync or async callable without arguments.
            name: Name shown in `jobs` (default: the function's name).
            initial_delay: Delay before the first run (default ``interval``).
            jitter: Random extra delay up to this many seconds, per run.
            run_now: Run immediately, then every ``interval``.
        """
        ...

    def after(self, delay: float, func: JobFunc, *, name: str | None = None) -> JobHandle:
        """Run ``func`` once after ``delay`` seconds."""
        ...

    def jobs(self) -> list[JobHandle]:
        """Active jobs, sorted by owner and name."""
        ...


class _SchedulerImpl:
    def __init__(self) -> None:
        self._jobs: set[JobHandle] = set()

    def jobs(self) -> list[JobHandle]:
        return sorted(self._jobs, key=lambda j: (j.owner, j.name))

    def every(
        self,
        interval: float,
        func: JobFunc,
        *,
        name: str | None = None,
        initial_delay: float | None = None,
        jitter: float = 0.0,
        run_now: bool = False,
        owner: str | None = None,
    ) -> JobHandle:
        if interval <= 0:
            raise ValueError("interval must be positive")
        owner = owner or current_component.get() or EXTERNAL_OWNER
        handle = JobHandle(name or _name_of(func), owner, interval, _scheduler=self)
        first = 0.0 if run_now else (interval if initial_delay is None else initial_delay)
        self._launch(handle, self._loop_job(handle, func, first, interval, jitter))
        return handle

    def after(
        self, delay: float, func: JobFunc, *, name: str | None = None, owner: str | None = None
    ) -> JobHandle:
        owner = owner or current_component.get() or EXTERNAL_OWNER
        handle = JobHandle(name or _name_of(func), owner, None, _scheduler=self)
        self._launch(handle, self._once(handle, func, delay))
        return handle

    def _launch(self, handle: JobHandle, coro: Any) -> None:
        cid = handle.owner if handle.owner != EXTERNAL_OWNER else None
        handle._task = create_task(coro, name=f"job:{handle.owner}:{handle.name}", component_id=cid)
        self._jobs.add(handle)
        handle._task.add_done_callback(lambda _t: self._jobs.discard(handle))

    async def _run(self, handle: JobHandle, func: JobFunc) -> None:
        handle.last_run = time.time()
        try:
            result = func()
            if inspect.isawaitable(result):
                await result
            handle.runs += 1
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            handle.failures += 1
            handle.last_error = exc
            log.exception("job %s of %s failed", handle.name, handle.owner)

    async def _loop_job(
        self, handle: JobHandle, func: JobFunc, first: float, interval: float, jitter: float
    ) -> None:
        delay = first
        while True:
            if delay > 0:
                await asyncio.sleep(delay + (random.uniform(0, jitter) if jitter else 0.0))
            started = time.monotonic()
            await self._run(handle, func)
            # fixed-rate scheduling, never overlapping, never negative
            delay = max(0.0, interval - (time.monotonic() - started))

    async def _once(self, handle: JobHandle, func: JobFunc, delay: float) -> None:
        await asyncio.sleep(delay)
        await self._run(handle, func)

    def cancel_owner(self, owner: str) -> int:
        mine = [j for j in self._jobs if j.owner == owner]
        for j in mine:
            j.cancel()
        return len(mine)

    def cancel_all(self) -> None:
        for j in list(self._jobs):
            j.cancel()


def _name_of(func: Any) -> str:
    return getattr(func, "__qualname__", None) or getattr(func, "__name__", None) or repr(func)


@component(
    "corlet.scheduler",
    version="0.1.0",
    description="Interval and delayed jobs (service + extension point)",
    provides=[Scheduler],
    defines=[JOBS],
)
class SchedulerComponent(Component):
    """``corlet.scheduler``: registers `Scheduler` and runs jobs contributed to `JOBS`."""

    async def start(self, ctx: ComponentContext) -> None:
        impl = _SchedulerImpl()
        self.impl = impl
        self._declared: dict[int, JobHandle] = {}
        ctx.services.register(Scheduler, impl)

        # Jobs scheduled through the service by a component stop with it.
        def on_state(event: ComponentStateChanged) -> None:
            if event.new in (ComponentState.STOPPING, ComponentState.FAILED):
                n = impl.cancel_owner(event.component_id)
                if n:
                    log.debug("cancelled %d job(s) of %s", n, event.component_id)

        ctx.events.subscribe(ComponentStateChanged, on_state)
        ctx.extensions.watch(JOBS, self._add, self._remove)
        ctx.defer(impl.cancel_all)

    def _add(self, ext: Extension) -> None:
        job: Job = ext.impl
        self._declared[id(ext)] = self.impl.every(
            job.interval,
            job.func,
            name=job.name,
            initial_delay=job.initial_delay,
            jitter=job.jitter,
            run_now=job.run_on_start,
            owner=ext.owner,
        )

    def _remove(self, ext: Extension) -> None:
        handle = self._declared.pop(id(ext), None)
        if handle is not None:
            handle.cancel()
