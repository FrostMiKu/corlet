# 日志：`corlet.std.logging`

corlet 使用标准库 `logging`。这个模块提供两样东西：一个给日志记录加上组件 id 的过滤器，以及一个
根据配置来设置日志的组件。

## 组件 id

每个组件有自己的 logger：`ctx.logger`，名称为 `corlet.component.<id>`。

更重要的是，corlet 用一个上下文变量跟踪"当前代码在替哪个组件执行"。它在 `start` / `stop`、
`ctx.spawn` 启动的任务、事件处理器、调度任务中都会被设置。`ComponentFilter` 把它写入
`record.component`，所以即使是**第三方库**在组件代码中打出的日志，也能知道来自哪个组件：

```
2026-11-22 13:04:51,852 INFO    [shop.orders] httpx: HTTP Request: POST https://...
2026-11-22 13:04:51,853 INFO    [-] corlet.container: container 'shop' started: 9 active, 0 failed
```

不属于任何组件的代码显示为 `-`。

## LoggingComponent

```python
from corlet.std import LoggingComponent
Container(..., LoggingComponent)
```

```toml
[components."corlet.logging"]
level = "INFO"            # 根 logger 级别
format = "json"           # text | json
levels = { asyncio = "WARNING", "acme.noisy" = "ERROR" }
```

配置重载时，这个组件就地应用新设置，不需要重启。组件停止时，移除自己安装的 handler 并恢复原来的
根级别。

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

## 不用组件，直接配置

```python
from corlet.std import configure_logging

configure_logging("DEBUG", format="text", levels={"asyncio": "WARNING"})
```

`configure_logging` 是幂等的：它替换的是自己之前安装的 handler，不会重复添加。`corlet` 命令行在
启动时会用 `--log-level` / `--log-format` 调用它。

也可以只把 `ComponentFilter` 加到你自己的 handler 上：

```python
handler.addFilter(ComponentFilter())
handler.setFormatter(logging.Formatter("%(component)s %(message)s"))
```
