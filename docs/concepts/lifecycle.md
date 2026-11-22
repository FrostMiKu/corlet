# 生命周期

## 组件状态

```
INSTALLED ─resolve─► RESOLVED ─► STARTING ─► ACTIVE ─► STOPPING ─► STOPPED
    │                   ▲            │          │                    │
    ├─► DISABLED        │            ▼          ▼（崩溃）             │
    └─► UNSATISFIED ────┘          FAILED ◄─────┘ ── 重启 ──► STARTING
```

| 状态 | 含义 |
|---|---|
| `INSTALLED` | 已发现，尚未解析 |
| `RESOLVED` | 可以运行，但目前没有运行（lazy 且未被需要，或容器尚未启动） |
| `UNSATISFIED` | 已启用，但必需依赖缺失、被禁用或失败；`reason` 给出原因 |
| `DISABLED` | 被配置或 `disable()` 关闭 |
| `STARTING` | `start()` 执行中 |
| `ACTIVE` | 运行中 |
| `STOPPING` | `stop()` 与资源回收执行中 |
| `STOPPED` | 已停止（容器关闭后，或运行期离开应运行集合后的瞬态） |
| `FAILED` | 启动失败或运行中崩溃；`error_of()` 返回异常 |

`ComponentState.running` 对 STARTING、ACTIVE、STOPPING 为真。

```python
container.state_of("acme.orders")            # ComponentState.ACTIVE
container.error_of("acme.orders")            # 失败时的异常，否则 None
container.inspect().component("acme.orders").reason   # 人类可读的原因
```

## 启动与停止顺序

- 依赖图被分成若干**层**：每个组件的依赖都在更早的层。同一层的组件**并发**启动。
- 停止严格按**实际启动的逆序**进行，一次停止一个组件。

```python
container.resolution.layers   # [['acme.db'], ['acme.repo'], ['acme.api']]
```

## 运行期控制

```python
changes = await container.disable("acme.cache")
changes.stopped    # 被停止的组件（按停止顺序）
changes.started    # 被启动的组件
changes.failed     # 启动失败的组件

await container.enable("acme.cache")
await container.restart("acme.orders")        # 同时重启依赖它的运行中组件；会清除 FAILED
await container.reconcile()                   # 重新解析并收敛（例如注册外部服务之后）
await container.replace_component(NewImpl)    # 用同 id 的新类替换实现
await container.reload_config()               # 见"配置"一章
```

规则：

- **禁用**一个组件，会先停掉所有（传递地）**必需**它的运行中组件；它们变为 `UNSATISFIED`，
  原因中写明 `... (disabled)`。
- **启用**一个组件，它和之前因它而 `UNSATISFIED` 的组件会按依赖顺序启动。
- 服务依赖有多个提供者时，停掉其中一个提供者，所有必需该契约的使用方会被重启，以便切换到
  剩下的提供者。
- **可选**依赖消失不会停止依赖方。

!!! warning "不要在 `start()` 中调用生命周期操作"
    在 `start()` 等生命周期回调内部调用 `enable` / `disable` / `restart` 会抛出 `LifecycleError`
    （否则会死锁）。需要时用 `ctx.spawn(...)` 推迟执行。

    如果操作可能停止**调用者自己**（例如组件要重启自己），用容器拥有的任务执行，避免它随调用者
    的任务一起被取消：

    ```python
    container = ctx.container          # 严格模式下需要 privileged=True
    await asyncio.shield(container.spawn(container.restart(ctx.id)))
    ```

## 启动失败

`start()` 抛出异常或超时（`start_timeout`）即为启动失败：

1. 组件已注册的东西被回滚；
2. 组件变为 `FAILED`；
3. 必需它的组件变为 `UNSATISFIED`，原因为 `required component '...' is failed`；
4. 如果有重启策略，安排重启。

在**容器启动阶段**：如果 `fail_fast=True`（默认）且失败的组件没有重启策略，容器会停止已启动的
组件并抛出 `ComponentFailed`。设为 `fail_fast=False` 则以降级状态继续运行。

## 运行中崩溃

以下情况视为组件崩溃：

- 通过 `ctx.spawn(coro)` 启动的**关键**任务（默认 `critical=True`）抛出异常；
- 组件调用 `ctx.fail(error)`。

处理流程：先按逆序停掉依赖它的组件，再停掉它本身并标记为 `FAILED`，然后执行监督策略，
最后重新收敛。

## 监督策略

```python
from corlet import RestartPolicy

@component(
    "acme.consumer",
    restart=RestartPolicy.on_failure(
        max_restarts=5,    # window 秒内最多重启次数
        window=60.0,
        backoff=0.5,       # 第一次重启前等待的秒数
        factor=2.0,        # 指数退避因子
        backoff_max=30.0,  # 最长等待
    ),
)
```

- 默认策略为 `never`：失败后保持 `FAILED`，直到手动 `restart()` 或 `enable()`。
- `on_failure`：失败后按 `backoff × factor^(n-1)`（上限 `backoff_max`）等待后重启；
  `window` 秒内超过 `max_restarts` 次则放弃，原因中标注 `restart limit reached`。
- 被重启的组件恢复后，此前因它而停止的依赖方也会重新启动。
- `inspect()` 中的 `restarts` 字段记录自动重启次数。

## 惰性组件

```python
@component("acme.reports", activation="lazy", provides=[ReportService])
```

惰性组件在以下情况启动：

- 某个运行中的组件**必需**它（直接或通过服务依赖）；
- 有代码对它提供的契约调用 `acquire()`：

```python
ref = await ctx.services.acquire(ReportService)   # 必要时先启动提供者
```

不再被任何运行中组件必需时，惰性组件会随下一次调和停止（被 `acquire()` 激活过的会保持运行）。

## 生命周期事件

每次状态变化都会发布 `ComponentStateChanged`，topic 为 `corlet.lifecycle.<新状态>`：

```python
from corlet import ComponentStateChanged

ctx.events.subscribe("corlet.lifecycle.failed", lambda e: alert(e.component_id, e.reason))
ctx.events.subscribe(ComponentStateChanged, lambda e: print(e.old, "->", e.new))
```

容器级事件：

| 事件 | topic | 时机 |
|---|---|---|
| `ContainerReady` | `corlet.container.ready` | `start()` 完成后 |
| `ContainerStopping` | `corlet.container.stopping` | 开始关闭时（等待处理器完成） |
| `ContainerStopped` | `corlet.container.stopped` | 全部组件停止后 |
| `ConfigChanged` | `corlet.config.changed` | `reload_config()` 对某组件生效后 |

## 运行容器

```python
# 同步入口：启动 → 运行到 Ctrl-C / SIGTERM → 停止，返回退出码
raise SystemExit(container.run())

# 运行一个主协程，返回后停止
container.run(main)                      # async def main(container): ...

# 异步代码中
async with container:
    ...
exit_code = await container.serve()      # 安装信号处理器并运行

# 从任意线程、任意组件请求关闭
container.request_stop(exit_code=0)
ctx.request_shutdown()
```

收到信号时的退出码为 `128 + 信号编号`（SIGINT 为 130）。
