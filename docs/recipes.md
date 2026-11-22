# 实践指南

一些常见问题的做法。

## 替换一个服务的实现

让使用方依赖契约，再提供两个实现组件，通过配置选择启用哪一个：

```python
@component("acme.store.postgres", provides=[Store])
class PostgresStore(Component): ...

@component("acme.store.memory", provides=[Store])
class MemoryStore(Component): ...
```

```toml
[container]
disabled = ["acme.store.memory"]     # 生产环境
```

两个都启用时，使用方拿到 `ranking` 最高的那个（相同则先注册者）。也可以按属性选择：

```python
ctx.services.register(Store, impl, properties={"kind": "postgres"})
store = ctx.services.get(Store, {"kind": "postgres"})
```

## 可选功能

功能组件依赖核心组件，核心组件对功能毫不知情。功能通过扩展点挂到核心上：

```python
# 核心定义扩展点
EXPORTERS = ExtensionPoint("acme.exporters", strategy=Strategy.COLLECT, method="export")

# 功能组件贡献
@component("acme.export.csv", extensions=[contribution(EXPORTERS, CsvExporter())])
class CsvExport(Component): ...
```

运行期 `disable("acme.export.csv")` 后，CSV 导出立即消失，核心不需要任何改动。

## 依赖一个可能不存在的服务

```python
@component("acme.api")
class Api(Component):
    async def start(self, ctx, cache: ServiceRef[Cache] | None = None):
        self.cache = cache

    async def get(self, key):
        if self.cache is not None and self.cache.available:
            hit = await self.cache.get(key)
            ...
```

`ServiceRef` 是动态句柄：缓存组件之后启用、重启或替换，`self.cache` 都会指向当前的提供者，
`acme.api` 不会因此重启。

## 让组件在依赖故障时自愈

给可能出错的组件加上重启策略，把真正的工作放进关键后台任务：

```python
@component("acme.consumer", restart=RestartPolicy.on_failure(max_restarts=10, backoff=1))
class Consumer(Component):
    async def start(self, ctx, broker: Broker):
        self.conn = await broker.connect()
        ctx.defer(self.conn.close)
        ctx.spawn(self.consume())          # 抛异常 → 组件崩溃 → 退避后重启

    async def consume(self):
        async for msg in self.conn:
            await self.handle(msg)
```

## 一次性任务（CLI 风格应用）

```python
async def main(container: Container) -> None:
    report = container.services.instance(ReportService)
    await report.generate()

raise SystemExit(Container(...).run(main))
```

或者在组件中完成工作后调用 `ctx.request_shutdown()`。

## 在启动时做的事依赖另一个组件"已准备好"

把"准备好"建模为服务：提供者在准备工作完成后才注册服务，使用方声明对该服务的依赖。容器保证
使用方在提供者的 `start()` 返回之后才启动。

```python
@component("acme.migrations", provides=[SchemaReady])
class Migrations(Component):
    async def start(self, ctx, db: Database):
        await run_migrations(db)
        ctx.services.register(SchemaReady, object())
```

## 组件需要控制容器

例如一个"维护模式"组件，在收到事件时禁用一组组件：

```python
@component("acme.maintenance", privileged=True)
class Maintenance(Component):
    async def start(self, ctx):
        container = ctx.container

        async def enter(event):
            for cid in ("acme.api", "acme.scheduler"):
                await container.disable(cid)

        ctx.events.subscribe("acme.maintenance.enter", enter)
```

事件处理器运行在独立的任务中，可以直接调用生命周期操作。如果操作可能停止组件自己，用
`container.spawn(...)` 执行，见[生命周期](concepts/lifecycle.md#运行期控制)。

## 把第三方 Web 框架接成组件

```python
@component("acme.web", config=WebConfig, defines=[ROUTES])
class Web(Component):
    async def start(self, ctx, config: WebConfig):
        app = Starlette(routes=ctx.extensions.implementations(ROUTES))
        self.server = uvicorn.Server(uvicorn.Config(app, host=config.host, port=config.port))
        self.task = ctx.spawn(self.server.serve())

    async def stop(self, ctx):
        # stop() 在后台任务被取消之前执行：这里做优雅关闭
        self.server.should_exit = True
        await self.task
```

其他组件把路由贡献到 `ROUTES`。如果需要在路由贡献变化时更新应用，用
`ctx.extensions.watch(ROUTES, ...)`。

## 渐进地把老项目改造成组件

1. 先把全局单例（数据库、配置、HTTP 客户端）包装成提供服务的组件；
2. 把业务模块逐个改为组件，用注入替代对全局变量的直接引用；
3. 把 "if feature_enabled:" 一类的开关改为可启用 / 禁用的组件与扩展点；
4. 在 CI 中加入 `corlet check`。
