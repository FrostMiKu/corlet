import asyncio
import threading
from dataclasses import dataclass

from corlet import Event, EventBus, HandlerFailed, Message
from corlet.core.events import topic_matches


@dataclass(frozen=True)
class UserEvent(Event):
    topic = "user.base"
    uid: int


@dataclass(frozen=True)
class UserCreated(UserEvent):
    topic = "user.created"


def test_topic_matching() -> None:
    assert topic_matches("user.*", "user.created")
    assert not topic_matches("user.*", "user.created.v2")
    assert topic_matches("user.**", "user.created.v2")
    assert topic_matches("user.**", "user")
    assert topic_matches("**", "anything.at.all")
    assert topic_matches("*.created", "order.created")
    assert not topic_matches("user.created", "user.deleted")


async def test_publish_and_wait_by_topic_and_type() -> None:
    bus = EventBus()
    got: list[str] = []
    bus.subscribe("user.*", lambda e: got.append(f"topic:{e.uid}"))
    bus.subscribe(UserEvent, lambda e: got.append(f"type:{e.uid}"))  # subclasses match

    async def async_handler(e: UserCreated) -> None:
        await asyncio.sleep(0)
        got.append("async")

    bus.subscribe(UserCreated, async_handler)
    result = await bus.publish_and_wait(UserCreated(7))
    assert result.delivered == 3 and result.ok
    assert sorted(got) == ["async", "topic:7", "type:7"]


async def test_handler_isolation() -> None:
    bus = EventBus()
    good: list[object] = []
    failures: list[HandlerFailed] = []

    def bad(e: object) -> None:
        raise ValueError("nope")

    bus.subscribe("x", bad)
    bus.subscribe("x", good.append)
    bus.subscribe(HandlerFailed, failures.append)
    result = await bus.publish_and_wait(Message("x", 1))
    assert len(good) == 1
    assert len(result.errors) == 1
    await bus.drain(1)
    assert failures and failures[0].event_topic == "x"


async def test_fire_and_forget_and_drain() -> None:
    bus = EventBus()
    got: list[object] = []
    bus.subscribe("m", got.append)
    bus.publish(Message("m"))
    assert got == []
    await bus.drain(1)
    assert len(got) == 1


async def test_publish_from_thread() -> None:
    bus = EventBus()
    bus.bind_loop(asyncio.get_running_loop())
    got: list[object] = []
    bus.subscribe("t", got.append)
    th = threading.Thread(target=bus.publish, args=(Message("t"),))
    th.start()
    th.join()
    for _ in range(50):
        await asyncio.sleep(0.01)
        if got:
            break
    assert len(got) == 1


async def test_blocking_handler_runs_in_thread() -> None:
    bus = EventBus()
    main = threading.get_ident()
    seen: list[int] = []
    bus.subscribe("b", lambda e: seen.append(threading.get_ident()), blocking=True)
    await bus.publish_and_wait(Message("b"))
    assert seen and seen[0] != main


async def test_unsubscribe_owner() -> None:
    bus = EventBus()
    bus.subscribe("a", print, owner="c1")
    bus.subscribe("b", print, owner="c1")
    assert bus.unsubscribe_owner("c1") == 2
    assert (await bus.publish_and_wait(Message("a"))).delivered == 0
