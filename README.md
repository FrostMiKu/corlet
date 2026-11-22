# corlet

**一个 Python 微内核框架：组件 (component) + 容器 (container)。**

应用由一组 *组件* 组成。每个组件在清单 (manifest) 中声明自己**需要什么**、**提供什么**；
*容器* 把它们解析成依赖图，按序启动，在组件之间连接服务、扩展点和事件，并在组件停止时
自动回收它注册过的一切。

```
┌──────────────────────────────────────────────────────────┐
│ 应用组件              shop.orders · shop.pricing.* · ...  │
├──────────────────────────────────────────────────────────┤
│ corlet.std (可选)     config · logging · scheduler ·      │
│                       health · admin · reload · cli       │
├──────────────────────────────────────────────────────────┤
│ corlet.core (零依赖)  Container · Resolver · Lifecycle ·   │
│                       ServiceRegistry · ExtensionRegistry │
│                       · EventBus · ComponentContext       │
└──────────────────────────────────────────────────────────┘
```

- **内核极小**：`corlet.core` 只依赖标准库；配置分层、日志、HTTP、CLI 都是 `corlet.std` 里的普通组件。
- **面向契约**：组件依赖服务契约（`Protocol`/ABC），而不是彼此的实现。
- **静态装配，动态启停**：组件集合在启动前确定；运行期可 `enable` / `disable` / `restart`，依赖方自动级联。
- **自动回收**：通过 `ctx` 注册的服务、扩展、订阅、后台任务、清理回调，停止时由容器统一释放。
- **异步优先**：内核基于 asyncio；同步组件可直接使用，阻塞代码可声明在线程中运行。
- **不占用终端**：框架只写入 `corlet` logger，日志输出到哪里由应用决定，适合 CLI / TUI 程序。
- Python ≥ 3.11；推荐（非必需）Pydantic v2 做配置 schema。

## 安装

```bash
pip install corlet            # 仅内核 + std（无第三方依赖）
pip install "corlet[pydantic]" # 推荐：Pydantic 配置模型
pip install "corlet[all]"      # + YAML 配置文件支持
```

## 快速上手

```python
from typing import Protocol, runtime_checkable
from corlet import Component, ComponentContext, Container, component

@runtime_checkable
class Greeter(Protocol):
    def greet(self, name: str) -> str: ...

class English:
    def greet(self, name: str) -> str:
        return f"Hello, {name}!"

@component("demo.greeter", version="1.0.0", provides=[Greeter])
class GreeterComponent(Component):
    async def start(self, ctx: ComponentContext) -> None:
        ctx.services.register(Greeter, English())

@component("demo.app")
class App(Component):
    # 类型标注为契约的参数会被自动注入，并自动成为依赖
    async def start(self, ctx: ComponentContext, greeter: Greeter) -> None:
        print(greeter.greet("world"))
        ctx.request_shutdown()   # 演示用；服务型应用会一直运行到 Ctrl-C

raise SystemExit(Container(GreeterComponent, App).run())
```

## 核心概念

### 组件与清单

```python
@component(
    "acme.orders",                      # 全局唯一 id
    version="1.2.0",                    # SemVer
    requires=["acme.audit>=1.0", "?acme.cache"],  # 依赖组件（? = 可选）
    requires_services=[Database, optional(Cache)], # 依赖服务契约（推荐）
    provides=[Orders],                  # 提供的服务契约
    defines=[PRICING_RULES],            # 定义的扩展点
    extensions=[contribution(HEALTH_CHECKS, HealthCheck("orders", check))],
    config=OrdersConfig,                # dataclass 或 Pydantic 模型
    activation="lazy",                  # eager（默认）| lazy
    restart=RestartPolicy.on_failure(max_restarts=3, backoff=0.5),
    start_timeout=30, stop_timeout=10,
    blocking=False,                     # True: 同步 start/stop 放线程池
)
class OrdersComponent(Component):
    async def start(self, ctx, db: Database, config: OrdersConfig): ...
    async def stop(self, ctx): ...                 # 可省略
    async def reconfigure(self, ctx, config) -> bool: ...  # 可选：配置热更新
```

`start()` 的注入规则：

| 参数标注 | 注入值 |
|---|---|
| 清单的 `config` schema | 校验后的配置对象 |
| `T` | 服务对象（必需依赖） |
| `T \| None` / 有默认值 / 声明为 `optional(T)` | 服务对象或 `None`（可选依赖） |
| `ServiceRef[T]` | 动态句柄：总指向当前最优提供者，可用 `ref.available` 检查 |

### 生命周期

