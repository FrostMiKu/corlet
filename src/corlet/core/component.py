"""The ``Component`` base class, the ``@component`` decorator and DI planning."""

from __future__ import annotations

import inspect
import types
import typing
from collections.abc import Awaitable, Callable, Iterable, Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, ClassVar, Literal, TypeVar, overload

from ._util import contract_key
from .errors import InvalidManifestError
from .extensions import ExtensionPoint, ExtensionSpec
from .manifest import (
    Activation,
    Manifest,
    Requirement,
    RestartPolicy,
    ServiceRequirement,
    normalize_extensions,
    normalize_requires,
    normalize_service_requires,
    validate_id,
)
from .registry import ServiceRef
from .version import Version

if TYPE_CHECKING:
    from .context import ComponentContext

__all__ = ["Component", "InjectionPlan", "component", "is_component", "manifest_of"]

C = TypeVar("C", bound=type)
MANIFEST_ATTR = "__corlet_manifest__"


class Component:
    """Base class for components (subclassing is recommended, not required).

    Lifecycle hooks may be ``async`` or plain functions:

    * ``start(ctx, *injected)`` -- acquire resources, register services.
      Extra parameters annotated with service contracts are injected.
    * ``stop(ctx)`` -- optional; everything registered through ``ctx`` is
      released automatically afterwards.
    * ``reconfigure(ctx, config)`` -- apply a new configuration in place.
      Return ``True`` if applied; ``False`` makes the container restart the
      component instead.
    """

    __corlet_manifest__: ClassVar[Manifest]

    # The permissive signatures let subclasses override with either ``def`` or
    # ``async def`` and with additional injected parameters, type-safely.
    def start(self, ctx: ComponentContext, /, *args: Any, **kwargs: Any) -> Awaitable[None] | None:
        """Acquire resources and register services.

        Parameters after ``ctx`` are injected from their annotations: the
        config schema receives the validated configuration, a contract
        ``T`` the service object, ``ServiceRef[T]`` a dynamic reference.
        ``T | None`` or a default value makes the dependency optional.
        """
        return None

    def stop(self, ctx: ComponentContext, /) -> Awaitable[None] | None:
        """Release resources that were not acquired through ``ctx``.

        Optional: everything registered through the context (services,
        contributions, subscriptions, spawned tasks, deferred callbacks)
        is released automatically after ``stop`` returns.
        """
        return None

    def reconfigure(self, ctx: ComponentContext, config: Any, /) -> Awaitable[bool] | bool:
        """Apply a changed configuration in place.

        Returns:
            ``True`` if the new configuration was applied; ``False`` (the
            default) makes the container restart the component instead.
        """
        return False


def is_component(obj: object) -> bool:
    """Return ``True`` if ``obj`` is a class decorated with `component`."""
    return isinstance(obj, type) and MANIFEST_ATTR in obj.__dict__


def manifest_of(obj: object) -> Manifest:
    """Return the declared `Manifest` of a component class or instance.

    Raises:
        InvalidManifestError: ``obj`` is not a component.
    """
    cls = obj if isinstance(obj, type) else type(obj)
    m = cls.__dict__.get(MANIFEST_ATTR)
    if not isinstance(m, Manifest):
        raise InvalidManifestError(f"{cls!r} is not a component (missing @component)")
    return m


