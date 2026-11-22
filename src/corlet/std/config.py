"""Layered configuration: defaults < files < environment < explicit overrides.

::

    from corlet.std.config import LayeredConfig

    config = LayeredConfig(
        files=["app.toml", "?local.toml"],      # "?" = optional file
        env_prefix="MYAPP",                     # MYAPP__COMPONENTS__ACME__DB__PORT=5433
        overrides=["components.acme.db.debug=true"],
    )
    Container(..., config=config)

File formats are picked by extension: ``.toml`` (stdlib), ``.json`` (stdlib),
``.yaml``/``.yml`` (requires PyYAML).

Environment variables use ``__`` as the path separator and are lower-cased:
``MYAPP__COMPONENTS__ACME__DB__PORT=5433`` becomes
``{"components": {"acme": {"db": {"port": 5433}}}}`` -- the container accepts
this nested form for the component ``acme.db``.  Values are parsed as JSON
when possible (numbers, booleans, lists), otherwise kept as strings.

The `ConfigWatcher` component reloads the container's configuration
when one of the files changes.
"""

from __future__ import annotations

import asyncio
import json
import os
import tomllib
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from ..core import Component, ComponentContext, component
from ..core.config import canonicalize, deep_merge
from ..core.errors import ConfigError

__all__ = [
    "ConfigService",
    "ConfigServiceComponent",
    "ConfigWatcher",
    "LayeredConfig",
    "load_file",
    "parse_override",
    "set_path",
]


def load_file(path: str | Path) -> dict[str, Any]:
    p = Path(path)
    suffix = p.suffix.lower()
    try:
        if suffix == ".toml":
            with p.open("rb") as fh:
                return tomllib.load(fh)
        text = p.read_text(encoding="utf-8")
        if suffix == ".json":
            data = json.loads(text) if text.strip() else {}
        elif suffix in (".yaml", ".yml"):
            try:
                import yaml
            except ImportError as exc:  # pragma: no cover - depends on env
                raise ConfigError(None, f"{p}: YAML support requires 'pip install pyyaml'") from exc
            data = yaml.safe_load(text) or {}
        else:
            raise ConfigError(None, f"{p}: unsupported config format {suffix!r}")
    except (OSError, ValueError) as exc:
        if isinstance(exc, ConfigError):
            raise
        raise ConfigError(None, f"cannot read {p}: {exc}") from exc
    if not isinstance(data, Mapping):
        raise ConfigError(None, f"{p}: top level must be a mapping")
    return dict(data)


def _parse_value(raw: str) -> Any:
    try:
        return json.loads(raw)
    except ValueError:
        return raw


def set_path(tree: dict[str, Any], path: Iterable[str], value: Any) -> None:
    parts = list(path)
    node = tree
    for part in parts[:-1]:
        nxt = node.get(part)
        if not isinstance(nxt, dict):
            nxt = {}
            node[part] = nxt
        node = nxt
    node[parts[-1]] = value


def parse_override(text: str) -> dict[str, Any]:
    """``"components.acme.db.port=5433"`` -> nested dict (value parsed as JSON)."""
    key, sep, raw = text.partition("=")
    if not sep or not key.strip():
        raise ConfigError(None, f"invalid override {text!r}; expected key.path=value")
    tree: dict[str, Any] = {}
    set_path(tree, key.strip().split("."), _parse_value(raw))
    return tree