```
INSTALLED ─► RESOLVED ─► STARTING ─► ACTIVE ─► STOPPING ─► STOPPED
   │            ▲            │          │
   ├─► DISABLED │            ▼          ▼ (崩溃)
   └─► UNSATISFIED ◄──────  FAILED ── supervisor 重启 ──► STARTING
```

- 按依赖**分层启动**，同层组件**并发**启动；停止严格按启动的逆序。
- 必需依赖缺失 / 被禁用 / 失败 → 依赖方进入 `UNSATISFIED`，并带上可读原因（如
  `no available provider of Storage: shop.storage.memory (disabled)`）。
- 运行期崩溃（`ctx.spawn` 的关键任务抛异常或 `ctx.fail(err)`）→ 先停依赖方，再按
  `RestartPolicy` 指数退避重启，超过窗口内上限则放弃。
- 每次状态变化都发布 `ComponentStateChanged` 事件（topic `corlet.lifecycle.<state>`）。

容器是一个**调和器 (reconciler)**：`enable/disable/restart`、崩溃、惰性激活、热替换都只是改变"意图"，
然后统一收敛：重新解析 → 计算应运行集合 → 逆序停掉离开的组件及其依赖方 → 分层启动缺失的组件。

```python
async with Container(...) as c:
    await c.disable("acme.cache")      # 返回 ChangeSet(stopped=..., started=...)
    await c.enable("acme.cache")
    await c.restart("acme.orders")
    await c.reload_config()            # 调 reconfigure()，返回 False 则重启
    await c.replace_component(NewImpl) # 热替换实现（同 id）
    snapshot = c.inspect()             # 依赖图、状态、服务表、扩展表
```

### 服务注册表

```python
reg = ctx.services.register(Database, PgPool(), properties={"region": "eu"}, ranking=10)
db  = ctx.services.get(Database)                  # 动态 ServiceRef，排名最高者
eu  = ctx.services.get_all(Database, {"region": "eu"})   # 绑定型 ref 列表
raw = ctx.services.instance(Database)             # 直接拿对象
ref = await ctx.services.acquire(Database)        # 必要时激活 lazy 提供者
ctx.services.watch(Database, on_add, on_remove)   # 跟踪服务出现/消失
```

- **动态 ref**（`get`）每次访问都重新选择最优提供者——提供者替换、重启、热重载对使用方透明。
- **绑定 ref**（`get_all`/`watch`）固定于某次注册，提供者离开后抛 `ServiceUnavailable`，避免悬挂引用。
- 契约按限定名做键，热重载后重新创建的 Protocol 类仍映射到同一槽位。

### 扩展点

服务是"我提供一个对象给你用"；扩展点是"我定义一个槽，大家往里贡献"。

```python
PRICING_RULES = ExtensionPoint("shop.pricing.rules", strategy=Strategy.PIPELINE, method="apply")

# 贡献方：声明式（随组件激活/停止自动出现/撤销）
@component("shop.pricing.vat", extensions=[contribution(PRICING_RULES, Vat(), name="vat")])
# 或命令式
ctx.extensions.contribute(PRICING_RULES, Discount(), name="discount", before=["vat"])

# 定义方
total = await ctx.extensions.invoke(PRICING_RULES, subtotal, customer=c)
```

| 策略 | 行为 |
|---|---|
| `BROADCAST` | 全部调用，忽略返回值 |
| `COLLECT` | 全部调用，返回结果列表 |
| `FIRST` | 返回第一个非 `None` 结果 |
| `PIPELINE` | 链式传递：上一个输出是下一个输入 |

顺序由 `before` / `after`（按贡献名或组件 id）拓扑决定，再按 `order`、贡献先后打破平局——结果确定。

### 事件总线

```python
@dataclass(frozen=True)
class OrderPlaced(Event):
    topic = "shop.order.placed"
    order_id: str

ctx.events.subscribe(OrderPlaced, handler)        # 按类型（含子类）
ctx.events.subscribe("shop.order.*", handler)     # 按 topic，* 一段，** 多段
ctx.events.publish(OrderPlaced("o1"))             # 发出即返回；可跨线程调用
await ctx.events.publish_and_wait(evt)            # 等待全部处理器
```

处理器相互隔离：一个抛异常只会记录日志并发布 `HandlerFailed`，不影响其他处理器。
**事件是通知（不关心返回值），扩展点是契约调用（关心返回值）。**

### ComponentContext

`ctx` 是组件访问容器的唯一入口，所有经由它的操作都记在该组件名下，停止时自动回收：

```python
ctx.id, ctx.manifest, ctx.config, ctx.logger
ctx.services / ctx.extensions / ctx.events
ctx.spawn(coro, critical=True)   # 托管后台任务；关键任务崩溃 → supervisor
ctx.defer(callback)              # 清理回调（同步/异步，LIFO）
await ctx.enter_async_context(cm)
ctx.fail(err)                    # 主动报告崩溃
ctx.request_shutdown()
```

