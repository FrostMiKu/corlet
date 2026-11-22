"""The service registry: runtime exchange of capabilities between components.

Services are registered under a *contract* (normally a ``typing.Protocol`` or
ABC, but any class or a plain string works).  Contracts are keyed by their
qualified name, so registrations survive hot reloads of the contract module.

Lookups return `ServiceRef` handles rather than raw objects:

* a **dynamic** ref (``registry.get(T)``) re-resolves the best matching
  registration on every access, so providers can be swapped, restarted or
  reloaded transparently;
* a **bound** ref (from ``get_all``/``watch``) is tied to one registration and
  raises `ServiceUnavailable` once it is gone.

Both forward attribute access to the service, i.e. ``ref.method()`` works.
``ref.resolve()`` returns the service object itself.  The names ``resolve``,
``available``, ``provider``, ``properties``, ``registration``, ``contract`` and
``dynamic`` belong to the ref; reach a service attribute with one of these
names through ``ref.resolve().<name>``.
"""

from __future__ import annotations

import inspect
import itertools
import logging
from collections.abc import Awaitable, Callable, Iterator, Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Generic, TypeVar, cast, overload

from ._util import EXTERNAL_OWNER, contract_key, contract_name, create_task, mro_keys
from .errors import ServiceNotFound, ServiceUnavailable

__all__ = [
    "PropertyFilter",
    "ServiceRef",
    "ServiceRegistration",
    "ServiceRegistry",
    "ServiceWatch",
]

T = TypeVar("T")
log = logging.getLogger("corlet.registry")

PropertyFilter = Mapping[str, Any] | Callable[[Mapping[str, Any]], bool] | None
_ids = itertools.count(1)


def _matches(flt: PropertyFilter, props: Mapping[str, Any]) -> bool:
    if flt is None:
        return True
    if callable(flt):
        return bool(flt(props))
    return all(k in props and props[k] == v for k, v in flt.items())


def check_contract(contract: object, instance: object) -> bool:
    """Best-effort structural/nominal check that ``instance`` fits ``contract``."""
    if isinstance(contract, str) or not isinstance(contract, type):
        return True
    if getattr(contract, "_is_protocol", False):
        if getattr(contract, "_is_runtime_protocol", False):
            return isinstance(instance, contract)
        return True  # non runtime-checkable protocol: cannot verify
    if isinstance(instance, contract):
        return True
    # Tolerate class identity changes caused by hot reloads.
    return contract_key(contract) in mro_keys(instance)


class ServiceRegistration:
    """A live entry in the registry. Call `unregister()` to withdraw it."""

    __slots__ = (
        "_registry",
        "active",
        "contract",
        "id",
        "instance",
        "key",
        "owner",
        "properties",
        "ranking",
    )

    def __init__(
        self,
        registry: ServiceRegistry,
        contract: object,
        instance: object,
        owner: str,
        properties: Mapping[str, Any],
        ranking: int,
    ) -> None:
        self._registry = registry
        self.id = next(_ids)
        self.contract = contract
        self.key = contract_key(contract)
        self.instance = instance
        self.owner = owner
        self.properties: Mapping[str, Any] = MappingProxyType(dict(properties))
        self.ranking = ranking
        self.active = True

    def sort_key(self) -> tuple[int, int]:
        """Sort key: higher ranking first, then registration order."""
        return (-self.ranking, self.id)

    def unregister(self) -> None:
        """Withdraw the service (idempotent); watchers are notified."""
        self._registry.unregister(self)

    close = unregister

    def __repr__(self) -> str:
        state = "" if self.active else " (withdrawn)"
        return (
            f"<ServiceRegistration #{self.id} {contract_name(self.contract)} "
            f"by {self.owner} rank={self.ranking}{state}>"
        )


class ServiceRef(Generic[T]):
    """Handle to a service. See the module docstring for dynamic vs. bound refs."""

    __slots__ = ("_bound", "_contract", "_filter", "_key", "_registry")

    def __init__(
        self,
        registry: ServiceRegistry,
        contract: object,
        flt: PropertyFilter = None,
        bound: ServiceRegistration | None = None,
    ) -> None:
        self._registry = registry
        self._contract = contract
        self._key = contract_key(contract)
        self._filter = flt
        self._bound = bound

    @property
    def dynamic(self) -> bool:
        """``True`` if the ref follows the best matching registration."""
        return self._bound is None

    @property
    def contract(self) -> object:
        """The contract this ref was obtained for."""
        return self._contract

    @property
    def registration(self) -> ServiceRegistration:
        """The registration currently backing this ref.

        Raises:
            ServiceUnavailable: no matching registration exists (dynamic ref)
                or the bound registration was withdrawn.
        """
        if self._bound is not None:
            if not self._bound.active:
                raise ServiceUnavailable(
                    f"service {contract_name(self._contract)} provided by "
                    f"{self._bound.owner!r} has been withdrawn"
                )
            return self._bound
        regs = self._registry.lookup(self._key, self._filter)
        if not regs:
            raise ServiceUnavailable(
                f"no provider of {contract_name(self._contract)} is currently available"
            )
        return regs[0]

    @property
    def available(self) -> bool:
        """Whether a backing registration currently exists."""
        try:
            _ = self.registration
        except ServiceUnavailable:
            return False
        return True

    def resolve(self) -> T:
        """Return the service object (raises ServiceUnavailable if gone)."""
        return cast(T, self.registration.instance)

    @property
    def provider(self) -> str:
        """Id of the component providing the service right now."""
        return self.registration.owner

    @property
    def properties(self) -> Mapping[str, Any]:
        """Properties of the current registration."""
        return self.registration.properties

    def __getattr__(self, name: str) -> Any:
        if name.startswith("__") and name.endswith("__"):
            raise AttributeError(name)
        return getattr(self.resolve(), name)

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        return cast(Callable[..., Any], self.resolve())(*args, **kwargs)

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, ServiceRef):
            return NotImplemented
        return (self._key, self._bound, self._registry) == (
            other._key,
            other._bound,
            other._registry,
        )

    def __hash__(self) -> int:
        return hash((self._key, id(self._bound)))

    def __repr__(self) -> str:
        kind = "dynamic" if self._bound is None else f"bound #{self._bound.id}"
        return f"<ServiceRef {contract_name(self._contract)} {kind}>"


