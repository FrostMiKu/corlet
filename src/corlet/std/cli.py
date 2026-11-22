"""The ``corlet`` command line.

::

    corlet -a myapp.components -c app.toml run
    corlet -a ./components --std health,admin check
    corlet -a myapp:create_container graph --format mermaid

Global options select the application:

``-a/--app TARGET`` (repeatable)
    a module (``myapp.components``), an attribute (``myapp:create_container``
    -- a `Container`, a factory returning one, a component or a list),
    a directory of component files, or a ``.py`` file.
``-c/--config FILE``, ``-s/--set key.path=value``, ``--env-prefix``
    layered configuration (see `corlet.std.config`).
``--std NAMES``
    add standard components: ``config, config-watcher, logging, scheduler,
    health, admin, reload`` or ``all``.

Defaults can live in ``pyproject.toml``::

    [tool.corlet]
    app = ["myapp.components"]
    config = ["app.toml", "?local.toml"]
    std = ["logging", "health"]
    disabled = ["myapp.experimental"]

Components can add sub-commands by contributing `CliCommand` objects to
`CLI_COMMANDS` in their manifest; they are read statically, so adding a
command does not require starting the application.
"""

from __future__ import annotations

import argparse
import asyncio
import importlib
import importlib.util
import inspect
import json
import os
import platform
import sys
import tomllib
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType
from typing import Any

from ..core import (
    Container,
    CorletError,
    DirectorySource,
    ExtensionPoint,
    Strategy,
    components_in_module,
    is_component,
    to_dot,
    to_mermaid,
)
from .config import LayeredConfig
from .logging import configure_logging

__all__ = ["CLI_COMMANDS", "CliCommand", "build_container", "main"]


@dataclass(frozen=True)
class CliCommand:
    """A sub-command contributed by a component.

    ``run(container, args)`` may be sync or async and returns an exit code
    (``None`` = 0).  With ``needs_running=True`` the container is started
    before and stopped after the command.
    """

    name: str
    help: str
    run: Callable[[Container, argparse.Namespace], Awaitable[int | None] | int | None]
    configure: Callable[[argparse.ArgumentParser], None] | None = None
    needs_running: bool = True


CLI_COMMANDS: ExtensionPoint[CliCommand] = ExtensionPoint(
    "corlet.cli.commands",
    contract=CliCommand,
    strategy=Strategy.COLLECT,
    description="Sub-commands of the corlet CLI",
)


class UsageError(Exception):
    pass


# ---------------------------------------------------------------------------
# application loading
# ---------------------------------------------------------------------------


def _std_components(names: Sequence[str]) -> list[type]:
    from . import STD_COMPONENTS

    wanted: list[str] = []
    for n in names:
        wanted += [p.strip() for p in n.split(",") if p.strip()]
    if "all" in wanted:
        wanted = [n for n in STD_COMPONENTS if n != "reload"]
    out = []
    for n in wanted:
        if n not in STD_COMPONENTS:
            raise UsageError(
                f"unknown std component {n!r}; choose from {', '.join(STD_COMPONENTS)}"
            )
        out.append(STD_COMPONENTS[n])
    return out


