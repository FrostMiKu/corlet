"""Read-only snapshots of a container, and dependency graph rendering."""

from __future__ import annotations

import dataclasses
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from .errors import Diagnostic
from .lifecycle import ComponentState, ContainerState

__all__ = ["ComponentInfo", "ContainerSnapshot", "ExtensionInfo", "to_dot", "to_mermaid"]


@dataclass(frozen=True)
class DependencyInfo:
    """One dependency edge of a component."""

    target: str
    optional: bool
    via: str


@dataclass(frozen=True)
class ComponentInfo:
    """Snapshot of one component (see `Container.inspect`)."""

    id: str
    version: str
    description: str
    state: ComponentState
    enabled: bool
    activation: str
    reason: str | None
    error: str | None
    layer: int | None
    depends_on: tuple[DependencyInfo, ...]
    dependents: tuple[str, ...]
    provides: tuple[str, ...]
    requires_services: tuple[str, ...]
    defines: tuple[str, ...]
    contributes: tuple[str, ...]
    restarts: int
    owns: Mapping[str, int] = field(default_factory=dict)
    tags: tuple[str, ...] = ()


@dataclass(frozen=True)
class ExtensionInfo:
    """Snapshot of one live contribution."""

    point: str
    name: str
    owner: str
    order: int


@dataclass(frozen=True)
class PointInfo:
    """Snapshot of one extension point definition."""

    id: str
    owner: str
    strategy: str
    description: str
    contributions: int


@dataclass(frozen=True)
class ServiceEntry:
    """Snapshot of one service registration."""

    id: int
    contract: str
    owner: str
    ranking: int
    properties: Mapping[str, Any]


@dataclass(frozen=True)
class ContainerSnapshot:
    """Read-only snapshot of a container, returned by `Container.inspect`."""

    name: str
    state: ContainerState
    strict: bool
    components: tuple[ComponentInfo, ...]
    services: tuple[ServiceEntry, ...]
    extension_points: tuple[PointInfo, ...]
    extensions: tuple[ExtensionInfo, ...]
    subscriptions: int
    layers: tuple[tuple[str, ...], ...]
    diagnostics: tuple[Diagnostic, ...]

    def component(self, cid: str) -> ComponentInfo:
        """Info of component ``cid`` (raises ``KeyError``)."""
        for c in self.components:
            if c.id == cid:
                return c
        raise KeyError(cid)

    def by_state(self, state: ComponentState) -> list[str]:
        """Ids of the components in ``state``."""
        return [c.id for c in self.components if c.state is state]

    def to_dict(self) -> dict[str, Any]:
        """JSON-serializable representation of the snapshot."""

        def conv(obj: Any) -> Any:
            if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
                return {f.name: conv(getattr(obj, f.name)) for f in dataclasses.fields(obj)}
            if isinstance(obj, Mapping):
                return {str(k): conv(v) for k, v in obj.items()}
            if isinstance(obj, list | tuple | set | frozenset):
                return [conv(v) for v in obj]
            if isinstance(obj, ComponentState | ContainerState):
                return obj.value
            if isinstance(obj, str | int | float | bool) or obj is None:
                return obj
            return repr(obj)

        return conv(self)  # type: ignore[no-any-return]


_STATE_COLORS = {
    ComponentState.ACTIVE: "palegreen",
    ComponentState.FAILED: "salmon",
    ComponentState.UNSATISFIED: "khaki",
    ComponentState.DISABLED: "lightgray",
    ComponentState.RESOLVED: "lightblue",
}


def to_dot(snapshot: ContainerSnapshot, *, include_disabled: bool = True) -> str:
    """Render the component graph in Graphviz DOT."""
    lines = [
        f'digraph "{snapshot.name}" {{',
        "  rankdir=BT;",
        '  node [shape=box, style="rounded,filled", fontname="Helvetica"];',
    ]
    shown = {
        c.id
        for c in snapshot.components
        if include_disabled or c.state is not ComponentState.DISABLED
    }
    for c in snapshot.components:
        if c.id not in shown:
            continue
        color = _STATE_COLORS.get(c.state, "white")
        label = f"{c.id}\\n{c.version} · {c.state.value}"
        lines.append(f'  "{c.id}" [label="{label}", fillcolor="{color}"];')
    for c in snapshot.components:
        for d in c.depends_on:
            if c.id in shown and d.target in shown:
                style = "dashed" if d.optional else "solid"
                label = "" if d.via == "component" else d.via.rsplit(".", 1)[-1]
                lines.append(f'  "{c.id}" -> "{d.target}" [style={style}, label="{label}"];')
    lines.append("}")
    return "\n".join(lines)


def to_mermaid(snapshot: ContainerSnapshot) -> str:
    """Render the component graph as a Mermaid flowchart."""
    ids = {c.id: f"c{i}" for i, c in enumerate(snapshot.components)}
    lines = ["flowchart BT"]
    for c in snapshot.components:
        lines.append(f'  {ids[c.id]}["{c.id}<br/><small>{c.state.value}</small>"]')
    for c in snapshot.components:
        for d in c.depends_on:
            if d.target in ids:
                arrow = "-.->" if d.optional else "-->"
                label = "" if d.via == "component" else f"|{d.via.rsplit('.', 1)[-1]}|"
                lines.append(f"  {ids[c.id]} {arrow}{label} {ids[d.target]}")
    for state, color in _STATE_COLORS.items():
        members = [ids[c.id] for c in snapshot.components if c.state is state]
        if members:
            lines.append(f"  classDef {state.value} fill:{color}")
            lines.append(f"  class {','.join(members)} {state.value}")
    return "\n".join(lines)
