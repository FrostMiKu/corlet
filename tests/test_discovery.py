import textwrap
from importlib.metadata import EntryPoint
from pathlib import Path

import pytest

from corlet import (
    Container,
    DirectorySource,
    EntryPointSource,
    ExplicitSource,
    ModuleSource,
    component,
    manifest_of,
)
from corlet.core.errors import InvalidManifestError

COMPONENT_FILE = """
from corlet import Component, component

@component("{cid}")
class C(Component):
    async def start(self, ctx):
        pass
"""


def write(path: Path, cid: str) -> None:
    path.write_text(textwrap.dedent(COMPONENT_FILE.format(cid=cid)))


def test_directory_source(tmp_path: Path) -> None:
    write(tmp_path / "alpha.py", "dir.alpha")
    pkg = tmp_path / "beta"
    pkg.mkdir()
    write(pkg / "__init__.py", "dir.beta")
    (tmp_path / "_private.py").write_text("raise RuntimeError('must be skipped')")
    (tmp_path / "broken.py").write_text("raise RuntimeError('import error')")
    src = DirectorySource(tmp_path)
    ids = sorted(manifest_of(c).id for c in src.discover())
    assert ids == ["dir.alpha", "dir.beta"]
    assert len(src.issues) == 1 and "broken.py" in str(src.issues[0])
    # rediscovery is stable (same classes)
    assert sorted(manifest_of(c).id for c in src.discover()) == ids
    with pytest.raises(RuntimeError):
        DirectorySource(tmp_path, strict=True).discover()


def test_same_file_names_in_two_directories(tmp_path: Path) -> None:
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    write(tmp_path / "a" / "mod.py", "two.a")
    write(tmp_path / "b" / "mod.py", "two.b")
    c = Container(DirectorySource(tmp_path / "a"), DirectorySource(tmp_path / "b"))
    c.assemble()
    assert c.component_ids == ["two.a", "two.b"]


def test_module_source_and_attr() -> None:
    src = ModuleSource("corlet.std.scheduler")
    assert [manifest_of(c).id for c in src.discover()] == ["corlet.scheduler"]
    src = ModuleSource("corlet.std:HealthComponent")
    assert [manifest_of(c).id for c in src.discover()] == ["corlet.health"]
    with pytest.raises(ModuleNotFoundError):
        ModuleSource("does.not.exist").discover()
    lenient = ModuleSource("does.not.exist", strict=False)
    assert lenient.discover() == [] and lenient.issues


def test_module_source_recursive() -> None:
    ids = {manifest_of(c).id for c in ModuleSource("corlet.std", recursive=True).discover()}
    assert {"corlet.scheduler", "corlet.health", "corlet.admin", "corlet.logging"} <= ids


def test_entry_point_source() -> None:
    eps = [
        EntryPoint("sched", "corlet.std.scheduler:SchedulerComponent", "corlet.components"),
        EntryPoint("health", "corlet.std.health", "corlet.components"),
        EntryPoint("bad", "nope.nothing:X", "corlet.components"),
    ]
    src = EntryPointSource(entry_points=lambda: eps)
    ids = sorted(manifest_of(c).id for c in src.discover())
    assert ids == ["corlet.health", "corlet.scheduler"]
    assert len(src.issues) == 1
    only = EntryPointSource(entry_points=lambda: eps, names=["sched"])
    assert [manifest_of(c).id for c in only.discover()] == ["corlet.scheduler"]


def test_explicit_source_factories_and_lists() -> None:
    @component("x.one")
    class One:
        async def start(self, ctx: object) -> None: ...

    @component("x.two")
    class Two:
        async def start(self, ctx: object) -> None: ...

    src = ExplicitSource([One], lambda: [Two])
    assert [manifest_of(c).id for c in src.discover()] == ["x.one", "x.two"]


def test_component_decorator_validation() -> None:
    with pytest.raises(InvalidManifestError):
        component("bad id!")(type("X", (), {"start": lambda self, ctx: None}))
    with pytest.raises(InvalidManifestError):
        component("ok.id")(type("NoStart", (), {}))

    @component
    class Bare:
        """First line becomes the description.

        More text."""

        async def start(self, ctx: object) -> None: ...

    m = manifest_of(Bare)
    assert m.id.endswith("Bare") and m.description == "First line becomes the description."


def test_injection_requires_annotations() -> None:
    from corlet.core.component import effective_manifest

    @component("x.untyped")
    class Untyped:
        async def start(self, ctx, thing):  # type: ignore[no-untyped-def]
            ...

    with pytest.raises(InvalidManifestError, match="type annotation"):
        effective_manifest(Untyped)