def _import_file(path: Path) -> ModuleType:
    name = f"corlet_app_{path.stem}"
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise UsageError(f"cannot import {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _call_factory(factory: Callable[..., Any], config: LayeredConfig) -> Any:
    try:
        params = inspect.signature(factory).parameters
    except (TypeError, ValueError):
        params = {}  # type: ignore[assignment]
    return factory(config=config) if "config" in params else factory()


def _resolve_target(target: str, config: LayeredConfig) -> list[Any]:
    path = Path(target)
    if path.is_dir():
        return [DirectorySource(path, strict=True)]
    if path.suffix == ".py" and path.is_file():
        module = _import_file(path.resolve())
        found = [v for v in vars(module).values() if isinstance(v, Container)]
        return found[:1] or components_in_module(module)
    modname, _, attr = target.partition(":")
    try:
        module = importlib.import_module(modname)
    except ImportError as exc:
        raise UsageError(f"cannot import {modname!r}: {exc}") from exc
    if not attr:
        comps = components_in_module(module)
        if not comps:
            raise UsageError(f"module {modname!r} defines no components")
        return comps
    obj: Any = module
    for part in attr.split("."):
        try:
            obj = getattr(obj, part)
        except AttributeError:
            raise UsageError(f"{modname!r} has no attribute {attr!r}") from None
    if isinstance(obj, Container) or is_component(obj):
        return [obj]
    if isinstance(obj, list | tuple):
        return list(obj)
    if callable(obj):
        result = _call_factory(obj, config)
        return result if isinstance(result, list) else [result]
    raise UsageError(f"{target!r} is not a container, component, list or factory")


def _pyproject_defaults(cwd: Path) -> dict[str, Any]:
    pp = cwd / "pyproject.toml"
    if not pp.is_file():
        return {}
    try:
        with pp.open("rb") as fh:
            data = tomllib.load(fh)
    except (OSError, ValueError):
        return {}
    section = data.get("tool", {}).get("corlet", {})
    return section if isinstance(section, dict) else {}


def build_container(args: argparse.Namespace) -> Container:
    """Create the application container from parsed global options."""
    defaults = _pyproject_defaults(Path.cwd())
    targets = list(args.app or []) or list(defaults.get("app") or [])
    env_app = os.environ.get("CORLET_APP")
    if not targets and env_app:
        targets = [t for t in env_app.split(",") if t]
    if not targets:
        raise UsageError(
            "no application given: use --app, $CORLET_APP or [tool.corlet] app in pyproject.toml"
        )
    files = list(args.config or []) or list(defaults.get("config") or [])
    config = LayeredConfig(
        files=files,
        env_prefix=args.env_prefix or None,
        overrides=list(args.set or []),
    )
    if str(Path.cwd()) not in sys.path:
        sys.path.insert(0, str(Path.cwd()))
    items: list[Any] = []
    for t in targets:
        items.extend(_resolve_target(t, config))
    containers = [i for i in items if isinstance(i, Container)]
    std = _std_components(list(args.std or []) or list(defaults.get("std") or []))
    disabled = list(args.disable or []) + list(defaults.get("disabled") or [])
    if containers:
        if len(containers) > 1 or len(items) > 1:
            raise UsageError("a container target must be the only --app target")
        container = containers[0]
        if std:
            container.add(*std)
        for cid in disabled:
            container._ctor_disabled.add(cid)
        if args.strict:
            container.strict = True
        return container
    return Container(
        *items,
        *std,
        name=Path.cwd().name or "corlet",
        config=config,
        disabled=disabled,
        strict=args.strict or bool(defaults.get("strict", False)),
        entry_points=args.entry_points,
    )


# ---------------------------------------------------------------------------
# commands
# ---------------------------------------------------------------------------


def _table(rows: list[list[str]], headers: list[str]) -> str:
    widths = [max(len(str(x)) for x in col) for col in zip(headers, *rows, strict=False)]
    fmt = "  ".join(f"{{:<{w}}}" for w in widths)
    lines = [fmt.format(*headers), fmt.format(*("-" * w for w in widths))]
    lines += [fmt.format(*r) for r in rows]
    return "\n".join(line.rstrip() for line in lines)


def _print_diagnostics(diags: Sequence[Any]) -> None:
    for d in diags:
        print(f"  {d}")


def cmd_check(container: Container, args: argparse.Namespace) -> int:
    diags = container.check()
    errors = [d for d in diags if d.severity == "error"]
    res = container.resolution
    print(
        f"{len(container.component_ids)} component(s), {len(res.wanted)} runnable, "
        f"{len(res.layers)} layer(s)"
    )
    if diags:
        _print_diagnostics(diags)
    print("FAILED" if errors else "OK")
    return 1 if errors else 0


def cmd_list(container: Container, args: argparse.Namespace) -> int:
    snap = container.inspect()
    if args.json:
        print(json.dumps(snap.to_dict()["components"], indent=2))
        return 0
    rows = [
        [
            c.id,
            c.version,
            c.state.value,
            c.activation,
            ", ".join(p.rsplit(".", 1)[-1] for p in c.provides) or "-",
            c.reason or "",
        ]
        for c in snap.components
    ]
    print(_table(rows, ["ID", "VERSION", "STATE", "ACTIVATION", "PROVIDES", "REASON"]))
    return 0


def cmd_graph(container: Container, args: argparse.Namespace) -> int:
    snap = container.inspect()
    if args.format == "dot":
        print(to_dot(snap))
    elif args.format == "mermaid":
        print(to_mermaid(snap))
    else:
        for i, layer in enumerate(snap.layers):
            print(f"layer {i}:")
            for cid in layer:
                info = snap.component(cid)
                deps = ", ".join(f"{d.target}{'?' if d.optional else ''}" for d in info.depends_on)
                print(f"  {cid}" + (f"  <- {deps}" if deps else ""))
        layered = {cid for layer in snap.layers for cid in layer}
        others = [c for c in snap.components if c.id not in layered]
        if others:
            print("not runnable:")
        for c in others:
            print(f"  ({c.state.value}) {c.id}" + (f": {c.reason}" if c.reason else ""))
    return 0


def cmd_inspect(container: Container, args: argparse.Namespace) -> int:
    print(json.dumps(container.inspect().to_dict(), indent=2))
    return 0


def cmd_config(container: Container, args: argparse.Namespace) -> int:
    container.assemble()
    if args.component:
        try:
            value = container.config_of(args.component)
        except KeyError as exc:
            raise UsageError(str(exc)) from None
        dump = getattr(value, "model_dump", None)
        if callable(dump):
            value = dump()
        elif hasattr(value, "__dataclass_fields__"):
            import dataclasses

            value = dataclasses.asdict(value)
        print(
            json.dumps(
                {"raw": container.raw_config(args.component), "effective": value},
                indent=2,
                default=str,
            )
        )
    else:
        print(json.dumps(container.config_tree, indent=2, default=str))
    return 0


def cmd_doctor(container: Container, args: argparse.Namespace) -> int:
    print(f"python   {platform.python_version()} ({sys.executable})")
    from .. import __version__

    print(f"corlet   {__version__}")
    issues: list[Any] = []
    for src in container._sources:
        issues.extend(getattr(src, "issues", []))
    print(f"sources  {len(container._sources)}")
    for src in container._sources:
        print(f"  - {src!r}")
    if issues:
        print("discovery issues:")
        for i in issues:
            print(f"  - {i}")
    print()
    rc = cmd_check(container, args)
    return 1 if issues else rc


async def _run_app(container: Container, args: argparse.Namespace) -> int:
    return await container.serve()


BUILTIN: dict[str, tuple[str, Callable[..., Any]]] = {
    "run": ("start the application and run until interrupted", _run_app),
    "check": ("validate the assembly and configuration without starting", cmd_check),
    "list": ("list installed components", cmd_list),
    "graph": ("show the dependency graph", cmd_graph),
    "inspect": ("print a JSON snapshot of the assembled container", cmd_inspect),
    "config": ("show the configuration tree or a component's effective config", cmd_config),
    "doctor": ("diagnose environment, discovery and assembly problems", cmd_doctor),
}


def _global_parser(add_help: bool) -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="corlet", description="corlet microkernel application runner", add_help=add_help
    )
    p.add_argument(
        "-a",
        "--app",
        action="append",
        metavar="TARGET",
        help="module, module:attr, directory or .py file (repeatable)",
    )
    p.add_argument(
        "-c",
        "--config",
        action="append",
        metavar="FILE",
        help="configuration file; prefix with '?' if optional (repeatable)",
    )
    p.add_argument(
        "-s",
        "--set",
        action="append",
        metavar="KEY=VALUE",
        help="configuration override, e.g. components.acme.db.port=5433",
    )
    p.add_argument(
        "--env-prefix",
        default="CORLET",
        metavar="PREFIX",
        help="environment variable prefix for configuration ('' to disable)",
    )
    p.add_argument(
        "--std",
        action="append",
        metavar="NAMES",
        help="standard components to add (comma separated, or 'all')",
    )
    p.add_argument("--disable", action="append", metavar="ID", help="disable a component")
    p.add_argument("--strict", action="store_true", help="enforce manifests at runtime")
    p.add_argument(
        "--entry-points",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="discover installed components via entry points (default: on)",
    )
    p.add_argument("--log-level", default=os.environ.get("CORLET_LOG_LEVEL", "INFO"))
    p.add_argument("--log-format", choices=["text", "json"], default="text")
    return p


