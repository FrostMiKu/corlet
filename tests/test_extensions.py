from typing import Protocol, runtime_checkable

import pytest

from corlet import ExtensionContractError, ExtensionPoint, ExtensionRegistry, Strategy
from corlet.core.extensions import Extension


@runtime_checkable
class Plugin(Protocol):
    def run(self, x: int) -> int: ...


class Add:
    def __init__(self, n: int) -> None:
        self.n = n

    def run(self, x: int) -> int:
        return x + self.n


async def test_strategies() -> None:
    reg = ExtensionRegistry()
    collect = ExtensionPoint("p.collect", strategy=Strategy.COLLECT)
    first = ExtensionPoint("p.first", strategy=Strategy.FIRST)
    pipe = ExtensionPoint("p.pipe", strategy=Strategy.PIPELINE)
    broadcast = ExtensionPoint("p.bcast", strategy=Strategy.BROADCAST)
    for p in (collect, first, pipe, broadcast):
        reg.define(p)

    reg.contribute(collect, lambda: 1)
    reg.contribute(collect, lambda: 2)
    assert await reg.invoke(collect) == [1, 2]

    reg.contribute(first, lambda k: None)
    reg.contribute(first, lambda k: k * 2)
    reg.contribute(first, lambda k: 99)
    assert await reg.invoke(first, 4) == 8

    async def double(v: int) -> int:
        return v * 2

    reg.contribute(pipe, lambda v: v + 1)
    reg.contribute(pipe, double)
    assert await reg.invoke(pipe, 3) == 8

    calls: list[str] = []
    reg.contribute(broadcast, lambda: calls.append("a"))
    assert await reg.invoke(broadcast) is None
    assert calls == ["a"]


async def test_method_contract() -> None:
    reg = ExtensionRegistry()
    point = ExtensionPoint("p", contract=Plugin, method="run", strategy=Strategy.PIPELINE)
    reg.define(point)
    reg.contribute(point, Add(1))
    reg.contribute(point, Add(10))
    assert await reg.invoke(point, 0) == 11
    assert reg.invoke_sync(point, 0) == 11
    with pytest.raises(ExtensionContractError):
        reg.contribute(point, object())


def test_ordering_constraints() -> None:
    reg = ExtensionRegistry()
    p = ExtensionPoint("p")
    reg.contribute(p, "c", name="c", owner="comp.c")
    reg.contribute(p, "a", name="a", owner="comp.a", after=["b"])
    reg.contribute(p, "b", name="b", owner="comp.b")
    reg.contribute(p, "z", name="z", order=-5)
    reg.contribute(p, "first", name="first", before=["z"], order=100)
    # constraints win; among ready contributions lower ``order`` goes first
    assert reg.implementations(p) == ["c", "b", "a", "first", "z"]
    reg.contribute(p, "early", name="early", order=-10)
    assert reg.implementations(p)[0] == "early"


def test_before_after_by_owner_and_cycle_fallback() -> None:
    reg = ExtensionRegistry()
    p = ExtensionPoint("p")
    reg.contribute(p, 1, name="x", owner="o1", after=["o2"])
    reg.contribute(p, 2, name="y", owner="o2")
    assert reg.implementations(p) == [2, 1]
    q = ExtensionPoint("q")
    reg.contribute(q, 1, name="x", after=["y"])
    reg.contribute(q, 2, name="y", after=["x"])
    assert sorted(reg.implementations(q)) == [1, 2]  # cycle: still returns everything


async def test_errors_log_mode() -> None:
    reg = ExtensionRegistry()
    p = ExtensionPoint("p")

    def boom() -> int:
        raise RuntimeError("x")

    reg.contribute(p, boom)
    reg.contribute(p, lambda: 1)
    with pytest.raises(RuntimeError):
        await reg.invoke(p)
    assert await reg.invoke(p, errors="log") == [1]


def test_invoke_sync_rejects_async() -> None:
    reg = ExtensionRegistry()
    p = ExtensionPoint("p")

    async def a() -> None: ...

    reg.contribute(p, a)
    with pytest.raises(ExtensionContractError):
        reg.invoke_sync(p)


def test_watch_and_revoke_owner() -> None:
    reg = ExtensionRegistry()
    p = ExtensionPoint("p")
    added: list[Extension] = []
    removed: list[Extension] = []
    reg.watch(p, added.append, removed.append)
    reg.contribute(p, 1, owner="a")
    reg.contribute(p, 2, owner="a")
    reg.contribute(p, 3, owner="b")
    assert reg.revoke_owner("a") == 2
    assert [e.impl for e in added] == [1, 2, 3]
    assert [e.impl for e in removed] == [1, 2]
    assert reg.implementations(p) == [3]


def test_define_conflict() -> None:
    reg = ExtensionRegistry()
    reg.define(ExtensionPoint("p"), owner="a")
    reg.define(ExtensionPoint("p"), owner="a")  # same owner may redefine
    with pytest.raises(ValueError):
        reg.define(ExtensionPoint("p"), owner="b")
