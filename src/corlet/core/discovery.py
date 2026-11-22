"""Component discovery: where component classes come from.

A `ComponentSource` yields component classes.  Sources compose:

* `ExplicitSource`   -- classes passed in code (tests, embedding)
* `ModuleSource`     -- import modules and collect their components
* `EntryPointSource` -- installed distributions advertising the
  ``corlet.components`` entry point group (the production path: ``pip install``
  is the component installer)
* `DirectorySource`  -- ``*.py`` files / packages in a directory
"""

from __future__ import annotations

import hashlib
import importlib
import importlib.metadata
import importlib.util
import inspect
import logging
import sys
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from types import ModuleType
from typing import Any, Protocol, runtime_checkable

from .component import is_component

__all__ = [
    "ComponentSource",
    "DirectorySource",
    "DiscoveryIssue",
    "EntryPointSource",
    "ExplicitSource",
    "ModuleSource",
    "components_in_module",
]

log = logging.getLogger("corlet.discovery")
ENTRY_POINT_GROUP = "corlet.components"


@dataclass
class DiscoveryIssue:
    """A target that could not be loaded by a lenient (non-strict) source."""

    source: str
    target: str
    error: BaseException = field(repr=False)

    def __str__(self) -> str:
        return (
            f"{self.source}: cannot load {self.target}: {type(self.error).__name__}: {self.error}"
        )


@runtime_checkable
class ComponentSource(Protocol):
    """Protocol of component sources: anything with a ``discover()`` method."""

    def discover(self) -> Iterable[type]:
        """Return the component classes this source provides."""
        ...


def components_in_module(module: ModuleType, *, own_only: bool = True) -> list[type]:
    """Component classes defined in ``module`` (declaration order)."""
    found = []
    for obj in vars(module).values():
        if is_component(obj) and (not own_only or obj.__module__ == module.__name__):
            found.append(obj)
    return found


def _expand(obj: Any) -> list[type]:
    """Turn an entry point / explicit target into component classes."""
    if isinstance(obj, ModuleType):
        return components_in_module(obj)
    if is_component(obj):
        return [obj]
    if isinstance(obj, list | tuple | set | frozenset):
        return [c for item in obj for c in _expand(item)]
    if callable(obj) and not inspect.isclass(obj):
        return _expand(obj())
    raise TypeError(f"{obj!r} is not a component, module, sequence or factory")


class ExplicitSource:
    """Components passed directly in code.

    Accepts component classes, modules (all their components),
    sequences, and zero-argument factories returning any of these.
    """

    def __init__(self, *components: Any) -> None:
        self.components = components

    def discover(self) -> list[type]:
        """Return the given components."""
        return [c for item in self.components for c in _expand(item)]

    def __repr__(self) -> str:
        return f"ExplicitSource({len(self.components)})"


class _IssueCollector:
    issues: list[DiscoveryIssue]
    strict: bool

    def _fail(self, target: str, exc: BaseException) -> None:
        issue = DiscoveryIssue(repr(self), target, exc)
        if self.strict:
            raise exc
        log.error("%s", issue)
        self.issues.append(issue)


class ModuleSource(_IssueCollector):
    """Import modules by dotted name and collect the components they define.

    ``"pkg.mod:Name"`` selects a single attribute.  With ``recursive=True``
    sub-modules of packages are scanned too.
    """

    def __init__(self, *modules: str, recursive: bool = False, strict: bool = True) -> None:
        self.modules = modules
        self.recursive = recursive
        self.strict = strict
        self.issues = []

    def discover(self) -> list[type]:
        """Import the modules and return their components."""
        found: list[type] = []
        for spec in self.modules:
            try:
                found.extend(self._load(spec))
            except Exception as exc:
                self._fail(spec, exc)
        return found

    def _load(self, spec: str) -> list[type]:
        modname, _, attr = spec.partition(":")
        mod = importlib.import_module(modname)
        if attr:
            obj: Any = mod
            for part in attr.split("."):
                obj = getattr(obj, part)
            return _expand(obj)
        result = components_in_module(mod)
        if self.recursive and hasattr(mod, "__path__"):
            import pkgutil

            for info in pkgutil.walk_packages(mod.__path__, prefix=mod.__name__ + "."):
                try:
                    result.extend(components_in_module(importlib.import_module(info.name)))
                except Exception as exc:
                    self._fail(info.name, exc)
        return result

    def __repr__(self) -> str:
        return f"ModuleSource({', '.join(self.modules)})"


