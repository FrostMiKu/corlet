"""Container lifecycle: the heart of the framework."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

import pydantic
import pytest

from corlet import (
    Component,
    ComponentContext,
    ComponentFailed,
    ComponentState,
    ComponentStateChanged,
    Container,
    ContainerState,
    ExtensionPoint,
    LifecycleError,
    Message,
    PermissionDenied,
    ResolutionError,
    RestartPolicy,
    ServiceNotFound,
    ServiceRef,
    component,
    contribution,
    optional,
)
from corlet.testing import EventRecorder, assert_no_leaks, leaks, running, wait_for_state

S = ComponentState


@runtime_checkable
class Database(Protocol):
    def query(self, q: str) -> str: ...


@runtime_checkable
class Cache(Protocol):
    def get(self, k: str) -> Any: ...


class MemDB:
    def __init__(self, name: str = "mem") -> None:
        self.name = name

    def query(self, q: str) -> str:
        return f"{self.name}:{q}"


class DictCache(dict[str, Any]):
    pass


journal: list[str] = []


@pytest.fixture(autouse=True)
def _clear_journal() -> None:
    journal.clear()


def make_db(cid: str = "t.db", *, ranking: int = 0, name: str = "mem", **kw: Any) -> type:
    @component(cid, provides=[Database], **kw)
    class DbComponent(Component):
        async def start(self, ctx: ComponentContext) -> None:
            journal.append(f"start {cid}")
            ctx.services.register(Database, MemDB(name), ranking=ranking)

        async def stop(self, ctx: ComponentContext) -> None:
            journal.append(f"stop {cid}")

    return DbComponent


@component("t.repo", requires_services=[Database])
class Repo(Component):
    async def start(self, ctx: ComponentContext, db: Database) -> None:
        journal.append("start t.repo")
        self.db = db

    async def stop(self, ctx: ComponentContext) -> None:
        journal.append("stop t.repo")


@component("t.api", requires=["t.repo"])
class Api(Component):
    async def start(self, ctx: ComponentContext) -> None:
        journal.append("start t.api")

    async def stop(self, ctx: ComponentContext) -> None:
        journal.append("stop t.api")


async def test_start_order_injection_and_reverse_stop() -> None:
    db = make_db()
    async with running(Api, Repo, db) as c:
        assert journal == ["start t.db", "start t.repo", "start t.api"]
        assert c.resolution.layers == [["t.db"], ["t.repo"], ["t.api"]]
        assert c.instance_of("t.repo").db.query("x") == "mem:x"
        assert all(c.state_of(i) is S.ACTIVE for i in c.component_ids)
    assert journal[3:] == ["stop t.api", "stop t.repo", "stop t.db"]
    assert all(c.state_of(i) is S.STOPPED for i in c.component_ids)
    assert c.state is ContainerState.STOPPED


async def test_layers_start_concurrently() -> None:
    gate = asyncio.Event()
    arrived: list[str] = []

    def make(cid: str) -> type:
        @component(cid)
        class Waiter(Component):
            async def start(self, ctx: ComponentContext) -> None:
                arrived.append(cid)
                if len(arrived) == 2:
                    gate.set()
                await asyncio.wait_for(gate.wait(), 1)  # deadlocks if sequential

        return Waiter

    async with running(make("w.a"), make("w.b")):
        assert sorted(arrived) == ["w.a", "w.b"]


async def test_disable_cascades_and_enable_restores() -> None:
    db = make_db()
    async with running(Api, Repo, db) as c:
        changes = await c.disable("t.db")
        assert changes.stopped == ("t.api", "t.repo", "t.db")
        assert c.state_of("t.db") is S.DISABLED
        assert c.state_of("t.repo") is S.UNSATISFIED
        assert "t.db (disabled)" in (c.inspect().component("t.repo").reason or "")
        assert c.state_of("t.api") is S.UNSATISFIED
        assert_no_leaks(c, "t.db")

        changes = await c.enable("t.db")
        assert changes.started == ("t.db", "t.repo", "t.api")
        assert all(c.state_of(i) is S.ACTIVE for i in c.component_ids)


async def test_disable_one_of_two_providers_restarts_static_consumers() -> None:
    pg, lite = make_db("t.pg", ranking=10, name="pg"), make_db("t.lite", name="lite")
    async with running(Repo, pg, lite) as c:
        assert c.instance_of("t.repo").db.name == "pg"
        changes = await c.disable("t.pg")
        # the consumer held a raw instance of the departing provider: restarted
        assert changes.stopped == ("t.repo", "t.pg")
        assert changes.started == ("t.repo",)
        assert c.instance_of("t.repo").db.name == "lite"


async def test_disabled_via_config_and_constructor() -> None:
    db = make_db()
    async with running(Repo, db, config={"container": {"disabled": ["t.repo"]}}) as c:
        assert c.state_of("t.repo") is S.DISABLED
        assert c.state_of("t.db") is S.ACTIVE
    async with running(Repo, db, disabled=["t.db"]) as c:
        assert c.state_of("t.repo") is S.UNSATISFIED


async def test_allow_list() -> None:
    async with running(Api, Repo, make_db(), enabled=["t.db"]) as c:
        assert c.state_of("t.db") is S.ACTIVE
        assert c.state_of("t.api") is S.DISABLED


async def test_resolution_error_fails_fast() -> None:
    c = Container(Repo)  # nobody provides Database
    with pytest.raises(ResolutionError) as info:
        await c.start()
    assert info.value.diagnostics[0].code == "missing-service"
    assert c.state is ContainerState.FAILED


async def test_degraded_start_without_fail_fast() -> None:
    async with running(Repo, Api, fail_fast=False) as c:
        assert c.state_of("t.repo") is S.UNSATISFIED
        assert c.state_of("t.api") is S.UNSATISFIED


@component("t.broken")
class Broken(Component):
    async def start(self, ctx: ComponentContext) -> None:
        ctx.services.register("leaky", object())  # must be rolled back
        raise RuntimeError("boom")


@component("t.needs_broken", requires=["t.broken"])
class NeedsBroken(Component):
    async def start(self, ctx: ComponentContext) -> None:
        journal.append("never")


async def test_start_failure_fail_fast() -> None:
    c = Container(Broken, make_db())
    with pytest.raises(ComponentFailed) as info:
        await c.start()
    assert info.value.component_id == "t.broken"
    assert c.state is ContainerState.FAILED
    assert journal == ["start t.db", "stop t.db"]  # others were stopped again
    assert not c.services.has("leaky")


async def test_start_failure_degraded_blocks_dependents() -> None:
    async with running(Broken, NeedsBroken, make_db(), fail_fast=False) as c:
        assert c.state_of("t.broken") is S.FAILED
        assert isinstance(c.error_of("t.broken"), RuntimeError)
        assert c.state_of("t.needs_broken") is S.UNSATISFIED
        assert "failed" in (c.inspect().component("t.needs_broken").reason or "")
        assert c.state_of("t.db") is S.ACTIVE
        assert "never" not in journal
        assert not c.services.has("leaky")


async def test_start_timeout() -> None:
    @component("t.slow", start_timeout=0.05)
    class Slow(Component):
        async def start(self, ctx: ComponentContext) -> None:
            await asyncio.sleep(10)

    async with running(Slow, fail_fast=False) as c:
        assert c.state_of("t.slow") is S.FAILED
        assert "did not finish" in str(c.error_of("t.slow"))


async def test_supervisor_restarts_crashed_component() -> None:
    attempts: list[int] = []

    @component("t.flaky", restart=RestartPolicy.on_failure(backoff=0.01, max_restarts=5))
    class Flaky(Component):
        async def start(self, ctx: ComponentContext) -> None:
            attempts.append(1)
            if len(attempts) == 1:
                ctx.spawn(self.crash())

        async def crash(self) -> None:
            await asyncio.sleep(0.01)
            raise RuntimeError("crashed")

    @component("t.dep", requires=["t.flaky"])
    class Dep(Component):
        async def start(self, ctx: ComponentContext) -> None:
            journal.append("dep start")

    async with running(Flaky, Dep) as c:
        rec = EventRecorder(c)
        await rec.wait_for(lambda e: isinstance(e, ComponentStateChanged) and e.new is S.FAILED)
        await wait_for_state(c, "t.flaky", S.ACTIVE)
        await wait_for_state(c, "t.dep", S.ACTIVE)
        assert len(attempts) == 2
        assert journal == ["dep start", "dep start"]  # dependent restarted with it
        assert c.inspect().component("t.flaky").restarts == 1
        rec.close()


async def test_supervisor_gives_up() -> None:
    @component("t.always_fails", restart=RestartPolicy.on_failure(backoff=0.001, max_restarts=2))
    class AlwaysFails(Component):
        async def start(self, ctx: ComponentContext) -> None:
            journal.append("try")
            raise RuntimeError("no")

    async with running(AlwaysFails) as c:  # fail_fast ignores supervised components
        for _ in range(100):
            await asyncio.sleep(0.005)
            if "restart limit" in (c.inspect().component("t.always_fails").reason or ""):
                break
        assert journal == ["try", "try", "try"]
        assert c.state_of("t.always_fails") is S.FAILED
        # manual restart is still possible
        await c.restart("t.always_fails")
        assert journal.count("try") == 4


async def test_restart_command_restarts_dependents() -> None:
    async with running(Api, Repo, make_db()) as c:
        journal.clear()
        changes = await c.restart("t.repo")
        assert set(changes.stopped) == {"t.repo", "t.api"}
        assert journal == ["stop t.api", "stop t.repo", "start t.repo", "start t.api"]


async def test_optional_dependency_degrades_gracefully() -> None:
    @component("t.consumer", requires_services=[optional(Cache)])
    class Consumer(Component):
        async def start(self, ctx: ComponentContext, cache: ServiceRef[Cache]) -> None:
            self.cache = cache

    @component("t.cache", provides=[Cache])
    class CacheComp(Component):
        async def start(self, ctx: ComponentContext) -> None:
            ctx.services.register(Cache, DictCache(a=1))

    async with running(Consumer, CacheComp) as c:
        consumer = c.instance_of("t.consumer")
        assert consumer.cache.get("a") == 1
        await c.disable("t.cache")
        assert c.state_of("t.consumer") is S.ACTIVE  # optional: keeps running
        assert not consumer.cache.available
        await c.enable("t.cache")
        assert consumer.cache.get("a") == 1  # dynamic ref sees the provider again


async def test_injection_of_optional_missing_service() -> None:
    @component("t.opt")
    class Opt(Component):
        async def start(self, ctx: ComponentContext, cache: Cache | None = None) -> None:
            self.cache = cache

    async with running(Opt) as c:
        assert c.instance_of("t.opt").cache is None
        assert c.manifest("t.opt").requires_services[0].optional


async def test_lazy_component_activated_on_demand() -> None:
    db = make_db("t.lazydb", activation="lazy")

    @component("t.user")
    class User(Component):
        async def start(self, ctx: ComponentContext) -> None:
            ctx.spawn(self.later(ctx))

        async def later(self, ctx: ComponentContext) -> None:
            await asyncio.sleep(0.01)
            ref = await ctx.services.acquire(Database)
            journal.append(ref.query("q"))

    async with running(db, User) as c:
        assert c.state_of("t.lazydb") is S.RESOLVED
        for _ in range(100):
            await asyncio.sleep(0.005)
            if len(journal) == 2:
                break
        assert journal == ["start t.lazydb", "mem:q"]
        assert c.state_of("t.lazydb") is S.ACTIVE


async def test_lazy_dependency_follows_its_dependents() -> None:
    db = make_db("t.lazydb", activation="lazy")
    async with running(db, Repo) as c:
        assert c.state_of("t.lazydb") is S.ACTIVE  # pulled in by the eager repo
        await c.disable("t.repo")
        assert c.state_of("t.lazydb") is S.RESOLVED  # no longer needed: stopped


async def test_lazy_activation_during_start_via_injection() -> None:
    db = make_db("t.lazydb", activation="lazy")

    @component("t.undeclared")
    class Undeclared(Component):
        async def start(self, ctx: ComponentContext) -> None:
            ref = await ctx.services.acquire(Database)  # nested activation
            journal.append(ref.query("n"))

    async with running(db, Undeclared) as c:
        assert journal == ["start t.lazydb", "mem:n"]
        assert c.state_of("t.lazydb") is S.ACTIVE


async def test_service_not_found_message_mentions_disabled_provider() -> None:
    @component("t.lookup")
    class Lookup(Component):
        async def start(self, ctx: ComponentContext) -> None:
            pass

    async with running(Lookup, make_db(), disabled=["t.db"]) as c:
        with pytest.raises(ServiceNotFound, match=r"t\.db \(disabled\)"):
            c.get(Database)


async def test_auto_cleanup_of_everything_a_component_owns() -> None:
    point = ExtensionPoint("t.point")
    cleaned: list[str] = []

    @component("t.owner", defines=[point])
    class Owner(Component):
        async def start(self, ctx: ComponentContext) -> None:
            ctx.services.register("svc", 1)
            ctx.extensions.contribute(point, "x")
            ctx.events.subscribe("any", lambda e: None)
            ctx.services.watch("svc", lambda r: None)
            ctx.extensions.watch(point, lambda e: None)
            self.task = ctx.spawn(asyncio.sleep(100))
            ctx.defer(lambda: cleaned.append("sync"))

            async def acleanup() -> None:
                cleaned.append("async")

            ctx.defer(acleanup)
            # no stop() method at all

    async with running(Owner) as c:
        assert c.context_of("t.owner").owns() == {  # type: ignore[union-attr]
            "ServiceRegistration": 1,
            "Extension": 1,
            "Subscription": 1,
            "ServiceWatch": 1,
            "ExtensionWatch": 1,
            "Task": 1,
        }
        task = c.instance_of("t.owner").task
        await c.disable("t.owner")
        assert leaks(c) == {}
        assert task.cancelled()
        assert cleaned == ["async", "sync"]  # LIFO


async def test_manifest_contributions_appear_only_while_active() -> None:
    point = ExtensionPoint("t.commands")

    @component("t.host", defines=[point])
    class Host(Component):
        async def start(self, ctx: ComponentContext) -> None:
            pass

    @component("t.contrib", extensions=[contribution(point, "hello", name="hello")])
    class Contrib(Component):
        async def start(self, ctx: ComponentContext) -> None:
            pass

    async with running(Host, Contrib) as c:
        assert c.extensions.implementations(point) == ["hello"]
        await c.disable("t.contrib")
        assert c.extensions.implementations(point) == []


async def test_strict_mode() -> None:
    @component("t.sneaky")
    class Sneaky(Component):
        async def start(self, ctx: ComponentContext) -> None:
            ctx.services.get(Database)

    c = Container(Sneaky, make_db(), strict=True)
    with pytest.raises(ComponentFailed) as info:
        await c.start()
    assert isinstance(info.value.cause, PermissionDenied)

    @component("t.registers")
    class Registers(Component):
        async def start(self, ctx: ComponentContext) -> None:
            ctx.services.register(Database, MemDB())

    async with running(Registers, strict=True, fail_fast=False) as c:
        assert isinstance(c.error_of("t.registers"), PermissionDenied)

    @component("t.container_user")
    class ContainerUser(Component):
        async def start(self, ctx: ComponentContext) -> None:
            ctx.container  # noqa: B018

    async with running(ContainerUser, strict=True, fail_fast=False) as c:
        assert isinstance(c.error_of("t.container_user"), PermissionDenied)

    async with running(Repo, make_db(), strict=True) as c:  # declared use is fine
        assert c.state_of("t.repo") is S.ACTIVE


@dataclass
class PoolConfig:
    size: int = 4
    name: str = "pool"


class PydConfig(pydantic.BaseModel):
    url: str
    timeout: float = 1.0


async def test_dataclass_and_pydantic_config() -> None:
    @component("acme.pool", config=PoolConfig)
    class Pool(Component):
        async def start(self, ctx: ComponentContext, config: PoolConfig) -> None:
            self.config = config

    @component("acme.client", config=PydConfig)
    class Client(Component):
        async def start(self, ctx: ComponentContext) -> None:
            self.config = ctx.config

    cfg = {
        "components": {
            "acme": {"pool": {"size": "8"}},  # nested form, coerced to int
            "acme.client": {"url": "http://x", "timeout": "2.5"},
        }
    }
    async with running(Pool, Client, config=cfg) as c:
        assert c.instance_of("acme.pool").config == PoolConfig(size=8)
        assert c.instance_of("acme.client").config.timeout == 2.5


async def test_invalid_config_fails_component_and_check_reports_it() -> None:
    @component("acme.client2", config=PydConfig)
    class Client(Component):
        async def start(self, ctx: ComponentContext) -> None:
            pass

    c = Container(Client, config={"components": {"acme.client2": {"timeout": "x"}}})
    diags = c.check()
    assert [d.code for d in diags] == ["invalid-config"]
    assert "url" in diags[0].message
    with pytest.raises(ComponentFailed):
        await c.start()


async def test_reload_config_reconfigure_or_restart() -> None:
    @component("t.hot", config=PoolConfig)
    class Hot(Component):
        async def start(self, ctx: ComponentContext, config: PoolConfig) -> None:
            journal.append(f"start {config.size}")

        async def reconfigure(self, ctx: ComponentContext, config: PoolConfig) -> bool:
            journal.append(f"reconf {config.size}")
            return config.size < 100

    data: dict[str, Any] = {"components": {"t.hot": {"size": 1}}}
    from corlet import DictConfigSource

    async with running(Hot, config=DictConfigSource(data)) as c:
        data["components"]["t.hot"] = {"size": 2}
        assert await c.reload_config() == {"t.hot": "reconfigured"}
        assert c.config_of("t.hot").size == 2
        data["components"]["t.hot"] = {"size": 500}
        assert await c.reload_config() == {"t.hot": "restarted"}
        data["components"]["t.hot"] = {"size": "bad"}
        assert await c.reload_config() == {"t.hot": "invalid"}
        assert journal == ["start 1", "reconf 2", "reconf 500", "start 500"]
        # enable/disable lists are applied on reload
        data["container"] = {"disabled": ["t.hot"]}
        await c.reload_config()
        assert c.state_of("t.hot") is S.DISABLED


async def test_runtime_toggles_survive_unrelated_config_reload() -> None:
    from corlet import DictConfigSource

    data: dict[str, Any] = {}
    async with running(make_db(), config=DictConfigSource(data)) as c:
        await c.disable("t.db")
        data["components"] = {"other": {}}
        await c.reload_config()
        assert c.state_of("t.db") is S.DISABLED


async def test_external_services_and_reconcile() -> None:
    c = Container(Repo, fail_fast=False)
    async with c:
        assert c.state_of("t.repo") is S.UNSATISFIED
        c.register_service(Database, MemDB("ext"))
        changes = await c.reconcile()
        assert changes.started == ("t.repo",)
        assert c.instance_of("t.repo").db.name == "ext"


async def test_lifecycle_events_and_container_events() -> None:
    c = Container(make_db())
    rec = EventRecorder(c)
    async with c:
        await rec.wait_for("corlet.container.ready")
    await asyncio.sleep(0)
    topics = rec.topics
    assert topics.index("corlet.lifecycle.starting") < topics.index("corlet.lifecycle.active")
    assert "corlet.container.stopping" in topics
    assert "corlet.container.stopped" in topics


async def test_mutating_from_start_is_rejected_but_spawn_works() -> None:
    @component("t.selfish")
    class Selfish(Component):
        async def start(self, ctx: ComponentContext) -> None:
            with pytest.raises(LifecycleError):
                await ctx._container.disable("t.db")
            container = ctx._container
            ctx.spawn(container.disable("t.db"), critical=False)

    async with running(Selfish, make_db()) as c:
        await wait_for_state(c, "t.db", S.DISABLED)


async def test_replace_component() -> None:
    v1 = make_db(name="v1")
    v2 = make_db(name="v2")
    async with running(v1, Repo) as c:
        assert c.instance_of("t.repo").db.name == "v1"
        changes = await c.replace_component(v2)
        assert set(changes.started) == {"t.db", "t.repo"}
        assert c.instance_of("t.repo").db.name == "v2"
        assert c.component_class("t.db") is v2


async def test_serve_with_main_and_request_stop() -> None:
    c = Container(make_db())

    async def main(container: Container) -> None:
        journal.append("main")

    assert await c.serve(main, install_signal_handlers=False) == 0
    assert journal == ["start t.db", "main", "stop t.db"]

    c2 = Container(make_db("t.db2"))

    c2.events.subscribe("corlet.container.ready", lambda e: c2.request_stop(3))
    assert await c2.serve(install_signal_handlers=False) == 3


def test_run_sync_entry_point() -> None:
    c = Container(make_db())

    async def main(container: Container) -> None:
        container.events.publish(Message("hi"))

    assert c.run(main, install_signal_handlers=False) == 0
    assert c.state is ContainerState.STOPPED


async def test_add_after_start_is_rejected() -> None:
    from corlet import ContainerStateError

    async with running(make_db()) as c:
        with pytest.raises(ContainerStateError):
            c.add(Repo)


async def test_duplicate_ids_rejected() -> None:
    from corlet import DuplicateComponentError

    c = Container(make_db(), make_db())
    with pytest.raises(DuplicateComponentError):
        c.assemble()


async def test_sync_and_blocking_components() -> None:
    import threading

    main_thread = threading.get_ident()

    @component("t.sync")
    class SyncComp(Component):
        def start(self, ctx: ComponentContext) -> None:
            journal.append("sync start")

    @component("t.blocking", blocking=True)
    class BlockingComp(Component):
        def start(self, ctx: ComponentContext) -> None:
            journal.append("thread" if threading.get_ident() != main_thread else "loop")

        def stop(self, ctx: ComponentContext) -> None:
            journal.append("blocking stop")

    async with running(SyncComp, BlockingComp):
        assert sorted(journal) == ["sync start", "thread"]
    assert journal[-1] == "blocking stop"


async def test_inspect_snapshot() -> None:
    async with running(Api, Repo, make_db(), disabled=["t.api"]) as c:
        snap = c.inspect()
        repo = snap.component("t.repo")
        assert repo.layer == 1
        assert [d.target for d in repo.depends_on] == ["t.db"]
        assert snap.component("t.db").dependents == ("t.repo",)
        assert snap.by_state(S.DISABLED) == ["t.api"]
        d = snap.to_dict()
        assert d["components"][0]["state"] in {s.value for s in S}
        from corlet import to_dot, to_mermaid

        assert '"t.repo" -> "t.db"' in to_dot(snap)
        assert "flowchart" in to_mermaid(snap)
