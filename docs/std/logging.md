# 日志：`corlet.std.logging`

corlet 使用标准库 `logging`。框架和组件的日志都写入 `corlet` logger 层级（组件写入
`corlet.component.<id>`），**输出到哪里完全由应用决定**：不做任何配置时，corlet 不会向终端或其他
地方写入内容，适合 CLI 和 TUI 程序；需要日志时，用本模块的 `configure_logging()` 或
`corlet.logging` 组件把它们输出到 stderr、stdout、文件或你自己的 handler。

使用标准库方式也完全可以，例如 `logging.basicConfig(level="INFO")`，或者单独调整框架的级别：

```python
logging.getLogger("corlet").setLevel(logging.WARNING)
```

## 组件 id

每个组件有自己的 logger：`ctx.logger`，名称为 `corlet.component.<id>`。

corlet 还用一个上下文变量跟踪"当前代码在替哪个组件执行"。它在 `start` / `stop`、`ctx.spawn`
启动的任务、事件处理器、调度任务中都会被设置。`ComponentFilter` 把它写入 `record.component`，
所以即使是**第三方库**在组件代码中打出的日志，也能知道来自哪个组件：

```
2026-11-22 13:04:51,852 INFO    [shop.orders] httpx: HTTP Request: POST https://...
2026-11-22 13:04:51,853 INFO    [-] corlet.container: container 'shop' started: 9 active, 0 failed
```

不属于任何组件的代码显示为 `-`。

## configure_logging

```python
from corlet.std import configure_logging

configure_logging("INFO")                                       # stderr，文本格式
configure_logging("DEBUG", output="stdout", format="json")
configure_logging("INFO", output="file", file="~/.myapp/app.log")
configure_logging("INFO", levels={"asyncio": "WARNING", "corlet": "WARNING"})
configure_logging("INFO", output="none")                        # 只设置级别，不安装 handler
configure_logging("INFO", handler=my_handler)                   # 使用自定义 handler
```

| 参数 | 说明 |
|---|---|
| `level` | 根 logger 的级别 |
| `format` | `text`（默认）或 `json` |
| `levels` | 各 logger 的级别，如 `{"asyncio": "WARNING"}` |
| `output` | `stderr`（默认）、`stdout`、`file`、`none` |
| `file` | `output="file"` 时的日志文件；支持 `~`，自动创建上级目录，以追加方式写入 |
| `handler` | 自定义 handler，优先于 `output`；会被加上 `ComponentFilter`，没有 formatter 时按 `format` 设置 |

handler 安装在根 logger 上，因此应用自己的日志和第三方库的日志也会一起输出。`configure_logging`
是幂等的：每次调用都会替换上一次安装的 handler（并关闭它自己创建的文件），不会重复添加。

## LoggingComponent

需要通过配置文件控制日志时，把 `corlet.logging` 组件加入容器：

```python
from corlet.std import LoggingComponent
Container(..., LoggingComponent)
```

```toml
[components."corlet.logging"]
level = "INFO"            # 根 logger 级别
format = "json"           # text | json
output = "file"           # stderr | stdout | file | none
file = "/var/log/myapp/app.log"
levels = { asyncio = "WARNING", "acme.noisy" = "ERROR" }
```

`output = "file"` 时必须设置 `file`，否则组件的配置校验失败。配置重载时，这个组件就地应用新设置，
不需要重启；组件停止时，移除自己安装的 handler 并恢复原来的根级别。

## 在 CLI 和 TUI 程序中

终端界面属于程序自己，日志应当写到别处：

```python
# 写入文件
configure_logging("INFO", output="file", file="~/.myapp/app.log")
```

```python
# 显示在 TUI 的日志面板中（以 Textual 为例）
class PanelHandler(logging.Handler):
    def __init__(self, widget):
        super().__init__()
        self.widget = widget

    def emit(self, record):
        self.widget.write(self.format(record))

configure_logging("INFO", handler=PanelHandler(log_widget))
```

组件的启动失败、崩溃等状态也可以不经日志、直接通过 API 获取并展示，见
[生命周期](../concepts/lifecycle.md)：`container.state_of()`、`error_of()`、`inspect()` 以及
`corlet.lifecycle.*` 事件。

使用 `corlet` 命令行运行时，日志的输出由命令行参数控制，见[命令行](../cli.md#日志)。

## JSON 格式

`format = "json"` 时每条记录输出一行 JSON：

```json
{"ts": "2026-11-22T05:04:51.852Z", "level": "info", "logger": "corlet.component.shop.orders",
 "component": "shop.orders", "message": "order placed", "data": {"id": "o1"}}
```

通过 `extra={"data": {...}}` 附加结构化字段：

```python
ctx.logger.info("order placed", extra={"data": {"id": order_id}})
```

## 只使用过滤器

也可以只把 `ComponentFilter` 加到你自己的 handler 上：

```python
handler.addFilter(ComponentFilter())
handler.setFormatter(logging.Formatter("%(component)s %(message)s"))
```
