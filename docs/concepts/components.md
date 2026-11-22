# 组件与清单

组件是一个被 `@component` 装饰的类。装饰器给类附加一个**清单 (`Manifest`)**，描述组件的
身份、依赖和贡献；类本身负责行为（`start` / `stop` / `reconfigure`）。

## 最小组件

```python
from corlet import Component, ComponentContext, component

@component("acme.hello")
class Hello(Component):
    async def start(self, ctx: ComponentContext) -> None:
        ctx.logger.info("hello")
```

- 继承 `Component` 是推荐做法，但不是必需的；只要类有 `start()` 方法即可。
- 不写 id（直接用 `@component`）时，id 默认为 `模块名.类名`。
- 不写 `description` 时，取类 docstring 的第一行。

## 清单字段

```python
@component(
    "acme.orders",                       # id：全局唯一，点分标识符
    version="1.2.0",                     # 语义化版本
    description="下单服务",
    requires=["acme.audit>=1.0", "?acme.cache"],   # 依赖其他组件（? 表示可选）
    requires_services=[Database, optional(Cache)], # 依赖服务契约
    provides=[Orders],                   # start() 中会注册的契约
    defines=[PRICING_RULES],             # 本组件拥有的扩展点
    extensions=[contribution(HEALTH_CHECKS, HealthCheck("orders", check))],
    config=OrdersConfig,                 # 配置 schema
    activation="eager",                  # eager | lazy
    restart=RestartPolicy.on_failure(),  # 失败 / 崩溃后的监督策略
    start_timeout=30.0,                  # start() 超时（秒，None 为不限）
    stop_timeout=10.0,                   # stop() 与清理超时
    blocking=False,                      # True：同步 start/stop 在线程中执行
    privileged=False,                    # True：严格模式下可访问 ctx.container
    tags=["core"],                       # 自由标签，自省时展示
)
class OrdersComponent(Component): ...
```

### id 与版本

- id 是点分标识符，如 `acme.auth`、`shop.pricing.vat`，建议使用反向域名风格的前缀避免冲突。
  两个不同的类声明同一个 id 会在装配时抛出 `DuplicateComponentError`。
- 版本遵循 SemVer，用于 `requires` 中的版本约束。

### 依赖：`requires` 与 `requires_services`

| | `requires=["acme.db>=1.2"]` | `requires_services=[Database]` |
|---|---|---|
| 依赖的对象 | 某个具体组件 | 某个契约，任何提供者都可以 |
| 可替换性 | 低：换实现要改依赖方 | 高：换一个提供者组件即可 |
| 版本约束 | 支持 | 不适用 |
| 推荐程度 | 只在确实依赖某个组件本身时使用 | **推荐** |

服务依赖会被映射成组件依赖：容器找出所有声明了 `provides=[Database]` 的组件，依赖方排在它们
**全部**之后启动。只要至少有一个提供者可用，必需的服务依赖就算满足。

可选依赖不会让组件无法启动，只影响启动顺序：

```python
requires=["?acme.cache"]                 # 可选组件依赖
requires_services=[optional(Cache)]      # 可选服务依赖（也可写成字符串 "?契约名"）
```

版本约束语法（多个子句用逗号连接，取交集）：

| 写法 | 含义 |
|---|---|
| `*` 或省略 | 任意版本 |
| `1.2.3` / `==1.2.3` | 精确匹配 |
| `>=1.2,<2` | 范围 |
| `!=1.5.0` | 排除 |
| `^1.2.3` | 兼容版本：`>=1.2.3,<2.0.0`（`^0.2.3` → `<0.3.0`） |
| `~1.2.3` | 补丁级别：`>=1.2.3,<1.3.0` |

### `provides`

声明组件会在 `start()` 中注册哪些契约。它有三个作用：

1. 让依赖解析知道谁能满足 `requires_services`；
2. 在严格模式下，只有声明过的契约才能注册；
3. 如果组件声明了却没有注册，容器会记录一条警告。

### `activation`

