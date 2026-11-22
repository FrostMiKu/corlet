# API 参考

本部分由源码中的 docstring 自动生成，与代码保持同步（docstring 为英文）。概念性的说明请看
[核心概念](../concepts/architecture.md)。

## 导入路径

日常使用的名字都从顶层包导出：

```python
from corlet import Container, Component, ComponentContext, component, contribution, optional
from corlet.std import SchedulerComponent, HealthComponent, LayeredConfig
from corlet.testing import running, EventRecorder
```

`corlet` 与 `corlet.core` 导出完全相同的名字；下面按定义它们的模块组织。

## 内核 `corlet.core`

| 模块 | 主要内容 |
|---|---|
| [Container](container.md) | `Container`、`ChangeSet` |
| [组件与清单](component.md) | `component`、`Component`、`Manifest`、`RestartPolicy`、`Requirement`、`optional` |
| [ComponentContext](context.md) | `ComponentContext`、`ComponentServices`、`ComponentExtensions`、`ComponentEvents` |
| [服务注册表](registry.md) | `ServiceRegistry`、`ServiceRef`、`ServiceRegistration` |
| [扩展点](extensions.md) | `ExtensionPoint`、`Strategy`、`contribution`、`ExtensionRegistry`、`Extension` |
| [事件总线](events.md) | `Event`、`Message`、`EventBus`、`HandlerFailed`、`PublishResult` |
| [生命周期](lifecycle.md) | `ComponentState`、`ContainerState`、`ComponentStateChanged` 等事件 |
| [依赖解析](resolver.md) | `Resolver`、`Resolution`、`Edge` |
| [配置](config.md) | `ConfigSource`、`DictConfigSource`、`validate_config`、`config_section` |
| [组件发现](discovery.md) | `ExplicitSource`、`ModuleSource`、`EntryPointSource`、`DirectorySource` |
| [自省](introspect.md) | `ContainerSnapshot`、`ComponentInfo`、`to_dot`、`to_mermaid` |
| [版本](version.md) | `Version`、`VersionConstraint` |
| [异常](errors.md) | `CorletError` 及其子类 |

## 标准组件 `corlet.std`

| 模块 | 主要内容 |
|---|---|
| [corlet.std.config](std/config.md) | `LayeredConfig`、`ConfigService`、`ConfigWatcher` |
| [corlet.std.logging](std/logging.md) | `LoggingComponent`、`configure_logging`、`ComponentFilter`、`JsonFormatter` |
| [corlet.std.scheduler](std/scheduler.md) | `Scheduler`、`Job`、`JOBS`、`SchedulerComponent` |
| [corlet.std.health](std/health.md) | `HealthService`、`HealthCheck`、`CheckResult`、`HEALTH_CHECKS` |
| [corlet.std.admin](std/admin.md) | `AdminComponent`、`Route`、`Request`、`Response`、`ADMIN_ROUTES` |
| [corlet.std.reload](std/reload.md) | `ReloadComponent` |
| [corlet.std.cli](std/cli.md) | `main`、`CliCommand`、`CLI_COMMANDS` |

## 测试 `corlet.testing`

[测试工具](testing.md)：`running`、`EventRecorder`、`wait_for_state`、`assert_no_leaks`、pytest fixture。

## 异常层次

```
CorletError
├── VersionError                 (ValueError)
├── InvalidManifestError         (ValueError)
├── DuplicateComponentError
├── ResolutionError              .diagnostics
├── LifecycleError
│   └── ContainerStateError
├── ComponentFailed              .component_id, .cause
├── ServiceNotFound              (LookupError)
├── ServiceUnavailable
├── ExtensionContractError       (TypeError)
├── ConfigError                  (ValueError) .component_id
└── PermissionDenied             (PermissionError)
```
