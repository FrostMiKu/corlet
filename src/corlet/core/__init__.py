"""corlet.core -- the dependency-free microkernel.

Everything an application or component needs is re-exported here (and from
the top-level `corlet` package).
"""

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
