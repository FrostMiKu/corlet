"""Component manifests: the static, declarative description of a component."""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field, replace
from enum import StrEnum
from typing import Any, Literal

from ._util import contract_key, contract_name
from .errors import InvalidManifestError
from .extensions import ExtensionPoint, ExtensionSpec
from .version import Version, VersionConstraint

__all__ = [
    "Activation",
    "Manifest",
    "Requirement",
    "RestartPolicy",
    "ServiceRequirement",
    "optional",
]

_ID_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_\-]*(?:\.[A-Za-z_][A-Za-z0-9_\-]*)*$")
_REQ_RE = re.compile(r"^(?P<opt>\?)?\s*(?P<id>[A-Za-z0-9_.\-]+)\s*(?P<constraint>.*)$")


def validate_id(value: str) -> str:
    if not isinstance(value, str) or not _ID_RE.match(value):
        raise InvalidManifestError(
            f"invalid component id {value!r}: use dotted identifiers like 'acme.auth'"
        )
    return value


class Activation(StrEnum):
    """When a component is started."""

    EAGER = "eager"
    """started at boot"""
    LAZY = "lazy"
    """started on first demand (dependency or service acquisition)"""


@dataclass(frozen=True)
class RestartPolicy:
    """Supervisor policy applied when a component fails or crashes.

    ``mode="on_failure"`` restarts with exponential backoff, at most
    ``max_restarts`` times within ``window`` seconds.
    """

    mode: Literal["never", "on_failure"] = "never"
    max_restarts: int = 3
    window: float = 60.0
    backoff: float = 0.5
    backoff_max: float = 30.0
    factor: float = 2.0

    def delay(self, attempt: int) -> float:
        """Backoff delay in seconds before restart attempt ``attempt`` (1-based)."""
        return float(min(self.backoff_max, self.backoff * (self.factor ** max(0, attempt - 1))))

    @classmethod
    def on_failure(cls, **kw: Any) -> RestartPolicy:
        """Shortcut for ``RestartPolicy(mode='on_failure', **kw)``."""
        return cls(mode="on_failure", **kw)


@dataclass(frozen=True)
class Requirement:
    """Dependency on another component (by id), with an optional version constraint."""

    id: str
    constraint: VersionConstraint = VersionConstraint.ANY
    optional: bool = False

    @classmethod
    def parse(cls, spec: str | Requirement) -> Requirement:
        """Parse ``"acme.db>=1.2"``, ``"?acme.cache ^2"`` (``?`` = optional)."""
        if isinstance(spec, Requirement):
            return spec
        m = _REQ_RE.match(spec.strip())
        if not m:
            raise InvalidManifestError(f"invalid requirement: {spec!r}")
        return cls(
            validate_id(m["id"]),
            VersionConstraint.parse(m["constraint"] or None),
            bool(m["opt"]),
        )

    def __str__(self) -> str:
        c = "" if str(self.constraint) == "*" else str(self.constraint)
        return f"{'?' if self.optional else ''}{self.id}{c}"


@dataclass(frozen=True)
class ServiceRequirement:
    """Dependency on a service contract, satisfied by whichever component provides it."""

    contract: Any
    optional: bool = False

    @property
    def key(self) -> str:
        """Registry key of the contract."""
        return contract_key(self.contract)

    def __str__(self) -> str:
        return f"{'?' if self.optional else ''}{contract_name(self.contract)}"


def optional(contract: Any) -> ServiceRequirement:
    """Mark a service requirement as optional: ``requires_services=[optional(Cache)]``."""
    return ServiceRequirement(contract, optional=True)


@dataclass(frozen=True)
class Manifest:
    """The static description of a component, built by `component`.

    Attributes:
        id: Globally unique dotted id, e.g. ``acme.auth``.
        version: Semantic version of the component.
        description: One-line description (defaults to the class docstring).
        requires: Dependencies on other components by id.
        requires_services: Dependencies on service contracts, satisfied by any
            component that provides them (preferred over ``requires``).
        provides_services: Contracts the component registers during ``start``.
        defines: Extension points owned by the component.
        extensions: Declarative contributions, active while the component runs.
        config_schema: Dataclass, Pydantic model, ``dict`` or ``None``.
        activation: `Activation.EAGER` or `Activation.LAZY`.
        restart: Supervisor policy for failures and crashes.
        start_timeout: Seconds ``start`` may take (``None`` = unlimited).
        stop_timeout: Seconds ``stop`` and cleanup may take.
        blocking: Run *sync* ``start``/``stop`` in a worker thread.
        privileged: May access ``ctx.container`` in strict mode.
        tags: Free-form labels (shown by introspection).
    """

    id: str
    version: Version = field(default_factory=lambda: Version(0, 1, 0))
    description: str = ""
    requires: tuple[Requirement, ...] = ()
    requires_services: tuple[ServiceRequirement, ...] = ()
    provides_services: tuple[Any, ...] = ()
    defines: tuple[ExtensionPoint[Any], ...] = ()
    extensions: tuple[ExtensionSpec, ...] = ()
    config_schema: Any = None
    activation: Activation = Activation.EAGER
    restart: RestartPolicy = field(default_factory=RestartPolicy)
    start_timeout: float | None = 30.0
    stop_timeout: float | None = 10.0
    blocking: bool = False
    privileged: bool = False
    tags: frozenset[str] = frozenset()

    @property
    def provides_keys(self) -> frozenset[str]:
        """Registry keys of ``provides_services``."""
        return frozenset(contract_key(c) for c in self.provides_services)

    def with_changes(self, **changes: Any) -> Manifest:
        """Return a copy with the given fields replaced."""
        return replace(self, **changes)


def normalize_requires(items: Iterable[str | Requirement]) -> tuple[Requirement, ...]:
    return tuple(Requirement.parse(i) for i in items)


def normalize_service_requires(items: Iterable[Any]) -> tuple[ServiceRequirement, ...]:
    out: list[ServiceRequirement] = []
    for item in items:
        if isinstance(item, ServiceRequirement):
            out.append(item)
        elif isinstance(item, str) and item.startswith("?"):
            out.append(ServiceRequirement(item[1:], optional=True))
        else:
            out.append(ServiceRequirement(item))
    return tuple(out)


def normalize_extensions(
    items: Iterable[ExtensionSpec] | Mapping[ExtensionPoint[Any] | str, Any] | None,
) -> tuple[ExtensionSpec, ...]:
    if not items:
        return ()
    if isinstance(items, Mapping):
        out: list[ExtensionSpec] = []
        for point, impls in items.items():
            seq = impls if isinstance(impls, list | tuple) else [impls]
            out.extend(i if isinstance(i, ExtensionSpec) else ExtensionSpec(point, i) for i in seq)
        return tuple(out)
    result = tuple(items)
    for spec in result:
        if not isinstance(spec, ExtensionSpec):
            raise InvalidManifestError(
                "extensions must be ExtensionSpec objects (see corlet.contribution) "
                "or a mapping {point: impl | [impls]}"
            )
    return result
