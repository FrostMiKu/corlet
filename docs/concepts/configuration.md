# 配置

## 配置树

容器从一个**配置源**加载一棵嵌套的配置树。树中有两个约定的部分：

```toml
[container]                       # 容器自身
disabled = ["shop.pricing.vat"]   # 启动时禁用的组件
# enabled = ["..."]               # 白名单：设置后只有这些组件启用

[components."shop.orders"]        # 组件 shop.orders 的配置节
currency = "EUR"

[components.shop.pricing.discount]   # 嵌套写法同样有效
percent = 15
```

组件 `a.b.c` 的配置节可以写成扁平形式 `components."a.b.c"`，也可以写成嵌套形式
`components.a.b.c`。父组件和子组件可以同时存在：组件 `acme` 的配置节不会包含子组件 `acme.db`
的那一部分。

树中的其他顶层键由应用自由使用，可以通过 [`ConfigService`](../std/config.md#configservice) 读取。

## 配置源

```python
Container(..., config={"components": {...}})       # 普通映射
Container(..., config=DictConfigSource(data))       # 引用一个可变映射，便于重载
Container(..., config=LayeredConfig(files=[...]))   # 分层配置：文件 + 环境变量 + 覆盖
```

任何实现了 `load() -> Mapping` 的对象都可以作为配置源（`ConfigSource` 协议）。分层配置见
[std 配置组件](../std/config.md)。

## 配置 schema

清单中的 `config` 决定组件拿到什么样的配置对象：

=== "Pydantic（推荐）"

    ```python
    from pydantic import BaseModel, Field

    class DbConfig(BaseModel):
        dsn: str
        pool_size: int = Field(10, ge=1)
        timeout: float = 5.0

    @component("acme.db", config=DbConfig)
    class Db(Component):
        async def start(self, ctx, config: DbConfig): ...
    ```

=== "dataclass（零依赖）"

    ```python
    from dataclasses import dataclass, field

    @dataclass
    class DbConfig:
        dsn: str
        pool_size: int = 10
        tags: list[str] = field(default_factory=list)

    @component("acme.db", config=DbConfig)
    ```

=== "无 schema"

    ```python
    @component("acme.db")             # ctx.config 是一个 dict
    ```

dataclass 的校验支持嵌套 dataclass、`list` / `tuple` / `set` / `dict`、`Optional`、`Literal`、
`Enum`，以及从字符串到 `int` / `float` / `bool` 的转换（方便环境变量）。未知字段和缺失的必填字段
都会报错。

此外，`config` 也可以是任意可调用对象，它接收原始 dict 并返回配置对象。

## 校验时机

- `container.check()` 会校验所有可运行组件的配置，并以 `invalid-config` 诊断报告错误；
- 组件启动时校验；校验失败等同于启动失败，错误信息指明字段，例如
  `configuration of 'acme.db' is invalid: dsn: Field required`。

```python
container.config_of("acme.db")   # 组件当前（或将要）拿到的配置对象
container.raw_config("acme.db")  # 未校验的配置节
container.config_tree            # 整棵配置树
```

## 运行期重载

```python
outcomes = await container.reload_config()
# {'acme.db': 'reconfigured', 'shop.orders': 'restarted', 'acme.cache': 'invalid'}
```

重载会重新调用配置源的 `load()`，然后对每个配置节发生变化的组件：

| 结果 | 条件 |
|---|---|
| `reconfigured` | 组件运行中，且 `reconfigure(ctx, config)` 返回 `True` |
| `restarted` | 组件运行中，`reconfigure` 返回 `False`（默认）、不存在或抛出异常 → 重启组件及其依赖方 |
| `deferred` | 组件当前没有运行；下次启动时使用新配置 |
| `invalid` | 新配置校验失败；保留旧配置，记录错误 |

`container` 节中 `disabled` / `enabled` 的变化也会生效。重载只会修改**配置发生变化**的组件的启用状态，
所以运行期通过 `enable()` / `disable()` 做的调整不会被无关的配置变更覆盖。

```python
@component("acme.limits", config=LimitsConfig)
class Limits(Component):
    async def start(self, ctx, config: LimitsConfig):
        self.limiter = RateLimiter(config.rps)

    async def reconfigure(self, ctx, config: LimitsConfig) -> bool:
        self.limiter.rps = config.rps     # 就地生效，无需重启
        return True
```

每个生效的组件都会触发一个 `ConfigChanged` 事件。[`corlet.config.watcher`](../std/config.md#configwatcher)
组件可以在配置文件变化时自动调用 `reload_config()`。
