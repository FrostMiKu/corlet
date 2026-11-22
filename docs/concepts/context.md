# ComponentContext

每次组件启动，容器都会创建一个新的 `ComponentContext`（下文称 `ctx`），作为参数传给 `start`、
`stop` 和 `reconfigure`。**`ctx` 是组件访问容器的唯一入口**：所有经由它的操作都记在该组件名下，
容器据此自动回收资源，并在严格模式下检查权限。

## 属性一览

```python
ctx.id              # 组件 id
ctx.manifest        # 有效清单
ctx.config          # 校验后的配置对象
ctx.logger          # logging.Logger，名称为 corlet.component.<id>
ctx.services        # 服务注册表（见"服务"）
ctx.extensions      # 扩展注册表（见"扩展点"）
ctx.events          # 事件总线（见"事件"）
ctx.stopping        # asyncio.Event，组件开始停止时被设置
ctx.container       # 容器本身（严格模式下需要 privileged=True）
ctx.container_name  # 容器名称（始终可用）
```

## 自动回收

以下资源在组件停止时自动释放，按下面的顺序：

1. 取消 `ctx.spawn()` 启动的后台任务，并等待它们结束；
2. 以后进先出的顺序执行 `ctx.defer()` 注册的清理回调、退出 `enter_context` /
   `enter_async_context` 进入的上下文管理器；
3. 撤销服务注册、扩展贡献、事件订阅、服务和扩展的 watch；
4. 兜底：撤销所有以该组件 id 为 owner 的注册，即使它们没有经过 `ctx`。

因此大多数组件**不需要写 `stop()`**：

```python
@component("acme.poller", provides=[Feed])
class Poller(Component):
    async def start(self, ctx, http: HttpClient):
        self.session = await ctx.enter_async_context(http.session())   # 停止时自动关闭
        ctx.services.register(Feed, FeedImpl(self.session))            # 停止时自动撤销
        ctx.events.subscribe("feed.refresh", self.refresh)             # 停止时自动取消
        ctx.spawn(self.poll_forever())                                 # 停止时自动取消
        ctx.defer(lambda: ctx.logger.info("bye"))                      # 停止时执行
```

`ctx.owns()` 返回该组件当前持有的资源计数，便于调试和测试：

```python
ctx.owns()   # {'ServiceRegistration': 1, 'Subscription': 1, 'Task': 1}
```

## 后台任务

```python
task = ctx.spawn(self.consume(), name="consumer")             # 关键任务（默认）
ctx.spawn(self.warm_cache(), critical=False)                  # 非关键任务
```

- 任务在组件的上下文中运行：日志带有组件 id，任务中查找服务也按该组件计算权限。
- **关键任务**抛出异常时视为组件崩溃，触发[监督策略](lifecycle.md#监督策略)；非关键任务只记录警告。
- 任务被取消（包括组件停止时）不算失败。
- 组件停止后再调用 `spawn` 会抛出 `RuntimeError`。

长期运行的循环可以配合 `ctx.stopping`：

```python
async def consume(self):
    while not self.ctx.stopping.is_set():
        await self.handle(await self.queue.get())
```

## 主动报告与请求关闭

```python
ctx.fail(RuntimeError("lost connection"))   # 报告崩溃，效果同关键任务失败
ctx.request_shutdown()                       # 请求整个容器关闭（如 CLI 命令执行完毕）
```

## 严格模式

`Container(strict=True)` 时，`ctx` 会检查组件的行为是否与清单一致：

| 操作 | 要求 |
|---|---|
| `ctx.services.get/find/instance/acquire/watch(T)` | `T` 在 `requires_services` 或 `provides` 中 |
| `ctx.services.register(T, ...)` | `T` 在 `provides` 中 |
| `ctx.container` | 清单中 `privileged=True` |

违反时抛出 `PermissionDenied`。通过注入获得的服务总是被允许，因为注入会自动登记依赖。

!!! warning
    严格模式用于保持架构整洁，**不是安全边界**。同一进程中的代码总能绕过它。

## 什么时候需要 `ctx.container`

大多数组件不需要直接访问容器。需要的通常是运维类组件，例如管理端点、热重载、配置监视器，它们要
调用 `enable` / `disable` / `inspect` 等。这类组件应声明 `privileged=True`，让依赖关系在清单中可见。