WatchCallback = Callable[[ServiceRef[Any]], Any]


class ServiceWatch:
    """Subscription returned by `ServiceRegistry.watch()`."""

    __slots__ = ("_registry", "active", "flt", "key", "on_add", "on_remove", "owner")

    def __init__(
        self,
        registry: ServiceRegistry,
        key: str,
        on_add: WatchCallback | None,
        on_remove: WatchCallback | None,
        flt: PropertyFilter,
        owner: str,
    ) -> None:
        self._registry = registry
        self.key = key
        self.on_add = on_add
        self.on_remove = on_remove
        self.flt = flt
        self.owner = owner
        self.active = True

    def cancel(self) -> None:
        """Stop watching (idempotent)."""
        if self.active:
            self.active = False
            watches = self._registry._watches.get(self.key, [])
            if self in watches:
                watches.remove(self)

    close = cancel


@dataclass(frozen=True)
class ServiceInfo:
    """Plain-data description of a registration (see `ServiceRegistry.info`)."""

    id: int
    contract: str
    owner: str
    ranking: int
    properties: Mapping[str, Any]


MissingHook = Callable[[str], Awaitable[bool]]


class ServiceRegistry:
    """Thread-unsafe (event loop bound) registry of services."""

    def __init__(self) -> None:
        self._by_key: dict[str, list[ServiceRegistration]] = {}
        self._watches: dict[str, list[ServiceWatch]] = {}
        self.missing_hook: MissingHook | None = None
        """Called by `acquire()` when nothing matches; the container uses it to
        activate lazy providers.  Returns True if it activated something."""
        self.describe_missing: Callable[[str], str | None] | None = None
        """Describes who *could* provide a contract, for error messages."""

    # -- registration ---------------------------------------------------
    def register(
        self,
        contract: type[T] | str,
        instance: T,
        *,
        owner: str = EXTERNAL_OWNER,
        properties: Mapping[str, Any] | None = None,
        ranking: int = 0,
    ) -> ServiceRegistration:
        """Register ``instance`` under ``contract``.

        Args:
            contract: Class, Protocol or string the service is looked up by.
            instance: The service object.
            owner: Owning component id (components use ``ctx.services``,
                which sets it).
            properties: Arbitrary metadata, usable as lookup filter.
            ranking: Higher wins in lookups; ties go to the earlier
                registration.

        Raises:
            TypeError: ``instance`` does not implement ``contract`` (checked
                for classes and runtime-checkable protocols).
        """
        if not check_contract(contract, instance):
            raise TypeError(
                f"{owner!r} registered {instance!r} which does not implement "
                f"{contract_name(contract)}"
            )
        reg = ServiceRegistration(self, contract, instance, owner, properties or {}, ranking)
        bucket = self._by_key.setdefault(reg.key, [])
        bucket.append(reg)
        bucket.sort(key=ServiceRegistration.sort_key)
        log.debug("registered %r", reg)
        for w in list(self._watches.get(reg.key, ())):
            if w.on_add is not None and _matches(w.flt, reg.properties):
                self._notify(w, w.on_add, ServiceRef(self, contract, None, reg))
        return reg

    def unregister(self, reg: ServiceRegistration) -> None:
        """Withdraw ``reg`` (idempotent); watchers are notified first."""
        if not reg.active:
            return
        bucket = self._by_key.get(reg.key, [])
        if reg in bucket:
            bucket.remove(reg)
            if not bucket:
                del self._by_key[reg.key]
        log.debug("unregistered %r", reg)
        # Notify while the registration is still marked active, so removal
        # callbacks may still talk to the departing service.
        for w in list(self._watches.get(reg.key, ())):
            if w.on_remove is not None and _matches(w.flt, reg.properties):
                self._notify(w, w.on_remove, ServiceRef(self, reg.contract, None, reg))
        reg.active = False

    def unregister_owner(self, owner: str) -> int:
        """Withdraw all services of ``owner``; returns how many."""
        regs = [r for r in self.registrations() if r.owner == owner]
        for r in regs:
            self.unregister(r)
        return len(regs)

    # -- lookup -----------------------------------------------------------
    def lookup(self, contract: object, flt: PropertyFilter = None) -> list[ServiceRegistration]:
        """Matching registrations, best first."""
        key = contract_key(contract)
        return [r for r in self._by_key.get(key, ()) if _matches(flt, r.properties)]

    def has(self, contract: object, flt: PropertyFilter = None) -> bool:
        """Whether a matching service is registered."""
        return bool(self.lookup(contract, flt))

    def ref(self, contract: type[T] | str, flt: PropertyFilter = None) -> ServiceRef[T]:
        """Dynamic reference, without checking current availability."""
        return ServiceRef(self, contract, flt)

    def get(self, contract: type[T] | str, flt: PropertyFilter = None) -> ServiceRef[T]:
        """Dynamic reference to the best matching service; raises if none."""
        if not self.lookup(contract, flt):
            raise ServiceNotFound(self._missing_message(contract))
        return ServiceRef(self, contract, flt)

    def find(self, contract: type[T] | str, flt: PropertyFilter = None) -> ServiceRef[T] | None:
        """Dynamic reference to the best matching service, or ``None``."""
        return ServiceRef(self, contract, flt) if self.lookup(contract, flt) else None

    def get_all(self, contract: type[T] | str, flt: PropertyFilter = None) -> list[ServiceRef[T]]:
        """Bound references to every matching registration, best first."""
        return [ServiceRef(self, contract, None, r) for r in self.lookup(contract, flt)]

    @overload
    def instance(self, contract: type[T], flt: PropertyFilter = None) -> T: ...
    @overload
    def instance(self, contract: str, flt: PropertyFilter = None) -> Any: ...
    def instance(self, contract: type[T] | str, flt: PropertyFilter = None) -> Any:
        """Shortcut for ``get(contract).resolve()``: the raw object right now."""
        return self.get(contract, flt).resolve()

    async def acquire(self, contract: type[T] | str, flt: PropertyFilter = None) -> ServiceRef[T]:
        """Like `get()`, but activates a lazy provider if necessary."""
        if not self.lookup(contract, flt) and self.missing_hook is not None:
            await self.missing_hook(contract_key(contract))
        return self.get(contract, flt)

    def _missing_message(self, contract: object) -> str:
        msg = f"no service registered for {contract_name(contract)}"
        if self.describe_missing is not None:
            hint = self.describe_missing(contract_key(contract))
            if hint:
                msg += f"; {hint}"
        return msg

    # -- watching ---------------------------------------------------------
    def watch(
        self,
        contract: type[T] | str,
        on_add: Callable[[ServiceRef[T]], Any] | None = None,
        on_remove: Callable[[ServiceRef[T]], Any] | None = None,
        *,
        flt: PropertyFilter = None,
        replay: bool = True,
        owner: str = EXTERNAL_OWNER,
    ) -> ServiceWatch:
        """Track services appearing/disappearing.

        With ``replay=True`` ``on_add`` is invoked immediately for every
        matching service that already exists.  Async callbacks are scheduled
        as tasks; sync callbacks run inline.
        """
        key = contract_key(contract)
        w = ServiceWatch(self, key, on_add, on_remove, flt, owner)  # type: ignore[arg-type]
        self._watches.setdefault(key, []).append(w)
        if replay and on_add is not None:
            for reg in self.lookup(key, flt):
                self._notify(w, w.on_add, ServiceRef(self, contract, None, reg))  # type: ignore[arg-type]
        return w

    def _notify(self, w: ServiceWatch, cb: WatchCallback, ref: ServiceRef[Any]) -> None:
        try:
            result = cb(ref)
        except Exception:
            log.exception("service watch callback of %r failed", w.owner)
            return
        if inspect.isawaitable(result):
            try:
                create_task(_await_logged(result, w.owner), component_id=w.owner)
            except RuntimeError:  # no running event loop
                if inspect.iscoroutine(result):
                    result.close()
                log.error("async watch callback of %r needs a running event loop", w.owner)

    # -- introspection --------------------------------------------------
    def registrations(self) -> Iterator[ServiceRegistration]:
        """Iterate over all live registrations."""
        for bucket in list(self._by_key.values()):
            yield from list(bucket)

    def info(self) -> list[ServiceInfo]:
        """Plain-data snapshot of all registrations, in registration order."""
        return [
            ServiceInfo(r.id, r.key, r.owner, r.ranking, dict(r.properties))
            for r in sorted(self.registrations(), key=lambda r: r.id)
        ]

    def __len__(self) -> int:
        return sum(len(b) for b in self._by_key.values())


async def _await_logged(aw: Awaitable[Any], owner: str) -> None:
    try:
        await aw
    except Exception:
        log.exception("async service watch callback of %r failed", owner)