@overload
def component(cls: C, /) -> C: ...
@overload
def component(
    id: str | None = None,
    /,
    *,
    version: str | Version = ...,
    description: str | None = ...,
    requires: Iterable[str | Requirement] = ...,
    requires_services: Iterable[Any] = ...,
    provides: Iterable[Any] = ...,
    defines: Iterable[ExtensionPoint[Any]] = ...,
    extensions: Iterable[ExtensionSpec] | Mapping[Any, Any] | None = ...,
    config: Any = ...,
    activation: Activation | Literal["eager", "lazy"] = ...,
    restart: RestartPolicy | Literal["never", "on_failure"] | None = ...,
    start_timeout: float | None = ...,
    stop_timeout: float | None = ...,
    blocking: bool = ...,
    privileged: bool = ...,
    tags: Iterable[str] = ...,
) -> Callable[[C], C]: ...
def component(
    id: Any = None,
    /,
    *,
    version: str | Version = "0.1.0",
    description: str | None = None,
    requires: Iterable[str | Requirement] = (),
    requires_services: Iterable[Any] = (),
    provides: Iterable[Any] = (),
    defines: Iterable[ExtensionPoint[Any]] = (),
    extensions: Iterable[ExtensionSpec] | Mapping[Any, Any] | None = None,
    config: Any = None,
    activation: Activation | Literal["eager", "lazy"] = Activation.EAGER,
    restart: RestartPolicy | Literal["never", "on_failure"] | None = None,
    start_timeout: float | None = 30.0,
    stop_timeout: float | None = 10.0,
    blocking: bool = False,
    privileged: bool = False,
    tags: Iterable[str] = (),
) -> Any:
    """Declare a class as a component and attach its `Manifest`.

    Usable bare (``@component``; the id defaults to ``module.ClassName``) or
    with arguments (``@component("acme.auth", version="1.2.0", ...)``).
    """

    def decorate(cls: C, comp_id: str | None) -> C:
        if not isinstance(cls, type):
            raise InvalidManifestError("@component can only decorate classes")
        if not callable(getattr(cls, "start", None)):
            raise InvalidManifestError(f"component {cls.__qualname__} has no start() method")
        if isinstance(restart, RestartPolicy):
            policy = restart
        elif restart is None:
            policy = RestartPolicy()
        else:
            policy = RestartPolicy(mode=restart)
        doc = inspect.getdoc(cls) if cls.__doc__ else ""
        manifest = Manifest(
            id=validate_id(comp_id or f"{cls.__module__}.{cls.__name__}"),
            version=Version.parse(version),
            description=description if description is not None else (doc or "").split("\n")[0],
            requires=normalize_requires(requires),
            requires_services=normalize_service_requires(requires_services),
            provides_services=tuple(provides),
            defines=tuple(defines),
            extensions=normalize_extensions(extensions),
            config_schema=config,
            activation=Activation(activation),
            restart=policy,
            start_timeout=start_timeout,
            stop_timeout=stop_timeout,
            blocking=blocking,
            privileged=privileged,
            tags=frozenset(tags),
        )
        setattr(cls, MANIFEST_ATTR, manifest)
        return cls

    if isinstance(id, type):  # bare @component
        return decorate(id, None)

    def wrapper(cls: C) -> C:
        return decorate(cls, id)

    return wrapper


# ---------------------------------------------------------------------------
# Dependency injection planning
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Injection:
    """How one ``start()`` parameter is injected."""

    param: str
    kind: Literal["service", "ref", "config"]
    contract: Any = None
    optional: bool = False


@dataclass(frozen=True)
class InjectionPlan:
    """Injections derived from the annotations of a component's ``start()``."""

    injections: tuple[Injection, ...] = ()

    @property
    def service_requirements(self) -> tuple[ServiceRequirement, ...]:
        """Service requirements implied by the injections."""
        return tuple(
            ServiceRequirement(i.contract, i.optional)
            for i in self.injections
            if i.kind in ("service", "ref")
        )


def _strip_optional(tp: Any) -> tuple[Any, bool]:
    origin = typing.get_origin(tp)
    if origin is typing.Union or origin is types.UnionType:
        args = [a for a in typing.get_args(tp) if a is not type(None)]
        if len(args) == 1 and len(typing.get_args(tp)) == 2:
            return args[0], True
    return tp, False


def _raw_annotations(fn: Any) -> dict[str, Any]:
    """Annotations without forcing evaluation of names we do not need.

    Only the injected parameters are evaluated, so ``ctx: ComponentContext``
    may refer to a name imported under ``TYPE_CHECKING``.
    """
    try:
        import annotationlib  # type: ignore[import-not-found]  # Python >= 3.14
    except ImportError:
        return dict(inspect.get_annotations(fn))
    return dict(annotationlib.get_annotations(fn, format=annotationlib.Format.FORWARDREF))


