# 快速上手

本页用一个小例子走完 corlet 的主要流程：定义契约、写组件、组装容器、配置、运行、运行期启停、测试。

## 安装

```bash
pip install corlet              # 内核 + 标准组件，无第三方依赖
pip install "corlet[pydantic]"  # 推荐：用 Pydantic 写配置模型
pip install "corlet[all]"       # 再加 YAML 配置支持
```

## 1. 定义契约

组件之间通过**契约**交互。契约通常是一个 `Protocol`：

```python title="todo/contracts.py"
from typing import Protocol, runtime_checkable

@runtime_checkable
class TodoStore(Protocol):
    async def add(self, text: str) -> int: ...
    async def list(self) -> list[str]: ...
```

`@runtime_checkable` 不是必需的，但加上后容器会在注册时检查实现是否满足契约。

## 2. 写一个提供者组件

```python title="todo/memory.py"
from corlet import Component, ComponentContext, component
from .contracts import TodoStore

class MemoryStore:
    def __init__(self) -> None:
        self.items: list[str] = []

    async def add(self, text: str) -> int:
        self.items.append(text)
        return len(self.items)

    async def list(self) -> list[str]:
        return list(self.items)

@component("todo.store.memory", version="1.0.0", provides=[TodoStore])
class MemoryStoreComponent(Component):
    """内存存储。"""

    async def start(self, ctx: ComponentContext) -> None:
        ctx.services.register(TodoStore, MemoryStore())
```

- `@component` 给类附加一个**清单 (manifest)**：id、版本、提供和依赖的东西。
- `start()` 里通过 `ctx.services.register` 注册服务。组件停止时，这个注册会**自动撤销**。

## 3. 写一个使用者组件

```python title="todo/app.py"
from dataclasses import dataclass
from corlet import Component, ComponentContext, component
from .contracts import TodoStore

@dataclass
class AppConfig:
    greeting: str = "Todo list"

@component("todo.app", version="1.0.0", config=AppConfig)
class TodoApp(Component):
    async def start(self, ctx: ComponentContext, store: TodoStore, config: AppConfig) -> None:
        await store.add("learn corlet")
        print(config.greeting, await store.list())
```

`start()` 中 `ctx` 之后的参数由容器根据类型标注**注入**：

- `store: TodoStore` —— 注入服务，同时自动成为该组件的**必需依赖**；
- `config: AppConfig` —— 注入校验后的配置（因为它是清单里的 `config` schema）。

## 4. 组装并运行

```python title="main.py"
from corlet import Container
from todo.app import TodoApp
from todo.memory import MemoryStoreComponent

container = Container(
    TodoApp,
    MemoryStoreComponent,
    config={"components": {"todo.app": {"greeting": "My todos:"}}},
)
raise SystemExit(container.run())
```

容器会：

1. 发现两个组件并读取清单；
2. 解析依赖：`todo.app` 需要 `TodoStore`，由 `todo.store.memory` 提供，所以后者先启动；
3. 按层启动组件，然后一直运行，直到收到 Ctrl-C / SIGTERM；
4. 按启动的逆序停止组件。

!!! tip "不想一直运行？"
    `container.run(main)` 接受一个 `async def main(container)`，它返回后容器就会停止。
    在 `async` 代码中也可以写 `async with container: ...`。

## 5. 检查装配

不启动就能检查装配是否正确：

```python
diagnostics = Container(TodoApp).check()   # 故意漏掉存储组件
for d in diagnostics:
    print(d)
# ERROR missing-service: [todo.app] requires service TodoStore but no installed component provides it
```

用命令行也可以做同样的检查：`corlet -a todo.app -a todo.memory check`，详见[命令行](cli.md)。

## 6. 运行期启停

```python
async with Container(TodoApp, MemoryStoreComponent) as c:
    changes = await c.disable("todo.store.memory")
    print(changes.stopped)                 # ('todo.app', 'todo.store.memory')
    print(c.state_of("todo.app"))          # ComponentState.UNSATISFIED
    print(c.inspect().component("todo.app").reason)
    # no available provider of TodoStore: todo.store.memory (disabled)

    await c.enable("todo.store.memory")    # 两个组件都重新启动
```

禁用一个提供者时，所有**必需**它的组件会先按逆序停止；重新启用后，它们按依赖顺序重新启动。

## 7. 测试

```python
from corlet.testing import running
from todo.app import TodoApp
from todo.contracts import TodoStore

class FakeStore:
    def __init__(self): self.added = []
    async def add(self, text): self.added.append(text); return 1
    async def list(self): return self.added

async def test_app_adds_an_item():
    store = FakeStore()
    async with running(TodoApp, services={TodoStore: store}):
        assert store.added == ["learn corlet"]
    # 退出时自动断言：组件注册的东西已全部回收
```

## 下一步

- [架构总览](concepts/architecture.md)：内核由哪些子系统组成，容器怎样工作
- [组件与清单](concepts/components.md)：清单的所有字段、依赖注入规则
- [标准组件](std/index.md)：日志、调度、健康检查、管理端点等现成组件
- 仓库里的 `examples/shop` 是一个更完整的示例
