"""The container: assembles components and drives their lifecycle.

The container is a *reconciler*.  Every lifecycle operation (boot, enable,
disable, restart, crash recovery, lazy activation, hot replacement) only
changes intent -- which components are enabled, demanded or failed -- and
then converges the running system towards it:

1. re-resolve the dependency graph for the currently available components;
2. compute the *run set*: wanted eager (or demanded lazy) components plus
   their required dependencies;
3. stop running components that left the run set, together with everything
   that (transitively) required them, in reverse start order;
4. start whatever in the run set is not running, layer by layer, components
   of a layer concurrently.

Lifecycle operations are serialized by a lock.  Assembly is static: the set
of installed components is fixed once the container starts, but components
can be enabled and disabled at runtime.
"""

from __future__ import annotations

import asyncio
import contextlib
import functools
import logging
import signal
import threading
import time
from collections import deque
from collections.abc import AsyncIterator, Awaitable, Callable, Iterable, Mapping
from dataclasses import dataclass, field
from types import ModuleType, TracebackType
from typing import Any, Self, TypeVar

from ._util import (
    EXTERNAL_OWNER,
    call_maybe_async,
    create_task,
    current_component,
    in_lifecycle,
)
from ._util import (
    activation_chain as _activation_chain,
)
from .component import InjectionPlan, effective_manifest, is_component, manifest_of
from .config import (
    ConfigSource,
    DictConfigSource,
    config_section,
    validate_config,
)
from .context import ComponentContext
from .discovery import ComponentSource, EntryPointSource, ExplicitSource, ModuleSource
from .errors import (
    ComponentFailed,
    ConfigError,
    ContainerStateError,
    Diagnostic,
    DuplicateComponentError,
    InvalidManifestError,
    LifecycleError,
    ResolutionError,
)
from .events import EventBus
from .extensions import ExtensionRegistry
from .introspect import (
    ComponentInfo,
    ContainerSnapshot,
    DependencyInfo,
    ExtensionInfo,
    PointInfo,
    ServiceEntry,
)
from .lifecycle import (
    ComponentState,
    ComponentStateChanged,
    ConfigChanged,
    ContainerReady,
    ContainerState,
    ContainerStopped,
    ContainerStopping,
)
from .manifest import Activation, Manifest
from .registry import PropertyFilter, ServiceRef, ServiceRegistration, ServiceRegistry
from .resolver import Resolution, Resolver

__all__ = ["ChangeSet", "Container"]

log = logging.getLogger("corlet.container")
T = TypeVar("T")

CONTAINER_KEY = "container"


@dataclass(frozen=True)
class ChangeSet:
    """Which components a lifecycle operation stopped and started."""

    stopped: tuple[str, ...] = ()
    started: tuple[str, ...] = ()
    failed: tuple[str, ...] = ()

    def __bool__(self) -> bool:
        return bool(self.stopped or self.started or self.failed)


@dataclass(eq=False)
class _Record:
    id: str
    cls: type
    manifest: Manifest
    plan: InjectionPlan
    enabled: bool = True
    state: ComponentState = ComponentState.INSTALLED
    reason: str | None = None
    error: BaseException | None = None
    instance: Any = None
    ctx: ComponentContext | None = None
    last_ctx: ComponentContext | None = None
    """context of the previous activation (for leak diagnostics)"""
    demanded: bool = False
    start_seq: int = 0
    requires_ids: frozenset[str] = frozenset()
    config_raw: dict[str, Any] | None = None
    starting: asyncio.Future[bool] | None = None
    restarts: deque[float] = field(default_factory=deque)
    total_restarts: int = 0
    gave_up: bool = False

    @property
    def lazy(self) -> bool:
        return self.manifest.activation is Activation.LAZY


