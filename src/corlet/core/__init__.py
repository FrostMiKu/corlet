"""corlet.core -- the dependency-free microkernel.

Everything an application or component needs is re-exported here (and from
the top-level `corlet` package).

The framework and its components log to the ``corlet`` logger hierarchy
(components to ``corlet.component.<id>``) and never configure output
themselves: the application decides where records go, e.g. with
`corlet.std.logging.configure_logging()` or the ``corlet.logging`` component.
"""

import logging as _logging

from .component import Component, component, is_component, manifest_of
from .config import ConfigSource, DictConfigSource, config_section, deep_merge, validate_config
from .container import ChangeSet, Container
from .context import ComponentContext
from .discovery import (
    ComponentSource,
    DirectorySource,
    EntryPointSource,
    ExplicitSource,
    ModuleSource,
    components_in_module,
)
from .errors import (
    ComponentFailed,
    ConfigError,
    ContainerStateError,
    CorletError,
    Diagnostic,
    DuplicateComponentError,
    ExtensionContractError,
    InvalidManifestError,
    LifecycleError,
    PermissionDenied,
    ResolutionError,
    ServiceNotFound,
    ServiceUnavailable,
    VersionError,
)
from .events import Event, EventBus, HandlerFailed, Message, PublishResult, Subscription
from .extensions import (
    Extension,
    ExtensionPoint,
    ExtensionRegistry,
    ExtensionSpec,
    Strategy,
    contribution,
)
from .introspect import ComponentInfo, ContainerSnapshot, to_dot, to_mermaid
from .lifecycle import (
    ComponentState,
    ComponentStateChanged,
    ConfigChanged,
    ContainerReady,
    ContainerState,
    ContainerStopped,
    ContainerStopping,
)
from .manifest import (
    Activation,
    Manifest,
    Requirement,
    RestartPolicy,
    ServiceRequirement,
    optional,
)
from .registry import ServiceRef, ServiceRegistration, ServiceRegistry
from .resolver import Resolution, Resolver
from .version import Version, VersionConstraint

_logging.getLogger("corlet").addHandler(_logging.NullHandler())

__all__ = [
    "Activation",
    "ChangeSet",
    "Component",
    "ComponentContext",
    "ComponentFailed",
    "ComponentInfo",
    "ComponentSource",
    "ComponentState",
    "ComponentStateChanged",
    "ConfigChanged",
    "ConfigError",
    "ConfigSource",
    "Container",
    "ContainerReady",
    "ContainerSnapshot",
    "ContainerState",
    "ContainerStateError",
    "ContainerStopped",
    "ContainerStopping",
    "CorletError",
    "Diagnostic",
    "DictConfigSource",
    "DirectorySource",
    "DuplicateComponentError",
    "EntryPointSource",
    "Event",
    "EventBus",
    "ExplicitSource",
    "Extension",
    "ExtensionContractError",
    "ExtensionPoint",
    "ExtensionRegistry",
    "ExtensionSpec",
    "HandlerFailed",
    "InvalidManifestError",
    "LifecycleError",
    "Manifest",
    "Message",
    "ModuleSource",
    "PermissionDenied",
    "PublishResult",
    "Requirement",
    "Resolution",
    "ResolutionError",
    "Resolver",
    "RestartPolicy",
    "ServiceNotFound",
    "ServiceRef",
    "ServiceRegistration",
    "ServiceRegistry",
    "ServiceRequirement",
    "ServiceUnavailable",
    "Strategy",
    "Subscription",
    "Version",
    "VersionConstraint",
    "VersionError",
    "component",
    "components_in_module",
    "config_section",
    "contribution",
    "deep_merge",
    "is_component",
    "manifest_of",
    "optional",
    "to_dot",
    "to_mermaid",
    "validate_config",
]
