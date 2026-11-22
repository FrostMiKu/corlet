# 事件

事件总线用于解耦的通知：发布方不知道、也不关心谁在订阅。

## 定义事件

事件通常是继承 `Event` 的冻结 dataclass，用类属性 `topic` 指定主题：

```python
from dataclasses import dataclass
from corlet import Event

@dataclass(frozen=True)
class OrderPlaced(Event):
    topic = "shop.order.placed"
    order_id: str
    total: float
```

临时、无类型的事件可以用 `Message`：

```python
from corlet import Message
ctx.events.publish(Message("cache.flush", {"region": "eu"}))
```

任何对象都可以作为事件发布；没有 `topic` 属性的对象，用其类的限定名作为 topic。

## 订阅

```python
ctx.events.subscribe(OrderPlaced, on_order)        # 按类型：也匹配子类
ctx.events.subscribe("shop.order.*", on_order)     # 按 topic 模式
ctx.events.subscribe("shop.**", audit)             # ** 匹配任意多段

@ctx.events.on("cache.flush")
async def flush(event): ...

ctx.events.subscribe("report.render", render_pdf, blocking=True)   # 同步处理器在线程中执行
```

topic 模式：

| 模式 | 匹配 | 不匹配 |
|---|---|---|
| `user.created` | `user.created` | `user.deleted` |
| `user.*` | `user.created` | `user.created.v2`、`user` |
| `user.**` | `user`、`user.created`、`user.created.v2` | `order.created` |
| `**` | 一切 | — |

订阅记在组件名下，组件停止时自动取消。处理器在订阅组件的上下文中运行，因此它们产生的日志会带上
该组件的 id。

## 发布

```python
ctx.events.publish(OrderPlaced("o1", 9.9))                 # 发出即返回
result = await ctx.events.publish_and_wait(evt, timeout=5) # 并发执行所有处理器并等待
result.delivered   # 投递给了几个处理器
result.errors      # 处理器抛出的异常
result.ok
```

- `publish` 可以在同步代码中调用，也可以**从其他线程**调用（会转交给容器的事件循环）。
- `publish_and_wait` 适合需要确认处理完毕的场景，例如关闭前刷盘。

## 失败隔离

一个处理器抛出异常：

- 不影响其他处理器；
- 异常被记录到日志；
- 总线发布一个 `HandlerFailed` 事件（topic `corlet.event.handler_error`），包含原事件 topic、
  处理器名称、所属组件和异常对象。

```python
ctx.events.subscribe(HandlerFailed, lambda e: metrics.inc("handler_errors", owner=e.owner))
```

## 内置事件

| topic | 事件类 | 说明 |
|---|---|---|
| `corlet.lifecycle.<state>` | `ComponentStateChanged` | 每次组件状态变化 |
| `corlet.container.ready` | `ContainerReady` | 容器启动完成 |
| `corlet.container.stopping` | `ContainerStopping` | 开始关闭 |
| `corlet.container.stopped` | `ContainerStopped` | 关闭完成 |
| `corlet.config.changed` | `ConfigChanged` | 配置重载后对某组件生效 |
| `corlet.event.handler_error` | `HandlerFailed` | 事件处理器抛出异常 |

## 关闭时的投递

容器关闭时，会先等待所有正在投递中的 `publish` 事件处理完毕（最多 `shutdown_timeout` 秒），
再取消剩余的投递。