def _contributed(container: Container) -> list[CliCommand]:
    container.assemble()
    out: list[CliCommand] = []
    for cid in container.component_ids:
        if not container.is_enabled(cid):
            continue
        for spec in container.manifest(cid).extensions:
            if spec.point_id == CLI_COMMANDS.id and isinstance(spec.impl, CliCommand):
                out.append(spec.impl)
    return out


def main(argv: Sequence[str] | None = None) -> int:
    """Entry point of the ``corlet`` command; returns the process exit code."""
    argv = list(sys.argv[1:] if argv is None else argv)
    pre, _ = _global_parser(add_help=False).parse_known_args(argv)
    configure_logging(pre.log_level, format=pre.log_format)

    container: Container | None = None
    load_error: BaseException | None = None
    wants_help_only = not argv or argv in (["-h"], ["--help"])
    if not wants_help_only:
        try:
            container = build_container(pre)
        except (UsageError, CorletError, ImportError) as exc:
            load_error = exc

    parser = _global_parser(add_help=True)
    sub = parser.add_subparsers(dest="command", metavar="COMMAND")
    for name, (help_text, _fn) in BUILTIN.items():
        sp = sub.add_parser(name, help=help_text)
        if name == "graph":
            sp.add_argument("--format", choices=["text", "dot", "mermaid"], default="text")
        if name == "list":
            sp.add_argument("--json", action="store_true")
        if name == "config":
            sp.add_argument("component", nargs="?")
    contributed: dict[str, CliCommand] = {}
    if container is not None:
        try:
            for cmd in _contributed(container):
                if cmd.name in BUILTIN or cmd.name in contributed:
                    print(f"warning: ignoring duplicate command {cmd.name!r}", file=sys.stderr)
                    continue
                sp = sub.add_parser(cmd.name, help=cmd.help)
                if cmd.configure:
                    cmd.configure(sp)
                contributed[cmd.name] = cmd
        except CorletError as exc:
            load_error = exc
            container = None

    args = parser.parse_args(argv)
    if args.command is None:
        parser.print_help()
        return 2
    if container is None:
        print(f"error: {load_error}", file=sys.stderr)
        return 2

    try:
        if args.command in contributed:
            return _run_contributed(container, contributed[args.command], args)
        _help, fn = BUILTIN[args.command]
        result = fn(container, args)
        if inspect.isawaitable(result):
            result = asyncio.run(_finish(result))
        return int(result or 0)
    except UsageError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except CorletError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130


async def _finish(aw: Awaitable[Any]) -> Any:
    return await aw


def _run_contributed(container: Container, cmd: CliCommand, args: argparse.Namespace) -> int:
    async def go() -> int:
        if cmd.needs_running:
            await container.start()
        try:
            result = cmd.run(container, args)
            if inspect.isawaitable(result):
                result = await result
            return int(result or 0)
        finally:
            if cmd.needs_running:
                await container.stop()

    return asyncio.run(go())


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
