"""Internal helpers shared by the core subsystems. Not part of the public API."""

from __future__ import annotations

import asyncio
import contextvars
import inspect
from collections.abc import Awaitable, Callable, Coroutine
from typing import Any, TypeVar

T = TypeVar("T")

current_component: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "corlet_current_component", default=None
)
"""Id of the component on whose behalf the current code runs (``None`` for container/application
code).  Set while a component starts/stops, inside tasks it spawns and inside event handlers it
subscribed."""

in_lifecycle: contextvars.ContextVar[object | None] = contextvars.ContextVar(
    "corlet_in_lifecycle", default=None
)
"""Set while the current task executes *inside* a container lifecycle operation (i.e. it already
holds the container's lifecycle lock)."""

activation_chain: contextvars.ContextVar[tuple[str, ...]] = contextvars.ContextVar(
    "corlet_activation_chain", default=()
)
"""Component ids whose start() is executing in the current task chain; used to detect circular
*runtime* activation (lazy acquire cycles)."""

EXTERNAL_OWNER = "<external>"


def contract_key(contract: object) -> str:
    """Stable string key for a service contract or event type.

    Keys are based on the qualified *name* rather than object identity, so a
    contract class that is re-created by a hot reload still maps to the same
    registry slot.  Strings are used verbatim.
    """
    if isinstance(contract, str):
        return contract
    module = getattr(contract, "__module__", None)
    qualname = getattr(contract, "__qualname__", None) or getattr(contract, "__name__", None)
    if module is None or qualname is None:
        raise TypeError(f"cannot derive a contract key from {contract!r}")
    return f"{module}.{qualname}"


def contract_name(contract: object) -> str:
    if isinstance(contract, str):
        return contract
    return getattr(contract, "__qualname__", None) or repr(contract)


def mro_keys(obj: object) -> set[str]:
    return {contract_key(k) for k in type(obj).__mro__ if k is not object}


async def maybe_await(value: T | Awaitable[T]) -> T:
    if inspect.isawaitable(value):
        return await value
    return value


async def call_maybe_async(
    fn: Callable[..., Any], *args: Any, blocking: bool = False, **kwargs: Any
) -> Any:
    """Call ``fn`` which may be sync or async.

    With ``blocking=True`` a *sync* callable runs in a worker thread so it does
    not stall the event loop.  The current context (incl. ``current_component``)
    is propagated to the thread by `asyncio.to_thread()`.
    """
    if blocking and not inspect.iscoroutinefunction(fn):
        result = await asyncio.to_thread(fn, *args, **kwargs)
    else:
        result = fn(*args, **kwargs)
    return await maybe_await(result)


def detached_context(component_id: str | None) -> contextvars.Context:
    """A copy of the current context that is *outside* any lifecycle operation.

    Background tasks (spawned tasks, fire-and-forget event deliveries, crash
    handlers) must not inherit the "I hold the lifecycle lock" marker or the
    activation chain, otherwise they could bypass the container lock or be
    mistaken for a circular activation.
    """
    ctx = contextvars.copy_context()
    ctx.run(in_lifecycle.set, None)
    ctx.run(activation_chain.set, ())
    ctx.run(current_component.set, component_id)
    return ctx


def create_task(
    coro: Coroutine[Any, Any, T],
    *,
    name: str | None = None,
    component_id: str | None = None,
    loop: asyncio.AbstractEventLoop | None = None,
) -> asyncio.Task[T]:
    loop = loop or asyncio.get_running_loop()
    return loop.create_task(coro, name=name, context=detached_context(component_id))


def describe(obj: object) -> str:
    """Human readable name of a callable/object for diagnostics."""
    if inspect.isfunction(obj) or inspect.ismethod(obj) or inspect.isclass(obj):
        return f"{obj.__module__}.{obj.__qualname__}"
    cls = type(obj)
    return f"<{cls.__module__}.{cls.__qualname__} instance>"
