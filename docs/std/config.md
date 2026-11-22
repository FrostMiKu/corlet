# 配置：`corlet.std.config`

## LayeredConfig

分层配置源，按以下优先级合并（后者覆盖前者）：

**默认值 < 配置文件（按顺序） < 环境变量 < 显式覆盖**

```python
from corlet.std import LayeredConfig

config = LayeredConfig(
    defaults={"components": {"acme.db": {"pool_size": 5}}},
    files=["app.toml", "?local.toml"],         # "?" 开头表示文件可以不存在
    env_prefix="MYAPP",                        # 默认 "CORLET"；None 表示不读环境变量
    overrides=["components.acme.db.debug=true"],
)
Container(..., config=config)
```

### 文件格式

按扩展名识别：

| 扩展名 | 依赖 |
|---|---|
| `.toml` | 标准库 |
| `.json` | 标准库 |
| `.yaml` / `.yml` | 需要 `pip install pyyaml` |

### 环境变量

以 `前缀__` 开头的变量会被读入，`__` 作为路径分隔符，路径转为小写：

```bash
MYAPP__COMPONENTS__ACME__DB__PORT=5433       # → components.acme.db.port = 5433
MYAPP__CONTAINER__DISABLED='["acme.cache"]'  # → container.disabled = ["acme.cache"]
```

值会先尝试按 JSON 解析（数字、布尔、列表、对象），失败则作为字符串。环境变量产生的是嵌套形式，
因此组件 `acme.db` 的配置节写作 `COMPONENTS__ACME__DB__...`。

### 覆盖

`overrides` 中的字符串格式为 `点分路径=值`，值同样按 JSON 解析；也可以直接传映射。命令行的
`-s/--set` 就是这一层。

### 合并规则

每一层在合并前都会被规范化（`components."a.b"` 展开成嵌套形式），所以无论各层用扁平写法还是嵌套
写法，后面的层总能覆盖前面的层。映射递归合并，其他值（包括列表）整体替换。

## ConfigService

`corlet.config` 组件把整棵配置树作为服务暴露出来，供组件读取应用级（不属于某个组件的）设置：

```python
from corlet.std import ConfigService, ConfigServiceComponent

@component("acme.banner")
class Banner(Component):
    async def start(self, ctx, settings: ConfigService):
        title = settings.get("app.title", "Untitled")
        tree = settings.tree()
```

组件自己的配置应当通过清单的 `config` schema 获取，而不是 `ConfigService`。

## ConfigWatcher

`corlet.config.watcher` 定期检查 `LayeredConfig` 中各文件的修改时间，变化时调用
`container.reload_config()`。重载的语义见[配置](../concepts/configuration.md#运行期重载)。

```toml
[components."corlet.config.watcher"]
interval = 2.0     # 秒
```

配置源不是 `LayeredConfig`（或没有 `fingerprint()` 方法）时，监视器什么也不做，只记录一条警告。
