"""Decoupled, typed publish/subscribe.

Events are usually dataclasses deriving from `Event` with a class-level
``topic``::

    @dataclass(frozen=True)
    class UserCreated(Event):
        topic = "user.created"
        user_id: int

Subscriptions match either a *topic pattern* (``"user.*"`` matches exactly one
segment, ``"user.**"`` any number of segments) or an *event type* (matching
subclasses too).

* `EventBus.publish()` is fire-and-forget (safe to call from sync code and
  from other threads);
* `EventBus.publish_and_wait()` awaits every handler.

A failing handler never affects other handlers: the error is logged and a
`HandlerFailed` event is published.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Any, ClassVar, TypeVar

from ._util import (
    EXTERNAL_OWNER,
    contract_key,
    create_task,
    current_component,
    describe,
    mro_keys,
)

__all__ = ["Event", "EventBus", "HandlerFailed", "Message", "PublishResult", "Subscription"]

log = logging.getLogger("corlet.events")
E = TypeVar("E")
Handler = Callable[[Any], Any]


@dataclass(frozen=True)
class Event:
    """Base class for events. Subclasses set ``topic`` as a class attribute."""

    topic: ClassVar[str] = "event"


@dataclass(frozen=True)
class Message(Event):
    """An untyped event: ``Message("order.paid", {"id": 1})``."""

    topic: str  # type: ignore[misc]
    data: Any = None


@dataclass(frozen=True)
class HandlerFailed(Event):
    """Published when an event handler raises (topic ``corlet.event.handler_error``)."""

    topic = "corlet.event.handler_error"
    event_topic: str
    handler: str
    owner: str
    error: BaseException = field(repr=False)


@dataclass(frozen=True)
class PublishResult:
    """Outcome of `EventBus.publish_and_wait`."""

    delivered: int
    errors: tuple[BaseException, ...] = ()

    @property
    def ok(self) -> bool:
        """``True`` if no handler raised."""
        return not self.errors


def topic_of(event: object) -> str:
    topic = getattr(event, "topic", None)
    if isinstance(topic, str):
        return topic
    return contract_key(type(event))


@lru_cache(maxsize=1024)
def _compile(pattern: str) -> tuple[str, ...]:
    return tuple(pattern.split("."))


def topic_matches(pattern: str, topic: str) -> bool:
    if pattern in ("**", "#") or pattern == topic:
        return True
    return _match(_compile(pattern), tuple(topic.split(".")))


def _match(pat: tuple[str, ...], parts: tuple[str, ...]) -> bool:
    if not pat:
        return not parts
    head, rest = pat[0], pat[1:]
    if head in ("**", "#"):
        return any(_match(rest, parts[i:]) for i in range(len(parts) + 1))
    if not parts:
        return False
    return (head == "*" or head == parts[0]) and _match(rest, parts[1:])


class Subscription:
    """A live subscription. Call `cancel` to end it."""

    __slots__ = ("_bus", "active", "blocking", "handler", "owner", "pattern", "type_key")

    def __init__(
        self,
        bus: EventBus,
        pattern: str | None,
        type_key: str | None,
        handler: Handler,
        owner: str,
        blocking: bool,
    ) -> None:
        self._bus = bus
        self.pattern = pattern
        self.type_key = type_key
        self.handler = handler
        self.owner = owner
        self.blocking = blocking
        self.active = True

    def matches(self, event: object, topic: str) -> bool:
        """Whether this subscription receives ``event``."""
        if self.type_key is not None:
            return self.type_key in mro_keys(event)
        assert self.pattern is not None
        return topic_matches(self.pattern, topic)

    def cancel(self) -> None:
        """Stop receiving events (idempotent)."""
        if self.active:
            self.active = False
            if self in self._bus._subs:
                self._bus._subs.remove(self)

    close = cancel

    def __repr__(self) -> str:
        target = self.pattern if self.pattern is not None else self.type_key
        return f"<Subscription {target} -> {describe(self.handler)} by {self.owner}>"


class EventBus:
    """Publish/subscribe hub; see the module documentation."""

    def __init__(self) -> None:
        self._subs: list[Subscription] = []
        self._pending: set[asyncio.Task[Any]] = set()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._loop_thread: int | None = None
        self.published = 0

    def bind_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        """Bind the bus to the container's loop (enables cross-thread publish)."""
        self._loop = loop
        self._loop_thread = threading.get_ident()

    # -- subscribing ----------------------------------------------------
    def subscribe(
        self,
        target: str | type[Any],
        handler: Handler,
        *,
        owner: str = EXTERNAL_OWNER,
        blocking: bool = False,
    ) -> Subscription:
        """Subscribe ``handler`` to a topic pattern or an event type.

        ``blocking=True`` runs a sync handler in a worker thread.
        """
        if isinstance(target, str):
            sub = Subscription(self, target, None, handler, owner, blocking)
        else:
            sub = Subscription(self, None, contract_key(target), handler, owner, blocking)
        self._subs.append(sub)
        return sub

    def on(
        self, target: str | type[Any], *, owner: str = EXTERNAL_OWNER, blocking: bool = False
    ) -> Callable[[Handler], Handler]:
        """Decorator form of `subscribe()`."""

        def deco(fn: Handler) -> Handler:
            self.subscribe(target, fn, owner=owner, blocking=blocking)
            return fn

        return deco

    def unsubscribe_owner(self, owner: str) -> int:
        """Cancel all subscriptions of ``owner``; returns how many."""
        mine = [s for s in self._subs if s.owner == owner]
        for s in mine:
            s.cancel()
        return len(mine)

    def subscriptions(self) -> list[Subscription]:
        """All active subscriptions."""
        return list(self._subs)

    def _matching(self, event: object) -> tuple[str, list[Subscription]]:
        topic = topic_of(event)
        return topic, [s for s in self._subs if s.active and s.matches(event, topic)]

    # -- publishing -----------------------------------------------------
    def publish(self, event: object) -> None:
        """Deliver ``event`` asynchronously; returns immediately.

        Safe to call from synchronous code running on the loop and from other
        threads (when the bus is bound to a loop).
        """
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        if running is None:
            if self._loop is not None and not self._loop.is_closed():
                self._loop.call_soon_threadsafe(self._dispatch, event)
            else:
                log.debug("dropping event %s: no running event loop", topic_of(event))
            return
        if self._loop is not None and running is not self._loop and not self._loop.is_closed():
            self._loop.call_soon_threadsafe(self._dispatch, event)
            return
        self._dispatch(event)

    def _dispatch(self, event: object) -> None:
        topic, subs = self._matching(event)
        self.published += 1
        for sub in subs:
            task = create_task(
                self._deliver(sub, event, topic),
                name=f"corlet-event:{topic}",
                component_id=sub.owner if sub.owner != EXTERNAL_OWNER else None,
            )
            self._pending.add(task)
            task.add_done_callback(self._pending.discard)

    async def publish_and_wait(
        self, event: object, *, timeout: float | None = None
    ) -> PublishResult:
        """Deliver ``event`` to all handlers concurrently and wait for them."""
        topic, subs = self._matching(event)
        self.published += 1
        if not subs:
            return PublishResult(0)

        async def run(sub: Subscription) -> BaseException | None:
            token = current_component.set(sub.owner if sub.owner != EXTERNAL_OWNER else None)
            try:
                return await self._deliver(sub, event, topic)
            finally:
                current_component.reset(token)

        coro = asyncio.gather(*(run(s) for s in subs))
        if timeout is not None:
            results = await asyncio.wait_for(coro, timeout)
        else:
            results = await coro
        errors = tuple(r for r in results if r is not None)
        return PublishResult(len(subs), errors)

    async def _deliver(self, sub: Subscription, event: object, topic: str) -> BaseException | None:
        try:
            if sub.blocking and not inspect.iscoroutinefunction(sub.handler):
                result = await asyncio.to_thread(sub.handler, event)
            else:
                result = sub.handler(event)
            if inspect.isawaitable(result):
                await result
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.exception(
                "event handler %s (owner %s) failed for %s", describe(sub.handler), sub.owner, topic
            )
            if not isinstance(event, HandlerFailed):
                self.publish(HandlerFailed(topic, describe(sub.handler), sub.owner, exc))
            return exc
        return None

    async def drain(self, timeout: float | None = None) -> None:
        """Wait until every in-flight fire-and-forget delivery has finished."""
        deadline = None if timeout is None else time.monotonic() + timeout
        while self._pending:
            remaining = None if deadline is None else max(0.0, deadline - time.monotonic())
            done, _ = await asyncio.wait(set(self._pending), timeout=remaining)
            if deadline is not None and time.monotonic() >= deadline:
                break
            del done

    def cancel_pending(self) -> None:
        """Cancel all in-flight fire-and-forget deliveries."""
        for t in list(self._pending):
            t.cancel()
