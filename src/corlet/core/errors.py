"""Exception hierarchy for corlet.

Every exception raised deliberately by the framework derives from
`CorletError`, so applications can catch framework failures with a
single ``except`` clause while still distinguishing the specific cases.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field

__all__ = [
    "ComponentFailed",
    "ConfigError",
    "ContainerStateError",
    "CorletError",
    "Diagnostic",
    "DuplicateComponentError",
    "ExtensionContractError",
    "InvalidManifestError",
    "LifecycleError",
    "PermissionDenied",
    "ResolutionError",
    "ServiceNotFound",
    "ServiceUnavailable",
    "VersionError",
]


class CorletError(Exception):
    """Base class of all corlet errors."""


class VersionError(CorletError, ValueError):
    """A version string or version constraint could not be parsed."""


class InvalidManifestError(CorletError, ValueError):
    """A component manifest is malformed."""


class DuplicateComponentError(CorletError):
    """Two different component classes declare the same id."""


@dataclass(frozen=True)
class Diagnostic:
    """A single finding produced while resolving the component graph."""

    severity: str  # "error" | "warning"
    code: str
    message: str
    component: str | None = None
    path: tuple[str, ...] = field(default_factory=tuple)

    def __str__(self) -> str:
        where = f"[{self.component}] " if self.component else ""
        trail = f"  (path: {' -> '.join(self.path)})" if self.path else ""
        return f"{self.severity.upper()} {self.code}: {where}{self.message}{trail}"


class ResolutionError(CorletError):
    """The component graph cannot be assembled (missing deps, cycles, ...)."""

    def __init__(self, diagnostics: Sequence[Diagnostic]) -> None:
        self.diagnostics = tuple(diagnostics)
        errors = [d for d in self.diagnostics if d.severity == "error"]
        lines = "\n".join(f"  - {d}" for d in errors)
        super().__init__(f"component graph has {len(errors)} error(s):\n{lines}")


class LifecycleError(CorletError):
    """An illegal lifecycle operation was requested."""


class ContainerStateError(LifecycleError):
    """The container is not in a state that allows the requested operation."""


class ComponentFailed(CorletError):
    """A component failed to start, or crashed while running."""

    def __init__(self, component_id: str, cause: BaseException | None = None) -> None:
        self.component_id = component_id
        self.cause = cause
        detail = f": {type(cause).__name__}: {cause}" if cause is not None else ""
        super().__init__(f"component {component_id!r} failed{detail}")


class ServiceNotFound(CorletError, LookupError):
    """No registered service matches the requested contract/filter."""


class ServiceUnavailable(CorletError):
    """A service reference was used after its provider went away."""


class ExtensionContractError(CorletError, TypeError):
    """A contribution does not satisfy the extension point's contract."""


class ConfigError(CorletError, ValueError):
    """Component configuration is invalid."""

    def __init__(self, component_id: str | None, message: str) -> None:
        self.component_id = component_id
        prefix = f"configuration of {component_id!r} is invalid: " if component_id else ""
        super().__init__(prefix + message)


class PermissionDenied(CorletError, PermissionError):
    """A component used an API it did not declare (strict mode)."""