class LayeredConfig:
    """A `ConfigSource` merging several layers."""

    def __init__(
        self,
        *,
        defaults: Mapping[str, Any] | None = None,
        files: Iterable[str | Path] = (),
        env_prefix: str | None = "CORLET",
        environ: Mapping[str, str] | None = None,
        overrides: Iterable[str | Mapping[str, Any]] = (),
    ) -> None:
        self.defaults = dict(defaults or {})
        self.files: list[tuple[Path, bool]] = []
        for f in files:
            s = str(f)
            optional = s.startswith("?")
            self.files.append((Path(s[1:] if optional else s), optional))
        self.env_prefix = env_prefix
        self._environ = environ
        self.overrides = [parse_override(o) if isinstance(o, str) else dict(o) for o in overrides]

    def env_layer(self) -> dict[str, Any]:
        """The environment layer as a nested mapping."""
        if not self.env_prefix:
            return {}
        environ = os.environ if self._environ is None else self._environ
        prefix = self.env_prefix.upper() + "__"
        tree: dict[str, Any] = {}
        for name in sorted(environ):
            if name.upper().startswith(prefix) and len(name) > len(prefix):
                path = [p.lower() for p in name[len(prefix) :].split("__") if p]
                if path:
                    set_path(tree, path, _parse_value(environ[name]))
        return tree

    def load(self) -> dict[str, Any]:
        # Every layer is canonicalized (dotted component keys -> nested) so a
        # later layer wins no matter which form each layer was written in.
        """Merge all layers and return the configuration tree."""
        tree: dict[str, Any] = canonicalize(self.defaults)
        for path, optional in self.files:
            if not path.exists():
                if optional:
                    continue
                raise ConfigError(None, f"config file not found: {path}")
            tree = deep_merge(tree, canonicalize(load_file(path)))
        tree = deep_merge(tree, canonicalize(self.env_layer()))
        for o in self.overrides:
            tree = deep_merge(tree, canonicalize(o))
        return tree

    def fingerprint(self) -> tuple[tuple[str, float | None], ...]:
        """Modification times of the files (used by `ConfigWatcher`)."""
        out: list[tuple[str, float | None]] = []
        for path, _ in self.files:
            try:
                out.append((str(path), path.stat().st_mtime))
            except OSError:
                out.append((str(path), None))
        return tuple(out)

    def __repr__(self) -> str:
        return f"LayeredConfig(files={[str(p) for p, _ in self.files]}, env={self.env_prefix})"


# ---------------------------------------------------------------------------
# components
# ---------------------------------------------------------------------------


@runtime_checkable
class ConfigService(Protocol):
    """Read access to the raw configuration tree (for app-level settings)."""

    def get(self, path: str, default: Any = None) -> Any:
        """Value at the dotted ``path``, or ``default``."""
        ...

    def tree(self) -> Mapping[str, Any]:
        """The full configuration tree."""
        ...


class _TreeView:
    def __init__(self, ctx: ComponentContext) -> None:
        self._ctx = ctx

    def tree(self) -> Mapping[str, Any]:
        return self._ctx.container.config_tree

    def get(self, path: str, default: Any = None) -> Any:
        node: Any = self.tree()
        for part in path.split("."):
            if not isinstance(node, Mapping) or part not in node:
                return default
            node = node[part]
        return node


@component(
    "corlet.config",
    version="0.1.0",
    description="Exposes the configuration tree as a service",
    provides=[ConfigService],
    privileged=True,
)
class ConfigServiceComponent(Component):
    """``corlet.config``: registers `ConfigService`."""

    async def start(self, ctx: ComponentContext) -> None:
        ctx.services.register(ConfigService, _TreeView(ctx))


@dataclass
class WatcherConfig:
    """Configuration of ``corlet.config.watcher``: poll ``interval`` in seconds."""

    interval: float = 2.0


@component(
    "corlet.config.watcher",
    version="0.1.0",
    description="Reloads configuration when config files change",
    config=WatcherConfig,
    privileged=True,
)
class ConfigWatcher(Component):
    """``corlet.config.watcher``: reloads configuration when files change.

    Polls the modification times of the container's
    `LayeredConfig` files and calls `Container.reload_config`.
    """

    async def start(self, ctx: ComponentContext, config: WatcherConfig) -> None:
        source = ctx.container._config_source
        fingerprint = getattr(source, "fingerprint", None)
        if not callable(fingerprint):
            ctx.logger.warning("config source %r cannot be watched; watcher idle", source)
            return
        ctx.spawn(self._watch(ctx, fingerprint, config.interval), name="config-watcher")

    async def _watch(self, ctx: ComponentContext, fingerprint: Any, interval: float) -> None:
        last = fingerprint()
        while True:
            await asyncio.sleep(interval)
            now = fingerprint()
            if now == last:
                continue
            last = now
            ctx.logger.info("configuration changed on disk; reloading")
            container = ctx.container
            try:
                # Run detached: reloading may restart this very component.
                outcomes = await asyncio.shield(container.spawn(container.reload_config()))
            except Exception:
                ctx.logger.exception("configuration reload failed")
                continue
            if outcomes:
                ctx.logger.info("configuration applied: %s", outcomes)