class Container:
    """Holds components, the service registry, extension registry and event bus.

    Args:
        *items: Component classes, modules, dotted module names (``"pkg.mod"``
            or ``"pkg.mod:Name"``) or `ComponentSource` objects.  More can be
            added with `add()` before the container starts.
        name: Name of the container (used in logs and events).
        config: A mapping or `ConfigSource`.  Component ``acme.db`` reads
            ``components.acme.db``; the ``container`` section may contain
            ``disabled: [ids]`` and ``enabled: [ids]`` (an allow-list).
        disabled: Ids of components that start disabled.
        enabled: Allow-list: if given, only these components are enabled.
        strict: Enforce manifests at runtime (see `ComponentContext`).
        fail_fast: Abort `start()` on resolution errors, or if a component
            without a restart policy fails to start.  Otherwise the container
            starts degraded.
        entry_points: Also discover installed components from the
            ``corlet.components`` entry point group.
        shutdown_timeout: Seconds to wait for in-flight event deliveries
            during shutdown.

    Attributes:
        services: The `ServiceRegistry`.
        extensions: The `ExtensionRegistry`.
        events: The `EventBus`.
        state: The `ContainerState`.
        exit_code: Exit code returned by `serve()` / `run()`.
    """

    def __init__(
        self,
        *items: Any,
        name: str = "corlet",
        config: Mapping[str, Any] | ConfigSource | None = None,
        disabled: Iterable[str] = (),
        enabled: Iterable[str] | None = None,
        strict: bool = False,
        fail_fast: bool = True,
        entry_points: bool = False,
        shutdown_timeout: float | None = 30.0,
    ) -> None:
        self.name = name
        self.strict = strict
        self.fail_fast = fail_fast
        self.shutdown_timeout = shutdown_timeout
        self.services = ServiceRegistry()
        self.extensions = ExtensionRegistry()
        self.events = EventBus()
        self.resolver = Resolver()
        self.state = ContainerState.CREATED
        self.exit_code = 0

        if config is None:
            self._config_source: ConfigSource = DictConfigSource()
        elif isinstance(config, Mapping):
            self._config_source = DictConfigSource(dict(config))
        else:
            self._config_source = config
        self._config_tree: Mapping[str, Any] = {}
        self._ctor_disabled = set(disabled)
        self._ctor_enabled = set(enabled) if enabled is not None else None
        self._config_disabled: set[str] = set()
        self._config_enabled: set[str] | None = None

        self._sources: list[ComponentSource] = []
        self._records: dict[str, _Record] = {}
        self._assembled = False
        self._resolution = Resolution()
        self._lock = asyncio.Lock()
        self._seq = 0
        self._stop_requested: asyncio.Event | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._loop_thread: int | None = None
        self._background: set[asyncio.Task[Any]] = set()
        self._diagnostics: list[Diagnostic] = []

        self.services.missing_hook = self._on_missing_service
        self.services.describe_missing = self._describe_missing
        if entry_points:
            self._sources.append(EntryPointSource())
        self.add(*items)

    # ------------------------------------------------------------------
    # assembly
    # ------------------------------------------------------------------
    def add(self, *items: Any) -> Self:
        """Add components or component sources (only before `start()`)."""
        if self.state is not ContainerState.CREATED:
            raise ContainerStateError("components can only be added before the container starts")
        explicit: list[Any] = []
        for item in items:
            if isinstance(item, list | tuple | set | frozenset):
                self.add(*item)
            elif isinstance(item, str):
                self._sources.append(ModuleSource(item))
            elif is_component(item) or isinstance(item, ModuleType):
                explicit.append(item)
            elif isinstance(item, ComponentSource):
                self._sources.append(item)
            else:
                raise TypeError(f"cannot add {item!r}: not a component, module or source")
        if explicit:
            self._sources.append(ExplicitSource(*explicit))
        self._assembled = False
        return self

    def register_service(
        self,
        contract: type[T] | str,
        instance: T,
        *,
        properties: Mapping[str, Any] | None = None,
        ranking: int = 0,
    ) -> ServiceRegistration:
        """Register an application-provided service (owned by the container).

        External services satisfy ``requires_services`` during resolution.
        Register them before `start()`, or call `reconcile()` after.
        """
        return self.services.register(
            contract, instance, owner=EXTERNAL_OWNER, properties=properties, ranking=ranking
        )

    def assemble(self) -> Resolution:
        """Discover components, define extension points and resolve the graph.

        Called by `start()`; call it directly to inspect or check an
        application without starting it.  Idempotent.
        """
        if self._assembled:
            return self._resolution
        self._config_tree = self._load_config_tree()
        found: dict[str, type] = {}
        for source in self._sources:
            for cls in source.discover():
                cid = manifest_of(cls).id
                prev = found.get(cid)
                if prev is not None and prev is not cls:
                    raise DuplicateComponentError(
                        f"component id {cid!r} is declared by both "
                        f"{prev.__module__}.{prev.__qualname__} and "
                        f"{cls.__module__}.{cls.__qualname__}"
                    )
                found[cid] = cls
        for cid, cls in found.items():
            if cid in self._records and self._records[cid].cls is cls:
                continue
            manifest, plan = effective_manifest(cls)
            self._records[cid] = _Record(cid, cls, manifest, plan)
        for rec in self._records.values():
            for point in rec.manifest.defines:
                try:
                    self.extensions.define(point, owner=rec.id)
                except ValueError as exc:
                    raise InvalidManifestError(str(exc)) from None
        self._apply_enablement()
        self._resolution = self._resolve()
        self._refresh_idle_states(self._resolution)
        self._assembled = True
        return self._resolution

    def _load_config_tree(self) -> Mapping[str, Any]:
        tree = self._config_source.load()
        section = tree.get(CONTAINER_KEY) or {}
        if not isinstance(section, Mapping):
            raise ConfigError(None, f"'{CONTAINER_KEY}' section must be a mapping")
        self._config_disabled = set(section.get("disabled") or ())
        allow = section.get("enabled")
        self._config_enabled = set(allow) if allow is not None else None
        return tree

    def _configured_enabled(self) -> dict[str, bool]:
        """Enablement derived from constructor arguments + configuration."""
        disabled = self._ctor_disabled | self._config_disabled
        allow = self._ctor_enabled
        if self._config_enabled is not None:
            allow = self._config_enabled if allow is None else allow & self._config_enabled
        return {
            cid: cid not in disabled and (allow is None or cid in allow) for cid in self._records
        }

    def _apply_enablement(self, previous: Mapping[str, bool] | None = None) -> None:
        """Apply configured enablement.

        With ``previous`` only ids whose *configured* value changed are touched,
        so runtime ``enable()``/``disable()`` calls survive a config reload.
        """
        mentioned = self._ctor_disabled | self._config_disabled
        mentioned |= self._ctor_enabled or set()
        mentioned |= self._config_enabled or set()
        for cid in sorted(mentioned - set(self._records)):
            log.warning("enable/disable list mentions unknown component %r", cid)
        for cid, value in self._configured_enabled().items():
            if previous is None or previous.get(cid) != value:
                self._records[cid].enabled = value

    # ------------------------------------------------------------------
    # queries
    # ------------------------------------------------------------------
    @property
    def component_ids(self) -> list[str]:
        """Ids of all installed components, sorted."""
        return sorted(self._records)

    def __contains__(self, cid: object) -> bool:
        return cid in self._records

    def manifest(self, cid: str) -> Manifest:
        """The effective manifest of ``cid`` (declared + implied by injection)."""
        return self._record(cid).manifest

    def component_class(self, cid: str) -> type:
        """The class currently implementing ``cid``."""
        return self._record(cid).cls

    def state_of(self, cid: str) -> ComponentState:
        """Current `ComponentState` of ``cid``."""
        return self._record(cid).state

    def instance_of(self, cid: str) -> Any:
        """The live component instance (``None`` if not running)."""
        return self._record(cid).instance

    def context_of(self, cid: str) -> ComponentContext | None:
        """Context of the running activation of ``cid`` (``None`` if not running)."""
        return self._record(cid).ctx

    def error_of(self, cid: str) -> BaseException | None:
        """The exception that made ``cid`` fail, if any."""
        return self._record(cid).error

    def is_enabled(self, cid: str) -> bool:
        """Whether ``cid`` is enabled (by configuration or `enable`/`disable`)."""
        return self._record(cid).enabled

    @property
    def resolution(self) -> Resolution:
        """The most recent `Resolution` of the component graph."""
        return self._resolution

    @property
    def config_tree(self) -> Mapping[str, Any]:
        """The configuration tree as last loaded from the config source."""
        return self._config_tree

    def raw_config(self, cid: str) -> dict[str, Any]:
        """The unvalidated configuration section of ``cid``."""
        if not self._assembled:
            self.assemble()
        return config_section(self._config_tree, cid, self._records)

    def config_of(self, cid: str) -> Any:
        """The validated configuration ``cid`` would receive right now."""
        rec = self._record(cid)
        if rec.ctx is not None:
            return rec.ctx.config
        return validate_config(rec.manifest.config_schema, self.raw_config(cid), cid)

    def get(self, contract: type[T] | str, flt: PropertyFilter = None) -> ServiceRef[T]:
        """Shortcut for ``container.services.get(contract, flt)``."""
        return self.services.get(contract, flt)

    async def acquire(self, contract: type[T] | str, flt: PropertyFilter = None) -> ServiceRef[T]:
        """Shortcut for ``container.services.acquire(...)``; activates lazy providers."""
        return await self.services.acquire(contract, flt)

    def _record(self, cid: str) -> _Record:
        if not self._assembled and cid not in self._records:
            self.assemble()
        try:
            return self._records[cid]
        except KeyError:
            raise KeyError(f"unknown component {cid!r}") from None

    # ------------------------------------------------------------------
    # start / stop
    # ------------------------------------------------------------------
    async def start(self) -> Self:
        """Assemble the container and start all eager components."""
        if self.state is not ContainerState.CREATED:
            raise ContainerStateError(f"container is {self.state.value}; cannot start")
        self._bind_loop()
        async with self._op():
            self.state = ContainerState.STARTING
            try:
                res = self.assemble()
            except BaseException:
                self.state = ContainerState.FAILED
                raise
            for d in res.diagnostics:
                (log.error if d.severity == "error" else log.warning)("%s", d)
            if res.errors and self.fail_fast:
                self.state = ContainerState.FAILED
                raise ResolutionError(res.diagnostics)
            changes = await self._reconcile()
            fatal = [
                cid for cid in changes.failed if self._records[cid].manifest.restart.mode == "never"
            ]
            if fatal and self.fail_fast:
                first = self._records[fatal[0]]
                await self._shutdown_locked()
                self.state = ContainerState.FAILED
                raise ComponentFailed(first.id, first.error)
            self.state = ContainerState.RUNNING
        active = tuple(r.id for r in self._records.values() if r.state is ComponentState.ACTIVE)
        failed = tuple(r.id for r in self._records.values() if r.state is ComponentState.FAILED)
        log.info("container %r started: %d active, %d failed", self.name, len(active), len(failed))
        self.events.publish(ContainerReady(self.name, active, failed))
        return self

    async def stop(self) -> None:
        """Stop every running component in reverse start order. Idempotent."""
        if self.state in (ContainerState.STOPPED, ContainerState.CREATED, ContainerState.FAILED):
            if self.state is not ContainerState.FAILED:
                self.state = ContainerState.STOPPED
            if self._stop_requested is not None:
                self._stop_requested.set()
            return
        if self.state is ContainerState.STOPPING:
            # A concurrent stop() is in progress: wait for it.
            async with self._op():
                return
        async with self._op():
            if self.state is not ContainerState.STOPPED:  # type: ignore[comparison-overlap]
                await self._shutdown_locked()

    async def _shutdown_locked(self) -> None:
        self.state = ContainerState.STOPPING
        log.info("stopping container %r", self.name)
        with contextlib.suppress(Exception):
            await self.events.publish_and_wait(ContainerStopping(self.name), timeout=5)
        current = asyncio.current_task()
        for t in list(self._background):
            if t is not current:
                t.cancel()
        running = [r.id for r in self._records.values() if r.state.running]
        await self._stop_many(running, final=ComponentState.STOPPED)
        await self.events.drain(timeout=self.shutdown_timeout)
        with contextlib.suppress(Exception):
            await self.events.publish_and_wait(ContainerStopped(self.name), timeout=5)
        self.events.cancel_pending()
        self.state = ContainerState.STOPPED
        if self._stop_requested is not None:
            self._stop_requested.set()

    async def __aenter__(self) -> Self:
        return await self.start()

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.stop()

    def request_stop(self, exit_code: int = 0) -> None:
        """Ask `serve()` / `run()` to shut down. Thread-safe."""
        self.exit_code = exit_code or self.exit_code

        def _set() -> None:
            if self._stop_requested is None:
                self._stop_requested = asyncio.Event()
            self._stop_requested.set()

        loop = self._loop
        if loop is not None and threading.get_ident() != self._loop_thread:
            if not loop.is_closed():
                loop.call_soon_threadsafe(_set)
        else:
            _set()

    async def wait_stopped(self) -> None:
        """Wait until `request_stop()` is called (or the container stopped)."""
        if self._stop_requested is None:
            self._stop_requested = asyncio.Event()
        if self.state is ContainerState.STOPPED:
            return
        await self._stop_requested.wait()

    async def serve(
        self,
        main: Callable[[Container], Awaitable[Any]] | None = None,
        *,
        install_signal_handlers: bool = True,
    ) -> int:
        """Start, run until stop is requested (or ``main`` returns), then stop."""
        self._bind_loop()
        loop = asyncio.get_running_loop()
        installed: list[signal.Signals] = []
        if install_signal_handlers and threading.current_thread() is threading.main_thread():
            for sig in (signal.SIGINT, signal.SIGTERM):
                try:
                    loop.add_signal_handler(sig, self.request_stop, 128 + sig)
                    installed.append(sig)
                except (NotImplementedError, RuntimeError, ValueError):
                    pass
        try:
            await self.start()
            if main is None:
                await self.wait_stopped()
            else:
                main_task = asyncio.ensure_future(main(self))
                stop_task = asyncio.ensure_future(self.wait_stopped())
                done, _ = await asyncio.wait(
                    {main_task, stop_task}, return_when=asyncio.FIRST_COMPLETED
                )
                if main_task in done:
                    stop_task.cancel()
                    main_task.result()
                else:
                    main_task.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await main_task
        finally:
            await self.stop()
            for sig in installed:
                loop.remove_signal_handler(sig)
        return self.exit_code

    def run(
        self,
        main: Callable[[Container], Awaitable[Any]] | None = None,
        *,
        install_signal_handlers: bool = True,
    ) -> int:
        """Synchronous entry point: ``raise SystemExit(container.run())``."""
        return asyncio.run(self.serve(main, install_signal_handlers=install_signal_handlers))

    def _bind_loop(self) -> None:
        loop = asyncio.get_running_loop()
        if self._loop is not loop:
            self._loop = loop
            self._loop_thread = threading.get_ident()
            self._lock = asyncio.Lock()
            self.events.bind_loop(loop)
            previous = self._stop_requested
            self._stop_requested = asyncio.Event()
            if previous is not None and previous.is_set():
                self._stop_requested.set()

    # ------------------------------------------------------------------
    # runtime control
    # ------------------------------------------------------------------
    async def enable(self, cid: str) -> ChangeSet:
        """Enable a component; it (and blocked dependents) start if possible."""
        async with self._op(mutating=True):
            rec = self._record(cid)
            rec.enabled = True
            rec.gave_up = False
            if rec.state is ComponentState.FAILED:
                self._clear_failure(rec)
            return await self._reconcile_if_running()

    async def disable(self, cid: str) -> ChangeSet:
        """Disable a component; everything that requires it stops too."""
        async with self._op(mutating=True):
            rec = self._record(cid)
            rec.enabled = False
            if rec.state is ComponentState.FAILED:
                self._clear_failure(rec)
            return await self._reconcile_if_running()

    async def restart(self, cid: str) -> ChangeSet:
        """Restart a component (and its running dependents); clears FAILED."""
        async with self._op(mutating=True):
            rec = self._record(cid)
            if not rec.enabled:
                raise LifecycleError(f"component {cid!r} is disabled; enable it first")
            self._require_running()
            stopped = await self._stop_many(self._with_dependents({cid}))
            self._clear_failure(rec)
            rec.gave_up = False
            if rec.lazy:
                rec.demanded = True
            changes = await self._reconcile()
            return ChangeSet(
                tuple(dict.fromkeys(stopped + list(changes.stopped))),
                changes.started,
                changes.failed,
            )

    async def reconcile(self) -> ChangeSet:
        """Re-resolve and converge (e.g. after registering external services)."""
        async with self._op(mutating=True):
            return await self._reconcile_if_running()

    async def replace_component(self, cls: type) -> ChangeSet:
        """Swap the implementation of an installed component (hot reload).

        The new class must declare the same id.  The component and its running
        dependents are restarted with the new implementation.
        """
        if not is_component(cls):
            raise InvalidManifestError(f"{cls!r} is not a component")
        manifest, plan = effective_manifest(cls)
        async with self._op(mutating=True):
            rec = self._record(manifest.id)
            was_running = rec.state.running
            stopped = await self._stop_many(self._with_dependents({rec.id}))
            rec.cls, rec.manifest, rec.plan = cls, manifest, plan
            for point in manifest.defines:
                self.extensions.define(point, owner=rec.id)
            self._clear_failure(rec)
            if was_running and rec.lazy:
                rec.demanded = True
            changes = await self._reconcile_if_running()
            return ChangeSet(tuple(stopped), changes.started, changes.failed)

    async def reload_config(self) -> dict[str, str]:
        """Reload configuration and apply it.

        Running components whose section changed get ``reconfigure(ctx,
        config)``; if that returns ``False`` (the default) they are restarted.
        Changes to the ``container`` section's enable/disable lists are applied.
        Returns ``{component_id: outcome}``.
        """
        async with self._op(mutating=True):
            previous = self._configured_enabled()
            self._config_tree = self._load_config_tree()
            outcomes: dict[str, str] = {}
            for rec in sorted(self._records.values(), key=lambda r: r.start_seq):
                raw = self.raw_config(rec.id)
                if rec.config_raw is None or raw == rec.config_raw:
                    continue
                try:
                    new = validate_config(rec.manifest.config_schema, raw, rec.id)
                except ConfigError as exc:
                    log.error("%s; keeping the previous configuration", exc)
                    outcomes[rec.id] = "invalid"
                    continue
                if rec.state is not ComponentState.ACTIVE or rec.ctx is None:
                    outcomes[rec.id] = "deferred"
                    rec.config_raw = raw
                    continue
                applied = False
                reconf = getattr(rec.instance, "reconfigure", None)
                if callable(reconf):
                    try:
                        async with rec.ctx._as_current():
                            applied = bool(await call_maybe_async(reconf, rec.ctx, new))
                    except Exception:
                        log.exception("reconfigure() of %r failed; restarting it", rec.id)
                if applied:
                    rec.ctx.config = new
                    rec.config_raw = raw
                    outcomes[rec.id] = "reconfigured"
                else:
                    await self._stop_many(self._with_dependents({rec.id}))
                    outcomes[rec.id] = "restarted"
            before = {r.id: r.enabled for r in self._records.values()}
            self._apply_enablement(previous)
            changed_enablement = any(before[r.id] != r.enabled for r in self._records.values())
            if self.state is ContainerState.RUNNING and (
                changed_enablement or "restarted" in outcomes.values()
            ):
                await self._reconcile()
            for cid, outcome in outcomes.items():
                if outcome != "invalid":
                    self.events.publish(ConfigChanged(cid, outcome))
            return outcomes

    def check(self) -> list[Diagnostic]:
        """Assemble and validate everything without starting anything."""
        res = self.assemble()
        diags = list(res.diagnostics)
        for cid in sorted(res.wanted):
            rec = self._records[cid]
            try:
                validate_config(rec.manifest.config_schema, self.raw_config(cid), cid)
            except ConfigError as exc:
                diags.append(Diagnostic("error", "invalid-config", str(exc), cid))
        for cid, reasons in sorted(res.unsatisfied.items()):
            if not any(d.component == cid and d.severity == "error" for d in diags):
                diags.append(Diagnostic("warning", "unsatisfied", "; ".join(reasons), cid))
        return diags

    # ------------------------------------------------------------------
    # reconciliation
    # ------------------------------------------------------------------
    @contextlib.asynccontextmanager
    async def _op(self, *, mutating: bool = False) -> AsyncIterator[None]:
        holder = in_lifecycle.get()
        if holder is self:
            if mutating:
                raise LifecycleError(
                    "cannot enable/disable/restart components from inside a lifecycle "
                    "operation (e.g. a start() method); use ctx.spawn() to defer it"
                )
            yield
            return
        self._bind_loop()
        async with self._lock:
            token = in_lifecycle.set(self)
            try:
                yield
            finally:
                in_lifecycle.reset(token)

    def _require_running(self) -> None:
        if self.state is not ContainerState.RUNNING:
            raise ContainerStateError(f"container is {self.state.value}")

    async def _reconcile_if_running(self) -> ChangeSet:
        if self.state is ContainerState.RUNNING:
            return await self._reconcile()
        if self._assembled:
            self._resolution = self._resolve()
            self._refresh_idle_states(self._resolution)
        return ChangeSet()

    def _resolve(self) -> Resolution:
        manifests = {cid: r.manifest for cid, r in self._records.items()}
        reasons: dict[str, str] = {}
        available: set[str] = set()
        for r in self._records.values():
            if not r.enabled:
                reasons[r.id] = "disabled"
            elif r.state is ComponentState.FAILED:
                reasons[r.id] = "failed"
            else:
                available.add(r.id)
        external = {reg.key for reg in self.services.registrations() if reg.owner == EXTERNAL_OWNER}
        return self.resolver.resolve(manifests, available, external, unavailable_reasons=reasons)

    def _run_set(self, res: Resolution) -> set[str]:
        roots = {
            cid for cid in res.wanted if not self._records[cid].lazy or self._records[cid].demanded
        }
        return res.closure(roots, required_only=True)

    def _with_dependents(self, ids: set[str]) -> list[str]:
        """``ids`` plus running components that (transitively) required them."""
        result = set(ids)
        changed = True
        while changed:
            changed = False
            for r in self._records.values():
                if r.id not in result and r.state.running and r.requires_ids & result:
                    result.add(r.id)
                    changed = True
        return [cid for cid in result if self._records[cid].state.running]

    async def _reconcile(self) -> ChangeSet:
        res = self._resolve()
        self._resolution = res
        run = self._run_set(res)
        leaving = {r.id for r in self._records.values() if r.state.running and r.id not in run}
        stopped = await self._stop_many(self._with_dependents(leaving))
        started, failed = await self._activate(run, res)
        # Failures change availability: resolve again so that dependents of
        # failed components are reported as unsatisfied with a reason.
        self._resolution = self._resolve()
        self._refresh_idle_states(self._resolution)
        return ChangeSet(tuple(stopped), tuple(started), tuple(failed))

    async def _activate(self, ids: set[str], res: Resolution) -> tuple[list[str], list[str]]:
        started: list[str] = []
        failed: list[str] = []
        for layer in res.layers:
            todo = [
                self._records[c]
                for c in layer
                if c in ids and self._records[c].state is not ComponentState.ACTIVE
            ]
            if not todo:
                continue
            results = await asyncio.gather(*(self._ensure_started(r, res) for r in todo))
            for rec, ok in zip(todo, results, strict=True):
                if ok is True:
                    started.append(rec.id)
                elif rec.state is ComponentState.FAILED:
                    failed.append(rec.id)
        return started, failed

    def _unmet_runtime(self, rec: _Record, res: Resolution) -> str | None:
        """Required dependencies that are not ACTIVE right now (after failures)."""
        by_via: dict[str, list[str]] = {}
        for e in res.edges.get(rec.id, ()):
            if not e.optional:
                by_via.setdefault(e.via if e.via != "component" else f"c:{e.target}", []).append(
                    e.target
                )
        external = {reg.key for reg in self.services.registrations() if reg.owner == EXTERNAL_OWNER}
        for via, targets in by_via.items():
            if not via.startswith("c:") and via in external:
                continue
            if not any(self._records[t].state is ComponentState.ACTIVE for t in targets):
                bad = targets[0]
                return f"required component {bad!r} is {self._records[bad].state.value}"
        return None

    async def _ensure_started(self, rec: _Record, res: Resolution) -> bool:
        if rec.state is ComponentState.ACTIVE:
            return True
        if rec.id in _activation_chain.get():
            raise LifecycleError(
                f"circular activation: {' -> '.join((*_activation_chain.get(), rec.id))}"
            )
        if rec.state is ComponentState.STARTING and rec.starting is not None:
            return await asyncio.shield(rec.starting)
        if rec.state is ComponentState.FAILED or not rec.enabled:
            return False
        problem = self._unmet_runtime(rec, res)
        if problem is not None:
            self._set_state(rec, ComponentState.UNSATISFIED, problem)
            return False
        return await self._start_one(rec, res)

    async def _start_one(self, rec: _Record, res: Resolution) -> bool:
        loop = asyncio.get_running_loop()
        rec.starting = loop.create_future()
        self._set_state(rec, ComponentState.STARTING)
        m = rec.manifest
        ctx: ComponentContext | None = None
        ok = False
        chain_token = _activation_chain.set((*_activation_chain.get(), rec.id))
        comp_token = current_component.set(rec.id)
        try:
            raw = self.raw_config(rec.id)
            config = validate_config(m.config_schema, raw, rec.id)
            ctx = ComponentContext(self, m, config)
            instance = rec.cls()
            kwargs = await self._inject(rec, ctx)
            call = functools.partial(instance.start, ctx, **kwargs)
            try:
                await asyncio.wait_for(call_maybe_async(call, blocking=m.blocking), m.start_timeout)
            except TimeoutError:
                raise TimeoutError(f"start() did not finish within {m.start_timeout}s") from None
            for spec in m.extensions:
                ext = self.extensions.contribute_spec(spec, owner=rec.id)
                ctx._ledger.track(ext)
            missing = [
                k
                for k in m.provides_keys
                if not any(reg.owner == rec.id for reg in self.services.lookup(k))
            ]
            if missing:
                log.warning(
                    "component %r declares provides=%s but did not register them during start()",
                    rec.id,
                    missing,
                )
            rec.instance, rec.ctx = instance, ctx
            rec.config_raw = raw
            rec.requires_ids = frozenset(
                e.target for e in res.edges.get(rec.id, ()) if not e.optional
            )
            self._seq += 1
            rec.start_seq = self._seq
            rec.error = None
            self._set_state(rec, ComponentState.ACTIVE)
            ok = True
        except asyncio.CancelledError:
            if ctx is not None:
                await ctx._close(m.stop_timeout)
            rec.instance = rec.ctx = None
            self._set_state(rec, ComponentState.FAILED, "start cancelled")
            raise
        except Exception as exc:
            log.error("component %r failed to start: %s", rec.id, exc, exc_info=exc)
            if ctx is not None:
                await ctx._close(m.stop_timeout)
            rec.instance = rec.ctx = None
            rec.error = exc
            self._set_state(rec, ComponentState.FAILED, f"{type(exc).__name__}: {exc}", exc)
            self._supervise(rec)
        finally:
            current_component.reset(comp_token)
            _activation_chain.reset(chain_token)
            if rec.starting is not None and not rec.starting.done():
                rec.starting.set_result(ok)
            rec.starting = None
        return ok

    async def _inject(self, rec: _Record, ctx: ComponentContext) -> dict[str, Any]:
        """Build keyword arguments for ``start()`` from the injection plan.

        * config            -> the validated configuration
        * ``ServiceRef[T]`` -> a *dynamic* ref (also when optional: it may
          become available later; check ``ref.available``)
        * ``T``             -> the service object; ``None`` if optional and absent
        """
        kwargs: dict[str, Any] = {}
        for inj in rec.plan.injections:
            if inj.kind == "config":
                kwargs[inj.param] = ctx.config
                continue
            if inj.optional:
                ref = self.services.find(inj.contract) or await self._try_acquire(inj.contract)
            else:
                ref = await self.services.acquire(inj.contract)
            if inj.kind == "ref":
                kwargs[inj.param] = self.services.ref(inj.contract)
            else:
                kwargs[inj.param] = ref.resolve() if ref is not None else None
        return kwargs

    async def _try_acquire(self, contract: Any) -> ServiceRef[Any] | None:
        try:
            return await self.services.acquire(contract)
        except LookupError:
            return None

    async def _stop_many(
        self, ids: Iterable[str], *, final: ComponentState | None = None
    ) -> list[str]:
        recs = sorted(
            (self._records[c] for c in set(ids) if self._records[c].state.running),
            key=lambda r: r.start_seq,
            reverse=True,
        )
        for rec in recs:
            await self._stop_one(rec, final or ComponentState.STOPPED)
        return [r.id for r in recs]

    async def _stop_one(self, rec: _Record, final: ComponentState) -> None:
        if rec.state is ComponentState.STARTING and rec.starting is not None:
            await asyncio.shield(rec.starting)
        if rec.state is not ComponentState.ACTIVE:
            return
        self._set_state(rec, ComponentState.STOPPING)
        ctx, instance = rec.ctx, rec.instance
        assert ctx is not None
        m = rec.manifest
        token = current_component.set(rec.id)
        try:
            stop = getattr(instance, "stop", None)
            if callable(stop):
                try:
                    await asyncio.wait_for(
                        call_maybe_async(functools.partial(stop, ctx), blocking=m.blocking),
                        m.stop_timeout,
                    )
                except TimeoutError:
                    log.error("component %r: stop() timed out after %ss", rec.id, m.stop_timeout)
                except Exception:
                    log.exception("component %r: stop() failed", rec.id)
            await ctx._close(m.stop_timeout)
        finally:
            current_component.reset(token)
            rec.last_ctx = ctx
            rec.instance = rec.ctx = None
            self._set_state(rec, final)

    def _refresh_idle_states(self, res: Resolution) -> None:
        stopping = self.state in (ContainerState.STOPPING, ContainerState.STOPPED)
        for rec in self._records.values():
            if rec.state.running or rec.state is ComponentState.FAILED:
                continue
            if not rec.enabled:
                new, reason = ComponentState.DISABLED, None
            elif rec.id in res.unsatisfied:
                new, reason = ComponentState.UNSATISFIED, "; ".join(res.unsatisfied[rec.id])
            elif stopping and rec.state is ComponentState.STOPPED:
                continue
            else:
                new, reason = ComponentState.RESOLVED, None
            if rec.state is not new or rec.reason != reason:
                self._set_state(rec, new, reason)

    def _set_state(
        self,
        rec: _Record,
        new: ComponentState,
        reason: str | None = None,
        error: BaseException | None = None,
    ) -> None:
        old = rec.state
        rec.state = new
        rec.reason = reason
        if old is new and reason is None:
            return
        log.debug("component %s: %s -> %s%s", rec.id, old, new, f" ({reason})" if reason else "")
        self.events.publish(ComponentStateChanged(rec.id, old, new, reason, error))

    def _clear_failure(self, rec: _Record) -> None:
        if rec.state is ComponentState.FAILED:
            rec.error = None
            self._set_state(rec, ComponentState.RESOLVED)

    # ------------------------------------------------------------------
    # supervision
    # ------------------------------------------------------------------
    def _spawn_background(self, coro: Any, name: str) -> asyncio.Task[Any]:
        task = create_task(coro, name=name)
        self._background.add(task)
        task.add_done_callback(self._background.discard)
        return task

    def spawn(self, coro: Awaitable[T], *, name: str = "corlet-op") -> asyncio.Task[T]:
        """Run ``coro`` as a container-owned task.

        Use it from components to trigger lifecycle operations that may stop
        the calling component itself, e.g.
        ``await asyncio.shield(ctx.container.spawn(ctx.container.restart(ctx.id)))``
        -- the operation is not cancelled when the caller's tasks are.
        """

        async def runner() -> T:
            return await coro

        return self._spawn_background(runner(), name)

    def _report_crash(self, cid: str, error: BaseException, ctx: ComponentContext) -> None:
        rec = self._records.get(cid)
        if rec is None or rec.ctx is not ctx or rec.state is not ComponentState.ACTIVE:
            return
        if self.state is not ContainerState.RUNNING:
            return
        self._spawn_background(self._handle_crash(rec, error, ctx), f"corlet-crash:{cid}")

    async def _handle_crash(
        self, rec: _Record, error: BaseException, ctx: ComponentContext
    ) -> None:
        async with self._op():
            if rec.ctx is not ctx or rec.state is not ComponentState.ACTIVE:
                return
            log.error("component %r crashed: %s", rec.id, error)
            dependents = [c for c in self._with_dependents({rec.id}) if c != rec.id]
            await self._stop_many(dependents)
            await self._stop_one(rec, ComponentState.FAILED)
            rec.error = error
            self._set_state(
                rec, ComponentState.FAILED, f"crashed: {type(error).__name__}: {error}", error
            )
            self._supervise(rec)
            await self._reconcile()

    def _supervise(self, rec: _Record) -> None:
        policy = rec.manifest.restart
        if policy.mode != "on_failure" or self.state is ContainerState.STOPPING:
            return
        now = time.monotonic()
        while rec.restarts and now - rec.restarts[0] > policy.window:
            rec.restarts.popleft()
        if len(rec.restarts) >= policy.max_restarts:
            rec.gave_up = True
            log.error(
                "component %r failed %d times within %ss; giving up",
                rec.id,
                len(rec.restarts),
                policy.window,
            )
            rec.reason = f"{rec.reason or 'failed'} (restart limit reached)"
            return
        rec.restarts.append(now)
        delay = policy.delay(len(rec.restarts))
        log.info("restarting %r in %.2fs (attempt %d)", rec.id, delay, len(rec.restarts))
        self._spawn_background(self._delayed_restart(rec, delay), f"corlet-restart:{rec.id}")

    async def _delayed_restart(self, rec: _Record, delay: float) -> None:
        await asyncio.sleep(delay)
        async with self._op():
            if (
                self.state is not ContainerState.RUNNING
                or rec.state is not ComponentState.FAILED
                or not rec.enabled
            ):
                return
            rec.total_restarts += 1
            self._clear_failure(rec)
            if rec.lazy:
                rec.demanded = True
            await self._reconcile()

    # ------------------------------------------------------------------
    # lazy activation
    # ------------------------------------------------------------------
    async def _on_missing_service(self, key: str) -> bool:
        if self.state not in (ContainerState.RUNNING, ContainerState.STARTING):
            return False
        candidates = [
            r
            for r in self._records.values()
            if r.lazy
            and r.enabled
            and key in r.manifest.provides_keys
            and r.state is not ComponentState.FAILED
        ]
        if not candidates:
            return False
        for r in candidates:
            r.demanded = True
        nested = in_lifecycle.get() is self
        async with self._op():
            res = self._resolution
            ids = res.closure([r.id for r in candidates], required_only=True)
            if not ids:
                return False
            await self._activate(ids, res)
            if not nested:  # an enclosing reconcile refreshes states itself
                self._resolution = self._resolve()
                self._refresh_idle_states(self._resolution)
        return any(r.state is ComponentState.ACTIVE for r in candidates)

    def _describe_missing(self, key: str) -> str | None:
        providers = [r for r in self._records.values() if key in r.manifest.provides_keys]
        if not providers:
            return None
        parts = []
        for r in providers:
            detail = r.state.value + (f": {r.reason}" if r.reason else "")
            parts.append(f"{r.id} ({detail})")
        return "declared by " + ", ".join(parts)

    # ------------------------------------------------------------------
    # introspection
    # ------------------------------------------------------------------
    def inspect(self) -> ContainerSnapshot:
        """Return a read-only `ContainerSnapshot` of the container."""
        if not self._assembled:
            self.assemble()
        res = self._resolution
        full = self.resolver.resolve({cid: r.manifest for cid, r in self._records.items()})
        comps = []
        for cid in sorted(self._records):
            r = self._records[cid]
            m = r.manifest
            edges = res.edges.get(cid) if cid in res.wanted else full.edges.get(cid, [])
            comps.append(
                ComponentInfo(
                    id=cid,
                    version=str(m.version),
                    description=m.description,
                    state=r.state,
                    enabled=r.enabled,
                    activation=m.activation.value,
                    reason=r.reason,
                    error=f"{type(r.error).__name__}: {r.error}" if r.error else None,
                    layer=res.layer_of(cid),
                    depends_on=tuple(
                        DependencyInfo(e.target, e.optional, e.via) for e in edges or ()
                    ),
                    dependents=tuple(full.dependents(cid)),
                    provides=tuple(sorted(m.provides_keys)),
                    requires_services=tuple(str(s) for s in m.requires_services),
                    defines=tuple(p.id for p in m.defines),
                    contributes=tuple(s.point_id for s in m.extensions),
                    restarts=r.total_restarts,
                    owns=r.ctx.owns() if r.ctx is not None else {},
                    tags=tuple(sorted(m.tags)),
                )
            )
        points = []
        for p, owner in self.extensions.points():
            points.append(
                PointInfo(
                    p.id, owner, p.strategy.value, p.description, len(self.extensions.extensions(p))
                )
            )
        return ContainerSnapshot(
            name=self.name,
            state=self.state,
            strict=self.strict,
            components=tuple(comps),
            services=tuple(
                ServiceEntry(s.id, s.contract, s.owner, s.ranking, s.properties)
                for s in self.services.info()
            ),
            extension_points=tuple(sorted(points, key=lambda p: p.id)),
            extensions=tuple(
                ExtensionInfo(e.point_id, e.name, e.owner, e.order)
                for e in self.extensions.all_extensions()
            ),
            subscriptions=len(self.events.subscriptions()),
            layers=tuple(tuple(layer) for layer in res.layers),
            diagnostics=tuple(res.diagnostics),
        )

    def __repr__(self) -> str:
        return f"<Container {self.name!r} {self.state.value} components={len(self._records)}>"
