# corlet

**corlet 是一个 Python 微内核框架。** 应用由一组*组件 (component)* 构成，每个组件在清单中声明
自己**需要什么**、**提供什么**；*容器 (container)* 把它们解析成依赖图、按序启动，在组件之间
连接服务、扩展点和事件，并在组件停止时自动回收它注册过的一切。

```python
from typing import Protocol, runtime_checkable
from corlet import Component, ComponentContext, Container, component

@runtime_checkable
class Greeter(Protocol):
    def greet(self, name: str) -> str: ...

class English:
    def greet(self, name: str) -> str:
        return f"Hello, {name}!"

@component("demo.greeter", provides=[Greeter])
class GreeterComponent(Component):
    async def start(self, ctx: ComponentContext) -> None:
        ctx.services.register(Greeter, English())

@component("demo.app")
class App(Component):
    async def start(self, ctx: ComponentContext, greeter: Greeter) -> None:
        print(greeter.greet("world"))
        ctx.request_shutdown()

raise SystemExit(Container(GreeterComponent, App).run())
```

## 特性

<div class="grid" markdown>

**内核极小，零依赖**
:   `corlet.core` 只依赖标准库。配置分层、日志、调度、HTTP 管理端点、CLI 都是 `corlet.std`
    中的普通组件，不需要就不加载。

**面向契约**
:   组件依赖服务契约（`Protocol` / ABC），不依赖彼此的实现；提供者可以替换、重启、热重载，
    使用方无感知。

**静态装配，动态启停**
:   组件集合在启动前确定；运行期可以 `enable` / `disable` / `restart`，依赖方自动级联，
    并给出可读的原因。

**自动回收**
:   通过 `ctx` 注册的服务、扩展贡献、事件订阅、后台任务和清理回调，组件停止时统一释放，
    无需手写 `stop()`。

**可观测、可诊断**
:   `corlet check` 不启动即可发现缺失依赖、版本冲突和循环依赖；`inspect()` 给出完整的
    依赖图、状态、服务表和扩展表。

**异步优先**
:   内核基于 asyncio；同步组件可直接使用，阻塞代码可声明在线程中运行。

**不占用终端**
:   框架只写入 `corlet` logger，日志输出到哪里由应用决定。CLI 和 TUI 程序的终端只属于程序自己。

</div>

## 文档导航

| 我想… | 去看 |
|---|---|
| 十分钟跑起来 | [快速上手](getting-started.md) |
| 理解整体设计 | [架构总览](concepts/architecture.md) |
| 写一个组件 | [组件与清单](concepts/components.md) · [ComponentContext](concepts/context.md) |
| 组件之间通信 | [服务](concepts/services.md) · [扩展点](concepts/extensions.md) · [事件](concepts/events.md) |
| 理解启停与失败处理 | [生命周期](concepts/lifecycle.md) |
| 配置应用 | [配置](concepts/configuration.md) · [std 配置组件](std/config.md) |
| 运维与调试 | [命令行](cli.md) · [管理端点](std/admin.md) · [健康检查](std/health.md) |
| 写测试 | [测试](testing.md) |
| 解决具体问题 | [实践指南](recipes.md) |
| 查阅某个类或函数 | [API 参考](api/index.md) |

## 要求

- Python ≥ 3.11
- 可选：Pydantic v2（推荐用于配置模型）、PyYAML（YAML 配置文件）
