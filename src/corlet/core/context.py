"""``ComponentContext``: a component's only gateway to the container.

Everything a component registers through its context -- services, extension
contributions, event subscriptions, watches, spawned tasks, cleanup callbacks
-- is recorded in a per-activation ledger and released automatically when the
component stops, whether or not it implements ``stop()``.

In *strict* mode the context also enforces the manifest: a component may only
look up services it declared in ``requires_services`` (or provides itself),
may only register services it declared in ``provides`` and may only touch the
container if it is ``privileged``.  This keeps the architecture honest; it is
**not** a security sandbox.
"""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import logging
from collections.abc import AsyncIterator, Awaitable, Callable, Coroutine, Mapping
from typing import TYPE_CHECKING, Any, TypeVar, overload

from ._util import contract_key, contract_name, create_task, current_component
from .errors import PermissionDenied
from .events import EventBus, PublishResult, Subscription
from .extensions import (
    ErrorMode,
    Extension,
    ExtensionPoint,
    ExtensionRegistry,
    ExtensionWatch,
)
from .manifest import Manifest
from .registry import (
    PropertyFilter,
    ServiceRef,
    ServiceRegistration,
    ServiceRegistry,
    ServiceWatch,
)

if TYPE_CHECKING:
    from .container import Container

__all__ = ["ComponentContext", "ComponentEvents", "ComponentExtensions", "ComponentServices"]

T = TypeVar("T")
Closeable = ServiceRegistration | Extension | Subscription | ServiceWatch | ExtensionWatch