- `eager`（默认）：容器启动时就启动。
- `lazy`：只有在被需要时才启动——被某个运行中的组件必需，或者有人对它提供的契约调用了
  `acquire()`。不再被任何组件需要时，它会随调和过程停止（被 `acquire()` 激活过的除外）。

## 依赖注入

`start()` 中 `ctx` 之后的参数由容器根据类型标注注入：

| 参数标注 | 注入值 | 依赖性质 |
|---|---|---|
| 清单的 `config` schema | 校验后的配置对象 | — |
| `T` | 服务对象（排名最高的提供者） | 必需 |
| `T \| None`，或带默认值 | 服务对象，或 `None` | 可选 |
| `ServiceRef[T]` | 动态句柄：每次访问都指向当前最优提供者 | 必需 |
| `ServiceRef[T] \| None`，或带默认值 | 动态句柄（可能暂不可用，用 `ref.available` 检查） | 可选 |

```python
@component("acme.report", config=ReportConfig)
class Report(Component):
    async def start(
        self,
        ctx: ComponentContext,
        db: Database,                      # 必需
        config: ReportConfig,              # 配置
        cache: Cache | None = None,        # 可选，没有就是 None
        mailer: ServiceRef[Mailer] | None = None,  # 可选的动态句柄
    ) -> None:
        ...
```

注入的服务契约会**自动并入** `requires_services`，不必重复声明。如果显式声明了，以声明为准：
例如声明 `requires_services=[optional(Cache)]` 后，即使参数写作 `cache: Cache`（没有默认值），
它也是可选的。

!!! note "裸对象还是句柄？"
    注入裸对象（`db: Database`）最简单。当它的提供者被禁用、重启或替换时，容器会**重启依赖方**，
    让它拿到新对象——所以裸对象不会悬挂。

    `ServiceRef[T]` 适合"提供者可以来来去去，但我不想因此被重启"的场景（通常配合可选依赖）：
    句柄总是指向当前可用的最优提供者。

每个被注入的参数都必须有类型标注，否则装配时抛出 `InvalidManifestError`。

## 生命周期方法

```python
class OrdersComponent(Component):
    async def start(self, ctx, db: Database) -> None:
        ...  # 获取资源、注册服务、订阅事件、启动后台任务

    async def stop(self, ctx) -> None:
        ...  # 可选：释放不是通过 ctx 获取的资源

    async def reconfigure(self, ctx, config) -> bool:
        ...  # 可选：就地应用新配置；返回 False 则由容器重启组件
```

- 三个方法都可以写成 `async def` 或普通 `def`。普通方法在事件循环线程中直接执行；如果其中有
  阻塞调用，在清单里设置 `blocking=True`，让它们在线程池中运行。
- `stop()` 是可选的：通过 `ctx` 注册的服务、贡献、订阅、任务、清理回调，会在 `stop()` 返回后
  **自动释放**（见 [ComponentContext](context.md)）。
- 每次启动都会创建一个新的实例和新的 `ctx`，组件不会被复用。
- `start()` 抛出异常即为启动失败，期间已注册的东西会被回滚。

## 同步组件

```python
@component("acme.legacy", blocking=True)
class Legacy(Component):
    def start(self, ctx) -> None:
        self.conn = legacy_driver.connect()     # 阻塞调用，在线程中执行

    def stop(self, ctx) -> None:
        self.conn.close()
```

## 声明式扩展贡献

清单中的 `extensions` 在组件变为 ACTIVE 时自动贡献，停止时自动撤销，不需要在 `start()` 中写代码：

```python
@component(
    "shop.pricing.vat",
    extensions=[contribution(PRICING_RULES, Vat(), name="vat")],
)
class VatComponent(Component):
    async def start(self, ctx) -> None:
        pass
```

`extensions` 也可以写成映射：`{PRICING_RULES: Vat(), HEALTH_CHECKS: [check1, check2]}`。
详见[扩展点](extensions.md)。

## 查看有效清单

```python
container.manifest("acme.report")             # 有效清单（含注入推导出的依赖）
corlet.manifest_of(Report)                    # 声明的清单
```