def _resolve_annotation(fn: Any, cls: type, ann: Any) -> Any:
    globalns = getattr(fn, "__globals__", {})
    localns = {cls.__name__: cls}
    if isinstance(ann, str):
        return eval(ann, globalns, localns)  # standard annotation evaluation
    if isinstance(ann, typing.ForwardRef):
        evaluate = getattr(ann, "evaluate", None)
        if evaluate is not None:  # Python >= 3.14
            return evaluate(globals=globalns, locals=localns)
        return eval(ann.__forward_arg__, globalns, localns)
    return ann


def injection_plan(cls: type, manifest: Manifest) -> InjectionPlan:
    """Derive what to inject into ``start`` from its type annotations.

    The first parameter after ``self`` is always the context.  Every further
    parameter is injected according to its annotation:

    * the manifest's config schema       -> the validated config object
    * ``ServiceRef[T]`` / ``ServiceRef[T] | None`` -> a dynamic service ref
    * ``T`` / ``T | None``               -> the service instance (or None)

    A parameter with a default value is treated as optional.
    """
    start = getattr(cls, "start", None)
    if start is None:
        return InjectionPlan()
    try:
        sig = inspect.signature(start)
    except (TypeError, ValueError):
        return InjectionPlan()
    params = [
        p
        for p in sig.parameters.values()
        if p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD, p.KEYWORD_ONLY)
    ]
    # Drop self (functions looked up on the class are unbound) and ctx.
    if params and params[0].name == "self":
        params = params[1:]
    params = params[1:]
    if not params:
        return InjectionPlan()
    raw = _raw_annotations(start)
    out: list[Injection] = []
    for p in params:
        if p.name not in raw:
            raise InvalidManifestError(
                f"{cls.__qualname__}.start() parameter {p.name!r} needs a type annotation "
                "to be injected"
            )
        try:
            hint = _resolve_annotation(start, cls, raw[p.name])
        except Exception as exc:
            raise InvalidManifestError(
                f"cannot resolve annotation of {cls.__qualname__}.start() parameter "
                f"{p.name!r} for injection: {exc}"
            ) from exc
        tp, opt = _strip_optional(hint)
        opt = opt or p.default is not inspect.Parameter.empty
        if manifest.config_schema is not None and tp is manifest.config_schema:
            out.append(Injection(p.name, "config"))
        elif typing.get_origin(tp) is ServiceRef:
            (inner,) = typing.get_args(tp) or (Any,)
            if inner is Any:
                raise InvalidManifestError(
                    f"{cls.__qualname__}.start(): ServiceRef parameter {p.name!r} needs a "
                    "contract, e.g. ServiceRef[Database]"
                )
            out.append(Injection(p.name, "ref", inner, opt))
        elif tp is ServiceRef:
            raise InvalidManifestError(
                f"{cls.__qualname__}.start(): parameterize ServiceRef for {p.name!r}"
            )
        else:
            out.append(Injection(p.name, "service", tp, opt))
    return InjectionPlan(tuple(out))


def effective_manifest(cls: type) -> tuple[Manifest, InjectionPlan]:
    """The declared manifest merged with service requirements implied by DI.

    An explicit ``requires_services`` declaration wins: declaring
    ``optional(Cache)`` makes an injected ``cache: Cache`` parameter optional
    even without a default value.  Contracts that are only injected (not
    declared) are added as requirements.
    """
    manifest = manifest_of(cls)
    plan = injection_plan(cls, manifest)
    declared = {r.key: r for r in manifest.requires_services}
    merged = list(manifest.requires_services)
    for req in plan.service_requirements:
        if req.key not in declared:
            merged.append(req)
            declared[req.key] = req
    injections = tuple(
        Injection(i.param, i.kind, i.contract, declared[contract_key(i.contract)].optional)
        if i.kind in ("service", "ref")
        else i
        for i in plan.injections
    )
    return manifest.with_changes(requires_services=tuple(merged)), InjectionPlan(injections)