class _Ledger:
    """Bookkeeping of everything a component activation owns."""

    def __init__(self) -> None:
        self.handles: list[Closeable] = []
        self.tasks: set[asyncio.Task[Any]] = set()
        self.stack = contextlib.AsyncExitStack()
        self.closed = False

    def track(self, handle: Closeable) -> None:
        self.handles.append(handle)

    def counts(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for h in self.handles:
            if getattr(h, "active", False):
                key = type(h).__name__
                out[key] = out.get(key, 0) + 1
        live = sum(1 for t in self.tasks if not t.done())
        if live:
            out["Task"] = live
        return out


class ComponentServices:
    """Service registry access recorded in the component's ledger (``ctx.services``)."""

    def __init__(self, ctx: ComponentContext, registry: ServiceRegistry) -> None:
        self._ctx = ctx
        self._registry = registry
        m = ctx.manifest
        self._may_use = {r.key for r in m.requires_services} | m.provides_keys
        self._may_provide = m.provides_keys

    def _check_use(self, contract: object) -> None:
        if self._ctx.strict and contract_key(contract) not in self._may_use:
            raise PermissionDenied(
                f"component {self._ctx.id!r} looked up {contract_name(contract)} which is not "
                "declared in requires_services (strict mode)"
            )

    def register(
        self,
        contract: type[T] | str,
        instance: T,
        *,
        properties: Mapping[str, Any] | None = None,
        ranking: int = 0,
    ) -> ServiceRegistration:
        """Register a service owned by this component (withdrawn on stop)."""
        if self._ctx.strict and contract_key(contract) not in self._may_provide:
            raise PermissionDenied(
                f"component {self._ctx.id!r} registered {contract_name(contract)} which is not "
                "declared in provides (strict mode)"
            )
        reg = self._registry.register(
            contract, instance, owner=self._ctx.id, properties=properties, ranking=ranking
        )
        self._ctx._ledger.track(reg)
        return reg

    def get(self, contract: type[T] | str, flt: PropertyFilter = None) -> ServiceRef[T]:
        """Dynamic reference to the best matching service; raises `ServiceNotFound`."""
        self._check_use(contract)
        return self._registry.get(contract, flt)

    def find(self, contract: type[T] | str, flt: PropertyFilter = None) -> ServiceRef[T] | None:
        """Like `get` but returns ``None`` instead of raising."""
        self._check_use(contract)
        return self._registry.find(contract, flt)

    def ref(self, contract: type[T] | str, flt: PropertyFilter = None) -> ServiceRef[T]:
        """Dynamic reference without checking current availability."""
        self._check_use(contract)
        return self._registry.ref(contract, flt)

    def get_all(self, contract: type[T] | str, flt: PropertyFilter = None) -> list[ServiceRef[T]]:
        """Bound references to every matching service, best first."""
        self._check_use(contract)
        return self._registry.get_all(contract, flt)

    @overload
    def instance(self, contract: type[T], flt: PropertyFilter = None) -> T: ...
    @overload
    def instance(self, contract: str, flt: PropertyFilter = None) -> Any: ...
    def instance(self, contract: type[T] | str, flt: PropertyFilter = None) -> Any:
        """The raw service object right now (``get(contract).resolve()``)."""
        self._check_use(contract)
        return self._registry.instance(contract, flt)

    async def acquire(self, contract: type[T] | str, flt: PropertyFilter = None) -> ServiceRef[T]:
        """Like `get()` but activates a lazy provider on demand."""
        self._check_use(contract)
        return await self._registry.acquire(contract, flt)

    def has(self, contract: object, flt: PropertyFilter = None) -> bool:
        """Whether a matching service is registered."""
        return self._registry.has(contract, flt)

    def watch(
        self,
        contract: type[T] | str,
        on_add: Callable[[ServiceRef[T]], Any] | None = None,
        on_remove: Callable[[ServiceRef[T]], Any] | None = None,
        *,
        flt: PropertyFilter = None,
        replay: bool = True,
    ) -> ServiceWatch:
        """Track matching services appearing and disappearing.

        The watch is cancelled when the component stops.  See
        `ServiceRegistry.watch`.
        """
        self._check_use(contract)
        w = self._registry.watch(
            contract, on_add, on_remove, flt=flt, replay=replay, owner=self._ctx.id
        )
        self._ctx._ledger.track(w)
        return w


class ComponentExtensions:
    """Extension registry access recorded in the component's ledger (``ctx.extensions``)."""

    def __init__(self, ctx: ComponentContext, registry: ExtensionRegistry) -> None:
        self._ctx = ctx
        self._registry = registry

    def define(self, point: ExtensionPoint[Any]) -> ExtensionPoint[Any]:
        """Define an extension point at runtime (prefer ``@component(defines=...)``)."""
        self._registry.define(point, owner=self._ctx.id)
        return point

    def contribute(
        self,
        point: ExtensionPoint[Any] | str,
        impl: Any,
        *,
        name: str | None = None,
        order: int = 0,
        before: tuple[str, ...] | list[str] = (),
        after: tuple[str, ...] | list[str] = (),
        metadata: Mapping[str, Any] | None = None,
    ) -> Extension:
        """Contribute ``impl`` to ``point``; withdrawn when the component stops.

        See `ExtensionRegistry.contribute` for the parameters.
        """
        ext = self._registry.contribute(
            point,
            impl,
            owner=self._ctx.id,
            name=name,
            order=order,
            before=before,
            after=after,
            metadata=metadata,
        )
        self._ctx._ledger.track(ext)
        return ext

    def extensions(self, point: ExtensionPoint[Any] | str) -> list[Extension]:
        """Live contributions to ``point`` in invocation order."""
        return self._registry.extensions(point)

    def implementations(self, point: ExtensionPoint[T] | str) -> list[T]:
        """Contributed objects of ``point`` in invocation order."""
        return self._registry.implementations(point)

    async def invoke(
        self, point: ExtensionPoint[Any] | str, *args: Any, errors: ErrorMode = "raise", **kw: Any
    ) -> Any:
        """Invoke ``point`` (see `ExtensionRegistry.invoke`)."""
        return await self._registry.invoke(point, *args, errors=errors, **kw)

    def invoke_sync(
        self, point: ExtensionPoint[Any] | str, *args: Any, errors: ErrorMode = "raise", **kw: Any
    ) -> Any:
        """Invoke ``point`` synchronously (see `ExtensionRegistry.invoke_sync`)."""
        return self._registry.invoke_sync(point, *args, errors=errors, **kw)

    def watch(
        self,
        point: ExtensionPoint[Any] | str,
        on_add: Callable[[Extension], Any] | None = None,
        on_remove: Callable[[Extension], Any] | None = None,
        *,
        replay: bool = True,
    ) -> ExtensionWatch:
        """Track contributions to ``point``; cancelled when the component stops."""
        w = self._registry.watch(point, on_add, on_remove, replay=replay, owner=self._ctx.id)
        self._ctx._ledger.track(w)
        return w


class ComponentEvents:
    """Event bus access recorded in the component's ledger (``ctx.events``)."""

    def __init__(self, ctx: ComponentContext, bus: EventBus) -> None:
        self._ctx = ctx
        self._bus = bus

    def subscribe(
        self, target: str | type[Any], handler: Callable[[Any], Any], *, blocking: bool = False
    ) -> Subscription:
        """Subscribe ``handler``; cancelled when the component stops."""
        sub = self._bus.subscribe(target, handler, owner=self._ctx.id, blocking=blocking)
        self._ctx._ledger.track(sub)
        return sub

    def on(
        self, target: str | type[Any], *, blocking: bool = False
    ) -> Callable[[Callable[[Any], Any]], Callable[[Any], Any]]:
        """Decorator form of `subscribe`."""

        def deco(fn: Callable[[Any], Any]) -> Callable[[Any], Any]:
            self.subscribe(target, fn, blocking=blocking)
            return fn

        return deco

    def publish(self, event: object) -> None:
        """Publish ``event`` without waiting (see `EventBus.publish`)."""
        self._bus.publish(event)

    async def publish_and_wait(
        self, event: object, *, timeout: float | None = None
    ) -> PublishResult:
        """Publish ``event`` and await all handlers."""
        return await self._bus.publish_and_wait(event, timeout=timeout)


class ComponentContext:
    """Per-activation context handed to ``start``/``stop``/``reconfigure``."""

    def __init__(self, container: Container, manifest: Manifest, config: Any) -> None:
        self._container = container
        self.manifest = manifest
        self.id = manifest.id
        self.config: Any = config
        self.strict = container.strict
        self.logger = logging.getLogger(f"corlet.component.{self.id}")
        self._ledger = _Ledger()
        self.services = ComponentServices(self, container.services)
        self.extensions = ComponentExtensions(self, container.extensions)
        self.events = ComponentEvents(self, container.events)
        self.stopping = asyncio.Event()

    # -- container access ------------------------------------------------
    @property
    def container(self) -> Container:
        """The container itself; requires ``privileged=True`` in strict mode."""
        if self.strict and not self.manifest.privileged:
            raise PermissionDenied(
                f"component {self.id!r} accessed the container but is not privileged"
            )
        return self._container

    @property
    def container_name(self) -> str:
        """Name of the owning container (allowed in strict mode)."""
        return self._container.name

    def request_shutdown(self) -> None:
        """Ask the container to shut down (e.g. a CLI command finished)."""
        self._container.request_stop()

    def fail(self, error: BaseException) -> None:
        """Report that this component has crashed; triggers the supervisor."""
        self._container._report_crash(self.id, error, self)

    # -- resources -------------------------------------------------------
    def spawn(
        self,
        coro: Coroutine[Any, Any, T],
        *,
        name: str | None = None,
        critical: bool = True,
    ) -> asyncio.Task[T]:
        """Run a background task owned by this component.

        The task is cancelled when the component stops.  If a *critical* task
        raises, the component is considered crashed (see ``RestartPolicy``).
        """
        if self._ledger.closed:
            coro.close()
            raise RuntimeError(f"component {self.id!r} is stopped; cannot spawn tasks")
        task = create_task(coro, name=name or f"{self.id}:task", component_id=self.id)
        self._ledger.tasks.add(task)

        def done(t: asyncio.Task[Any]) -> None:
            self._ledger.tasks.discard(t)
            if t.cancelled() or self._ledger.closed:
                return
            exc = t.exception()
            if exc is not None:
                if critical:
                    self.logger.error("background task %s crashed", t.get_name(), exc_info=exc)
                    self.fail(exc)
                else:
                    self.logger.warning("background task %s failed", t.get_name(), exc_info=exc)

        task.add_done_callback(done)
        return task

    def defer(self, callback: Callable[[], Any | Awaitable[Any]]) -> None:
        """Register a cleanup callback (sync or async), run LIFO on stop."""

        async def run() -> None:
            result = callback()
            if inspect.isawaitable(result):
                await result

        self._ledger.stack.push_async_callback(run)

    async def enter_async_context(self, cm: contextlib.AbstractAsyncContextManager[T]) -> T:
        """Enter an async context manager that is exited when the component stops."""
        return await self._ledger.stack.enter_async_context(cm)

    def enter_context(self, cm: contextlib.AbstractContextManager[T]) -> T:
        """Enter a context manager that is exited when the component stops."""
        return self._ledger.stack.enter_context(cm)

    def owns(self) -> dict[str, int]:
        """What this activation currently owns (for diagnostics / leak tests)."""
        return self._ledger.counts()

    # -- teardown (called by the container) --------------------------------
    async def _close(self, timeout: float | None) -> list[BaseException]:
        """Release everything; returns errors raised by cleanup callbacks."""
        errors: list[BaseException] = []
        self._ledger.closed = True
        self.stopping.set()
        tasks = [t for t in self._ledger.tasks if not t.done()]
        for t in tasks:
            t.cancel()
        if tasks:
            _done, pending = await asyncio.wait(tasks, timeout=timeout)
            if pending:
                self.logger.warning(
                    "%d background task(s) did not finish after cancellation", len(pending)
                )
        token = current_component.set(self.id)
        try:
            await self._ledger.stack.aclose()
        except Exception as exc:
            self.logger.exception("cleanup callback failed")
            errors.append(exc)
        finally:
            current_component.reset(token)
        for handle in reversed(self._ledger.handles):
            try:
                handle.close()
            except Exception as exc:
                self.logger.exception("failed to release %r", handle)
                errors.append(exc)
        self._ledger.handles.clear()
        # Safety net: anything registered with our id bypassing the context.
        self._container.services.unregister_owner(self.id)
        self._container.extensions.revoke_owner(self.id)
        self._container.events.unsubscribe_owner(self.id)
        return errors

    @contextlib.asynccontextmanager
    async def _as_current(self) -> AsyncIterator[None]:
        token = current_component.set(self.id)
        try:
            yield
        finally:
            current_component.reset(token)

    def __repr__(self) -> str:
        return f"<ComponentContext {self.id}>"
