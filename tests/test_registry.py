import asyncio
from typing import Protocol, runtime_checkable

import pytest

from corlet import ServiceNotFound, ServiceRegistry, ServiceUnavailable


@runtime_checkable
class Greeter(Protocol):
    def greet(self) -> str: ...


class Hello:
    def __init__(self, word: str = "hello") -> None:
        self.word = word

    def greet(self) -> str:
        return self.word


def test_ranking_and_dynamic_ref() -> None:
    reg = ServiceRegistry()
    low = reg.register(Greeter, Hello("low"), ranking=0)
    ref = reg.get(Greeter)
    assert ref.greet() == "low"
    high = reg.register(Greeter, Hello("high"), ranking=10)
    assert ref.greet() == "high"  # dynamic ref follows the best provider
    high.unregister()
    assert ref.greet() == "low"
    low.unregister()
    assert not ref.available
    with pytest.raises(ServiceUnavailable):
        ref.greet()


def test_bound_ref_is_invalidated() -> None:
    reg = ServiceRegistry()
    r = reg.register(Greeter, Hello())
    (bound,) = reg.get_all(Greeter)
    assert bound.greet() == "hello"
    reg.register(Greeter, Hello("other"), ranking=5)
    assert bound.greet() == "hello"  # bound refs stick to their registration
    r.unregister()
    with pytest.raises(ServiceUnavailable):
        bound.resolve()


def test_filters_and_properties() -> None:
    reg = ServiceRegistry()
    reg.register(Greeter, Hello("en"), properties={"lang": "en"})
    reg.register(Greeter, Hello("de"), properties={"lang": "de"})
    assert reg.instance(Greeter, {"lang": "de"}).greet() == "de"
    assert len(reg.get_all(Greeter, lambda p: p["lang"] != "fr")) == 2
    assert reg.find(Greeter, {"lang": "fr"}) is None
    with pytest.raises(ServiceNotFound):
        reg.get(Greeter, {"lang": "fr"})


def test_contract_is_checked() -> None:
    reg = ServiceRegistry()
    with pytest.raises(TypeError):
        reg.register(Greeter, object())


def test_string_contracts() -> None:
    reg = ServiceRegistry()
    reg.register("db.url", "sqlite://")
    assert reg.instance("db.url") == "sqlite://"


async def test_watch_replay_add_remove() -> None:
    reg = ServiceRegistry()
    reg.register(Greeter, Hello("a"))
    seen: list[str] = []
    gone: list[str] = []
    w = reg.watch(Greeter, lambda r: seen.append(r.greet()), lambda r: gone.append(r.greet()))
    assert seen == ["a"]
    b = reg.register(Greeter, Hello("b"))
    b.unregister()
    assert seen == ["a", "b"] and gone == ["b"]  # removal callback still sees the service

    async def async_cb(ref: object) -> None:
        seen.append("async")

    reg.watch(Greeter, async_cb)
    await asyncio.sleep(0)
    assert seen[-1] == "async"
    w.cancel()
    reg.register(Greeter, Hello("c"))
    assert "c" not in seen[:3]


def test_owner_cleanup() -> None:
    reg = ServiceRegistry()
    reg.register(Greeter, Hello(), owner="x")
    reg.register("other", 1, owner="x")
    reg.register("other", 2, owner="y")
    assert reg.unregister_owner("x") == 2
    assert len(reg) == 1
