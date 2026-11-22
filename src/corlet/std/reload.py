"""Development hot reload.

``corlet.reload`` polls the source files of installed components.  When a
file changes, its module is re-imported and every component defined in it is
swapped in place via `Container.replace_component()` -- the component and
its running dependents restart with the new code, everything else keeps
running.

Intended for development only.  Limitations are those of
`importlib.reload()`: objects created from the old module (e.g. instances
held by *other* modules) keep the old code; corlet's dynamic
`ServiceRef` handles and name-based contract keys
make the common cases work.
"""

from __future__ import annotations

import asyncio
import importlib
import inspect
import sys
from dataclasses import dataclass, field
from pathlib import Path

from ..core import Component, ComponentContext, component, components_in_module, manifest_of

__all__ = ["ReloadComponent", "ReloadConfig", "reload_module_components"]


@dataclass
class ReloadConfig:
    """Configuration of ``corlet.reload``.

    Attributes:
        interval: Poll interval in seconds.
        include: Component ids to watch (default: all non-corlet components).
        exclude: Component ids never to reload.
    """

    interval: float = 1.0
    include: list[str] = field(default_factory=list)
    exclude: list[str] = field(default_factory=list)


def _source(cls: type) -> Path | None:
    try:
        f = inspect.getsourcefile(cls)
    except TypeError:
        return None
    return Path(f).resolve() if f else None


def reload_module_components(module_name: str) -> list[type]:
    """Re-import ``module_name`` and return the component classes it defines."""
    module = sys.modules.get(module_name)
    if module is None:
        module = importlib.import_module(module_name)
    else:
        importlib.invalidate_caches()
        module = importlib.reload(module)
    return components_in_module(module)


@component(
    "corlet.reload",
    version="0.1.0",
    description="Development hot reload of component modules",
    config=ReloadConfig,
    privileged=True,
)
class ReloadComponent(Component):
    """``corlet.reload``: development hot reload (see the module documentation)."""

    async def start(self, ctx: ComponentContext, config: ReloadConfig) -> None:
        self.config = config
        ctx.spawn(self._watch(ctx), name="hot-reload")

    def _watched(self, ctx: ComponentContext) -> dict[str, tuple[Path, float]]:
        """module name -> (file, mtime) for every watched component module."""
        container = ctx.container
        modules: dict[str, tuple[Path, float]] = {}
        for cid in container.component_ids:
            if cid == ctx.id or cid in self.config.exclude:
                continue
            if self.config.include and cid not in self.config.include:
                continue
            cls = container.component_class(cid)
            if not self.config.include and cls.__module__.startswith("corlet."):
                continue
            path = _source(cls)
            if path is None:
                continue
            try:
                modules[cls.__module__] = (path, path.stat().st_mtime)
            except OSError:
                continue
        return modules

    async def _watch(self, ctx: ComponentContext) -> None:
        known = self._watched(ctx)
        ctx.logger.info("watching %d module(s) for changes", len(known))
        while True:
            await asyncio.sleep(self.config.interval)
            for name, (path, mtime) in list(known.items()):
                try:
                    current = path.stat().st_mtime
                except OSError:
                    continue
                if current == mtime:
                    continue
                known[name] = (path, current)
                await self._reload(ctx, name)

    async def _reload(self, ctx: ComponentContext, module_name: str) -> None:
        container = ctx.container
        ctx.logger.info("%s changed; reloading", module_name)
        try:
            classes = reload_module_components(module_name)
        except Exception:
            ctx.logger.exception("reloading %s failed; keeping the running version", module_name)
            return
        for cls in classes:
            cid = manifest_of(cls).id
            if cid not in container:
                ctx.logger.warning(
                    "%s now defines new component %r; restart the application to install it",
                    module_name,
                    cid,
                )
                continue
            try:
                changes = await asyncio.shield(container.spawn(container.replace_component(cls)))
            except Exception:
                ctx.logger.exception("replacing %r failed", cid)
                continue
            ctx.logger.info(
                "reloaded %r (restarted: %s)", cid, ", ".join(changes.started) or "nothing"
            )
