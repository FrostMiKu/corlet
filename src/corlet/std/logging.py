"""Logging integration.

corlet logs to the ``corlet`` logger hierarchy and leaves output to the
application.  This module provides the pieces to set it up:

* `ComponentFilter` stamps every record with ``record.component`` --
  the id of the component on whose behalf the code runs (tracked through a
  context variable, so it also works inside spawned tasks and event handlers).
* `JsonFormatter` emits one JSON object per line.
* `configure_logging()` installs a handler with both on the root logger and
  writes to stderr, stdout, a file, a handler of your choice, or nowhere.
* `LoggingComponent` (``corlet.logging``) applies its component
  configuration and re-applies it on reload.
"""

from __future__ import annotations

import json
import logging
import sys
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from ..core import Component, ComponentContext, component
from ..core._util import current_component

__all__ = [
    "ComponentFilter",
    "JsonFormatter",
    "LoggingComponent",
    "LoggingConfig",
    "Output",
    "configure_logging",
]

Output = Literal["stderr", "stdout", "file", "none"]
"""Where `configure_logging()` writes records."""

# Attribute set on handlers installed by configure_logging():
# "owned" (created here, closed on replacement) or "external" (passed in).
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
    output: Output = "stderr",
    file: str | Path | None = None,
    handler: logging.Handler | None = None,
) -> logging.Handler | None:
    """(Re)configure the root logger.

    Idempotent: the handler installed by a previous call is removed (and
    closed, if this function created it) before the new one is added.

    Args:
        level: Level of the root logger.
        format: ``text`` or ``json`` (see `JsonFormatter`).
        levels: Per-logger levels, e.g. ``{"asyncio": "WARNING"}``.
        output: ``stderr``, ``stdout``, ``file`` (requires ``file``) or
            ``none`` to install no handler at all.
        file: Log file path for ``output="file"``; ``~`` is expanded and
            missing parent directories are created.  Records are appended.
        handler: Use this handler instead of ``output`` -- for example one
            that feeds a log panel of a TUI.  It receives `ComponentFilter`,
            and the formatter for ``format`` unless it already has one.

    Returns:
        The installed handler, or ``None`` for ``output="none"``.

    Raises:
        ValueError: unknown level or output, or ``output="file"`` without ``file``.
    """
    new: logging.Handler | None
    if handler is not None:
        new, mark = handler, "external"
    elif output == "stderr":
        new, mark = logging.StreamHandler(sys.stderr), "owned"
    elif output == "stdout":
        new, mark = logging.StreamHandler(sys.stdout), "owned"
    elif output == "file":
        if not file:
            raise ValueError("output='file' requires a file path")
        path = Path(file).expanduser()
        path.parent.mkdir(parents=True, exist_ok=True)
        new, mark = logging.FileHandler(path, encoding="utf-8"), "owned"
    elif output == "none":
        new, mark = None, "owned"
    else:
        raise ValueError(f"unknown log output {output!r}")
    root_level = _level(level)
    per_logger = {name: _level(lvl) for name, lvl in (levels or {}).items()}

    root = logging.getLogger()
    for h in list(root.handlers):
        previous = getattr(h, _HANDLER_MARK, None)
        if previous:
            root.removeHandler(h)
            if previous == "owned":
                h.close()
    if new is not None:
        setattr(new, _HANDLER_MARK, mark)
        new.addFilter(ComponentFilter())
        if mark == "owned" or new.formatter is None:
            new.setFormatter(
                JsonFormatter() if format == "json" else logging.Formatter(TEXT_FORMAT)
            )
        root.addHandler(new)
    root.setLevel(root_level)
    for name, lvl in per_logger.items():
        logging.getLogger(name).setLevel(lvl)
    return new


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
        output: ``stderr``, ``stdout``, ``file`` or ``none``.
        file: Log file path, required for ``output = "file"``.
    """

    level: str = "INFO"
    format: Literal["text", "json"] = "text"
    levels: dict[str, str] = field(default_factory=dict)
    output: Output = "stderr"
    file: str | None = None

    def __post_init__(self) -> None:
        if self.output == "file" and not self.file:
            raise ValueError("file: required when output is 'file'")


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
        self._handler = configure_logging(
            config.level,
            format=config.format,
            levels=config.levels,
            output=config.output,
            file=config.file,
        )

    def _remove(self) -> None:
        root = logging.getLogger()
        if self._handler is not None:
            root.removeHandler(self._handler)
            self._handler.close()
        root.setLevel(self._previous)
