# 架构总览

## 分层

```
┌──────────────────────────────────────────────────────────┐
│ 应用组件              你的业务组件                         │
├──────────────────────────────────────────────────────────┤
│ corlet.std（可选）    config · logging · scheduler ·      │
│                       health · admin · reload · cli       │
├──────────────────────────────────────────────────────────┤
│ corlet.core（零依赖） Container                            │
│   ├─ Discovery        组件从哪里来                         │
│   ├─ Resolver         依赖解析、分层、诊断                  │
│   ├─ Lifecycle        状态机、启停、监督重启                 │
│   ├─ ServiceRegistry  运行期能力交换                       │
│   ├─ ExtensionRegistry 声明式贡献点                        │
│   ├─ EventBus         解耦通知                             │
│   └─ ComponentContext 组件访问内核的唯一入口                │
└──────────────────────────────────────────────────────────┘
```

内核不包含任何业务能力。`corlet.std` 中的组件和你自己写的组件地位完全相同，它们只是
"恰好随框架一起发布"。

## 设计原则

| 原则 | 体现 |
|---|---|
| 内核极小 | `corlet.core` 只用标准库；所有可选能力都是组件 |
| 面向契约 | 组件依赖 `requires_services=[Contract]`，而不是 `requires=["某个组件"]` |
| 生命周期可控 | 明确的状态机；启停按依赖顺序；失败的影响范围可预测 |
| 自动回收 | 经由 `ctx` 注册的一切都记在组件名下，停止时统一释放 |
| 静态装配、动态启停 | 组件集合在启动前确定；运行期只改变"启用 / 禁用 / 失败"这些意图 |
| 可诊断 | 解析与启动分离：`check()` 不启动任何组件就能报告装配问题 |
| 输出由应用决定 | 框架只写入 `corlet` logger，不配置任何日志输出 |

## 三种通信方式

| | 服务 | 扩展点 | 事件 |
|---|---|---|---|
| 语义 | "我提供一个对象给你用" | "我定义一个槽，大家往里贡献" | "某件事发生了" |
| 基数 | 一个契约，一个或多个提供者 | 一个定义者，多个贡献者 | 一个发布者，多个订阅者 |
| 是否关心返回值 | 是（直接调用） | 是（按策略汇总） | 否 |
| 是否影响启动顺序 | 是 | 否 | 否 |
| 典型用途 | 数据库连接池、HTTP 客户端 | 路由、CLI 子命令、中间件、校验规则 | 审计、统计、缓存失效 |

## 容器是一个调和器

容器内部只有一个核心循环，所有生命周期操作（启动、`enable`、`disable`、`restart`、崩溃恢复、
惰性激活、热替换、配置重载）都归结为两步：

1. **改变意图**：修改"哪些组件启用、哪些被需要（lazy）、哪些失败了"；
2. **收敛 (reconcile)**：

```
重新解析依赖图（只考虑可用的组件）
        │
        ▼
计算应运行集合 = 想要运行的 eager 组件（以及被需要的 lazy 组件）
              + 它们的必需依赖（传递闭包）
        │
        ▼
把正在运行但不在集合里的组件，连同必需它们的组件，按启动逆序停止
        │
        ▼
按依赖分层启动集合中尚未运行的组件，同一层并发启动
```

这样所有边界情况（比如禁用一个被三层组件依赖的提供者，或在依赖链中间某个组件崩溃）都走同一条
路径处理，行为一致、可预测。生命周期操作由一把异步锁串行化。

## 一次启动的完整流程

```
Container.start()
 1. assemble()
    ├─ 加载配置树
    ├─ 各个 ComponentSource 发现组件类，检查 id 冲突
    ├─ 读取清单，结合 start() 的类型标注得到"有效清单"
    ├─ 定义所有扩展点
    └─ 解析依赖图 → Resolution（分层 + 诊断）
 2. 存在解析错误且 fail_fast → 抛出 ResolutionError，不启动任何组件
 3. reconcile() → 分层启动；每个组件：
    ├─ 校验配置 → 创建 ComponentContext
    ├─ 实例化 → 解析注入参数 → 调用 start()（带超时）
    └─ 注册清单中声明的扩展贡献 → ACTIVE
 4. 有组件启动失败且 fail_fast（且该组件没有重启策略）→ 停止已启动的组件，抛出 ComponentFailed
 5. 发布 ContainerReady 事件
```

## 包结构

```
corlet/
├── core/         零依赖内核
│   ├── container.py    Container：装配、调和、监督、自省
│   ├── component.py    Component 基类、@component、依赖注入规划
│   ├── manifest.py     Manifest、Requirement、RestartPolicy
│   ├── context.py      ComponentContext 与资源账本
│   ├── registry.py     ServiceRegistry、ServiceRef
│   ├── extensions.py   ExtensionPoint、ExtensionRegistry
│   ├── events.py       EventBus、Event
│   ├── resolver.py     依赖图、分层、诊断
│   ├── lifecycle.py    状态枚举、生命周期事件
│   ├── config.py       配置抽象、schema 校验
│   ├── discovery.py    组件来源
│   ├── introspect.py   快照、DOT / Mermaid 输出
│   ├── version.py      语义化版本与约束
│   └── errors.py       异常体系
├── std/          标准组件（可选）
└── testing/      测试工具与 pytest 插件
```

## 不做什么

- **不是安全沙箱。** 严格模式能约束组件只用声明过的东西，但所有组件运行在同一个 Python 进程中，
  无法隔离恶意代码。
- **不支持运行期安装新组件。** 组件集合在启动前确定；运行期能做的是启用、禁用、重启和替换实现。
- **不是 Web 框架。** `corlet.admin` 只是一个运维用的小型 HTTP 端点。
