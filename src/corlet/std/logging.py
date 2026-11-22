"""Logging integration.

* `ComponentFilter` stamps every record with ``record.component`` --
  the id of the component on whose behalf the code runs (tracked through a
  context variable, so it also works inside spawned tasks and event handlers).
* `JsonFormatter` emits one JSON object per line.
* `configure_logging()` installs both on the root logger.
* `LoggingComponent` (``corlet.logging``) applies the ``logging``
  configuration of its component section and re-applies it on reload.
"""

from __future__ import annotations

import json
import logging
import sys
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Literal, TextIO

from ..core import Component, ComponentContext, component
from ..core._util import current_component

__all__ = [
    "ComponentFilter",
    "JsonFormatter",
    "LoggingComponent",
    "LoggingConfig",
    "configure_logging",
]

_HANDLER_MARK = "_corlet_handler"
TEXT_FORMAT = "%(asctime)s %(levelname)-7s [%(component)s] %(name)s: %(message)s"


class ComponentFilter(logging.Filter):
    """Adds ``record.component`` (``-`` outside of any component)."""

    def filter(self, record: logging.LogRecord) -> bool:
        if not hasattr(record, "component"):
            cid = current_component.get()
            if cid is None and record.name.startswith("corlet.component."):
                cid = record.name[len("corlet.component.") :]
            record.component = cid or "-"
        return True


class JsonFormatter(logging.Formatter):
    """Format records as one JSON object per line.

    Keys: ``ts``, ``level``, ``logger``, ``component``, ``message`` and,
    when present, ``exc`` and ``data`` (from ``extra={"data": {...}}``).
    """

    def __init__(self, *, static: Mapping[str, Any] | None = None) -> None:
        super().__init__()
        self.static = dict(static or {})

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(record.created))
            + f".{int(record.msecs):03d}Z",
            "level": record.levelname.lower(),
            "logger": record.name,
            "component": getattr(record, "component", "-"),
            "message": record.getMessage(),
            **self.static,
        }
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        extra = getattr(record, "data", None)
        if isinstance(extra, Mapping):
            payload["data"] = dict(extra)
        return json.dumps(payload, default=str, ensure_ascii=False)


def configure_logging(
    level: str | int = "INFO",
    *,
    format: Literal["text", "json"] = "text",
    levels: Mapping[str, str | int] | None = None,
    stream: TextIO | None = None,
) -> logging.Handler:
    """(Re)configure the root logger. Idempotent: replaces its own handler."""
    root = logging.getLogger()
    for h in list(root.handlers):
        if getattr(h, _HANDLER_MARK, False):
            root.removeHandler(h)
    handler = logging.StreamHandler(stream or sys.stderr)
    setattr(handler, _HANDLER_MARK, True)
    handler.addFilter(ComponentFilter())
    handler.setFormatter(JsonFormatter() if format == "json" else logging.Formatter(TEXT_FORMAT))
    root.addHandler(handler)
    root.setLevel(_level(level))
    for name, lvl in (levels or {}).items():
        logging.getLogger(name).setLevel(_level(lvl))
    return handler


def _level(value: str | int) -> int:
    if isinstance(value, int):
        return value
    resolved = logging.getLevelName(value.upper())
    if not isinstance(resolved, int):
        raise ValueError(f"unknown log level {value!r}")
    return resolved


@dataclass
class LoggingConfig:
    """Configuration of ``corlet.logging``.

    Attributes:
        level: Root log level.
        format: ``text`` or ``json``.
        levels: Per-logger levels, e.g. ``{"asyncio": "WARNING"}``.
    """

    level: str = "INFO"
    format: Literal["text", "json"] = "text"
    levels: dict[str, str] = field(default_factory=dict)


@component(
    "corlet.logging",
    version="0.1.0",
    description="Configures Python logging from the component configuration",
    config=LoggingConfig,
)
class LoggingComponent(Component):
    """``corlet.logging``: applies `LoggingConfig`; reconfigured in place on reload."""

    async def start(self, ctx: ComponentContext, config: LoggingConfig) -> None:
        self._previous = logging.getLogger().level
        self._apply(config)
        ctx.defer(self._remove)

    async def reconfigure(self, ctx: ComponentContext, config: LoggingConfig) -> bool:
        self._apply(config)
        return True

    def _apply(self, config: LoggingConfig) -> None:
        self._handler = configure_logging(config.level, format=config.format, levels=config.levels)

    def _remove(self) -> None:
        logging.getLogger().removeHandler(self._handler)
        logging.getLogger().setLevel(self._previous)
