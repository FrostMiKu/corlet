# 标准组件

`corlet.std` 提供一组常用组件。它们和你自己写的组件完全一样，只是随框架一起发布。**不加入容器就
不会加载**，也不会引入第三方依赖。

| 组件 id | 类 | 提供 | 作用 |
|---|---|---|---|
| `corlet.config` | `ConfigServiceComponent` | `ConfigService` | 读取配置树中的应用级设置 |
| `corlet.config.watcher` | `ConfigWatcher` | — | 配置文件变化时自动重载 |
| `corlet.logging` | `LoggingComponent` | — | 配置日志；记录带组件 id；可选 JSON 格式 |
| `corlet.scheduler` | `SchedulerComponent` | `Scheduler` | 周期 / 延迟任务；定义 `JOBS` 扩展点 |
| `corlet.health` | `HealthComponent` | `HealthService` | 汇总健康检查；定义 `HEALTH_CHECKS` 扩展点 |
| `corlet.admin` | `AdminComponent` | `AdminServer` | HTTP 管理端点；定义 `ADMIN_ROUTES` 扩展点 |
| `corlet.reload` | `ReloadComponent` | — | 开发期热重载 |

## 使用

```python
from corlet import Container
from corlet.std import HealthComponent, LoggingComponent, SchedulerComponent

Container(*my_components, LoggingComponent, SchedulerComponent, HealthComponent)
```

命令行中用 `--std` 添加：

```bash
corlet -a myapp --std logging,scheduler,health run
corlet -a myapp --std all run        # 除 reload 以外的全部
```

`--std` 接受的名称：`config`、`config-watcher`、`logging`、`scheduler`、`health`、`admin`、`reload`、
`all`。对应关系也可以在代码中通过 `corlet.std.STD_COMPONENTS` 查到。

## 扩展点一览

标准组件定义的扩展点，可以在你的组件清单中直接贡献：

| 扩展点 | 定义方 | 贡献类型 | 策略 |
|---|---|---|---|
| `JOBS` | `corlet.scheduler` | `Job` | BROADCAST |
| `HEALTH_CHECKS` | `corlet.health` | `HealthCheck` | COLLECT |
| `ADMIN_ROUTES` | `corlet.admin` | `Route` | COLLECT |
| `CLI_COMMANDS` | （命令行静态读取） | `CliCommand` | COLLECT |

```python
from corlet import component, contribution
from corlet.std import HEALTH_CHECKS, JOBS, HealthCheck, Job

@component(
    "acme.cache",
    extensions=[
        contribution(JOBS, Job("evict", 60, evict_expired)),
        contribution(HEALTH_CHECKS, HealthCheck("cache", ping_cache)),
    ],
)
class CacheComponent(Component): ...
```

即使对应的标准组件没有加入容器，这样的贡献也不会出错，只是没有组件去使用它。（用字符串 id 引用
一个没有任何组件定义的扩展点时，`check()` 会给出 `unknown-extension-point` 警告。）
