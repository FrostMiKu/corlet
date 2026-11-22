"""Lifecycle states and the events the container publishes about them."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum

from .events import Event

__all__ = [
    "ComponentState",
    "ComponentStateChanged",
    "ConfigChanged",
    "ContainerReady",
    "ContainerState",
    "ContainerStopped",
    "ContainerStopping",
]


class ComponentState(StrEnum):
    """Observable state of a component.

    ::

        INSTALLED ─resolve─► RESOLVED ─► STARTING ─► ACTIVE ─► STOPPING ─► STOPPED
            │                   ▲            │          │                    │
            ├─► DISABLED        │            ▼          ▼ (crash)            │
            └─► UNSATISFIED ────┘          FAILED ◄─────┘ ── restart ──► STARTING

    * ``RESOLVED``    -- wanted, not running (lazy and not yet demanded, or
      waiting for the container to start)
    * ``UNSATISFIED`` -- enabled, but required dependencies are missing,
      disabled or failed
    * ``DISABLED``    -- switched off via configuration or ``disable()``
    """

    INSTALLED = "installed"
    RESOLVED = "resolved"
    UNSATISFIED = "unsatisfied"
    DISABLED = "disabled"
    STARTING = "starting"
    ACTIVE = "active"
    STOPPING = "stopping"
    STOPPED = "stopped"
    FAILED = "failed"

    @property
    def running(self) -> bool:
        """``True`` for STARTING, ACTIVE and STOPPING (a live instance exists)."""
        return self in (ComponentState.STARTING, ComponentState.ACTIVE, ComponentState.STOPPING)


class ContainerState(StrEnum):
    """Overall state of a `Container`."""

    CREATED = "created"
    STARTING = "starting"
    RUNNING = "running"
    STOPPING = "stopping"
    STOPPED = "stopped"
    FAILED = "failed"


@dataclass(frozen=True)
class ComponentStateChanged(Event):
    """Published on every component state transition.

    Topic: ``corlet.lifecycle.<new state>`` (e.g. ``corlet.lifecycle.active``),
    so ``corlet.lifecycle.*`` observes all transitions.
    """

    component_id: str
    old: ComponentState
    new: ComponentState
    reason: str | None = None
    error: BaseException | None = field(default=None, repr=False, compare=False)

    @property
    def topic(self) -> str:  # type: ignore[override]
        """``corlet.lifecycle.<new state>``."""
        return f"corlet.lifecycle.{self.new.value}"


@dataclass(frozen=True)
class ContainerReady(Event):
    """Published after `Container.start` completed."""

    topic = "corlet.container.ready"
    name: str
    active: tuple[str, ...]
    failed: tuple[str, ...] = ()


@dataclass(frozen=True)
class ContainerStopping(Event):
    """Published when the container begins to shut down."""

    topic = "corlet.container.stopping"
    name: str


@dataclass(frozen=True)
class ContainerStopped(Event):
    """Published after every component has stopped."""

    topic = "corlet.container.stopped"
    name: str


@dataclass(frozen=True)
class ConfigChanged(Event):
    """Published per component after `Container.reload_config` applied a change."""

    topic = "corlet.config.changed"
    component_id: str
    applied: str  # "reconfigured" | "restarted" | "deferred"