**严格模式**（`Container(strict=True)`）：组件只能 `get` 自己在 `requires_services` 中声明过的服务、
只能注册 `provides` 中的契约、只有 `privileged=True` 才能访问 `ctx.container`。目的是保持架构整洁，
**不是安全沙箱**。

### 配置

```python
from corlet.std import LayeredConfig
config = LayeredConfig(
    files=["app.toml", "?local.toml"],        # ? = 可选文件；支持 toml/json/yaml
    env_prefix="SHOP",                        # SHOP__COMPONENTS__SHOP__ORDERS__CURRENCY=USD
    overrides=["components.shop.orders.currency=CHF"],
)
Container(..., config=config)
```

优先级：默认值 < 文件 < 环境变量 < 显式覆盖。组件 `acme.db` 读取 `components."acme.db"`
（或嵌套形式 `components.acme.db`）。`container.disabled` / `container.enabled` 控制启用。

```toml
[container]
disabled = ["shop.pricing.vat"]

[components.shop.orders]
currency = "EUR"
```

## 标准组件 `corlet.std`

| 组件 id | 作用 |
|---|---|
| `corlet.config` | `ConfigService`：读取原始配置树中的应用级设置 |
| `corlet.config.watcher` | 配置文件变更时自动 `reload_config()` |
| `corlet.logging` | 配置日志输出（stderr / stdout / 文件）；日志记录自动带 `component` 字段；可选 JSON 格式 |
| `corlet.scheduler` | `Scheduler` 服务 + `JOBS` 扩展点；任务随所属组件停止而取消 |
| `corlet.health` | `HealthService`：聚合 `HEALTH_CHECKS` 贡献 + 组件状态 |
| `corlet.admin` | 零依赖 HTTP 管理端点：`/health` `/components` `/services` `/extensions` `/graph`，`POST /components/{id}/enable|disable|restart`；路由可经 `ADMIN_ROUTES` 扩展 |
| `corlet.reload` | 开发期热重载：源文件变化 → 重新导入 → `replace_component` |

## CLI

```bash
corlet -a myapp.components -c app.toml run        # 运行直到 Ctrl-C
corlet -a myapp.components check                  # 只解析和校验，不启动
corlet -a myapp:create_container graph --format mermaid
corlet -a ./components --std scheduler,health list
corlet -a myapp config acme.db                    # 原始与生效配置
corlet -a myapp doctor
```

`-a` 可以是模块、`模块:属性`（容器、工厂函数、组件或列表）、目录或 `.py` 文件；也可在
`pyproject.toml` 的 `[tool.corlet]` 中设默认值。组件可通过向 `CLI_COMMANDS` 扩展点贡献
`CliCommand` 来增加子命令（静态读取，无需启动应用即可出现在 `--help` 中）。

已安装的发行包可通过入口点自动被发现：

```toml
[project.entry-points."corlet.components"]
auth = "acme_auth:AuthComponent"
```

## 测试

```python
from corlet.testing import running, EventRecorder, assert_no_leaks

async def test_orders():
    async with running(OrdersComponent, services={Storage: FakeStorage()}) as c:
        orders = c.services.instance(Orders)
        assert (await orders.place("bob", {"apple": 2}))["total"] == 1.0
    # 退出时自动停止并断言：组件名下的服务/扩展/订阅/任务全部清零
```

pytest 插件自动注册，提供 `corlet_container` 与 `event_recorder` fixture。

## 示例

`examples/shop` 是一个完整示例：存储、下单、定价规则流水线（折扣 → 增值税）、事件统计、
定时报告、管理端点路由以及自定义 CLI 子命令。

```bash
corlet -a examples.shop.app:create -c examples/shop/shop.toml demo alice bob
corlet -a examples.shop.app:create -c examples/shop/shop.toml run
curl -X POST localhost:8765/components/shop.pricing.vat/disable
```

## 文档

完整文档位于 [`docs/`](https://github.com/FrostMiKu/corlet/blob/main/docs/index.md)，包括快速上手、核心概念、标准组件、命令行、测试、实践指南，
以及从 docstring 生成的 API 参考。本地预览：

```bash
uv pip install mkdocs mkdocs-material "mkdocstrings[python]"   # 或 uv sync --group docs
mkdocs serve           # http://127.0.0.1:8000
mkdocs build --strict  # 构建静态站点到 site/
```

## 开发

```bash
uv venv -p 3.11 && uv pip install -e . pytest pytest-asyncio pydantic pyyaml mypy ruff
pytest && mypy && ruff check src tests && mkdocs build --strict
```
