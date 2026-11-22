# 测试

`corlet.testing` 提供测试组件所需的工具。下面的示例使用 pytest 和 pytest-asyncio
（`asyncio_mode = "auto"`）。

## running()：启动一个最小容器

```python
from corlet.testing import running

async def test_orders_are_stored():
    storage = FakeStorage()
    async with running(OrdersComponent, services={Storage: storage}) as c:
        orders = c.services.instance(Orders)
        await orders.place("bob", {"apple": 2})
        assert storage.saved
```

- 只启动你列出的组件；它们的服务依赖由 `services` 中的模拟对象满足（作为外部服务注册）。
- `config=` 传入配置，其他关键字参数（`strict=`、`fail_fast=`、`disabled=` …）原样传给 `Container`。
- 退出时停止容器，并**断言没有资源泄漏**：停止的组件名下不应再有服务、扩展贡献、事件订阅、
  watch 或未结束的任务。不需要时传 `check_leaks=False`。

测试启动失败的场景时，用 `fail_fast=False`，然后检查状态：

```python
async with running(Broken, fail_fast=False) as c:
    assert c.state_of("acme.broken") is ComponentState.FAILED
    assert isinstance(c.error_of("acme.broken"), ConnectionError)
```

## EventRecorder：记录事件

```python
from corlet.testing import EventRecorder

async def test_order_event():
    async with running(OrdersComponent, services={Storage: FakeStorage()}) as c:
        rec = EventRecorder(c, "shop.**")            # 默认 "**"：全部事件
        await c.services.instance(Orders).place("bob", {"apple": 1})
        event = await rec.wait_for(OrderPlaced, timeout=1)
        assert event.customer == "bob"
        rec.close()
```

`wait_for` 接受 topic 模式、事件类型或谓词函数，会先在已记录的事件中查找。其他方法：`events`、
`topics`、`of_type(cls)`、`matching(pattern)`、`clear()`。

## 等待状态变化

崩溃和自动重启是异步发生的，用 `wait_for_state` 等待：

```python
from corlet.testing import wait_for_state

await wait_for_state(c, "acme.flaky", ComponentState.ACTIVE, timeout=2)
```

## 泄漏检查

```python
from corlet.testing import assert_no_leaks, leaks

await c.disable("acme.cache")
assert_no_leaks(c, "acme.cache")      # 只检查这个组件
leaks(c)                              # {owner: [描述, ...]}，用于自定义断言
```

## 扩展契约

```python
from corlet.testing import assert_extension_contract

def test_rule_fits_pipeline():
    assert_extension_contract(PRICING_RULES, Discount(DiscountConfig()))
```

## pytest fixture

安装 corlet 后，pytest 插件会自动注册：

```python
async def test_with_fixture(corlet_container):
    c = await corlet_container(OrdersComponent, services={Storage: FakeStorage()})
    ...                                  # 测试结束时自动停止并检查泄漏

def test_events(event_recorder, some_bus):
    rec = event_recorder(some_bus, "user.*")
```

`corlet_container(*components, services=None, config=None, start=True, **kwargs)`：`start=False`
时只创建、不启动。

## 测试建议

- **面向契约测试**：用 `services={Contract: fake}` 替换依赖，而不是启动真实的提供者组件。
- **测试装配**：在 CI 中运行 `corlet -a myapp check`，或在测试中断言 `Container(...).check()`
  没有错误诊断。
- **测试运行期启停**：对关键组件做一次 `disable` → `enable`，确认依赖方能正确恢复且没有泄漏。