class EntryPointSource(_IssueCollector):
    """Components advertised by installed distributions.

    In the distribution's ``pyproject.toml``::

        [project.entry-points."corlet.components"]
        auth = "acme_auth:AuthComponent"      # a class
        billing = "acme_billing"              # a module (all its components)
    """

    def __init__(
        self,
        group: str = ENTRY_POINT_GROUP,
        *,
        names: Iterable[str] | None = None,
        strict: bool = False,
        entry_points: Callable[[], Iterable[importlib.metadata.EntryPoint]] | None = None,
    ) -> None:
        self.group = group
        self.names = set(names) if names is not None else None
        self.strict = strict
        self.issues = []
        self._entry_points = entry_points

    def _eps(self) -> Iterable[importlib.metadata.EntryPoint]:
        if self._entry_points is not None:
            return self._entry_points()
        return importlib.metadata.entry_points(group=self.group)

    def discover(self) -> list[type]:
        """Load the entry points and return their components."""
        found: list[type] = []
        for ep in self._eps():
            if self.names is not None and ep.name not in self.names:
                continue
            try:
                found.extend(_expand(ep.load()))
            except Exception as exc:
                self._fail(f"{ep.name} = {ep.value}", exc)
        return found

    def __repr__(self) -> str:
        return f"EntryPointSource({self.group})"


class DirectorySource(_IssueCollector):
    """Import ``*.py`` files and packages found directly in ``path``.

    Modules are imported under a synthetic, path-stable package name
    (``corlet_dir_<hash>.<stem>``) so that two directories may contain files
    with the same name.  Files starting with ``_`` or ``.`` are skipped.
    """

    def __init__(self, path: str | Path, *, strict: bool = False) -> None:
        self.path = Path(path).resolve()
        self.strict = strict
        self.issues = []
        digest = hashlib.sha1(str(self.path).encode()).hexdigest()[:10]
        self.package = f"corlet_dir_{digest}"

    def _ensure_package(self) -> None:
        if self.package in sys.modules:
            return
        pkg = ModuleType(self.package)
        pkg.__path__ = [str(self.path)]
        pkg.__package__ = self.package
        sys.modules[self.package] = pkg

    def _candidates(self) -> Iterator[tuple[str, Path]]:
        if not self.path.is_dir():
            return
        for entry in sorted(self.path.iterdir()):
            if entry.name.startswith(("_", ".")):
                continue
            if entry.is_file() and entry.suffix == ".py":
                yield entry.stem, entry
            elif entry.is_dir() and (entry / "__init__.py").exists():
                yield entry.name, entry / "__init__.py"

    def discover(self) -> list[type]:
        """Import the directory's modules and return their components."""
        if not self.path.is_dir():
            self._fail(str(self.path), FileNotFoundError(f"not a directory: {self.path}"))
            return []
        self._ensure_package()
        found: list[type] = []
        for stem, file in self._candidates():
            name = f"{self.package}.{stem}"
            try:
                mod = sys.modules.get(name)
                if mod is None:
                    is_pkg = file.name == "__init__.py"
                    spec = importlib.util.spec_from_file_location(
                        name,
                        file,
                        submodule_search_locations=[str(file.parent)] if is_pkg else None,
                    )
                    if spec is None or spec.loader is None:
                        raise ImportError(f"cannot create import spec for {file}")
                    mod = importlib.util.module_from_spec(spec)
                    sys.modules[name] = mod
                    try:
                        spec.loader.exec_module(mod)
                    except BaseException:
                        sys.modules.pop(name, None)
                        raise
                found.extend(components_in_module(mod))
            except Exception as exc:
                self._fail(str(file), exc)
        return found

    def __repr__(self) -> str:
        return f"DirectorySource({self.path})"
