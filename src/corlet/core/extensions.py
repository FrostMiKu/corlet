"""Extension points: named slots that many components contribute to.

A *service* is "here is one object you can use"; an *extension point* is "I
define a slot, everybody may put something in it".  The defining component
decides how contributions are invoked (`Strategy`) and contributors
control their position with ``order`` / ``before`` / ``after``.

Typical uses: CLI sub-commands, HTTP routes, middleware chains, serializers,
validation rules, scheduled jobs.
"""

from __future__ import annotations

import heapq
import inspect
import itertools
import logging
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from types import MappingProxyType
from typing import Any, Generic, Literal, TypeVar

from ._util import EXTERNAL_OWNER, call_maybe_async, create_task, describe
from .errors import ExtensionContractError
from .registry import check_contract

__all__ = [
    "Extension",
    "ExtensionPoint",
    "ExtensionRegistry",
    "ExtensionSpec",
    "ExtensionWatch",
    "Strategy",
    "contribution",
]

T = TypeVar("T")
log = logging.getLogger("corlet.extensions")
_seq = itertools.count(1)
ErrorMode = Literal["raise", "log"]


class Strategy(StrEnum):
    """How an extension point invokes its contributions."""

    BROADCAST = "broadcast"
    """call all, ignore results"""
    COLLECT = "collect"
    """call all, return list of results"""
    FIRST = "first"
    """return the first non-None result"""
    PIPELINE = "pipeline"
    """thread a value through every contribution"""


@dataclass(frozen=True, eq=False)
class ExtensionPoint(Generic[T]):
    """Declaration of an extension point.

    ``contract`` describes what contributions must look like (a class,
    a runtime-checkable Protocol, or ``None`` for "anything").  If ``method``
    is set, invoking the point calls ``getattr(contribution, method)``;
    otherwise the contribution itself is called.
    """

    id: str
    contract: Any = None
    strategy: Strategy = Strategy.COLLECT
    method: str | None = None
    description: str = ""
    blocking: bool = False
    """Run *sync* contributions in a worker thread during ``invoke``."""

    def __eq__(self, other: object) -> bool:
        return isinstance(other, ExtensionPoint) and other.id == self.id

    def __hash__(self) -> int:
        return hash(("ExtensionPoint", self.id))

    def __repr__(self) -> str:
        return f"<ExtensionPoint {self.id} strategy={self.strategy.value}>"


@dataclass(frozen=True)
class ExtensionSpec:
    """A declarative contribution, as listed in a component manifest."""

    point: ExtensionPoint[Any] | str
    impl: Any
    name: str | None = None
    order: int = 0
    before: frozenset[str] = frozenset()
    after: frozenset[str] = frozenset()
    metadata: Mapping[str, Any] = field(default_factory=dict)

    @property
    def point_id(self) -> str:
        """Id of the target extension point."""
        return self.point if isinstance(self.point, str) else self.point.id


def contribution(
    point: ExtensionPoint[Any] | str,
    impl: Any,
    *,
    name: str | None = None,
    order: int = 0,
    before: Iterable[str] = (),
    after: Iterable[str] = (),
    metadata: Mapping[str, Any] | None = None,
) -> ExtensionSpec:
    """Build an `ExtensionSpec` for ``@component(extensions=[...])``."""
    return ExtensionSpec(
        point, impl, name, order, frozenset(before), frozenset(after), dict(metadata or {})
    )


class Extension:
    """A live contribution. Call `revoke()` to withdraw it."""

    __slots__ = (
        "_registry",
        "active",
        "after",
        "before",
        "impl",
        "metadata",
        "name",
        "order",
        "owner",
        "point_id",
        "seq",
    )

    def __init__(
        self,
        registry: ExtensionRegistry,
        point_id: str,
        impl: Any,
        owner: str,
        name: str,
        order: int,
        before: frozenset[str],
        after: frozenset[str],
        metadata: Mapping[str, Any],
    ) -> None:
        self._registry = registry
        self.point_id = point_id
        self.impl = impl
        self.owner = owner
        self.name = name
        self.order = order
        self.before = before
        self.after = after
        self.metadata: Mapping[str, Any] = MappingProxyType(dict(metadata))
        self.seq = next(_seq)
        self.active = True

    def revoke(self) -> None:
        """Withdraw this contribution (idempotent)."""
        self._registry.revoke(self)

    close = revoke

    def __repr__(self) -> str:
        return f"<Extension {self.name} -> {self.point_id} by {self.owner}>"


ExtCallback = Callable[[Extension], Any]


class ExtensionWatch:
    """Subscription returned by `ExtensionRegistry.watch`."""

    __slots__ = ("_registry", "active", "on_add", "on_remove", "owner", "point_id")

    def __init__(
        self,
        registry: ExtensionRegistry,
        point_id: str,
        on_add: ExtCallback | None,
        on_remove: ExtCallback | None,
        owner: str,
    ) -> None:
        self._registry = registry
        self.point_id = point_id
        self.on_add = on_add
        self.on_remove = on_remove
        self.owner = owner
        self.active = True

    def cancel(self) -> None:
        """Stop watching (idempotent)."""
        if self.active:
            self.active = False
            ws = self._registry._watches.get(self.point_id, [])
            if self in ws:
                ws.remove(self)

    close = cancel


def order_extensions(exts: Iterable[Extension]) -> list[Extension]:
    """Topologically order contributions honoring before/after constraints.

    ``before``/``after`` entries may name either a contribution (``name``) or a
    contributing component id.  Ties are broken by ``order`` then by
    contribution sequence, so the result is deterministic.
    """
    items = list(exts)
    if not items:
        return []
    by_label: dict[str, list[int]] = {}
    for i, e in enumerate(items):
        by_label.setdefault(e.name, []).append(i)
        if e.owner != e.name:
            by_label.setdefault(e.owner, []).append(i)
    succ: dict[int, set[int]] = {i: set() for i in range(len(items))}
    for i, e in enumerate(items):
        for label in e.before:
            succ[i].update(j for j in by_label.get(label, ()) if j != i)
        for label in e.after:
            for j in by_label.get(label, ()):
                if j != i:
                    succ[j].add(i)
    indeg = dict.fromkeys(succ, 0)
    for targets in succ.values():
        for j in targets:
            indeg[j] += 1
    heap = [(items[i].order, items[i].seq, i) for i, d in indeg.items() if d == 0]
    heapq.heapify(heap)
    result: list[Extension] = []
    while heap:
        _, _, i = heapq.heappop(heap)
        result.append(items[i])
        for j in succ[i]:
            indeg[j] -= 1
            if indeg[j] == 0:
                heapq.heappush(heap, (items[j].order, items[j].seq, j))
    if len(result) != len(items):
        rest = sorted((e for e in items if e not in result), key=lambda e: (e.order, e.seq))
        log.warning(
            "cyclic before/after constraints among %s; falling back to 'order'",
            [e.name for e in rest],
        )
        result += rest
    return result


class ExtensionRegistry:
    """Extension point definitions and their live contributions."""

    def __init__(self) -> None:
        self._points: dict[str, ExtensionPoint[Any]] = {}
        self._point_owner: dict[str, str] = {}
        self._exts: dict[str, list[Extension]] = {}
        self._sorted: dict[str, list[Extension]] = {}
        self._watches: dict[str, list[ExtensionWatch]] = {}

    # -- definitions ----------------------------------------------------
    def define(self, point: ExtensionPoint[Any], owner: str = EXTERNAL_OWNER) -> None:
        """Define ``point`` on behalf of ``owner``.

        Raises:
            ValueError: the id is already defined by another owner.
        """
        existing = self._points.get(point.id)
        if existing is not None and self._point_owner[point.id] != owner:
            raise ValueError(
                f"extension point {point.id!r} is already defined by "
                f"{self._point_owner[point.id]!r}"
            )
        self._points[point.id] = point
        self._point_owner[point.id] = owner

    def point(self, point_id: str) -> ExtensionPoint[Any] | None:
        """The definition of ``point_id``, or ``None``."""
        return self._points.get(point_id)

    def points(self) -> list[tuple[ExtensionPoint[Any], str]]:
        """All definitions as ``(point, owner)`` pairs."""
        return [(p, self._point_owner[p.id]) for p in self._points.values()]

    def _resolve(self, point: ExtensionPoint[T] | str) -> ExtensionPoint[T]:
        if isinstance(point, ExtensionPoint):
            return self._points.get(point.id, point)
        found = self._points.get(point)
        return found if found is not None else ExtensionPoint(point)

    # -- contributions --------------------------------------------------
    def contribute(
        self,
        point: ExtensionPoint[Any] | str,
        impl: Any,
        *,
        owner: str = EXTERNAL_OWNER,
        name: str | None = None,
        order: int = 0,
        before: Iterable[str] = (),
        after: Iterable[str] = (),
        metadata: Mapping[str, Any] | None = None,
    ) -> Extension:
        """Contribute ``impl`` to ``point`` and return the live `Extension`.

        Args:
            point: The extension point or its id (it may be defined later).
            impl: The contribution; must satisfy the point's ``contract``
                and provide its ``method`` if set.
            owner: Contributing component id.
            name: Label used by ``before``/``after`` of other contributions
                (default ``"<owner>:<qualname>"``).
            order: Tie breaker, lower first.
            before: Names or component ids this contribution must precede.
            after: Names or component ids this contribution must follow.
            metadata: Arbitrary data for the defining component.

        Raises:
            ExtensionContractError: ``impl`` violates the point's contract.
        """
        p = self._resolve(point)
        if p.contract is not None and not check_contract(p.contract, impl):
            raise ExtensionContractError(
                f"{owner!r} contributed {describe(impl)} to {p.id!r}, which does "
                f"not satisfy contract {getattr(p.contract, '__qualname__', p.contract)!r}"
            )
        if p.method is not None and not callable(getattr(impl, p.method, None)):
            raise ExtensionContractError(
                f"contribution {describe(impl)} to {p.id!r} has no callable {p.method!r}"
            )
        ext = Extension(
            self,
            p.id,
            impl,
            owner,
            name or f"{owner}:{getattr(impl, '__qualname__', type(impl).__qualname__)}",
            order,
            frozenset(before),
            frozenset(after),
            metadata or {},
        )
        self._exts.setdefault(p.id, []).append(ext)
        self._sorted.pop(p.id, None)
        for w in list(self._watches.get(p.id, ())):
            if w.on_add is not None:
                self._notify(w, w.on_add, ext)
        return ext

    def contribute_spec(self, spec: ExtensionSpec, owner: str) -> Extension:
        """Contribute a declarative `ExtensionSpec` on behalf of ``owner``."""
        return self.contribute(
            spec.point,
            spec.impl,
            owner=owner,
            name=spec.name,
            order=spec.order,
            before=spec.before,
            after=spec.after,
            metadata=spec.metadata,
        )

    def revoke(self, ext: Extension) -> None:
        """Withdraw ``ext`` (idempotent); watchers are notified."""
        if not ext.active:
            return
        bucket = self._exts.get(ext.point_id, [])
        if ext in bucket:
            bucket.remove(ext)
            self._sorted.pop(ext.point_id, None)
        for w in list(self._watches.get(ext.point_id, ())):
            if w.on_remove is not None:
                self._notify(w, w.on_remove, ext)
        ext.active = False

    def revoke_owner(self, owner: str) -> int:
        """Withdraw all contributions of ``owner``; returns how many."""
        mine = [e for bucket in self._exts.values() for e in bucket if e.owner == owner]
        for e in mine:
            self.revoke(e)
        return len(mine)

    # -- queries --------------------------------------------------------
    def extensions(self, point: ExtensionPoint[Any] | str) -> list[Extension]:
        """Live contributions to ``point`` in invocation order."""
        pid = point if isinstance(point, str) else point.id
        cached = self._sorted.get(pid)
        if cached is None:
            cached = order_extensions(self._exts.get(pid, ()))
            self._sorted[pid] = cached
        return list(cached)

    def implementations(self, point: ExtensionPoint[T] | str) -> list[T]:
        """Contributed objects of ``point`` in invocation order."""
        return [e.impl for e in self.extensions(point)]

    def all_extensions(self) -> list[Extension]:
        """Every live contribution, grouped by point id."""
        return [e for pid in sorted(self._exts) for e in self.extensions(pid)]

    # -- invocation -----------------------------------------------------
    def _target(self, p: ExtensionPoint[Any], ext: Extension) -> Callable[..., Any]:
        target = getattr(ext.impl, p.method) if p.method else ext.impl
        if not callable(target):
            raise ExtensionContractError(f"contribution {ext.name} to {p.id!r} is not callable")
        return target  # type: ignore[no-any-return]

    async def invoke(
        self,
        point: ExtensionPoint[Any] | str,
        *args: Any,
        errors: ErrorMode = "raise",
        **kwargs: Any,
    ) -> Any:
        """Invoke all contributions according to the point's strategy.

        * ``BROADCAST`` -> ``None``
        * ``COLLECT``   -> ``list`` of results
        * ``FIRST``     -> first non-``None`` result (or ``None``)
        * ``PIPELINE``  -> the value after passing through every contribution;
          the first positional argument is the initial value.

        With ``errors="log"`` failing contributions are logged and skipped.
        """
        p = self._resolve(point)
        if p.strategy is Strategy.PIPELINE:
            if not args:
                raise TypeError(f"pipeline point {p.id!r} requires an initial value")
            value, rest = args[0], args[1:]
            for ext in self.extensions(p):
                try:
                    value = await call_maybe_async(
                        self._target(p, ext), value, *rest, blocking=p.blocking, **kwargs
                    )
                except Exception:
                    if errors == "raise":
                        raise
                    log.exception("extension %s of %s failed", ext.name, p.id)
            return value
        results: list[Any] = []
        for ext in self.extensions(p):
            try:
                r = await call_maybe_async(
                    self._target(p, ext), *args, blocking=p.blocking, **kwargs
                )
            except Exception:
                if errors == "raise":
                    raise
                log.exception("extension %s of %s failed", ext.name, p.id)
                continue
            if p.strategy is Strategy.FIRST and r is not None:
                return r
            results.append(r)
        if p.strategy is Strategy.COLLECT:
            return results
        return None

    def invoke_sync(
        self,
        point: ExtensionPoint[Any] | str,
        *args: Any,
        errors: ErrorMode = "raise",
        **kwargs: Any,
    ) -> Any:
        """Synchronous variant of `invoke()`; contributions must be sync."""
        p = self._resolve(point)

        def call(ext: Extension, *a: Any) -> Any:
            r = self._target(p, ext)(*a, **kwargs)
            if inspect.isawaitable(r):
                if inspect.iscoroutine(r):
                    r.close()
                raise ExtensionContractError(
                    f"contribution {ext.name} to {p.id!r} is async; use 'await invoke()'"
                )
            return r

        if p.strategy is Strategy.PIPELINE:
            if not args:
                raise TypeError(f"pipeline point {p.id!r} requires an initial value")
            value, rest = args[0], args[1:]
            for ext in self.extensions(p):
                try:
                    value = call(ext, value, *rest)
                except Exception:
                    if errors == "raise":
                        raise
                    log.exception("extension %s of %s failed", ext.name, p.id)
            return value
        results: list[Any] = []
        for ext in self.extensions(p):
            try:
                r = call(ext, *args)
            except Exception:
                if errors == "raise":
                    raise
                log.exception("extension %s of %s failed", ext.name, p.id)
                continue
            if p.strategy is Strategy.FIRST and r is not None:
                return r
            results.append(r)
        return results if p.strategy is Strategy.COLLECT else None

    # -- watching -------------------------------------------------------
    def watch(
        self,
        point: ExtensionPoint[Any] | str,
        on_add: ExtCallback | None = None,
        on_remove: ExtCallback | None = None,
        *,
        replay: bool = True,
        owner: str = EXTERNAL_OWNER,
    ) -> ExtensionWatch:
        """Track contributions to ``point`` being added and removed.

        With ``replay=True`` ``on_add`` is first called for existing
        contributions.  Async callbacks are scheduled as tasks.
        """
        pid = point if isinstance(point, str) else point.id
        w = ExtensionWatch(self, pid, on_add, on_remove, owner)
        self._watches.setdefault(pid, []).append(w)
        if replay and on_add is not None:
            for ext in self.extensions(pid):
                self._notify(w, on_add, ext)
        return w

    def _notify(self, w: ExtensionWatch, cb: ExtCallback, ext: Extension) -> None:
        try:
            result = cb(ext)
        except Exception:
            log.exception("extension watch callback of %r failed", w.owner)
            return
        if inspect.isawaitable(result):
            try:
                create_task(_await_logged(result, w.owner), component_id=w.owner)
            except RuntimeError:
                if inspect.iscoroutine(result):
                    result.close()
                log.error("async watch callback of %r needs a running event loop", w.owner)


async def _await_logged(aw: Any, owner: str) -> None:
    try:
        await aw
    except Exception:
        log.exception("async extension watch callback of %r failed", owner)
